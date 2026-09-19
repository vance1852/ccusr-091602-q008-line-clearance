"""批次页面: 从只追加事件日志还原一轮换线的全部证据。

质量员据此还原: 每个区域的证据、失效与重签原因、首件数据和最终令牌去向。
"""
from __future__ import annotations

from typing import Any

from .store import AppendOnlyStore

_REJECTED_SUFFIX = "_rejected"


def build_batch_page(store: AppendOnlyStore, changeover_id: str) -> dict[str, Any]:
    events = store.for_changeover(changeover_id)
    events.sort(key=lambda e: e["seq"])

    page: dict[str, Any] = {
        "changeover": None,
        "zones": {},
        "rejections": [],
        "clearance": None,
        "mold_program": None,
        "first_article": None,
        "token": None,
        "timeline": [],
    }
    items: dict[str, dict[str, Any]] = {}
    signatures: dict[str, dict[str, Any]] = {}

    def zone(zone_id: str) -> dict[str, Any]:
        return page["zones"].setdefault(
            zone_id, {"zone_id": zone_id, "items": [], "signatures": [], "recleans": []})

    for e in events:
        etype = e["type"]
        page["timeline"].append({"seq": e["seq"], "at": e["recorded_at"], "type": etype})

        if etype == "changeover_opened":
            page["changeover"] = {
                "changeover_id": changeover_id, "line": e["line"],
                "from_product": e["from_product"], "to_product": e["to_product"],
                "risk": e["risk"], "opened_by": e["opened_by"],
                "opened_at": e["recorded_at"], "state": "prepared",
                "matrix_version_opened": e["matrix_version"],
            }
        elif etype == "state_changed":
            if page["changeover"] is not None:
                page["changeover"]["state"] = e["to_state"]
        elif etype == "matrix_version_applied":
            if page["changeover"] is not None:
                page["changeover"]["matrix_version_current"] = e["to_version"]
        elif etype == "scope_item_added":
            item = {
                "item_id": e["item_id"], "zone_id": e["zone_id"], "kind": e["kind"],
                "round": e["round"], "spec": e["spec"], "result": None,
                "invalidated_reason": None, "evidence": [],
            }
            items[e["item_id"]] = item
            zone(e["zone_id"])["items"].append(item)
        elif etype == "evidence_submitted":
            item = items.get(e["item_id"])
            if item is not None:
                item["evidence"].append({
                    "evidence_id": e["evidence_id"], "kind": e["kind"],
                    "payload": e["payload"], "submitted_by": e["submitted_by"],
                    "device_id": e["device_id"], "device_seq": e["device_seq"],
                    "device_time": e["device_time"], "recorded_at": e["recorded_at"],
                })
        elif etype == "item_completed":
            item = items.get(e["item_id"])
            if item is not None:
                item["result"] = e["result"]
                item["completed_at"] = e["recorded_at"]
        elif etype == "item_invalidated":
            item = items.get(e["item_id"])
            if item is not None:
                item["result"] = "invalidated"
                item["invalidated_reason"] = e["reason"]
                item["invalidated_detail"] = e.get("detail")
        elif etype == "signature_added":
            sig = {
                "signature_id": e["signature_id"], "zone_id": e["zone_id"],
                "action": e["action"], "round": e["round"],
                "signer_id": e["signer_id"], "post": e["post"],
                "at": e["recorded_at"], "status": "valid",
                "invalidated_reason": None, "invalidated_at": None,
            }
            signatures[e["signature_id"]] = sig
            zone(e["zone_id"])["signatures"].append(sig)
        elif etype == "signature_invalidated":
            sig = signatures.get(e["signature_id"])
            if sig is not None:
                sig["status"] = "invalidated"
                sig["invalidated_reason"] = e["reason"]
                sig["invalidated_detail"] = e.get("detail")
                sig["invalidated_at"] = e["recorded_at"]
        elif etype == "reclean_triggered":
            zone(e["zone_id"])["recleans"].append({
                "triggered_at": e["recorded_at"], "reason": e["reason"],
                "detail": e.get("detail"), "trigger_item_id": e.get("trigger_item_id"),
                "completed_at": None, "completed_by": None,
            })
        elif etype == "reclean_completed":
            recleans = zone(e["zone_id"])["recleans"]
            if recleans:
                recleans[-1]["completed_at"] = e["recorded_at"]
                recleans[-1]["completed_by"] = e["completed_by"]
        elif etype == "clearance_concluded":
            page["clearance"] = {"result": e["result"],
                                 "matrix_version": e["matrix_version"],
                                 "at": e["recorded_at"]}
        elif etype == "clearance_invalidated":
            if page["clearance"] is not None:
                page["clearance"]["invalidated"] = True
                page["clearance"]["invalidated_reason"] = e["reason"]
        elif etype == "mold_program_verified":
            page["mold_program"] = {
                "receipt_id": e["receipt_id"], "verifier_id": e["verifier_id"],
                "consistent": e["consistent"], "expected": e["expected"],
                "actual": e["actual"], "at": e["recorded_at"],
            }
        elif etype == "first_article_submitted":
            page["first_article"] = {
                "measurements": e["measurements"], "all_pass": e["all_pass"],
                "submitted_by": e["submitted_by"], "at": e["recorded_at"],
                "approved_by": None, "approved_at": None,
            }
        elif etype == "first_article_approved":
            if page["first_article"] is not None:
                page["first_article"]["approved_by"] = e["approver_id"]
                page["first_article"]["approved_at"] = e["recorded_at"]
        elif etype == "first_article_invalidated":
            if page["first_article"] is not None:
                page["first_article"]["invalidated"] = True
                page["first_article"]["invalidated_reason"] = e["reason"]
        elif etype == "token_issued":
            page["token"] = {
                "token_id": e["token_id"], "state": "issued", "line": e["line"],
                "product_id": e["product_id"], "recipe_version": e["recipe_version"],
                "issued_at": e["issued_at"], "expires_at": e["expires_at"],
                "consumed_at": None, "expired_at": None, "revoked_at": None,
                "revoke_reason": None,
            }
        elif etype == "token_consumed":
            if page["token"] is not None:
                page["token"].update({"state": "consumed",
                                      "consumed_at": e["recorded_at"]})
        elif etype == "token_expired":
            if page["token"] is not None:
                page["token"].update({"state": "expired",
                                      "expired_at": e["recorded_at"]})
        elif etype == "token_revoked":
            if page["token"] is not None:
                page["token"].update({"state": "revoked",
                                      "revoked_at": e["recorded_at"],
                                      "revoke_reason": e["reason"]})
        elif etype.endswith(_REJECTED_SUFFIX):
            page["rejections"].append({
                "category": etype[: -len(_REJECTED_SUFFIX)],
                "reason": e["reason"], "at": e["recorded_at"],
                "details": e.get("details", {}),
            })

    page["zones"] = [page["zones"][z] for z in sorted(page["zones"])]
    page["readiness"] = {
        "clearance_passed": bool(page["clearance"]
                                 and not page["clearance"].get("invalidated")),
        "mold_program_consistent": bool(page["mold_program"]
                                        and page["mold_program"]["consistent"]),
        "first_article_approved": bool(page["first_article"]
                                       and page["first_article"].get("approved_by")),
        "token_active": bool(page["token"] and page["token"]["state"] == "issued"),
    }
    return page
