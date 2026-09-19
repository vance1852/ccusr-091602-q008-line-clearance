"""验收：一轮高风险换线（含致敏原 → 普通产品）的完整时间线。

2026-09-19，PKG-01 线由乳清蛋白棒（批次 B260918）切换到原味燕麦棒
（批次 B260919，配方 R-PLAIN-7）。转换矩阵 2.0 判定为高风险，范围覆盖
全部五个区域。时间线中包含：机器人提前换模、发现残留后重新清洁、
签字失效与重签、离线终端补传、首件获批、令牌签发与核销。最后质量员
从批次页面还原全部证据链。
"""
import json
import unittest
from datetime import datetime, timedelta

from app import (
    ChangeoverService,
    ChangeoverState,
    CheckResult,
    EvidenceKind,
    TokenState,
    build_batch_report,
    errors,
)

DAY = datetime(2026, 9, 19)


def at(hour, minute=0):
    return DAY.replace(hour=hour, minute=minute)


class HighRiskChangeoverAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service = ChangeoverService.from_data_dir()
        cls.service = service

        # 08:00 值班质量员建单：含致敏原产品 → 普通产品
        co = service.prepare_changeover(
            line_id="PKG-01",
            from_product_id="MILK-PROTEIN",
            to_product_id="OAT-PLAIN",
            previous_batch="B260918",
            new_batch="B260919",
            recipe_version="R-PLAIN-7",
            now=at(8, 0),
        )
        cls.co = co

        # 08:05 机器人已换好模具，机器回执先于正式检查到达
        service.append_machine_receipt(
            co.changeover_id, "MOLD-VERIFY",
            device_id="ROBOT-01", device_seq=101,
            payload={"mold_id": "MOLD-42"}, occurred_at=at(8, 5),
        )
        service.append_machine_receipt(
            co.changeover_id, "PROGRAM-VERIFY",
            device_id="ROBOT-01", device_seq=102,
            payload={"program": "PKT-3.2.1"}, occurred_at=at(8, 5),
        )

        # 08:06 开始逐项检查
        service.begin_checking(co.changeover_id, at(8, 6))

        # 贴标工位：旧标签清退、新标签装载
        service.record_scan(
            co.changeover_id, "LABEL-OLD-CLEARED",
            code="LBL-OLD-777", code_batch="B260918",
            actor="op-li", occurred_at=at(8, 10),
        )
        service.confirm_item(
            co.changeover_id, "LABEL-OLD-CLEARED",
            signer="op-li", role="operator", now=at(8, 11),
        )
        service.record_scan(
            co.changeover_id, "LABEL-NEW-LOADED",
            code="LBL-NEW-100", code_batch="B260919",
            actor="op-li", occurred_at=at(8, 12),
        )
        service.confirm_item(
            co.changeover_id, "LABEL-NEW-LOADED",
            signer="op-li", role="operator", now=at(8, 13),
        )

        # 输送段目视确认
        service.record_scan(
            co.changeover_id, "CONVEYOR-VISUAL",
            code="ZONE-CONV-01", actor="op-li", occurred_at=at(8, 14),
        )
        service.confirm_item(
            co.changeover_id, "CONVEYOR-VISUAL",
            signer="op-li", role="operator", now=at(8, 14),
        )

        # 机器人单元：技师依据机器回执确认模具与程序
        service.confirm_item(
            co.changeover_id, "MOLD-VERIFY",
            signer="tech-zhao", role="technician", now=at(8, 15),
        )
        service.confirm_item(
            co.changeover_id, "PROGRAM-VERIFY",
            signer="tech-zhao", role="technician", now=at(8, 15),
        )

        # 料斗首次称重合格并签字
        service.record_measurement(
            co.changeover_id, "HOPPER-RESIDUE",
            value=0.0, actor="op-wang", occurred_at=at(8, 16),
        )
        service.confirm_item(
            co.changeover_id, "HOPPER-RESIDUE",
            signer="op-wang", role="operator", now=at(8, 17),
        )

        # 化验室拭子检测合格并签字
        service.record_measurement(
            co.changeover_id, "SWAB-TEST",
            value=0.1, actor="qa-zhang", occurred_at=at(8, 18),
        )
        service.confirm_item(
            co.changeover_id, "SWAB-TEST",
            signer="qa-zhang", role="qa", now=at(8, 19),
        )

        # 08:20 复核发现下料口蛋白粉结块 → 残留，触发重新清洁
        service.declare_exception(
            co.changeover_id, "HOPPER-RESIDUE",
            actor="op-wang", description="下料口发现蛋白粉结块",
            occurred_at=at(8, 20),
        )

        # 08:50 重新清洁完成，称重归零
        service.record_measurement(
            co.changeover_id, "HOPPER-RESIDUE",
            value=0.0, actor="op-wang", occurred_at=at(8, 50),
        )
        service.complete_recleaning(co.changeover_id, at(8, 52))
        # 08:53 料斗区域重签
        service.confirm_item(
            co.changeover_id, "HOPPER-RESIDUE",
            signer="op-wang", role="operator", now=at(8, 53),
        )

        # 09:05 离线终端 TERM-2 补传：
        # - seq 7：08:14 的清洗记录扫码（该项唯一证据，补传后生效）
        # - seq 8：08:10 的超标称重旧记录（不得覆盖 08:50 完成的检查）
        service.record_scan(
            co.changeover_id, "HOPPER-CLEAN-REC",
            code="CLEAN-REC-551", actor="op-wang",
            occurred_at=at(8, 14), recorded_at=at(9, 5),
            device_id="TERM-2", device_seq=7,
        )
        service.record_measurement(
            co.changeover_id, "HOPPER-RESIDUE",
            value=30.0, actor="op-wang",
            occurred_at=at(8, 10), recorded_at=at(9, 5),
            device_id="TERM-2", device_seq=8,
        )
        # 终端重发 seq 7：幂等，不重复入账
        cls.resent = service.record_scan(
            co.changeover_id, "HOPPER-CLEAN-REC",
            code="CLEAN-REC-551", actor="op-wang",
            occurred_at=at(8, 14), recorded_at=at(9, 5),
            device_id="TERM-2", device_seq=7,
        )
        service.confirm_item(
            co.changeover_id, "HOPPER-CLEAN-REC",
            signer="op-wang", role="operator", now=at(9, 6),
        )

        # 09:10 清场通过，进入首件
        service.proceed_to_first_article(co.changeover_id, at(9, 10))
        service.submit_first_article(
            co.changeover_id,
            measurements={"net_weight_g": 42.1, "seal_strength_n": 18.3,
                          "code_check": "pass"},
            actor="op-li", now=at(9, 15),
        )
        service.approve_first_article(
            co.changeover_id, approver="qa-zhang", role="qa", now=at(9, 18),
        )

        # 09:20 放行，签发 30 分钟开线令牌
        cls.token = service.release(co.changeover_id, now=at(9, 20))
        # 09:25 开线核销
        service.consume_token(
            cls.token.token_id, line_id="PKG-01", product_id="OAT-PLAIN",
            recipe_version="R-PLAIN-7", now=at(9, 25),
        )
        cls.report = build_batch_report(service, co.changeover_id)

    # ---- 范围与状态 ----

    def test_scope_generated_from_matrix_at_prepare_time(self):
        co = self.co
        self.assertEqual(co.matrix_version, "2.0")
        self.assertEqual(co.risk, "high")
        self.assertEqual(
            {i.zone_id for i in co.scope},
            {"HOPPER", "LABELER", "ROBOT-CELL", "CONVEYOR", "QA-LAB"},
        )
        self.assertEqual(len(co.scope), 8)

    def test_final_state_released(self):
        self.assertIs(self.co.state, ChangeoverState.RELEASED)

    # ---- 残留、失效与重签 ----

    def test_residue_invalidated_only_hopper_signatures(self):
        invalidated = {inv.confirmation_id: inv for inv in self.co.invalidations}
        self.assertEqual(len(invalidated), 1)
        inv = next(iter(invalidated.values()))
        self.assertEqual(inv.zone_id, "HOPPER")
        self.assertEqual(inv.item_id, "HOPPER-RESIDUE")
        self.assertIn("残留", inv.reason)
        # 无关区域（化验室、贴标等）的签字保持有效
        valid_elsewhere = [
            c for c in self.co.confirmations
            if c.zone_id != "HOPPER" and c.confirmation_id not in invalidated
        ]
        self.assertEqual(len(valid_elsewhere), 6)

    def test_reclean_cycle_visible_in_item_result(self):
        self.assertIs(self.co.item_result("HOPPER-RESIDUE"), CheckResult.CLEAR)
        results = [c.result for c in self.co.confirmations if c.item_id == "HOPPER-RESIDUE"]
        self.assertEqual(len(results), 2)  # 失效一次 + 重签一次

    # ---- 离线补传 ----

    def test_offline_backfill_did_not_overwrite_later_check(self):
        # 补传的 30.0g 旧记录保留在日志里，但有效证据仍是 08:50 的归零称重
        events = self.co.events_for("HOPPER-RESIDUE")
        values = [e.payload["value"] for e in events
                  if e.kind is EvidenceKind.MEASUREMENT]
        self.assertIn(30.0, values)
        effective = self.co.effective_event("HOPPER-RESIDUE")
        self.assertEqual(effective.payload["value"], 0.0)
        self.assertEqual(effective.occurred_at, at(8, 50))
        self.assertIs(self.co.state, ChangeoverState.RELEASED)  # 未被拖回重新清洁

    def test_offline_resend_is_idempotent(self):
        clean_events = self.co.events_for("HOPPER-CLEAN-REC")
        self.assertEqual(len(clean_events), 1)
        self.assertIs(self.resent, clean_events[0])

    # ---- 令牌 ----

    def test_token_bound_and_consumed(self):
        token = self.token
        self.assertIs(token.state, TokenState.CONSUMED)
        self.assertEqual(token.line_id, "PKG-01")
        self.assertEqual(token.product_id, "OAT-PLAIN")
        self.assertEqual(token.recipe_version, "R-PLAIN-7")
        self.assertEqual(token.expires_at, at(9, 20) + timedelta(minutes=30))
        self.assertEqual(
            [h.state for h in token.history],
            [TokenState.ISSUED, TokenState.CONSUMED],
        )

    # ---- 批次页面还原 ----

    def test_report_reconstructs_zone_evidence(self):
        zones = {z["zone_id"]: z for z in self.report["zones"]}
        self.assertEqual(set(zones), {"HOPPER", "LABELER", "ROBOT-CELL", "CONVEYOR", "QA-LAB"})
        self.assertTrue(all(z["cleared"] for z in zones.values()))
        residue = {i["item_id"]: i for i in zones["HOPPER"]["items"]}["HOPPER-RESIDUE"]
        self.assertEqual(residue["result"], "clear")
        effective = [e for e in residue["evidence"] if e["effective"]]
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0]["payload"]["value"], 0.0)

    def test_report_shows_invalidation_and_resign_reason(self):
        zones = {z["zone_id"]: z for z in self.report["zones"]}
        residue = {i["item_id"]: i for i in zones["HOPPER"]["items"]}["HOPPER-RESIDUE"]
        by_status = {}
        for conf in residue["confirmations"]:
            by_status.setdefault(conf["status"], []).append(conf)
        self.assertEqual(len(by_status["invalidated"]), 1)
        self.assertIn("残留", by_status["invalidated"][0]["invalidation_reason"])
        self.assertEqual(len(by_status["valid"]), 1)
        self.assertEqual(by_status["valid"][0]["signed_at"], at(8, 53).isoformat())

    def test_report_shows_first_article_and_token_whereabouts(self):
        fa = self.report["first_article"]
        self.assertTrue(fa["approved"])
        self.assertEqual(fa["approved_by"], "qa-zhang")
        self.assertEqual(fa["measurements"]["net_weight_g"], 42.1)
        token = self.report["token"]
        self.assertEqual(token["state"], "consumed")
        self.assertEqual(
            [h["state"] for h in token["history"]], ["issued", "consumed"]
        )

    def test_report_timeline_is_chronological_and_serializable(self):
        ats = [entry["at"] for entry in self.report["timeline"]]
        self.assertEqual(ats, sorted(ats))
        json.dumps(self.report)  # 批次页面必须可序列化

    def test_readiness_after_release(self):
        self.assertTrue(self.report["readiness"]["ready"])


