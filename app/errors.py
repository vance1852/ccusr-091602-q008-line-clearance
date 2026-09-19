"""领域异常：所有被业务规则明确拒绝的操作。"""


class DomainError(Exception):
    """领域规则拒绝的基类。"""


class UnknownObjectError(DomainError):
    """引用了不存在的对象。"""


class InvalidStateError(DomainError):
    """当前状态不允许该操作。"""


class EvidenceMissingError(DomainError):
    """确认所需的证据尚未齐备。"""


class ResidueUnresolvedError(DomainError):
    """残留未清除，禁止确认清场。"""


class CrossPostSigningError(DomainError):
    """跨岗代签被拒绝。"""


class DuplicateScanError(DomainError):
    """同一编码重复扫描。"""


class StaleLabelError(DomainError):
    """上一批旧标签被当作本批证据。"""


class ReleaseBlockedError(DomainError):
    """开线条件未满足。"""


class FirstArticleError(DomainError):
    """首件流程违规。"""


class TokenStateError(DomainError):
    """令牌状态不允许该操作。"""


class TokenExpiredError(DomainError):
    """开线令牌已过期。"""


class TokenMismatchError(DomainError):
    """令牌与产线/产品/配方版本不匹配。"""
