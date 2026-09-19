"""数据目录：区域清单、产品目录和设备回执样例（机器回执必须吻合的期望值）。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    allergen: bool


@dataclass(frozen=True)
class CheckItemDef:
    """区域清单中的一个检查项定义。"""

    item_id: str
    zone_id: str
    name: str
    evidence: str               # scan | measurement | machine
    required_role: str          # 允许签字的岗位
    expected_batch: str = "any"  # any | previous | current（扫码证据应属的批次）
    unit: Optional[str] = None
    limit: Optional[float] = None  # 计量上限，超过即判定残留


@dataclass(frozen=True)
class ZoneDef:
    zone_id: str
    name: str
    required_role: str
    items: tuple


@dataclass(frozen=True)
class EquipmentExpectation:
    """设备回执样例：某产线某配方版本下，机器回执必须一致的字段。"""

    line_id: str
    recipe_version: str
    product_id: str
    expected: dict  # item_id -> {字段: 期望值}


def load_products(path=None) -> dict:
    raw = json.loads(Path(path or DATA_DIR / "products.json").read_text(encoding="utf-8"))
    return {
        p["product_id"]: Product(
            product_id=p["product_id"], name=p["name"], allergen=bool(p["allergen"])
        )
        for p in raw["products"]
    }


def load_zones(path=None) -> dict:
    raw = json.loads(Path(path or DATA_DIR / "zones.json").read_text(encoding="utf-8"))
    zones = {}
    for z in raw["zones"]:
        items = tuple(
            CheckItemDef(
                item_id=i["item_id"],
                zone_id=z["zone_id"],
                name=i["name"],
                evidence=i["evidence"],
                required_role=i.get("required_role", z["required_role"]),
                expected_batch=i.get("expected_batch", "any"),
                unit=i.get("unit"),
                limit=i.get("limit"),
            )
            for i in z["items"]
        )
        zones[z["zone_id"]] = ZoneDef(
            zone_id=z["zone_id"],
            name=z["name"],
            required_role=z["required_role"],
            items=items,
        )
    return zones


def load_expectations(path=None) -> dict:
    raw = json.loads(
        Path(path or DATA_DIR / "equipment_receipts.json").read_text(encoding="utf-8")
    )
    expectations = {}
    for e in raw["expectations"]:
        exp = EquipmentExpectation(
            line_id=e["line_id"],
            recipe_version=e["recipe_version"],
            product_id=e["product_id"],
            expected=dict(e["expected"]),
        )
        expectations[(exp.line_id, exp.recipe_version)] = exp
    return expectations
