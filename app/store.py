"""只追加的事件日志。

机器回执、人工确认、证据与状态变迁统一追加到这里;
不提供修改与删除, 批次页面由此还原全部历史。
"""
from __future__ import annotations

from typing import Any


class AppendOnlyStore:
    """内存版只追加日志(接口与持久化实现保持一致)。"""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def append(self, event_type: str, recorded_at: float, **payload: Any) -> dict[str, Any]:
        event = {
            "seq": len(self._events) + 1,
            "type": event_type,
            "recorded_at": recorded_at,
            **payload,
        }
        self._events.append(event)
        return event

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        wanted = set(types)
        return [e for e in self._events if e["type"] in wanted]

    def for_changeover(self, changeover_id: str) -> list[dict[str, Any]]:
        return [e for e in self._events if e.get("changeover_id") == changeover_id]
