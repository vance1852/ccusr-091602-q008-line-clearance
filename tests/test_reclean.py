"""残留发现、重新清洁与签字失效规则。"""
import unittest

from app.models import ChangeoverState, CheckKind, CheckResult
from app.service import DomainRejection

from helpers import MIN, T0, item_id, make_service, open_high_risk


def _clear_hopper_and_conv(service, co):
    """料斗区与输送区完成检查, 贴标区完成并签字。"""
    service.submit_evidence(co, item_id(service, co, "Z-HOPPER", CheckKind.HOPPER_RESIDUE),
                            {"value": 0.4}, "U-OP1", now=T0 + 5 * MIN)
    service.submit_evidence(co, item_id(service, co, "Z-HOPPER", CheckKind.CLEANING_RECORD),
                            {"record_code": "CLN-881"}, "U-OP1", now=T0 + 6 * MIN)
    service.submit_evidence(co, item_id(service, co, "Z-CONV", CheckKind.CLEANING_RECORD),
                            {"record_code": "CLN-883"}, "U-OP1", now=T0 + 7 * MIN)
    service.submit_evidence(co, item_id(service, co, "Z-CONV", CheckKind.SWAB_TEST),
                            {"value": 0.1}, "U-OP1", now=T0 + 8 * MIN)
    service.submit_evidence(co, item_id(service, co, "Z-PRINT", CheckKind.LABEL_SCAN),
                            {"label_code": "LBL-A-001", "product_id": "P-ALLERGEN",
                             "final": True}, "U-OP1", now=T0 + 9 * MIN)


class RecleanTest(unittest.TestCase):
    def test_residue_invalidates_only_related_zone(self):
        """发现残留: 相关区域项与签字失效, 无关区域不连带作废。"""
        _, _, service = make_service("v2")
        co = open_high_risk(service, T0)
        _clear_hopper_and_conv(service, co)
        service.sign_zone(co, "Z-HOPPER", "U-QA", now=T0 + 10 * MIN)
        service.sign_zone(co, "Z-CONV", "U-QA", now=T0 + 11 * MIN)
        service.sign_zone(co, "Z-PRINT", "U-QA", now=T0 + 12 * MIN)
        # 质量员巡检在料斗挡板后发现残留
        service.report_residue(co, "Z-HOPPER", "挡板后可见残粉", "U-QA",
                               now=T0 + 15 * MIN)
        changeover = service.changeovers[co]
        self.assertEqual(changeover.state, ChangeoverState.RECLEANING)
        # 料斗区: 项失效, 签字失效
        hopper_items = service.items_of(co, "Z-HOPPER")
        self.assertTrue(all(i.result is CheckResult.INVALIDATED for i in hopper_items))
        hopper_sig = next(s for s in changeover.signatures if s.zone_id == "Z-HOPPER")
        self.assertFalse(hopper_sig.valid)
        self.assertEqual(hopper_sig.invalidated_reason, "residue_reclean")
        # 无关区域: 项与签字保持有效
        conv_items = service.items_of(co, "Z-CONV")
        self.assertTrue(all(i.result is CheckResult.CLEAR for i in conv_items))
        conv_sig = next(s for s in changeover.signatures if s.zone_id == "Z-CONV")
        self.assertTrue(conv_sig.valid)
        print_sig = next(s for s in changeover.signatures if s.zone_id == "Z-PRINT")
        self.assertTrue(print_sig.valid)

    def test_reclean_opens_new_round_and_restores_checking(self):
        """重新清洁完成后该区域开启新一轮, 批次回到检查态。"""
        _, _, service = make_service("v2")
        co = open_high_risk(service, T0)
        _clear_hopper_and_conv(service, co)
        service.report_residue(co, "Z-HOPPER", "残粉", "U-QA", now=T0 + 15 * MIN)
        service.complete_reclean(co, "Z-HOPPER", "U-OP1", now=T0 + 30 * MIN)
        changeover = service.changeovers[co]
        self.assertEqual(changeover.state, ChangeoverState.CHECKING)
        self.assertEqual(changeover.rounds["Z-HOPPER"], 2)
        round2 = [i for i in service.items_of(co, "Z-HOPPER") if i.round == 2]
        self.assertEqual(len(round2), 2)  # 新一轮: 余料计量 + 清洗记录
        self.assertTrue(all(i.pending for i in round2))
        # 新一轮重新检查合格后可重签
        service.submit_evidence(co, item_id(service, co, "Z-HOPPER", CheckKind.HOPPER_RESIDUE),
                                {"value": 0.3}, "U-OP1", now=T0 + 31 * MIN)
        service.submit_evidence(co, item_id(service, co, "Z-HOPPER", CheckKind.CLEANING_RECORD),
                                {"record_code": "CLN-885"}, "U-OP1", now=T0 + 32 * MIN)
        service.sign_zone(co, "Z-HOPPER", "U-QA", now=T0 + 33 * MIN)

    def test_measurement_over_limit_triggers_reclean_automatically(self):
        """残留类计量超限自动判定 residue_found 并触发重新清洁。"""
        _, _, service = make_service("v2")
        co = open_high_risk(service, T0)
        hopper = item_id(service, co, "Z-HOPPER", CheckKind.HOPPER_RESIDUE)
        service.submit_evidence(co, hopper, {"value": 5.5}, "U-OP1", now=T0 + 5 * MIN)
        item = service.find_item(co, "Z-HOPPER", CheckKind.HOPPER_RESIDUE, round_no=1)
        self.assertEqual(item.result, CheckResult.RESIDUE_FOUND)
        self.assertEqual(service.changeovers[co].state, ChangeoverState.RECLEANING)

    def test_reclean_completion_requires_post(self):
        """重新清洁确认也校验岗位。"""
        _, _, service = make_service("v2")
        co = open_high_risk(service, T0)
        _clear_hopper_and_conv(service, co)
        service.report_residue(co, "Z-HOPPER", "残粉", "U-QA", now=T0 + 15 * MIN)
        with self.assertRaises(DomainRejection) as ctx:
            service.complete_reclean(co, "Z-HOPPER", "U-MECH", now=T0 + 30 * MIN)
        self.assertEqual(ctx.exception.reason, "cross_post_signature")


if __name__ == "__main__":
    unittest.main()
