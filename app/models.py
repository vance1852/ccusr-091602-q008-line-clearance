"""换线清场与首件放行的领域模型。

状态取值与 ``domain_contract.json`` 保持一致, 由 ``tests/test_contract.py`` 校验。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class ChangeoverState(str, enum.Enum):
    """换线批次状态机。"""

    PREPARED = "prepared"            # 已建批, 范围已生成
    CHECKING = "checking"            # 清场检查中
    RECLEANING = "recleaning"        # 发现残留, 重新清洁中
    FIRST_ARTICLE = "first_article"  # 清场通过, 首件检测中
    RELEASED = "released"            # 已放行(开线令牌已签发)
    CANCELLED = "cancelled"          # 已取消


class CheckResult(str, enum.Enum):
    """检查项结果。"""

    CLEAR = "clear"                  # 合格
    RESIDUE_FOUND = "residue_found"  # 发现残留
    NOT_APPLICABLE = "not_applicable"  # 不适用(异常声明豁免)
    INVALIDATED = "invalidated"      # 已失效(重新清洁或矩阵变更)


class TokenState(str, enum.Enum):
    """开线令牌状态。"""

    ISSUED = "issued"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class RiskLevel(str, enum.Enum):
    """产品转换风险等级, 由转换矩阵给出。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CheckKind(str, enum.Enum):
    """检查项类型。"""

    LABEL_SCAN = "label_scan"                # 上一批标签扫码清除
    HOPPER_RESIDUE = "hopper_residue"        # 料斗余料计量
    CLEANING_RECORD = "cleaning_record"      # 清洗记录核对
    SWAB_TEST = "swab_test"                  # 擦拭(致敏原)检测
    DOSING_CALIBRATION = "dosing_calibration"  # 计量器具校准


class EvidenceKind(str, enum.Enum):
    """证据类型: 扫码 / 计量 / 异常声明。"""

    SCAN = "scan"
    MEASUREMENT = "measurement"
    EXCEPTION = "exception"


# 每类检查项接受的证据类型
CHECK_EVIDENCE_KIND: dict[CheckKind, EvidenceKind] = {
    CheckKind.LABEL_SCAN: EvidenceKind.SCAN,
    CheckKind.CLEANING_RECORD: EvidenceKind.SCAN,
    CheckKind.HOPPER_RESIDUE: EvidenceKind.MEASUREMENT,
    CheckKind.SWAB_TEST: EvidenceKind.MEASUREMENT,
    CheckKind.DOSING_CALIBRATION: EvidenceKind.MEASUREMENT,
}

# 计量超限后判定为"发现残留"并触发重新清洁的检查类型;
# 其余计量类型超限属于无效提交, 直接拒绝。
RESIDUE_CHECK_KINDS = {CheckKind.HOPPER_RESIDUE, CheckKind.SWAB_TEST}


@dataclass
class CheckItem:
    """一个待核对对象(区域 x 检查类型 x 轮次)。"""

    item_id: str
    changeover_id: str
    zone_id: str
    kind: CheckKind
    round: int
    spec: dict[str, Any] = field(default_factory=dict)
    result: CheckResult | None = None  # None 表示待检
    completed_at: float | None = None
    invalidated_reason: str | None = None
    evidence_ids: list[str] = field(default_factory=list)

    @property
    def pending(self) -> bool:
        return self.result is None


@dataclass
class Signature:
    """人工签字(只追加; 失效通过新事件标记)。"""

    signature_id: str
    changeover_id: str
    zone_id: str
    action: str
    round: int
    signer_id: str
    post: str
    at: float
    valid: bool = True
    invalidated_reason: str | None = None
    invalidated_at: float | None = None


@dataclass
class Token:
    """开线令牌: 始终绑定产线、产品、配方版本和有效期。"""

    token_id: str
    changeover_id: str
    line: str
    product_id: str
    recipe_version: str
    issued_at: float
    expires_at: float
    state: TokenState = TokenState.ISSUED
    consumed_at: float | None = None
    expired_at: float | None = None
    revoked_at: float | None = None
    revoke_reason: str | None = None


@dataclass
class Changeover:
    """换线批次的当前投影(历史从事件日志还原)。"""

    changeover_id: str
    line: str
    from_product: str
    to_product: str
    risk: RiskLevel
    state: ChangeoverState
    matrix_version: str  # 当前检查范围所依据的转换矩阵版本
    opened_by: str
    opened_at: float
    rounds: dict[str, int] = field(default_factory=dict)  # zone_id -> 当前轮次
    items: dict[str, CheckItem] = field(default_factory=dict)
    signatures: list[Signature] = field(default_factory=list)
    open_recleans: set[str] = field(default_factory=set)
    scanned_labels: set[str] = field(default_factory=set)   # 已清除的旧标签码
    scanned_records: set[str] = field(default_factory=set)  # 已核对的清洗记录码
    clearance: dict[str, Any] | None = None
    mold_program: dict[str, Any] | None = None
    first_article: dict[str, Any] | None = None
    first_article_approved: bool = False
    token_id: str | None = None
