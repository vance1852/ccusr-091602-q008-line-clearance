"""产品转换矩阵：按致敏原转换关系决定风险等级与检查范围，按版本生效。

清场结论必须使用换线准备时点生效的矩阵版本，因此版本在这里按
effective_from 解析，工单创建时把版本与范围冻结进工单快照。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .catalog import DATA_DIR
from .errors import UnknownObjectError


@dataclass(frozen=True)
class MatrixRule:
    from_allergen: bool
    to_allergen: bool
    risk: str               # high | medium | low
    zones: tuple            # 该风险等级必须检查的区域


@dataclass(frozen=True)
class MatrixVersion:
    version: str
    effective_from: datetime
    rules: tuple

    def rule_for(self, from_allergen: bool, to_allergen: bool):
        for rule in self.rules:
            if rule.from_allergen == from_allergen and rule.to_allergen == to_allergen:
                return rule
        return None


class MatrixRegistry:
    def __init__(self, versions):
        if not versions:
            raise UnknownObjectError("转换矩阵没有任何版本")
        self._versions = tuple(sorted(versions, key=lambda v: v.effective_from))

    @property
    def versions(self) -> tuple:
        return self._versions

    def version_at(self, moment: datetime) -> MatrixVersion:
        """返回 moment 时点生效的矩阵版本。"""
        chosen = None
        for version in self._versions:
            if version.effective_from <= moment:
                chosen = version
        if chosen is None:
            raise UnknownObjectError(f"{moment.isoformat()} 之前没有生效的转换矩阵版本")
        return chosen


def load_matrix(path=None) -> MatrixRegistry:
    raw = json.loads(
        Path(path or DATA_DIR / "conversion_matrix.json").read_text(encoding="utf-8")
    )
    versions = []
    for v in raw["versions"]:
        rules = tuple(
            MatrixRule(
                from_allergen=bool(r["from_allergen"]),
                to_allergen=bool(r["to_allergen"]),
                risk=r["risk"],
                zones=tuple(r["zones"]),
            )
            for r in v["rules"]
        )
        versions.append(
            MatrixVersion(
                version=v["version"],
                effective_from=datetime.fromisoformat(v["effective_from"]),
                rules=rules,
            )
        )
    return MatrixRegistry(versions)
