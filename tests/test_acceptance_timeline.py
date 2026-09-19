"""验收: 一轮高风险换线(含致敏原 -> 普通产品)的完整时间线。

质量员从批次页面还原: 每个区域的证据、失效重签原因、首件数据和最终令牌去向。
"""
import unittest

from app import build_batch_page
from app.models import ChangeoverState, CheckKind, CheckResult, TokenState
from app.service import DomainRejection

from helpers import MIN, T0, item_id, make_service


class AcceptanceTimelineTest(unittest.TestCase):
    def test_high_risk_changeover_timeline(self):
        catalog, store, service = make_service(current_matrix="v1")
        t = T0

        # 08:00 值班质量员开批: 花生酱夹心 -> 原味苏打, 高风险
        co = service.open_changeover("L1", "P-ALLERGEN", "P-PLAIN", "U-QA", now=t)
        self.assertEqual(service.changeovers[co].risk.value, "high")
        self.assertEqual(len(service.items_of(co)), 7)  # v1 矩阵范围
        service.begin_checks(co, now=t + 2 * MIN)

        # 08:05 机器人回执: 模具已换 M-02 / 程序 v7 / 配方 R-200
        service.record_receipt(
            {"receipt_id": "RC-9001", "device_id": "ROBOT-01", "device_seq": 41,
             "line": "L1", "kind": "mold_change", "mold_id": "M-02",
             "program_version": "v7", "recipe_version": "R-200",
             "emitted_at": "2026-09-19T08:05:00"}, now=t + 5 * MIN)

        # 08:06-08:14 操作员分散在多台终端逐项扫码/上传计量
        service.submit_evidence(  # 贴标区: 两张旧标签, 第二张扫完确认无遗留
            co, item_id(service, co, "Z-PRINT", CheckKind.LABEL_SCAN),
            {"label_code": "LBL-A-001", "product_id": "P-ALLERGEN"},
            "U-OP1", device_id="TERM-01", device_seq=7, now=t + 6 * MIN)
        service.submit_evidence(
            co, item_id(service, co, "Z-PRINT", CheckKind.LABEL_SCAN),
            {"label_code": "LBL-A-003", "product_id": "P-ALLERGEN", "final": True},
            "U-OP1", device_id="TERM-01", device_seq=8, now=t + 7 * MIN)
        service.submit_evidence(
            co, item_id(service, co, "Z-LABEL", CheckKind.LABEL_SCAN),
            {"label_code": "LBL-A-002", "product_id": "P-ALLERGEN", "final": True},
            "U-OP2", device_id="TERM-02", device_seq=3, now=t + 8 * MIN)
        service.submit_evidence(
            co, item_id(service, co, "Z-HOPPER", CheckKind.CLEANING_RECORD),
            {"record_code": "CLN-881"}, "U-OP1", device_id="TERM-01", device_seq=9,
            now=t + 9 * MIN)
        service.submit_evidence(  # 料斗余料 0.4g, 合格
            co, item_id(service, co, "Z-HOPPER", CheckKind.HOPPER_RESIDUE),
            {"value": 0.4}, "U-OP1", device_id="TERM-01", device_seq=10,
            now=t + 10 * MIN)
        service.submit_evidence(
            co, item_id(service, co, "Z-FILL", CheckKind.CLEANING_RECORD),
            {"record_code": "CLN-882"}, "U-OP2", device_id="TERM-02", device_seq=4,
            now=t + 11 * MIN)
        service.submit_evidence(
            co, item_id(service, co, "Z-FILL", CheckKind.DOSING_CALIBRATION),
            {"value": 0.2}, "U-OP2", device_id="TERM-02", device_seq=5,
            now=t + 12 * MIN)
        service.submit_evidence(
            co, item_id(service, co, "Z-CONV", CheckKind.CLEANING_RECORD),
            {"record_code": "CLN-883"}, "U-OP1", device_id="TERM-01", device_seq=11,
            now=t + 13 * MIN)

        # 08:16 离线终端 TERM-07 补传 08:04 的旧扫码: 贴标区项已完成, 不得覆盖
        with self.assertRaises(DomainRejection) as late:
            service.submit_evidence(
                co, item_id(service, co, "Z-PRINT", CheckKind.LABEL_SCAN),
                {"label_code": "LBL-A-009", "product_id": "P-ALLERGEN"},
                "U-OP2", device_id="TERM-07", device_seq=12,
                device_time=t + 4 * MIN, now=t + 16 * MIN)
        self.assertEqual(late.exception.reason, "item_already_completed")

        # 08:18 旧标签 LBL-A-001 被重复扫描: 明确拒绝
        with self.assertRaises(DomainRejection) as dup:
            service.submit_evidence(
                co, item_id(service, co, "Z-LABEL", CheckKind.LABEL_SCAN),
                {"label_code": "LBL-A-001", "product_id": "P-ALLERGEN"},
                "U-OP2", device_id="TERM-02", device_seq=6, now=t + 18 * MIN)
        self.assertEqual(dup.exception.reason, "duplicate_label_scan")

        # 08:20 操作员跨岗代签贴标区: 明确拒绝; 08:21 质量员本人签字
        with self.assertRaises(DomainRejection) as cross:
            service.sign_zone(co, "Z-PRINT", "U-OP1", now=t + 20 * MIN)
        self.assertEqual(cross.exception.reason, "cross_post_signature")
        service.sign_zone(co, "Z-PRINT", "U-QA", now=t + 21 * MIN)
        service.sign_zone(co, "Z-LABEL", "U-QA", now=t + 22 * MIN)
        service.sign_zone(co, "Z-FILL", "U-QA", now=t + 23 * MIN)
        service.sign_zone(co, "Z-HOPPER", "U-QA", now=t + 24 * MIN)

        # 08:30 质量员巡检在料斗挡板后发现残粉: 触发重新清洁
        service.report_residue(co, "Z-HOPPER", "挡板后可见花生酱残粉", "U-QA",
                               now=t + 30 * MIN)
        changeover = service.changeovers[co]
        self.assertEqual(changeover.state, ChangeoverState.RECLEANING)
        hopper_sig = next(s for s in changeover.signatures if s.zone_id == "Z-HOPPER")
        self.assertFalse(hopper_sig.valid)  # 料斗区签字失效
        print_sig = next(s for s in changeover.signatures if s.zone_id == "Z-PRINT")
        self.assertTrue(print_sig.valid)    # 无关区域不连带

        # 08:45 重新清洁完成, 料斗区进入第二轮检查并重签
        service.complete_reclean(co, "Z-HOPPER", "U-OP1", now=t + 45 * MIN)
        service.submit_evidence(
            co, item_id(service, co, "Z-HOPPER", CheckKind.HOPPER_RESIDUE),
            {"value": 0.3}, "U-OP1", device_id="TERM-01", device_seq=13,
            now=t + 46 * MIN)
        service.submit_evidence(
            co, item_id(service, co, "Z-HOPPER", CheckKind.CLEANING_RECORD),
            {"record_code": "CLN-885"}, "U-OP1", device_id="TERM-01", device_seq=14,
            now=t + 47 * MIN)
        service.sign_zone(co, "Z-HOPPER", "U-QA", now=t + 48 * MIN)

        # 08:50 转换矩阵升版 v2: 输送区新增致敏原擦拭检测
        catalog.set_current_matrix_version("v2")
        with self.assertRaises(DomainRejection) as early:
            service.conclude_clearance(co, now=t + 50 * MIN)
        self.assertEqual(early.exception.reason, "clearance_incomplete")
        self.assertIn("swab_test", {m["kind"] for m in early.exception.details["missing"]})
        # 08:52 补做擦拭检测 0.1ppm 合格, 输送区签字
        service.submit_evidence(
            co, item_id(service, co, "Z-CONV", CheckKind.SWAB_TEST),
            {"value": 0.1}, "U-OP1", device_id="TERM-01", device_seq=15,
            now=t + 52 * MIN)
        service.sign_zone(co, "Z-CONV", "U-QA", now=t + 53 * MIN)

        # 08:55 清场结论: 按结论当时的 v2 矩阵判定, 通过
        conclusion = service.conclude_clearance(co, now=t + 55 * MIN)
        self.assertEqual(conclusion["matrix_version"], "v2")
        self.assertEqual(changeover.state, ChangeoverState.FIRST_ARTICLE)

        # 08:57 机修校验模具与程序: 与回执一致
        consistent = service.verify_mold_program(co, "RC-9001", "U-MECH",
                                                 now=t + 57 * MIN)
        self.assertTrue(consistent)

        # 09:00 首件检测: 全部在规格内, 质量员批准
        all_pass = service.submit_first_article(
            co, {"weight_g": 100.2, "seal_temp_c": 160, "code_grade": "A"},
            "U-OP1", now=t + 60 * MIN)
        self.assertTrue(all_pass)
        service.approve_first_article(co, "U-QA", now=t + 61 * MIN)

        # 09:02 签发开线令牌(高风险默认 30 分钟有效); 09:05 开线核销
        token_id = service.issue_token(co, now=t + 62 * MIN)
        token = service.tokens[token_id]
        self.assertEqual(token.expires_at - token.issued_at, 30 * MIN)
        self.assertEqual(changeover.state, ChangeoverState.RELEASED)
        service.consume_token(token_id, "L1", "P-PLAIN", "R-200", now=t + 65 * MIN)
        self.assertEqual(token.state, TokenState.CONSUMED)

        # ---- 批次页面还原 ----
        page = build_batch_page(store, co)
        zones = {z["zone_id"]: z for z in page["zones"]}

        # 每个区域的证据可还原
        print_labels = [e["payload"]["label_code"]
                        for i in zones["Z-PRINT"]["items"] for e in i["evidence"]]
        self.assertEqual(print_labels, ["LBL-A-001", "LBL-A-003"])
        hopper_rounds = {(i["round"], i["kind"]): i for i in zones["Z-HOPPER"]["items"]}
        self.assertEqual(hopper_rounds[(1, "hopper_residue")]["result"], "invalidated")
        self.assertEqual(hopper_rounds[(2, "hopper_residue")]["result"], "clear")
        self.assertEqual(
            hopper_rounds[(2, "hopper_residue")]["evidence"][0]["payload"]["value"], 0.3)

        # 失效与重签原因可还原
        sigs = zones["Z-HOPPER"]["signatures"]
        invalidated = next(s for s in sigs if s["status"] == "invalidated")
        self.assertEqual(invalidated["invalidated_reason"], "residue_reclean")
        self.assertIn("残粉", invalidated["invalidated_detail"])
        resigned = next(s for s in sigs if s["status"] == "valid")
        self.assertEqual(resigned["round"], 2)
        reclean = zones["Z-HOPPER"]["recleans"][0]
        self.assertEqual(reclean["reason"], "residue_reclean")
        self.assertIsNotNone(reclean["completed_at"])

        # 拒绝留痕: 离线补传、旧标签重复扫描、跨岗代签
        rejected_reasons = {r["reason"] for r in page["rejections"]}
        self.assertIn("item_already_completed", rejected_reasons)
        self.assertIn("duplicate_label_scan", rejected_reasons)
        self.assertIn("cross_post_signature", rejected_reasons)

        # 清场结论使用当时的矩阵版本
        self.assertEqual(page["changeover"]["matrix_version_opened"], "v1")
        self.assertEqual(page["clearance"]["matrix_version"], "v2")

        # 首件数据可还原
        fa = page["first_article"]
        self.assertTrue(fa["all_pass"])
        self.assertEqual(fa["approved_by"], "U-QA")
        weight = next(m for m in fa["measurements"] if m["name"] == "weight_g")
        self.assertEqual(weight["value"], 100.2)

        # 模具程序校验与最终令牌去向可还原
        self.assertTrue(page["mold_program"]["consistent"])
        self.assertEqual(page["mold_program"]["receipt_id"], "RC-9001")
        self.assertEqual(page["token"]["state"], "consumed")
        self.assertEqual(page["token"]["consumed_at"], t + 65 * MIN)
        self.assertEqual(page["token"]["recipe_version"], "R-200")
        self.assertTrue(all(page["readiness"][k] for k in
                            ("clearance_passed", "mold_program_consistent",
                             "first_article_approved")))

        # 时间线完整: 从开批到令牌核销每个事件都在
        types = [e["type"] for e in page["timeline"]]
        self.assertEqual(types[0], "changeover_opened")
        self.assertEqual(types[-1], "token_consumed")
        self.assertIn("reclean_triggered", types)
        self.assertIn("signature_invalidated", types)


if __name__ == "__main__":
    unittest.main()
