"""岗位、标签、模具程序与首件规则。"""
import unittest

from app.models import CheckKind
from app.service import DomainRejection

from helpers import MIN, T0, item_id, make_service, open_high_risk


class SignaturePostTest(unittest.TestCase):
    def test_cross_post_signature_rejected(self):
        """跨岗代签: 操作员代质量员签区域清场, 明确拒绝并留痕。"""
        _, store, service = make_service("v2")
        co = open_high_risk(service, T0)
        service.submit_evidence(co, item_id(service, co, "Z-PRINT", CheckKind.LABEL_SCAN),
                                {"label_code": "LBL-A-001", "product_id": "P-ALLERGEN",
                                 "final": True}, "U-OP1", now=T0 + 5 * MIN)
        with self.assertRaises(DomainRejection) as ctx:
            service.sign_zone(co, "Z-PRINT", "U-OP1", now=T0 + 6 * MIN)
        self.assertEqual(ctx.exception.reason, "cross_post_signature")
        rejected = store.of_type("signature_rejected")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["details"]["signer_id"], "U-OP1")
        # 具备岗位的质量员签字通过
        service.sign_zone(co, "Z-PRINT", "U-QA", now=T0 + 7 * MIN)

    def test_sign_before_zone_complete_rejected(self):
        _, _, service = make_service("v2")
        co = open_high_risk(service, T0)
        with self.assertRaises(DomainRejection) as ctx:
            service.sign_zone(co, "Z-HOPPER", "U-QA", now=T0 + 5 * MIN)
        self.assertEqual(ctx.exception.reason, "zone_incomplete")


class LabelScanTest(unittest.TestCase):
    def test_declare_exception_closes_item_as_not_applicable(self):
        """声明异常: 项以"不适用"结案, 原因留痕, 不阻塞清场结论。"""
        _, store, service = make_service("v2")
        co = open_high_risk(service, T0)
        dosing = item_id(service, co, "Z-FILL", CheckKind.DOSING_CALIBRATION)
        result = service.declare_exception(co, dosing, "标准砝码送检未归", "U-OP1",
                                           now=T0 + 5 * MIN)
        self.assertEqual(result["result"], "not_applicable")
        item = service.find_item(co, "Z-FILL", CheckKind.DOSING_CALIBRATION)
        self.assertEqual(item.result.value, "not_applicable")
        submitted = store.of_type("evidence_submitted")
        self.assertEqual(submitted[0]["payload"]["reason"], "标准砝码送检未归")
        # 已结案的项不能再提交证据
        with self.assertRaises(DomainRejection) as ctx:
            service.submit_evidence(co, dosing, {"value": 0.1}, "U-OP1",
                                    now=T0 + 6 * MIN)
        self.assertEqual(ctx.exception.reason, "item_already_completed")

    def test_duplicate_old_label_scan_rejected(self):
        """旧标签重复扫描: 同一旧标签码只能入账一次。"""
        _, _, service = make_service("v2")
        co = open_high_risk(service, T0)
        service.submit_evidence(co, item_id(service, co, "Z-PRINT", CheckKind.LABEL_SCAN),
                                {"label_code": "LBL-A-001", "product_id": "P-ALLERGEN"},
                                "U-OP1", now=T0 + 5 * MIN)
        with self.assertRaises(DomainRejection) as ctx:
            service.submit_evidence(
                co, item_id(service, co, "Z-LABEL", CheckKind.LABEL_SCAN),
                {"label_code": "LBL-A-001", "product_id": "P-ALLERGEN"},
                "U-OP1", now=T0 + 6 * MIN)
        self.assertEqual(ctx.exception.reason, "duplicate_label_scan")

    def test_new_product_label_in_old_label_scan_rejected(self):
        """清场扫到的必须是上一批标签; 扫到新产品标签明确拒绝。"""
        _, _, service = make_service("v2")
        co = open_high_risk(service, T0)
        with self.assertRaises(DomainRejection) as ctx:
            service.submit_evidence(
                co, item_id(service, co, "Z-PRINT", CheckKind.LABEL_SCAN),
                {"label_code": "LBL-B-001", "product_id": "P-PLAIN"},
                "U-OP1", now=T0 + 5 * MIN)
        self.assertEqual(ctx.exception.reason, "unexpected_label")