class RejectionAcceptanceTest(unittest.TestCase):
    """同一轮换线中必须被明确拒绝的操作。"""

    def setUp(self):
        self.service = ChangeoverService.from_data_dir()
        self.co = self.service.prepare_changeover(
            line_id="PKG-01",
            from_product_id="MILK-PROTEIN",
            to_product_id="OAT-PLAIN",
            previous_batch="B260918",
            new_batch="B260919",
            recipe_version="R-PLAIN-7",
            now=at(8, 0),
        )
        self.service.begin_checking(self.co.changeover_id, at(8, 6))

    def test_cross_post_signing_rejected(self):
        self.service.append_machine_receipt(
            self.co.changeover_id, "MOLD-VERIFY",
            device_id="ROBOT-01", device_seq=1,
            payload={"mold_id": "MOLD-42"}, occurred_at=at(8, 7),
        )
        with self.assertRaises(errors.CrossPostSigningError):
            self.service.confirm_item(
                self.co.changeover_id, "MOLD-VERIFY",
                signer="op-li", role="operator", now=at(8, 8),
            )

    def test_stale_label_rejected(self):
        with self.assertRaises(errors.StaleLabelError):
            self.service.record_scan(
                self.co.changeover_id, "LABEL-NEW-LOADED",
                code="LBL-OLD-778", code_batch="B260918",
                actor="op-li", occurred_at=at(8, 8),
            )

    def test_duplicate_scan_rejected(self):
        self.service.record_scan(
            self.co.changeover_id, "LABEL-OLD-CLEARED",
            code="LBL-OLD-777", code_batch="B260918",
            actor="op-li", occurred_at=at(8, 8),
        )
        with self.assertRaises(errors.DuplicateScanError):
            self.service.record_scan(
                self.co.changeover_id, "LABEL-NEW-LOADED",
                code="LBL-OLD-777", code_batch="B260919",
                actor="op-li", occurred_at=at(8, 9),
            )


if __name__ == "__main__":
    unittest.main()
