"""范围生成与转换矩阵版本规则。"""
import unittest

from app.models import ChangeoverState, CheckKind, CheckResult
from app.service import DomainRejection

from helpers import MIN, T0, item_id, make_service, open_high_risk


class ScopeGenerationTest(unittest.TestCase):
    def test_scope_follows_transition_risk(self):
        """检查范围按产品转换风险从矩阵生成。"""
        _, _, service = make_service("v2")
        high = service.open_changeover("L1", "P-ALLERGEN", "P-PLAIN", "U-QA", now=T0)
        low = service.open_changeover("L1", "P-PLAIN", "P-PLAIN", "U-QA", now=T0)
        self.assertEqual(len(service.items_of(high)), 8)   # 高风险: 5 区域 8 项
        self.assertEqual(len(service.items_of(low)), 2)    # 低风险: 2 项
        self.assertEqual(service.changeovers[high].risk.value, "high")
        self.assertEqual(service.changeovers[low].risk.value, "low")
        self.assertEqual(service.changeovers[high].state, ChangeoverState.PREPARED)

    def test_unknown_transition_rejected(self):
        _, _, service = make_service("v2")
        with self.assertRaises(DomainRejection) as ctx:
            service.open_changeover("L1", "P-ALLERGEN", "P-ALLERGEN", "U-QA", now=T0)
        self.assertEqual(ctx.exception.reason, "unknown_transition")


class MatrixVersionTest(unittest.TestCase):
    def test_conclusion_uses_matrix_version_current_at_conclusion(self):
        """清场结论必须使用结论当时的矩阵版本: 升版后新增项不补齐不得结论。"""
        catalog, _, service = make_service("v1")
        co = open_high_risk(service, T0)
        self.assertEqual(len(service.items_of(co)), 7)  # v1 范围
        # 完成 v1 全部检查项并逐区签字
        self._complete_v1_scope(service, co)
        for zone in ("Z-PRINT", "Z-LABEL", "Z-HOPPER", "Z-FILL", "Z-CONV"):
            service.sign_zone(co, zone, "U-QA", now=T0 + 30 * MIN)
        # 矩阵升版到 v2 (输送区新增擦拭检测), 结论必须按 v2 范围判定
        catalog.set_current_matrix_version("v2")
        with self.assertRaises(DomainRejection) as ctx:
            service.conclude_clearance(co, now=T0 + 40 * MIN)
        self.assertEqual(ctx.exception.reason, "clearance_incomplete")
        self.assertIn("swab_test", {m["kind"] for m in ctx.exception.details["missing"]})
        # 补齐新增项后重签输送区, 结论通过且记录 v2
        swab = service.find_item(co, "Z-CONV", CheckKind.SWAB_TEST)
        self.assertIsNotNone(swab)
        service.submit_evidence(co, swab.item_id, {"value": 0.1}, "U-OP1",
                                now=T0 + 41 * MIN)
        service.sign_zone(co, "Z-CONV", "U-QA", now=T0 + 42 * MIN)
        conclusion = service.conclude_clearance(co, now=T0 + 43 * MIN)
        self.assertEqual(conclusion["matrix_version"], "v2")

    def _complete_v1_scope(self, service, co):
        service.submit_evidence(co, item_id(service, co, "Z-PRINT", CheckKind.LABEL_SCAN),
                                {"label_code": "LBL-A-001", "product_id": "P-ALLERGEN",
                                 "final": True}, "U-OP1", now=T0 + 5 * MIN)
        service.submit_evidence(co, item_id(service, co, "Z-LABEL", CheckKind.LABEL_SCAN),
                                {"label_code": "LBL-A-002", "product_id": "P-ALLERGEN",
                                 "final": True}, "U-OP1", now=T0 + 6 * MIN)
        service.submit_evidence(co, item_id(service, co, "Z-HOPPER", CheckKind.HOPPER_RESIDUE),
                                {"value": 0.4}, "U-OP1", now=T0 + 7 * MIN)
        service.submit_evidence(co, item_id(service, co, "Z-HOPPER", CheckKind.CLEANING_RECORD),
                                {"record_code": "CLN-881"}, "U-OP1", now=T0 + 8 * MIN)
        service.submit_evidence(co, item_id(service, co, "Z-FILL", CheckKind.CLEANING_RECORD),
                                {"record_code": "CLN-882"}, "U-OP1", now=T0 + 9 * MIN)
        service.submit_evidence(co, item_id(service, co, "Z-FILL", CheckKind.DOSING_CALIBRATION),
                                {"value": 0.2}, "U-OP1", now=T0 + 10 * MIN)
        service.submit_evidence(co, item_id(service, co, "Z-CONV", CheckKind.CLEANING_RECORD),
                                {"record_code": "CLN-883"}, "U-OP1", now=T0 + 11 * MIN)

    def test_retired_pending_item_invalidated_on_matrix_change(self):
        """矩阵升版后不再要求的待检项作废, 不阻塞结论。"""
        catalog, _, service = make_service("v2")
        co = service.open_changeover("L1", "P-ALLERGEN", "P-PLAIN", "U-QA", now=T0)
        service.begin_checks(co, now=T0 + 1)
        swab = service.find_item(co, "Z-CONV", CheckKind.SWAB_TEST)
        self.assertIsNotNone(swab)
        # 回退到 v1: 擦拭项不再要求
        catalog.set_current_matrix_version("v1")
        service._sync_scope(service.changeovers[co], T0 + 2 * MIN)
        self.assertEqual(swab.result, CheckResult.INVALIDATED)
        self.assertEqual(swab.invalidated_reason, "matrix_scope_changed")


if __name__ == "__main__":
    unittest.main()
