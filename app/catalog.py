"""静态目录: 区域清单、产品、版本化转换矩阵、人员与签字策略。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Catalog:
    """从 ``data/`` 加载的只读主数据。

    转换矩阵按版本保存; ``current_version`` 指向当前生效版本,
    清场结论必须以结论当时的当前版本为准。
    """

    def __init__(
        self,
        zones: dict[str, Any],
        products: dict[str, Any],
        matrix: dict[str, Any],
        personnel: dict[str, Any],
    ) -> None:
        self.lines: list[str] = list(zones["lines"])
        self.zones: dict[str, dict] = {z["zone_id"]: z for z in zones["zones"]}
        self.products: dict[str, dict] = {p["product_id"]: p for p in products["products"]}
        self.matrix: dict[str, Any] = matrix
        self.people: dict[str, dict] = {p["user_id"]: p for p in personnel["people"]}
        self.signature_policy: dict[str, list[str]] = dict(personnel["signature_policy"])

    @classmethod
    def load(cls, data_dir: str | Path) -> "Catalog":
        data_dir = Path(data_dir)

        def read(name: str) -> Any:
            return json.loads((data_dir / name).read_text(encoding="utf-8"))

        return cls(
            zones=read("zones.json"),
            products=read("products.json"),
            matrix=read("transition_matrix.json"),
            personnel=read("personnel.json"),
        )

    # ---- 转换矩阵 ----

    def matrix_version(self, version: str) -> dict[str, Any]:
        for v in self.matrix["versions"]:
            if v["version"] == version:
                return v
        raise KeyError(f"未知矩阵版本: {version}")

    def current_matrix(self) -> dict[str, Any]:
        return self.matrix_version(self.matrix["current_version"])

    def set_current_matrix_version(self, version: str) -> None:
        """切换当前生效的矩阵版本(模拟矩阵升版)。"""
        self.matrix_version(version)  # 校验存在
        self.matrix["current_version"] = version

    @staticmethod
    def matrix_entry(version_obj: dict[str, Any], from_product: str, to_product: str) -> dict[str, Any] | None:
        for entry in version_obj["entries"]:
            if entry["from"] == from_product and entry["to"] == to_product:
                return entry
        return None

    # ---- 人员与岗位 ----

    def posts_of(self, user_id: str) -> list[str]:
        person = self.people.get(user_id)
        return list(person["posts"]) if person else []

    def holds_post_for(self, user_id: str, action: str) -> bool:
        """该人员是否具备执行 action 签字所需的岗位。"""
        required = set(self.signature_policy.get(action, ()))
        return bool(required & set(self.posts_of(user_id)))
