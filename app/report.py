"""批次页面：把只追加的记录还原成一轮换线的完整证据链。

质量员从这里能看到：每个区域的证据与有效结论、失效重签的原因、
首件数据，以及最终令牌去向。
"""
from __future__ import annotations


def _iso(dt):
    return dt.isoformat() if dt is not None else None


def build_batch_report(service, changeover_id) -> dict:
    changeover = service.get(changeover_id)
    invalidated = {inv.confirmation_id: inv for inv in changeover.invalidations}

    zones = []
    for zone_id in dict.fromkeys(item.zone_id for item in changeover.scope):
        zone_def = service.zones.get(zone_id)
        items = []
        for item in changeover.scope:
            if item.zone_id != zone_id:
                continue
            effective = changeover.effective_event(item.item_id)
            events = [
                {
                    "event_id": e.event_id,
                    "kind": e.kind.value,
                    "actor": e.actor,
                    "occurred_at": _iso(e.occurred_at),
                    "recorded_at": _iso(e.recorded_at),
                    "device_id": e.device_id,
                    "device_seq": e.device_seq,
                    "payload": dict(e.payload),
                    "effective": e is effective,
                }
                for e in changeover.events_for(item.item_id)
            ]
            confirmations = []
            for conf in changeover.confirmations:
                if conf.item_id != item.item_id:
                    continue
                inv = invalidated.get(conf.confirmation_id)
                confirmations.append(
                    {
                        "confirmation_id": conf.confirmation_id,
                        "signer": conf.signer,
                        "role": conf.role,
                        "result": conf.result.value,
                        "signed_at": _iso(conf.signed_at),
                        "note": conf.note,
                        "status": "invalidated" if inv else "valid",
                        "invalidation_reason": inv.reason if inv else None,
                        "invalidated_at": _iso(inv.invalidated_at) if inv else None,
                    }
                )
            result = changeover.item_result(item.item_id)
            items.append(
                {
                    "item_id": item.item_id,
                    "name": item.name,
                    "required_role": item.required_role,
                    "result": result.value if result else "pending",
                    "evidence": events,
                    "confirmations": confirmations,
                }
            )
        zones.append(
            {
                "zone_id": zone_id,
                "name": zone_def.name if zone_def else zone_id,
                "cleared": all(i["result"] in ("clear", "not_applicable") for i in items),
                "items": items,
            }
        )

    first_article = None
    if changeover.first_article is not None:
        fa = changeover.first_article
        first_article = {
            "submitted_by": fa.submitted_by,
            "submitted_at": _iso(fa.submitted_at),
            "measurements": dict(fa.measurements),
            "approved": fa.approved,
            "approved_by": fa.approved_by,
            "approved_at": _iso(fa.approved_at),
        }

    token = service.tokens.get(changeover.token_id) if changeover.token_id else None
    token_dict = None
    if token is not None:
        token_dict = {
            "token_id": token.token_id,
            "state": token.state.value,
            "line_id": token.line_id,
            "product_id": token.product_id,
            "recipe_version": token.recipe_version,
            "issued_at": _iso(token.issued_at),
            "expires_at": _iso(token.expires_at),
            "history": [
                {"at": _iso(h.at), "state": h.state.value, "reason": h.reason}
                for h in token.history
            ],
        }

    timeline = []
    for e in changeover.evidence:
        timeline.append(
            {
                "at": _iso(e.occurred_at),
                "recorded_at": _iso(e.recorded_at),
                "type": "evidence",
                "kind": e.kind.value,
                "zone_id": e.zone_id,
                "item_id": e.item_id,
                "actor": e.actor,
            }
        )
    for conf in changeover.confirmations:
        timeline.append(
            {
                "at": _iso(conf.signed_at),
                "type": "confirmation",
                "zone_id": conf.zone_id,
                "item_id": conf.item_id,
                "actor": conf.signer,
                "result": conf.result.value,
            }
        )
    for inv in changeover.invalidations:
        timeline.append(
            {
                "at": _iso(inv.invalidated_at),
                "type": "invalidation",
                "zone_id": inv.zone_id,
                "item_id": inv.item_id,
                "reason": inv.reason,
            }
        )
    if token is not None:
        for h in token.history:
            timeline.append(
                {"at": _iso(h.at), "type": "token", "state": h.state.value, "reason": h.reason}
            )
    timeline.sort(key=lambda entry: entry["at"])

    return {
        "changeover": {
            "changeover_id": changeover.changeover_id,
            "line_id": changeover.line_id,
            "state": changeover.state.value,
            "from_product": changeover.from_product.product_id,
            "to_product": changeover.to_product.product_id,
            "previous_batch": changeover.previous_batch,
            "new_batch": changeover.new_batch,
            "recipe_version": changeover.recipe_version,
            "risk": changeover.risk,
            "matrix_version": changeover.matrix_version,
            "created_at": _iso(changeover.created_at),
            "cancel_reason": changeover.cancel_reason,
        },
        "zones": zones,
        "first_article": first_article,
        "token": token_dict,
        "readiness": service.release_readiness(changeover_id),
        "timeline": timeline,
    }
