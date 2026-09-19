"""测试公共辅助: 加载目录、构造服务、常用时间基准。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import AppendOnlyStore, Catalog, ChangeoverService  # noqa: E402
from app.models import CheckKind  # noqa: E402

DATA_DIR = ROOT / "data"
T0 = 1_790_000_000.0  # 时间线基准(epoch 秒)
MIN = 60.0


def make_service(current_matrix: str = "v2"):
    catalog = Catalog.load(DATA_DIR)
    catalog.set_current_matrix_version(current_matrix)
    store = AppendOnlyStore()
    service = ChangeoverService(catalog, store)
    return catalog, store, service


def open_high_risk(service: ChangeoverService, now: float = T0) -> str:
    """含致敏原产品 -> 普通产品的高风险换线, 并进入检查态。"""
    co = service.open_changeover(
        line="L1", from_product="P-ALLERGEN", to_product="P-PLAIN",
        opened_by="U-QA", now=now)
    service.begin_checks(co, now=now + 1)
    return co


def item_id(service: ChangeoverService, co: str, zone: str, kind: CheckKind) -> str:
    item = service.find_item(co, zone, kind)
    assert item is not None, f"缺少检查项 {zone}/{kind.value}"
    return item.item_id
