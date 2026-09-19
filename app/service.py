"""换线清场与首件放行的核心服务。

规则要点:
- 检查范围由当前版本转换矩阵按产品转换风险生成; 清场结论以结论当时的矩阵版本为准。
- 证据、机器回执、人工签字只追加; 离线终端按 (device_id, device_seq) 幂等去重,
  补传不得覆盖后来完成的检查。
- 发现残留触发重新清洁, 仅作废相关区域的检查项与签字, 不连带无关区域。
- 清场通过 + 模具与程序校验一致 + 首件检测获批, 才签发有时限的开线令牌。
- 跨岗代签、旧标签重复扫描、令牌过期一律明确拒绝并留痕。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from .catalog import Catalog
from .models import (
    RESIDUE_CHECK_KINDS,
    Changeover,
    ChangeoverState,
    CheckItem,
    CheckKind,
    CheckResult,
    RiskLevel,
    Signature,
    Token,
    TokenState,
)
from .store import AppendOnlyStore

# 各风险等级的令牌默认有效期(秒)
TOKEN_TTL_BY_RISK = {
    RiskLevel.HIGH: 30 * 60,
    RiskLevel.MEDIUM: 60 * 60,
    RiskLevel.LOW: 2 * 60 * 60,
}


class DomainRejection(Exception):
    """业务规则拒绝; ``reason`` 为机器可读原因码, 拒绝事件同时写入日志。"""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


def _fingerprint(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _to_epoch(value: Any) -> float | None:
    """接受 epoch 秒或 ISO-8601 字符串。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value)).timestamp()


