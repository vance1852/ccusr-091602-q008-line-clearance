"""换线服务：工单生命周期、开线条件核验与开线令牌的签发/核销。"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .catalog import DATA_DIR, load_expectations, load_products, load_zones
from .changeover import Changeover
from .errors import (
    InvalidStateError,
    ReleaseBlockedError,
    TokenExpiredError,
    TokenMismatchError,
    TokenStateError,
    UnknownObjectError,
)
from .matrix import load_matrix
from .models import (
    ChangeoverState,
    EvidenceKind,
    LineStartToken,
    TokenState,
    TokenTransition,
)


class ChangeoverService:
    def __init__(self, *, products, zones, matrix, expectations,
                 token_ttl=timedelta(minutes=30)):
        self.products = products
        self.zones = zones
        self.matrix = matrix
        self.expectations = expectations
        self.token_ttl = token_ttl
        self.changeovers: dict = {}
        self.tokens: dict = {}
        self._co_seq = 0
        self._tok_seq = 0

    @classmethod
    def from_data_dir(cls, data_dir=None, *, token_ttl=timedelta(minutes=30)):
        base = Path(data_dir) if data_dir else DATA_DIR
        return cls(
            products=load_products(base / "products.json"),
            zones=load_zones(base / "zones.json"),
            matrix=load_matrix(base / "conversion_matrix.json"),
            expectations=load_expectations(base / "equipment_receipts.json"),
            token_ttl=token_ttl,
        )

    # ---- 工单 ----

    def get(self, changeover_id: str) -> Changeover:
        try:
            return self.changeovers[changeover_id]
        except KeyError:
            raise UnknownObjectError(f"未知换线工单 {changeover_id}") from None

    def prepare_changeover(self, *, line_id, from_product_id, to_product_id,
                           previous_batch, new_batch, recipe_version,
                           now) -> Changeover:
        for product_id in (from_product_id, to_product_id):
            if product_id not in self.products:
                raise UnknownObjectError(f"未知产品 {product_id}")
        from_product = self.products[from_product_id]
        to_product = self.products[to_product_id]
        # 按准备时点生效的转换矩阵版本生成本次检查范围
        matrix_version = self.matrix.version_at(now)
        rule = matrix_version.rule_for(from_product.allergen, to_product.allergen)
        if rule is None:
            raise UnknownObjectError("转换矩阵没有覆盖该产品转换")
        scope = []
        for zone_id in rule.zones:
            if zone_id not in self.zones:
                raise UnknownObjectError(f"转换矩阵引用了未知区域 {zone_id}")
            scope.extend(self.zones[zone_id].items)
        self._co_seq += 1
        changeover = Changeover(
            changeover_id=f"CO-{self._co_seq:04d}",
            line_id=line_id,
            from_product=from_product,
            to_product=to_product,
            previous_batch=previous_batch,
            new_batch=new_batch,
            recipe_version=recipe_version,
            matrix_version=matrix_version.version,
            risk=rule.risk,
            scope=tuple(scope),
            created_at=now,
        )
        self.changeovers[changeover.changeover_id] = changeover
        return changeover

    # ---- 证据与确认（委托给工单，之后检查是否需要吊销令牌） ----

    def _after_evidence(self, changeover, event) -> None:
        if changeover.state is ChangeoverState.RECLEANING and changeover.token_id:
            token = self.tokens.get(changeover.token_id)
            if token is not None and token.state is TokenState.ISSUED:
                token.transition(
                    TokenState.REVOKED, event.recorded_at, "清场复核发现残留，开线令牌吊销"
                )

    def record_scan(self, changeover_id, item_id, **kwargs):
        changeover = self.get(changeover_id)
        event = changeover.record_scan(item_id, **kwargs)
        self._after_evidence(changeover, event)
        return event

    def record_measurement(self, changeover_id, item_id, **kwargs):
        changeover = self.get(changeover_id)
        event = changeover.record_measurement(item_id, **kwargs)
        self._after_evidence(changeover, event)
        return event

    def declare_exception(self, changeover_id, item_id, **kwargs):
        changeover = self.get(changeover_id)
        event = changeover.declare_exception(item_id, **kwargs)
        self._after_evidence(changeover, event)
        return event

    def append_machine_receipt(self, changeover_id, item_id, **kwargs):
        changeover = self.get(changeover_id)
        event = changeover.append_machine_receipt(item_id, **kwargs)
        self._after_evidence(changeover, event)
        return event

    def confirm_item(self, changeover_id, item_id, **kwargs):
        return self.get(changeover_id).confirm_item(item_id, **kwargs)

    def begin_checking(self, changeover_id, now):
        self.get(changeover_id).begin_checking(now)

    def complete_recleaning(self, changeover_id, now):
        self.get(changeover_id).complete_recleaning(now)

    def proceed_to_first_article(self, changeover_id, now):
        self.get(changeover_id).proceed_to_first_article(now)

    def submit_first_article(self, changeover_id, **kwargs):
        return self.get(changeover_id).submit_first_article(**kwargs)

    def approve_first_article(self, changeover_id, **kwargs):
        self.get(changeover_id).approve_first_article(**kwargs)

    def cancel(self, changeover_id, *, reason, now):
        changeover = self.get(changeover_id)
        if changeover.state in (ChangeoverState.RELEASED, ChangeoverState.CANCELLED):
            raise InvalidStateError(f"{changeover.state.value} 状态不能取消")
        changeover.state = ChangeoverState.CANCELLED
        changeover.cancel_reason = reason

    # ---- 开线条件 ----

    def release_readiness(self, changeover_id) -> dict:
        """远程值守视角：是否具备开线条件，缺什么列什么。"""
        changeover = self.get(changeover_id)
        missing = []
        pending = changeover.pending_items()
        if pending:
            missing.append("未完成检查项: " + ", ".join(pending))
        expectation = self.expectations.get(
            (changeover.line_id, changeover.recipe_version)
        )
        if expectation is None:
            missing.append(f"配方 {changeover.recipe_version} 缺少设备回执样例")
        else:
            for item_id, expected in expectation.expected.items():
                effective = changeover.effective_event(item_id)
                if effective is None or effective.kind is not EvidenceKind.MACHINE_RECEIPT:
                    missing.append(f"{item_id} 缺少机器回执")
                    continue
                for key, want in expected.items():
                    got = effective.payload.get(key)
                    if got != want:
                        missing.append(
                            f"{item_id} 与设备回执样例不一致: {key} 期望 {want} 实收 {got}"
                        )
        if changeover.first_article is None or not changeover.first_article.approved:
            missing.append("首件检测未获批")
        return {"ready": not missing, "missing": missing}

    def release(self, changeover_id, *, now) -> LineStartToken:
        """清场通过、模具与程序校验一致且首件获批，才签发有时限的开线令牌。"""
        changeover = self.get(changeover_id)
        if changeover.state is not ChangeoverState.FIRST_ARTICLE:
            raise InvalidStateError(f"当前状态 {changeover.state.value}，不能放行")
        readiness = self.release_readiness(changeover_id)
        if not readiness["ready"]:
            raise ReleaseBlockedError("; ".join(readiness["missing"]))
        changeover.state = ChangeoverState.RELEASED
        self._tok_seq += 1
        token = LineStartToken(
            token_id=f"TOK-{self._tok_seq:04d}",
            changeover_id=changeover.changeover_id,
            line_id=changeover.line_id,
            product_id=changeover.to_product.product_id,
            recipe_version=changeover.recipe_version,
            issued_at=now,
            expires_at=now + self.token_ttl,
        )
        token.history.append(
            TokenTransition(
                at=now, state=TokenState.ISSUED, reason="清场通过、模具与程序一致、首件获批"
            )
        )
        self.tokens[token.token_id] = token
        changeover.token_id = token.token_id
        return token

    # ---- 令牌 ----

    def get_token(self, token_id: str) -> LineStartToken:
        try:
            return self.tokens[token_id]
        except KeyError:
            raise UnknownObjectError(f"未知令牌 {token_id}") from None

    def consume_token(self, token_id, *, line_id, product_id, recipe_version,
                      now) -> LineStartToken:
        token = self.get_token(token_id)
        if token.state is TokenState.ISSUED and now > token.expires_at:
            token.transition(TokenState.EXPIRED, now, "超过有效期未使用")
        if token.state is TokenState.EXPIRED:
            raise TokenExpiredError(
                f"令牌 {token_id} 已于 {token.expires_at.isoformat()} 过期，禁止使用"
            )
        if token.state is not TokenState.ISSUED:
            raise TokenStateError(f"令牌 {token_id} 当前状态 {token.state.value}，不能使用")
        if (line_id, product_id, recipe_version) != (
            token.line_id,
            token.product_id,
            token.recipe_version,
        ):
            raise TokenMismatchError("令牌绑定的产线/产品/配方版本与请求不一致")
        token.transition(TokenState.CONSUMED, now, "产线开线核销")
        return token

    def revoke_token(self, token_id, *, reason, now) -> LineStartToken:
        token = self.get_token(token_id)
        if token.state is not TokenState.ISSUED:
            raise TokenStateError(f"令牌 {token_id} 当前状态 {token.state.value}，不能吊销")
        token.transition(TokenState.REVOKED, now, reason)
        return token

    def expire_tokens(self, now) -> None:
        for token in self.tokens.values():
            if token.state is TokenState.ISSUED and now > token.expires_at:
                token.transition(TokenState.EXPIRED, now, "超过有效期未使用")
