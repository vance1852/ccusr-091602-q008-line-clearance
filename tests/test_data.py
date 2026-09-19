"""仓库样例数据的集成校验: 区域清单与设备回执样例是可用的。"""
import json
import unittest

from app.models import CheckKind

from helpers import DATA_DIR, MIN, T0, make_service, open_high_risk


class SampleDataTest(unittest.TestCase):
    def test_sample_receipts_recordable_and_verifiable(self):
        """设备回执样例可逐条登记, 模具回执可用于一致性校验。"""
        _, _, service = make_service("v2")
        sample = json.loads(
            (DATA_DIR / "equipment_receipts.sample.json").read_text(encoding="utf-8"))
        for i, receipt in enumerate(sample["receipts"]):
            service.record_receipt(receipt, now=T0 + i * MIN)
        self.assertEqual(len(service.receipts), 3)
        co = open_high_risk(service, T0 + 10 * MIN)
        # RC-9001 (M-02 / v7 / R-200) 与目标产品 P-PLAIN 要求一致
        self.assertTrue(service.verify_mold_program(co, "RC-9001", "U-MECH",
                                                    now=T0 + 20 * MIN))
        # RC-9002 (M-01 / v5 / R-100) 是旧产品配置, 不一致
        self.assertFalse(service.verify_mold_program(co, "RC-9002", "U-MECH",
                                                     now=T0 + 21 * MIN))

    def test_matrix_scope_zones_exist_in_zone_list(self):
        """转换矩阵引用的区域必须在区域清单中定义。"""
        catalog, _, _ = make_service("v2")
        for version in catalog.matrix["versions"]:
            for entry in version["entries"]:
                for scope in entry["scope"]:
                    self.assertIn(scope["zone_id"], catalog.zones)
                    for check in scope["checks"]:
                        CheckKind(check["kind"])  # 检查类型必须是已知枚举


if __name__ == "__main__":
    unittest.main()
