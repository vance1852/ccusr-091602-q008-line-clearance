"""一次换线的聚合：状态机、只追加证据、签字与失效记录。

关键规则：
- 机器回执与人工确认都只追加，不提供任何修改/删除入口；
- 离线终端补传只增加记录，有效证据按 occurred_at 取最新，
  因此补传的旧记录不会覆盖后来完成的检查；
- 发现残留只使相关区域的签字失效，不连带作废无关区域；
- 清场结论冻结在准备时点的转换矩阵版本与检查范围上。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .catalog import CheckItemDef, Product
from .errors import (
    CrossPostSigningError,
    DuplicateScanError,
    EvidenceMissingError,
    FirstArticleError,
    InvalidStateError,
    ResidueUnresolvedError,
    StaleLabelError,
    UnknownObjectError,
)
from .models import (
    ChangeoverState,
    CheckResult,
    Confirmation,
    EvidenceEvent,
    EvidenceKind,
    FirstArticle,
    Invalidation,
)

_EVIDENCE_KIND = {
    "scan": EvidenceKind.SCAN,
    "measurement": EvidenceKind.MEASUREMENT,
    "machine": EvidenceKind.MACHINE_RECEIPT,
}

# 允许入账证据的状态：机器人可能在正式检查开始前就换好模具；
# 离线终端的补传也可能在首件甚至放行后才到达，只追加入账，
# 若构成更新的残留事实则回到重新清洁并吊销令牌。
_EVIDENCE_STATES = (
    ChangeoverState.PREPARED,
    ChangeoverState.CHECKING,
    ChangeoverState.RECLEANING,
    ChangeoverState.FIRST_ARTICLE,
    ChangeoverState.RELEASED,
)

# 首件放行批准的岗位
QA_ROLE = "qa"


class Changeover:
    """一次换线清场。证据、签字、失效记录都只追加，不回改。"""

    def __init__(
        self,
        *,
        changeover_id: str,
        line_id: str,
        from_product: Product,
        to_product: Product,
        previous_batch: str,
        new_batch: str,
        recipe_version: str,
        matrix_version: str,
        risk: str,
        scope: tuple,
        created_at: datetime,
    ) -> None:
        self.changeover_id = changeover_id
        self.line_id = line_id
        self.from_product = from_product
        self.to_product = to_product
        self.previous_batch = previous_batch
        self.new_batch = new_batch
        self.recipe_version = recipe_version
        # 清场结论必须使用当时版本的转换矩阵：版本号与检查范围在此冻结
        self.matrix_version = matrix_version
        self.risk = risk
        self.scope = tuple(scope)
        self.created_at = created_at
        self.state = ChangeoverState.PREPARED
        self.evidence: list = []
        self.confirmations: list = []
        self.invalidations: list = []
        self.first_article: Optional[FirstArticle] = None
        self.token_id: Optional[str] = None
        self.cancel_reason: Optional[str] = None
        self._seq = 0
        self._scanned_codes: dict = {}      # code -> item_id，用于重复扫描拒绝
        self._device_keys: dict = {}        # (device_id, device_seq) -> event_id，补传去重

    # ---- 内部工具 ----

    def _next_seq(self, prefix: str):
        self._seq += 1
        return self._seq, f"{self.changeover_id}-{prefix}{self._seq:04d}"

    def _require_state(self, *states: ChangeoverState) -> None:
        if self.state not in states:
            names = "/".join(s.value for s in states)
            raise InvalidStateError(f"当前状态 {self.state.value}，需要 {names}")

    def item_def(self, item_id: str) -> CheckItemDef:
        for item in self.scope:
            if item.item_id == item_id:
                return item
        raise UnknownObjectError(f"{item_id} 不在本次检查范围内")

    def events_for(self, item_id: str) -> list:
        return [e for e in self.evidence if e.item_id == item_id]

    def effective_event(self, item_id: str) -> Optional[EvidenceEvent]:
        """该项当前的有效证据：按发生时间取最新。

        离线补传的旧记录（occurred_at 更早）保留在日志里，但不会成为
        有效证据，也就不会覆盖后来完成的检查。
        """
        events = self.events_for(item_id)
        if not events:
            return None
        return max(events, key=lambda e: (e.occurred_at, e.recorded_at, e.seq))

    def _dedup(self, device_id, device_seq) -> Optional[EvidenceEvent]:
        """设备内序号去重：离线终端重发同序号消息是幂等的。"""
        if device_id is None or device_seq is None:
            return None
        event_id = self._device_keys.get((device_id, device_seq))
        if event_id is None:
            return None
        return next(e for e in self.evidence if e.event_id == event_id)

    def _append_evidence(self, *, item, kind, actor, occurred_at, recorded_at,
                         payload, device_id, device_seq) -> EvidenceEvent:
        seq, event_id = self._next_seq("EV")
        event = EvidenceEvent(
            event_id=event_id,
            changeover_id=self.changeover_id,
            zone_id=item.zone_id,
            item_id=item.item_id,
            kind=kind,
            actor=actor,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            seq=seq,
            payload=dict(payload),
            device_id=device_id,
            device_seq=device_seq,
        )
        self.evidence.append(event)
        if device_id is not None and device_seq is not None:
            self._device_keys[(device_id, device_seq)] = event_id
        return event

    # ---- 证据入账（只追加） ----

    def record_scan(self, item_id: str, *, code, actor, occurred_at,
                    recorded_at=None, code_batch=None, device_id=None,
                    device_seq=None) -> EvidenceEvent:
        self._require_state(*_EVIDENCE_STATES)
        item = self.item_def(item_id)
        if item.evidence != "scan":
            raise InvalidStateError(f"{item.name} 不接受扫码证据")
        existing = self._dedup(device_id, device_seq)
        if existing is not None:
            return existing
        recorded_at = recorded_at or occurred_at
        if code in self._scanned_codes:
            raise DuplicateScanError(
                f"编码 {code} 已在检查项 {self._scanned_codes[code]} 扫描过，禁止重复扫描"
            )
        if (item.expected_batch == "current" and code_batch is not None
                and code_batch != self.new_batch):
            raise StaleLabelError(
                f"标签 {code} 属于批次 {code_batch}，不是本批 {self.new_batch}，旧标签不得作为本批证据"
            )
        if (item.expected_batch == "previous" and code_batch is not None
                and code_batch != self.previous_batch):
            raise StaleLabelError(
                f"标签 {code} 属于批次 {code_batch}，不是上一批 {self.previous_batch}"
            )
        event = self._append_evidence(
            item=item, kind=EvidenceKind.SCAN, actor=actor,
            occurred_at=occurred_at, recorded_at=recorded_at,
            payload={"code": code, "code_batch": code_batch},
            device_id=device_id, device_seq=device_seq,
        )
        self._scanned_codes[code] = item_id
        return event

    def record_measurement(self, item_id: str, *, value, actor, occurred_at,
                           recorded_at=None, unit=None, device_id=None,
                           device_seq=None) -> EvidenceEvent:
        self._require_state(*_EVIDENCE_STATES)
        item = self.item_def(item_id)
        if item.evidence != "measurement":
            raise InvalidStateError(f"{item.name} 不接受计量证据")
        existing = self._dedup(device_id, device_seq)
        if existing is not None:
            return existing
        recorded_at = recorded_at or occurred_at
        over_limit = item.limit is not None and value > item.limit
        event = self._append_evidence(
            item=item, kind=EvidenceKind.MEASUREMENT, actor=actor,
            occurred_at=occurred_at, recorded_at=recorded_at,
            payload={
                "value": value,
                "unit": unit or item.unit,
                "limit": item.limit,
                "over_limit": over_limit,
            },
            device_id=device_id, device_seq=device_seq,
        )
        if over_limit:
            self._maybe_reclean(item, event)
        return event

    def declare_exception(self, item_id: str, *, actor, description, occurred_at,
                          recorded_at=None, device_id=None,
                          device_seq=None) -> EvidenceEvent:
        self._require_state(*_EVIDENCE_STATES)
        item = self.item_def(item_id)
        existing = self._dedup(device_id, device_seq)
        if existing is not None:
            return existing
        recorded_at = recorded_at or occurred_at
        event = self._append_evidence(
            item=item, kind=EvidenceKind.EXCEPTION, actor=actor,
            occurred_at=occurred_at, recorded_at=recorded_at,
            payload={"description": description},
            device_id=device_id, device_seq=device_seq,
        )
        self._maybe_reclean(item, event)
        return event

    def append_machine_receipt(self, item_id: str, *, device_id, device_seq,
                               payload, occurred_at, recorded_at=None,
                               actor=None) -> EvidenceEvent:
        self._require_state(*_EVIDENCE_STATES)
        item = self.item_def(item_id)
        if item.evidence != "machine":
            raise InvalidStateError(f"{item.name} 不接受机器回执")
        existing = self._dedup(device_id, device_seq)
        if existing is not None:
            return existing
        recorded_at = recorded_at or occurred_at
        return self._append_evidence(
            item=item, kind=EvidenceKind.MACHINE_RECEIPT,
            actor=actor or device_id,
            occurred_at=occurred_at, recorded_at=recorded_at,
            payload=payload, device_id=device_id, device_seq=device_seq,
        )

    # ---- 残留与重新清洁 ----

    def _maybe_reclean(self, item, event) -> None:
        """残留只在它成为该项最新事实时才触发重新清洁。

        离线补传的旧记录不得覆盖后来完成的检查；若放行后才补传到更新的
        残留证据，则回到重新清洁，由服务层吊销已发令牌。
        """
        if self.effective_event(item.item_id) is not event:
            return
        reason = f"{item.name} 发现残留，触发重新清洁"
        self._invalidate_zone(item.zone_id, at=event.recorded_at, reason=reason)
        if self.state in (
            ChangeoverState.CHECKING,
            ChangeoverState.FIRST_ARTICLE,
            ChangeoverState.RELEASED,
        ):
            self.state = ChangeoverState.RECLEANING

    def _invalidate_zone(self, zone_id: str, *, at, reason: str) -> None:
        """只使相关区域的签字失效，不连带作废无关区域。"""
        invalidated_ids = {inv.confirmation_id for inv in self.invalidations}
        for conf in self.confirmations:
            if conf.zone_id == zone_id and conf.confirmation_id not in invalidated_ids:
                _, inv_id = self._next_seq("INV")
                self.invalidations.append(
                    Invalidation(
                        invalidation_id=inv_id,
                        confirmation_id=conf.confirmation_id,
                        zone_id=zone_id,
                        item_id=conf.item_id,
                        reason=reason,
                        invalidated_at=at,
                    )
                )

    def complete_recleaning(self, now: datetime) -> None:
        self._require_state(ChangeoverState.RECLEANING)
        for item in self.scope:
            effective = self.effective_event(item.item_id)
            if effective is not None and effective.indicates_residue():
                raise ResidueUnresolvedError(
                    f"{item.name} 的残留证据仍未被新的合格记录覆盖，不能结束重新清洁"
                )
        self.state = ChangeoverState.CHECKING

    # ---- 人工确认（签字） ----

    def confirm_item(self, item_id: str, *, signer, role, now,
                     result=CheckResult.CLEAR, note="") -> Confirmation:
        self._require_state(ChangeoverState.CHECKING)
        item = self.item_def(item_id)
        if role != item.required_role:
            raise CrossPostSigningError(
                f"{item.name} 需 {item.required_role} 岗位签字，{signer} 的岗位是 {role}，禁止跨岗代签"
            )
        if result is CheckResult.NOT_APPLICABLE:
            if not note:
                raise EvidenceMissingError(f"{item.name} 声明不适用必须填写理由")
        elif result is CheckResult.CLEAR:
            effective = self.effective_event(item_id)
            if effective is None:
                raise EvidenceMissingError(f"{item.name} 缺少证据，不能确认")
            if effective.indicates_residue():
                raise ResidueUnresolvedError(f"{item.name} 的残留尚未被新的合格证据覆盖")
            expected = _EVIDENCE_KIND[item.evidence]
            if effective.kind is not expected:
                raise EvidenceMissingError(f"{item.name} 缺少{item.evidence}类有效证据")
        else:
            raise InvalidStateError(f"人工确认不能给出 {result.value} 结论")
        _, conf_id = self._next_seq("SG")
        conf = Confirmation(
            confirmation_id=conf_id,
            changeover_id=self.changeover_id,
            zone_id=item.zone_id,
            item_id=item_id,
            signer=signer,
            role=role,
            result=result,
            signed_at=now,
            note=note,
        )
        self.confirmations.append(conf)
        return conf

    # ---- 清场结论（基于准备时点冻结的范围） ----

    def item_result(self, item_id: str) -> Optional[CheckResult]:
        effective = self.effective_event(item_id)
        if effective is not None and effective.indicates_residue():
            return CheckResult.RESIDUE_FOUND
        latest = None
        for conf in self.confirmations:
            if conf.item_id != item_id:
                continue
            if latest is None or (conf.signed_at, conf.confirmation_id) >= (
                latest.signed_at,
                latest.confirmation_id,
            ):
                latest = conf
        if latest is None:
            return None
        invalidated_ids = {inv.confirmation_id for inv in self.invalidations}
        if latest.confirmation_id in invalidated_ids:
            return CheckResult.INVALIDATED
        return latest.result

    def pending_items(self) -> list:
        done = (CheckResult.CLEAR, CheckResult.NOT_APPLICABLE)
        return [i.item_id for i in self.scope if self.item_result(i.item_id) not in done]

    def clearance_passed(self) -> bool:
        return not self.pending_items()

    # ---- 状态推进 ----

    def begin_checking(self, now: datetime) -> None:
        self._require_state(ChangeoverState.PREPARED)
        self.state = ChangeoverState.CHECKING

    def proceed_to_first_article(self, now: datetime) -> None:
        self._require_state(ChangeoverState.CHECKING)
        if not self.clearance_passed():
            raise InvalidStateError(f"清场未完成：{', '.join(self.pending_items())}")
        self.state = ChangeoverState.FIRST_ARTICLE

    def submit_first_article(self, *, measurements, actor, now) -> FirstArticle:
        self._require_state(ChangeoverState.FIRST_ARTICLE)
        self.first_article = FirstArticle(
            submitted_by=actor, submitted_at=now, measurements=dict(measurements)
        )
        return self.first_article

    def approve_first_article(self, *, approver, role, now) -> None:
        self._require_state(ChangeoverState.FIRST_ARTICLE)
        if self.first_article is None:
            raise FirstArticleError("首件数据尚未提交")
        if role != QA_ROLE:
            raise CrossPostSigningError(
                f"首件放行必须由 {QA_ROLE} 岗位批准，{approver} 的岗位是 {role}，禁止跨岗代签"
            )
        self.first_article.approved_by = approver
        self.first_article.approved_at = now
