"""领域记录类型。

证据、签字、失效记录全部设计为只追加的结构：任何更正都以新记录入账，
旧记录保留在原处供批次页面还原。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class ChangeoverState(str, enum.Enum):
    PREPARED = "prepared"
    CHECKING = "checking"
    RECLEANING = "recleaning"
    FIRST_ARTICLE = "first_article"
    RELEASED = "released"
    CANCELLED = "cancelled"


class CheckResult(str, enum.Enum):
    CLEAR = "clear"
    RESIDUE_FOUND = "residue_found"
    NOT_APPLICABLE = "not_applicable"
    INVALIDATED = "invalidated"


class TokenState(str, enum.Enum):
    ISSUED = "issued"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class EvidenceKind(str, enum.Enum):
    SCAN = "scan"                      # 扫码
    MEASUREMENT = "measurement"        # 计量结果
    EXCEPTION = "exception"            # 异常声明
    MACHINE_RECEIPT = "machine_receipt"  # 机器回执


@dataclass(frozen=True)
class EvidenceEvent:
    """一条证据。

    occurred_at 是终端本地时间，recorded_at 是入账时间；离线终端补传时
    两者可能相差很大。device_id + device_seq 是设备内序号，用于补传去重，
    重发同序号消息是幂等的。
    """

    event_id: str
    changeover_id: str
    zone_id: str
    item_id: str
    kind: EvidenceKind
    actor: str
    occurred_at: datetime
    recorded_at: datetime
    seq: int
    payload: dict = field(default_factory=dict)
    device_id: Optional[str] = None
    device_seq: Optional[int] = None

    def indicates_residue(self) -> bool:
        if self.kind is EvidenceKind.EXCEPTION:
            return True
        if self.kind is EvidenceKind.MEASUREMENT:
            return bool(self.payload.get("over_limit"))
        return False


@dataclass(frozen=True)
class Confirmation:
    """人工确认（签字）。签字本身不可改，失效以 Invalidation 记录表达。"""

    confirmation_id: str
    changeover_id: str
    zone_id: str
    item_id: str
    signer: str
    role: str
    result: CheckResult
    signed_at: datetime
    note: str = ""


@dataclass(frozen=True)
class Invalidation:
    """签字失效记录：残留触发重新清洁时追加，只覆盖相关区域。"""

    invalidation_id: str
    confirmation_id: str
    zone_id: str
    item_id: str
    reason: str
    invalidated_at: datetime


@dataclass
class FirstArticle:
    """首件检测数据与放行批准。"""

    submitted_by: str
    submitted_at: datetime
    measurements: dict
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None

    @property
    def approved(self) -> bool:
        return self.approved_by is not None


@dataclass
class TokenTransition:
    at: datetime
    state: TokenState
    reason: str


@dataclass
class LineStartToken:
    """开线令牌：始终绑定产线、产品、配方版本和有效期。"""

    token_id: str
    changeover_id: str
    line_id: str
    product_id: str
    recipe_version: str
    issued_at: datetime
    expires_at: datetime
    state: TokenState = TokenState.ISSUED
    history: list = field(default_factory=list)

    def transition(self, state: TokenState, at: datetime, reason: str) -> None:
        self.state = state
        self.history.append(TokenTransition(at=at, state=state, reason=reason))