class ChangeoverService:
    """应用服务: 校验规则 -> 追加事件 -> 更新投影。"""

    def __init__(self, catalog: Catalog, store: AppendOnlyStore | None = None) -> None:
        self.catalog = catalog
        self.store = store or AppendOnlyStore()
        self._seq = 0
        self.changeovers: dict[str, Changeover] = {}
        self.tokens: dict[str, Token] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        # (device_id, device_seq) -> {"kind", "id", "fingerprint"} 离线幂等索引
        self._device_keys: dict[tuple[str, int], dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def _reject(self, category: str, reason: str, changeover_id: str | None = None,
                now: float | None = None, **details: Any) -> None:
        """记录拒绝事件并抛出 DomainRejection。"""
        self.store.append(
            f"{category}_rejected",
            recorded_at=now if now is not None else time.time(),
            changeover_id=changeover_id,
            reason=reason,
            details=details,
        )
        raise DomainRejection(reason, **details)

    def _get(self, changeover_id: str) -> Changeover:
        co = self.changeovers.get(changeover_id)
        if co is None:
            raise DomainRejection("unknown_changeover", changeover_id=changeover_id)
        return co

    def _set_state(self, co: Changeover, new_state: ChangeoverState, now: float) -> None:
        old = co.state
        co.state = new_state
        self.store.append(
            "state_changed", recorded_at=now, changeover_id=co.changeover_id,
            from_state=old.value, to_state=new_state.value,
        )

    def _check_post(self, co: Changeover, user_id: str, action: str, now: float) -> None:
        """跨岗代签校验: 人员必须持有该动作要求的岗位。"""
        if not self.catalog.holds_post_for(user_id, action):
            self._reject(
                "signature", "cross_post_signature", co.changeover_id, now,
                signer_id=user_id, action=action,
                required=self.catalog.signature_policy.get(action, []),
                held=self.catalog.posts_of(user_id),
            )

    # ------------------------------------------------------------------
    # 建批与范围生成
    # ------------------------------------------------------------------

    def open_changeover(self, line: str, from_product: str, to_product: str,
                        opened_by: str, now: float | None = None) -> str:
        """按当前矩阵版本生成检查范围, 返回批次号。"""
        now = now if now is not None else time.time()
        if line not in self.catalog.lines:
            raise DomainRejection("unknown_line", line=line)
        if from_product not in self.catalog.products:
            raise DomainRejection("unknown_product", product_id=from_product)
        if to_product not in self.catalog.products:
            raise DomainRejection("unknown_product", product_id=to_product)
        matrix = self.catalog.current_matrix()
        entry = self.catalog.matrix_entry(matrix, from_product, to_product)
        if entry is None:
            raise DomainRejection(
                "unknown_transition", from_product=from_product, to_product=to_product,
                matrix_version=matrix["version"],
            )

        changeover_id = self._next_id("CO")
        co = Changeover(
            changeover_id=changeover_id,
            line=line,
            from_product=from_product,
            to_product=to_product,
            risk=RiskLevel(entry["risk"]),
            state=ChangeoverState.PREPARED,
            matrix_version=matrix["version"],
            opened_by=opened_by,
            opened_at=now,
        )
        self.changeovers[changeover_id] = co
        self.store.append(
            "changeover_opened", recorded_at=now, changeover_id=changeover_id,
            line=line, from_product=from_product, to_product=to_product,
            risk=entry["risk"], matrix_version=matrix["version"], opened_by=opened_by,
        )
        for scope in entry["scope"]:
            for check in scope["checks"]:
                self._create_item(co, scope["zone_id"], CheckKind(check["kind"]),
                                  check.get("spec", {}), round_no=1, now=now)
        return changeover_id

    def _create_item(self, co: Changeover, zone_id: str, kind: CheckKind,
                     spec: dict[str, Any], round_no: int, now: float,
                     matrix_version: str | None = None) -> CheckItem:
        item = CheckItem(
            item_id=self._next_id("CI"),
            changeover_id=co.changeover_id,
            zone_id=zone_id,
            kind=kind,
            round=round_no,
            spec=dict(spec),
        )
        co.items[item.item_id] = item
        co.rounds[zone_id] = max(round_no, co.rounds.get(zone_id, 0))
        self.store.append(
            "scope_item_added", recorded_at=now, changeover_id=co.changeover_id,
            item_id=item.item_id, zone_id=zone_id, kind=kind.value,
            round=round_no, spec=dict(spec),
            matrix_version=matrix_version or co.matrix_version,
        )
        return item

    def begin_checks(self, changeover_id: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        co = self._get(changeover_id)
        if co.state is not ChangeoverState.PREPARED:
            self._reject("state", "invalid_state", changeover_id, now,
                         expected="prepared", actual=co.state.value)
        self._set_state(co, ChangeoverState.CHECKING, now)

    # ------------------------------------------------------------------
    # 证据提交(扫码 / 计量 / 异常声明)
    # ------------------------------------------------------------------

    def _device_replay(self, device_id: str | None, device_seq: int | None,
                       fingerprint: str) -> dict[str, Any] | None:
        """离线终端幂等: 同设备同序号同内容 -> 返回原结果; 同序号不同内容 -> 冲突。"""
        if device_id is None or device_seq is None:
            return None
        key = (device_id, int(device_seq))
        seen = self._device_keys.get(key)
        if seen is None:
            self._device_keys[key] = {"fingerprint": fingerprint}
            return None
        if seen["fingerprint"] == fingerprint:
            # 仅当首次提交已成功入账才幂等返回; 曾被拒绝的允许重新校验
            return seen if "id" in seen else None
        raise DomainRejection("device_sequence_conflict", device_id=device_id,
                              device_seq=device_seq)

    def submit_evidence(self, changeover_id: str, item_id: str, payload: dict[str, Any],
                        submitted_by: str, device_id: str | None = None,
                        device_seq: int | None = None, device_time: Any = None,
                        now: float | None = None) -> dict[str, Any]:
        """操作员逐项扫码或上传计量结果。

        离线补传: 同 (device_id, device_seq) 重复到达幂等返回;
        检查项若已被后来完成的检查占据, 补传只留痕不覆盖。
        """
        now = now if now is not None else time.time()
        fingerprint = _fingerprint({"changeover_id": changeover_id, "item_id": item_id,
                                    "payload": payload, "submitted_by": submitted_by})
        replay = self._device_replay(device_id, device_seq, fingerprint)
        if replay is not None:
            return {"evidence_id": replay["id"], "idempotent": True}

        co = self._get(changeover_id)
        if co.state is not ChangeoverState.CHECKING:
            self._reject("evidence", "invalid_state", changeover_id, now,
                         item_id=item_id, actual=co.state.value)
        item = co.items.get(item_id)
        if item is None or item.changeover_id != changeover_id:
            self._reject("evidence", "unknown_item", changeover_id, now, item_id=item_id)

        # 标签类校验先于项状态校验: 旧标签重复扫描无论落在哪个项上都要拒绝
        if item.kind is CheckKind.LABEL_SCAN:
            self._validate_label(co, payload, item_id, now)
        elif item.kind is CheckKind.CLEANING_RECORD:
            self._validate_record(co, payload, item_id, now)
        elif item.kind in RESIDUE_CHECK_KINDS or item.kind is CheckKind.DOSING_CALIBRATION:
            self._validate_measurement_shape(payload, item_id, changeover_id, now)

        if item.result is CheckResult.INVALIDATED:
            self._reject("evidence", "item_invalidated", changeover_id, now,
                         item_id=item_id, invalidated_reason=item.invalidated_reason)
        if item.result is not None:
            # 离线补传不得覆盖后来完成的检查: 留痕拒绝, 项结果保持不变
            self._reject("evidence", "item_already_completed", changeover_id, now,
                         item_id=item_id, existing_result=item.result.value)
        if item.kind is CheckKind.DOSING_CALIBRATION:
            # 非残留类计量超限属于无效提交: 拒绝且不留下提交记录, 项保持待检可重测
            limit = item.spec.get("limit")
            if limit is not None and float(payload["value"]) > float(limit):
                self._reject("evidence", "measurement_out_of_limit", changeover_id, now,
                             item_id=item_id, value=payload["value"], limit=limit)

        evidence_id = self._next_id("EV")
        self.store.append(
            "evidence_submitted", recorded_at=now, changeover_id=changeover_id,
            evidence_id=evidence_id, item_id=item_id, zone_id=item.zone_id,
            kind=item.kind.value, payload=dict(payload), submitted_by=submitted_by,
            device_id=device_id, device_seq=device_seq,
            device_time=_to_epoch(device_time),
        )
        item.evidence_ids.append(evidence_id)
        if device_id is not None and device_seq is not None:
            self._device_keys[(device_id, int(device_seq))].update(
                {"kind": "evidence", "id": evidence_id})

        if item.kind is CheckKind.LABEL_SCAN:
            co.scanned_labels.add(payload["label_code"])
            if payload.get("final"):
                self._complete_item(co, item, CheckResult.CLEAR, now)
        elif item.kind is CheckKind.CLEANING_RECORD:
            co.scanned_records.add(payload["record_code"])
            self._complete_item(co, item, CheckResult.CLEAR, now)
        else:  # 计量类: 残留类超限是"发现", 触发重新清洁
            value = float(payload["value"])
            limit = item.spec.get("limit")
            if limit is None or value <= float(limit):
                self._complete_item(co, item, CheckResult.CLEAR, now)
            else:
                self._complete_item(co, item, CheckResult.RESIDUE_FOUND, now)
                self._trigger_reclean(co, item.zone_id, now,
                                      reason="residue_reclean",
                                      detail=f"{item.kind.value} 计量 {value} 超限 {limit}",
                                      trigger_item_id=item.item_id)
        return {"evidence_id": evidence_id, "item_id": item_id,
                "result": item.result.value if item.result else None}

    def _validate_label(self, co: Changeover, payload: dict[str, Any],
                        item_id: str, now: float) -> None:
        label_code = payload.get("label_code")
        product_id = payload.get("product_id")
        if not label_code or not product_id:
            self._reject("evidence", "invalid_payload", co.changeover_id, now,
                         item_id=item_id, need=["label_code", "product_id"])
        if product_id != co.from_product:
            self._reject("evidence", "unexpected_label", co.changeover_id, now,
                         item_id=item_id, label_code=label_code, product_id=product_id,
                         expected_product=co.from_product)
        if label_code in co.scanned_labels:
            self._reject("evidence", "duplicate_label_scan", co.changeover_id, now,
                         item_id=item_id, label_code=label_code)

    def _validate_record(self, co: Changeover, payload: dict[str, Any],
                         item_id: str, now: float) -> None:
        record_code = payload.get("record_code")
        if not record_code or not str(record_code).startswith("CLN-"):
            self._reject("evidence", "invalid_payload", co.changeover_id, now,
                         item_id=item_id, need="record_code 以 CLN- 开头")
        if record_code in co.scanned_records:
            self._reject("evidence", "duplicate_scan", co.changeover_id, now,
                         item_id=item_id, record_code=record_code)

    def _validate_measurement_shape(self, payload: dict[str, Any], item_id: str,
                                    changeover_id: str, now: float) -> None:
        if not isinstance(payload.get("value"), (int, float)):
            self._reject("evidence", "invalid_payload", changeover_id, now,
                         item_id=item_id, need="数值型 value")

    def declare_exception(self, changeover_id: str, item_id: str, reason: str,
                          declared_by: str, now: float | None = None) -> dict[str, Any]:
        """声明异常: 检查项以"不适用"结案, 原因留痕。"""
        now = now if now is not None else time.time()
        co = self._get(changeover_id)
        if co.state is not ChangeoverState.CHECKING:
            self._reject("evidence", "invalid_state", changeover_id, now,
                         actual=co.state.value)
        item = co.items.get(item_id)
        if item is None:
            self._reject("evidence", "unknown_item", changeover_id, now, item_id=item_id)
        if not reason:
            self._reject("evidence", "invalid_payload", changeover_id, now,
                         item_id=item_id, need="异常原因")
        if item.result is not None:
            self._reject("evidence", "item_already_completed", changeover_id, now,
                         item_id=item_id, existing_result=item.result.value)
        evidence_id = self._next_id("EV")
        self.store.append(
            "evidence_submitted", recorded_at=now, changeover_id=changeover_id,
            evidence_id=evidence_id, item_id=item_id, zone_id=item.zone_id,
            kind="exception", payload={"reason": reason}, submitted_by=declared_by,
            device_id=None, device_seq=None, device_time=None,
        )
        item.evidence_ids.append(evidence_id)
        self._complete_item(co, item, CheckResult.NOT_APPLICABLE, now)
        return {"evidence_id": evidence_id, "item_id": item_id,
                "result": CheckResult.NOT_APPLICABLE.value}

    def _complete_item(self, co: Changeover, item: CheckItem,
                       result: CheckResult, now: float) -> None:
        item.result = result
        item.completed_at = now
        self.store.append(
            "item_completed", recorded_at=now, changeover_id=co.changeover_id,
            item_id=item.item_id, zone_id=item.zone_id, kind=item.kind.value,
            round=item.round, result=result.value,
        )

    # ------------------------------------------------------------------
    # 签字
    # ------------------------------------------------------------------

    def sign_zone(self, changeover_id: str, zone_id: str, signer_id: str,
                  now: float | None = None, action: str = "zone_clearance") -> str:
        """区域清场签字: 区域内检查项全部完成且签字人具备对应岗位。"""
        now = now if now is not None else time.time()
        co = self._get(changeover_id)
        if co.state is not ChangeoverState.CHECKING:
            self._reject("signature", "invalid_state", changeover_id, now,
                         actual=co.state.value)
        self._check_post(co, signer_id, action, now)
        if zone_id not in co.rounds:
            self._reject("signature", "zone_not_in_scope", changeover_id, now,
                         zone_id=zone_id)
        round_no = co.rounds[zone_id]
        pending = [i.item_id for i in co.items.values()
                   if i.zone_id == zone_id and i.round == round_no
                   and i.result is None]
        if pending:
            self._reject("signature", "zone_incomplete", changeover_id, now,
                         zone_id=zone_id, pending_items=pending)
        for sig in co.signatures:
            if (sig.zone_id == zone_id and sig.action == action
                    and sig.round == round_no and sig.valid):
                self._reject("signature", "duplicate_signature", changeover_id, now,
                             zone_id=zone_id, signer_id=signer_id)
        signature_id = self._next_id("SG")
        post = next(p for p in self.catalog.signature_policy[action]
                    if p in self.catalog.posts_of(signer_id))
        sig = Signature(
            signature_id=signature_id, changeover_id=changeover_id, zone_id=zone_id,
            action=action, round=round_no, signer_id=signer_id, post=post, at=now,
        )
        co.signatures.append(sig)
        self.store.append(
            "signature_added", recorded_at=now, changeover_id=changeover_id,
            signature_id=signature_id, zone_id=zone_id, action=action,
            round=round_no, signer_id=signer_id, post=post,
        )
        return signature_id

    # ------------------------------------------------------------------
    # 残留发现与重新清洁
    # ------------------------------------------------------------------

    def report_residue(self, changeover_id: str, zone_id: str, detail: str,
                       reported_by: str, now: float | None = None) -> None:
        """人工发现残留(如质量员巡检), 触发重新清洁。"""
        now = now if now is not None else time.time()
        co = self._get(changeover_id)
        if co.state not in (ChangeoverState.CHECKING, ChangeoverState.FIRST_ARTICLE):
            self._reject("state", "invalid_state", changeover_id, now,
                         actual=co.state.value)
        if zone_id not in co.rounds:
            self._reject("state", "zone_not_in_scope", changeover_id, now, zone_id=zone_id)
        self._trigger_reclean(co, zone_id, now, reason="residue_reclean",
                              detail=detail or "人工发现残留", trigger_item_id=None)

    def _trigger_reclean(self, co: Changeover, zone_id: str, now: float,
                         reason: str, detail: str,
                         trigger_item_id: str | None) -> None:
        """重新清洁: 只作废相关区域的检查项与签字, 不连带无关区域。"""
        if co.clearance is not None:
            self.store.append(
                "clearance_invalidated", recorded_at=now,
                changeover_id=co.changeover_id, reason=reason, detail=detail,
            )
            co.clearance = None
        if co.first_article is not None:
            self.store.append(
                "first_article_invalidated", recorded_at=now,
                changeover_id=co.changeover_id, reason=reason, detail=detail,
            )
            co.first_article = None
            co.first_article_approved = False
        self._set_state(co, ChangeoverState.RECLEANING, now)
        self.store.append(
            "reclean_triggered", recorded_at=now, changeover_id=co.changeover_id,
            zone_id=zone_id, reason=reason, detail=detail,
            trigger_item_id=trigger_item_id,
        )
        round_no = co.rounds[zone_id]
        for item in co.items.values():
            if (item.zone_id == zone_id and item.round == round_no
                    and item.item_id != trigger_item_id
                    and item.result is not CheckResult.INVALIDATED
                    and item.result is not CheckResult.RESIDUE_FOUND):
                item.result = CheckResult.INVALIDATED
                item.invalidated_reason = reason
                self.store.append(
                    "item_invalidated", recorded_at=now,
                    changeover_id=co.changeover_id, item_id=item.item_id,
                    zone_id=zone_id, reason=reason, detail=detail,
                )
        for sig in co.signatures:
            if sig.zone_id == zone_id and sig.valid:
                sig.valid = False
                sig.invalidated_reason = reason
                sig.invalidated_at = now
                self.store.append(
                    "signature_invalidated", recorded_at=now,
                    changeover_id=co.changeover_id, signature_id=sig.signature_id,
                    zone_id=zone_id, reason=reason, detail=detail,
                )
        co.open_recleans.add(zone_id)

    def complete_reclean(self, changeover_id: str, zone_id: str, completed_by: str,
                         now: float | None = None) -> None:
        """重新清洁完成: 该区域开启新一轮检查项。"""
        now = now if now is not None else time.time()
        co = self._get(changeover_id)
        if co.state is not ChangeoverState.RECLEANING or zone_id not in co.open_recleans:
            self._reject("state", "invalid_state", changeover_id, now,
                         actual=co.state.value, zone_id=zone_id)
        self._check_post(co, completed_by, "reclean", now)
        self.store.append(
            "reclean_completed", recorded_at=now, changeover_id=changeover_id,
            zone_id=zone_id, completed_by=completed_by,
        )
        co.open_recleans.discard(zone_id)
        # 新一轮检查项按当前矩阵版本生成
        matrix = self.catalog.current_matrix()
        entry = self.catalog.matrix_entry(matrix, co.from_product, co.to_product)
        new_round = co.rounds[zone_id] + 1
        if entry is not None:
            for scope in entry["scope"]:
                if scope["zone_id"] == zone_id:
                    for check in scope["checks"]:
                        self._create_item(co, zone_id, CheckKind(check["kind"]),
                                          check.get("spec", {}), new_round, now,
                                          matrix_version=matrix["version"])
        if not co.open_recleans:
            self._set_state(co, ChangeoverState.CHECKING, now)

    # ------------------------------------------------------------------
    # 范围同步与清场结论
    # ------------------------------------------------------------------

    def _sync_scope(self, co: Changeover, now: float) -> bool:
        """把检查范围同步到当前矩阵版本; 返回是否有变更。"""
        matrix = self.catalog.current_matrix()
        if matrix["version"] == co.matrix_version:
            return False
        entry = self.catalog.matrix_entry(matrix, co.from_product, co.to_product)
        if entry is None:
            raise DomainRejection(
                "unknown_transition", from_product=co.from_product,
                to_product=co.to_product, matrix_version=matrix["version"],
            )
        required = {(s["zone_id"], c["kind"]): c.get("spec", {})
                    for s in entry["scope"] for c in s["checks"]}
        active: dict[tuple[str, str], CheckItem] = {}
        for item in co.items.values():
            if item.round == co.rounds[item.zone_id] and item.result is not CheckResult.INVALIDATED:
                active[(item.zone_id, item.kind.value)] = item
        # 新增的必检项: 补建; 区域内已有有效签字的, 签字失效需重签
        for (zone_id, kind), spec in required.items():
            if (zone_id, kind) not in active:
                self._create_item(co, zone_id, CheckKind(kind), spec,
                                  co.rounds.get(zone_id, 1), now)
                self._invalidate_zone_signatures(
                    co, zone_id, now, reason="matrix_scope_changed",
                    detail=f"矩阵 {matrix['version']} 新增 {kind}")
        # 不再要求的待检项: 作废(已完成的保留为历史证据)
        for key, item in active.items():
            if key not in required and item.result is None:
                item.result = CheckResult.INVALIDATED
                item.invalidated_reason = "matrix_scope_changed"
                self.store.append(
                    "item_invalidated", recorded_at=now,
                    changeover_id=co.changeover_id, item_id=item.item_id,
                    zone_id=item.zone_id, reason="matrix_scope_changed",
                    detail=f"矩阵 {matrix['version']} 不再要求 {item.kind.value}",
                )
        old_version = co.matrix_version
        co.matrix_version = matrix["version"]
        self.store.append(
            "matrix_version_applied", recorded_at=now, changeover_id=co.changeover_id,
            from_version=old_version, to_version=matrix["version"],
        )
        return True

    def _invalidate_zone_signatures(self, co: Changeover, zone_id: str, now: float,
                                    reason: str, detail: str) -> None:
        for sig in co.signatures:
            if sig.zone_id == zone_id and sig.valid:
                sig.valid = False
                sig.invalidated_reason = reason
                sig.invalidated_at = now
                self.store.append(
                    "signature_invalidated", recorded_at=now,
                    changeover_id=co.changeover_id, signature_id=sig.signature_id,
                    zone_id=zone_id, reason=reason, detail=detail,
                )

    def conclude_clearance(self, changeover_id: str, now: float | None = None) -> dict[str, Any]:
        """清场结论: 以结论当时的矩阵版本为准。"""
        now = now if now is not None else time.time()
        co = self._get(changeover_id)
        if co.state is not ChangeoverState.CHECKING:
            self._reject("clearance", "invalid_state", changeover_id, now,
                         actual=co.state.value)
        self._sync_scope(co, now)
        matrix = self.catalog.current_matrix()
        entry = self.catalog.matrix_entry(matrix, co.from_product, co.to_product)
        missing: list[dict[str, Any]] = []
        for scope in entry["scope"]:
            zone_id = scope["zone_id"]
            round_no = co.rounds[zone_id]
            for check in scope["checks"]:
                item = self._find_item(co, zone_id, check["kind"], round_no)
                if item is None or item.result not in (CheckResult.CLEAR,
                                                       CheckResult.NOT_APPLICABLE):
                    missing.append({"zone_id": zone_id, "kind": check["kind"],
                                    "item_id": item.item_id if item else None})
            if not any(s.zone_id == zone_id and s.action == "zone_clearance"
                       and s.round == round_no and s.valid for s in co.signatures):
                missing.append({"zone_id": zone_id, "kind": "zone_clearance_signature"})
        if missing:
            self._reject("clearance", "clearance_incomplete", changeover_id, now,
                         missing=missing)
        co.clearance = {"result": "passed", "matrix_version": matrix["version"], "at": now}
        self.store.append(
            "clearance_concluded", recorded_at=now, changeover_id=changeover_id,
            result="passed", matrix_version=matrix["version"],
        )
        self._set_state(co, ChangeoverState.FIRST_ARTICLE, now)
        return co.clearance

    @staticmethod
    def _find_item(co: Changeover, zone_id: str, kind: str, round_no: int) -> CheckItem | None:
        for item in co.items.values():
            if (item.zone_id == zone_id and item.kind.value == kind
                    and item.round == round_no
                    and item.result is not CheckResult.INVALIDATED):
                return item
        return None

    # ------------------------------------------------------------------
    # 设备回执与模具/程序校验
    # ------------------------------------------------------------------

    def record_receipt(self, receipt: dict[str, Any],
                       now: float | None = None) -> dict[str, Any]:
        """登记机器回执(只追加; 同设备同序号幂等, 冲突拒绝)。"""
        now = now if now is not None else time.time()
        required = ("receipt_id", "device_id", "device_seq", "line", "kind")
        if any(k not in receipt for k in required):
            raise DomainRejection("invalid_payload", need=list(required))
        fingerprint = _fingerprint(receipt)
        key = (receipt["device_id"], int(receipt["device_seq"]))
        seen = self._device_keys.get(key)
        if seen is not None:
            if seen["fingerprint"] == fingerprint:
                return {"receipt_id": seen["id"], "idempotent": True}
            self._reject("receipt", "receipt_conflict", None, now,
                         device_id=key[0], device_seq=key[1],
                         existing=seen["id"], incoming=receipt["receipt_id"])
        existing = self.receipts.get(receipt["receipt_id"])
        if existing is not None:
            if _fingerprint(existing) == fingerprint:
                return {"receipt_id": receipt["receipt_id"], "idempotent": True}
            self._reject("receipt", "receipt_conflict", None, now,
                         receipt_id=receipt["receipt_id"])
        if receipt["line"] not in self.catalog.lines:
            self._reject("receipt", "unknown_line", None, now, line=receipt["line"])
        self.receipts[receipt["receipt_id"]] = dict(receipt)
        self._device_keys[key] = {"kind": "receipt", "id": receipt["receipt_id"],
                                  "fingerprint": fingerprint}
        self.store.append(
            "receipt_recorded", recorded_at=now, receipt_id=receipt["receipt_id"],
            device_id=receipt["device_id"], device_seq=int(receipt["device_seq"]),
            line=receipt["line"], kind=receipt["kind"],
            receipt=dict(receipt), emitted_at=_to_epoch(receipt.get("emitted_at")),
        )
        return {"receipt_id": receipt["receipt_id"]}

    def verify_mold_program(self, changeover_id: str, receipt_id: str, verifier_id: str,
                            now: float | None = None) -> bool:
        """模具与程序校验: 回执与目标产品要求一致才算通过。"""
        now = now if now is not None else time.time()
        co = self._get(changeover_id)
        if co.state not in (ChangeoverState.CHECKING, ChangeoverState.FIRST_ARTICLE):
            self._reject("mold_program", "invalid_state", changeover_id, now,
                         actual=co.state.value)
        self._check_post(co, verifier_id, "mold_program", now)
        receipt = self.receipts.get(receipt_id)
        if receipt is None:
            self._reject("mold_program", "unknown_receipt", changeover_id, now,
                         receipt_id=receipt_id)
        if receipt["line"] != co.line:
            self._reject("mold_program", "receipt_line_mismatch", changeover_id, now,
                         receipt_id=receipt_id, receipt_line=receipt["line"], line=co.line)
        product = self.catalog.products[co.to_product]
        expected = {"mold_id": product["mold_id"],
                    "program_version": product["program_version"],
                    "recipe_version": product["recipe_version"]}
        actual = {"mold_id": receipt.get("mold_id"),
                  "program_version": receipt.get("program_version"),
                  "recipe_version": receipt.get("recipe_version")}
        consistent = actual == expected
        co.mold_program = {"receipt_id": receipt_id, "verifier_id": verifier_id,
                           "consistent": consistent, "at": now}
        self.store.append(
            "mold_program_verified", recorded_at=now, changeover_id=changeover_id,
            receipt_id=receipt_id, verifier_id=verifier_id, consistent=consistent,
            expected=expected, actual=actual,
        )
        return consistent

    # ------------------------------------------------------------------
    # 首件检测
    # ------------------------------------------------------------------

    def submit_first_article(self, changeover_id: str, measurements: dict[str, Any],
                             submitted_by: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        co = self._get(changeover_id)
        if co.state is not ChangeoverState.FIRST_ARTICLE:
            self._reject("first_article", "invalid_state", changeover_id, now,
                         actual=co.state.value)
        spec = self.catalog.products[co.to_product]["first_article_spec"]
        results = []
        for name, rule in spec.items():
            value = measurements.get(name)
            if isinstance(rule, (list, tuple)) and len(rule) == 2:
                ok = isinstance(value, (int, float)) and rule[0] <= value <= rule[1]
            else:
                ok = value == rule
            results.append({"name": name, "value": value, "expected": rule, "pass": ok})
        all_pass = all(r["pass"] for r in results)
        co.first_article = {"measurements": results, "all_pass": all_pass,
                            "submitted_by": submitted_by, "at": now}
        self.store.append(
            "first_article_submitted", recorded_at=now, changeover_id=changeover_id,
            measurements=results, all_pass=all_pass, submitted_by=submitted_by,
        )
        return all_pass

    def approve_first_article(self, changeover_id: str, approver_id: str,
                              now: float | None = None) -> None:
        now = now if now is not None else time.time()
        co = self._get(changeover_id)
        if co.state is not ChangeoverState.FIRST_ARTICLE:
            self._reject("first_article", "invalid_state", changeover_id, now,
                         actual=co.state.value)
        self._check_post(co, approver_id, "first_article", now)
        if co.first_article is None:
            self._reject("first_article", "first_article_missing", changeover_id, now)
        if not co.first_article["all_pass"]:
            self._reject("first_article", "first_article_out_of_spec", changeover_id, now,
                         approver_id=approver_id)
        co.first_article_approved = True
        self.store.append(
            "first_article_approved", recorded_at=now, changeover_id=changeover_id,
            approver_id=approver_id,
        )

    # ------------------------------------------------------------------
    # 开线令牌
    # ------------------------------------------------------------------

    def issue_token(self, changeover_id: str, now: float | None = None,
                    ttl_seconds: float | None = None) -> str:
        """清场通过 + 模具程序一致 + 首件获批, 才签发有时限的开线令牌。"""
        now = now if now is not None else time.time()
        co = self._get(changeover_id)
        if co.state is not ChangeoverState.FIRST_ARTICLE:
            self._reject("token", "invalid_state", changeover_id, now,
                         actual=co.state.value)
        gates = {
            "clearance_passed": co.clearance is not None,
            "mold_program_consistent": bool(co.mold_program and co.mold_program["consistent"]),
            "first_article_approved": co.first_article_approved,
        }
        if not all(gates.values()):
            self._reject("token", "release_gates_incomplete", changeover_id, now,
                         gates=gates)
        ttl = ttl_seconds if ttl_seconds is not None else TOKEN_TTL_BY_RISK[co.risk]
        product = self.catalog.products[co.to_product]
        token = Token(
            token_id=self._next_id("TK"), changeover_id=changeover_id, line=co.line,
            product_id=co.to_product, recipe_version=product["recipe_version"],
            issued_at=now, expires_at=now + ttl,
        )
        self.tokens[token.token_id] = token
        co.token_id = token.token_id
        self.store.append(
            "token_issued", recorded_at=now, changeover_id=changeover_id,
            token_id=token.token_id, line=token.line, product_id=token.product_id,
            recipe_version=token.recipe_version, issued_at=token.issued_at,
            expires_at=token.expires_at, ttl_seconds=ttl,
        )
        self._set_state(co, ChangeoverState.RELEASED, now)
        return token.token_id

    def consume_token(self, token_id: str, line: str, product_id: str,
                      recipe_version: str, now: float | None = None) -> None:
        """开线时核销令牌: 绑定不符、已用、过期一律拒绝。"""
        now = now if now is not None else time.time()
        token = self.tokens.get(token_id)
        if token is None:
            self._reject("token_use", "unknown_token", None, now, token_id=token_id)
        if token.state is not TokenState.ISSUED:
            self._reject("token_use", "token_not_usable", token.changeover_id, now,
                         token_id=token_id, state=token.state.value)
        if (line, product_id, recipe_version) != (token.line, token.product_id,
                                                  token.recipe_version):
            self._reject("token_use", "token_binding_mismatch", token.changeover_id, now,
                         token_id=token_id,
                         expected={"line": token.line, "product_id": token.product_id,
                                   "recipe_version": token.recipe_version},
                         actual={"line": line, "product_id": product_id,
                                 "recipe_version": recipe_version})
        if now > token.expires_at:
            token.state = TokenState.EXPIRED
            token.expired_at = now
            self.store.append(
                "token_expired", recorded_at=now, changeover_id=token.changeover_id,
                token_id=token_id, expires_at=token.expires_at,
            )
            self._reject("token_use", "token_expired", token.changeover_id, now,
                         token_id=token_id, expires_at=token.expires_at)
        token.state = TokenState.CONSUMED
        token.consumed_at = now
        self.store.append(
            "token_consumed", recorded_at=now, changeover_id=token.changeover_id,
            token_id=token_id, line=line, product_id=product_id,
            recipe_version=recipe_version,
        )

    def sweep_tokens(self, now: float | None = None) -> list[str]:
        """把已过期的签发令牌标记为过期, 返回本次过期的令牌号。"""
        now = now if now is not None else time.time()
        expired = []
        for token in self.tokens.values():
            if token.state is TokenState.ISSUED and now > token.expires_at:
                token.state = TokenState.EXPIRED
                token.expired_at = now
                expired.append(token.token_id)
                self.store.append(
                    "token_expired", recorded_at=now,
                    changeover_id=token.changeover_id, token_id=token.token_id,
                    expires_at=token.expires_at,
                )
        return expired

    def cancel_changeover(self, changeover_id: str, reason: str,
                          now: float | None = None) -> None:
        now = now if now is not None else time.time()
        co = self._get(changeover_id)
        if co.state is ChangeoverState.CANCELLED:
            self._reject("state", "invalid_state", changeover_id, now,
                         actual=co.state.value)
        token = self.tokens.get(co.token_id) if co.token_id else None
        if token is not None and token.state is TokenState.ISSUED:
            token.state = TokenState.REVOKED
            token.revoked_at = now
            token.revoke_reason = reason
            self.store.append(
                "token_revoked", recorded_at=now, changeover_id=changeover_id,
                token_id=token.token_id, reason=reason,
            )
        self.store.append(
            "changeover_cancelled", recorded_at=now, changeover_id=changeover_id,
            reason=reason,
        )
        self._set_state(co, ChangeoverState.CANCELLED, now)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def items_of(self, changeover_id: str, zone_id: str | None = None) -> list[CheckItem]:
        co = self._get(changeover_id)
        items = list(co.items.values())
        if zone_id is not None:
            items = [i for i in items if i.zone_id == zone_id]
        return sorted(items, key=lambda i: i.item_id)

    def find_item(self, changeover_id: str, zone_id: str, kind: CheckKind,
                  round_no: int | None = None) -> CheckItem | None:
        co = self._get(changeover_id)
        round_no = round_no if round_no is not None else co.rounds.get(zone_id)
        for item in co.items.values():
            if (item.zone_id == zone_id and item.kind is kind
                    and item.round == round_no
                    and item.result is not CheckResult.INVALIDATED):
                return item
        return None