class MoldProgramTest(unittest.TestCase):
    RECEIPT_OK = {"receipt_id": "RC-9001", "device_id": "ROBOT-01", "device_seq": 41,
                  "line": "L1", "kind": "mold_change", "mold_id": "M-02",
                  "program_version": "v7", "recipe_version": "R-200"}
    RECEIPT_WRONG = {"receipt_id": "RC-9002", "device_id": "ROBOT-01", "device_seq": 42,
                     "line": "L1", "kind": "mold_change", "mold_id": "M-01",
                     "program_version": "v5", "recipe_version": "R-100"}

    def test_mold_program_mismatch_blocks_token(self):
        """模具与程序校验不一致: 记录失败, 令牌不得签发。"""
        _, _, service = make_service("v2")
        co = _fully_cleared(service)
        service.record_receipt(dict(self.RECEIPT_WRONG), now=T0 + 50 * MIN)
        consistent = service.verify_mold_program(co, "RC-9002", "U-MECH",
                                                 now=T0 + 51 * MIN)
        self.assertFalse(consistent)
        service.submit_first_article(co, {"weight_g": 100, "seal_temp_c": 160,
                                          "code_grade": "A"}, "U-OP1", now=T0 + 52 * MIN)
        service.approve_first_article(co, "U-QA", now=T0 + 53 * MIN)
        with self.assertRaises(DomainRejection) as ctx:
            service.issue_token(co, now=T0 + 54 * MIN)
        self.assertEqual(ctx.exception.reason, "release_gates_incomplete")
        self.assertFalse(ctx.exception.details["gates"]["mold_program_consistent"])

    def test_mold_program_verify_requires_mechanic_post(self):
        _, _, service = make_service("v2")
        co = _fully_cleared(service)
        service.record_receipt(dict(self.RECEIPT_OK), now=T0 + 50 * MIN)
        with self.assertRaises(DomainRejection) as ctx:
            service.verify_mold_program(co, "RC-9001", "U-QA", now=T0 + 51 * MIN)
        self.assertEqual(ctx.exception.reason, "cross_post_signature")


class FirstArticleTest(unittest.TestCase):
    def test_out_of_spec_first_article_cannot_be_approved(self):
        _, _, service = make_service("v2")
        co = _fully_cleared(service)
        all_pass = service.submit_first_article(
            co, {"weight_g": 120, "seal_temp_c": 160, "code_grade": "A"},
            "U-OP1", now=T0 + 50 * MIN)
        self.assertFalse(all_pass)
        with self.assertRaises(DomainRejection) as ctx:
            service.approve_first_article(co, "U-QA", now=T0 + 51 * MIN)
        self.assertEqual(ctx.exception.reason, "first_article_out_of_spec")

    def test_first_article_approval_requires_quality_post(self):
        _, _, service = make_service("v2")
        co = _fully_cleared(service)
        service.submit_first_article(co, {"weight_g": 100, "seal_temp_c": 160,
                                          "code_grade": "A"}, "U-OP1", now=T0 + 50 * MIN)
        with self.assertRaises(DomainRejection) as ctx:
            service.approve_first_article(co, "U-OP1", now=T0 + 51 * MIN)
        self.assertEqual(ctx.exception.reason, "cross_post_signature")


def _fully_cleared(service) -> str:
    """完成全部检查与签字并给出清场结论, 返回批次号。"""
    co = open_high_risk(service, T0)
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
    service.submit_evidence(co, item_id(service, co, "Z-CONV", CheckKind.SWAB_TEST),
                            {"value": 0.1}, "U-OP1", now=T0 + 12 * MIN)
    for i, zone in enumerate(("Z-PRINT", "Z-LABEL", "Z-HOPPER", "Z-FILL", "Z-CONV")):
        service.sign_zone(co, zone, "U-QA", now=T0 + (20 + i) * MIN)
    service.conclude_clearance(co, now=T0 + 30 * MIN)
    return co


if __name__ == "__main__":
    unittest.main()
