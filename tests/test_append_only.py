"""只追加与离线补传规则。"""
import unittest

from app.models import CheckKind, CheckResult
from app.service import DomainRejection

from helpers import MIN, T0, item_id, make_service, open_high_risk


class OfflineBackfillTest(unittest.TestCase):
    def test_late_backfill_does_not_overwrite_completed_item(self):
        """离线终端补传不得覆盖后来完成的检查: 拒绝留痕, 项结果不变。"""
        _, store, service = make_service("v2")
        co = open_high_risk(service, T0)
        hopper = item_id(service, co, "Z-HOPPER", CheckKind.HOPPER_RESIDUE)
        # 在线终端 08:10 完成计量
        service.submit_evidence(co, hopper, {"value": 0.4}, "U-OP1",
                                device_id="TERM-01", device_seq=7, now=T0 + 10 * MIN)
        # 离线终端 TERM-07 的 08:05 旧消息 08:20 才补传到
        with self.assertRaises(DomainRejection) as ctx:
            service.submit_evidence(co, hopper, {"value": 9.9}, "U-OP2",
                                    device_id="TERM-07", device_seq=12,
                                    device_time=T0 + 5 * MIN, now=T0 + 20 * MIN)
        self.assertEqual(ctx.exception.reason, "item_already_completed")
        item = service.find_item(co, "Z-HOPPER", CheckKind.HOPPER_RESIDUE)
        self.assertEqual(item.result, CheckResult.CLEAR)  # 未被覆盖成残留
        rejected = store.of_type("evidence_rejected")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["details"]["item_id"], hopper)

    def test_same_device_message_replayed_is_idempotent(self):
        """同设备同序号同内容的重发幂等返回, 不产生第二条证据。"""
        _, store, service = make_service("v2")
        co = open_high_risk(service, T0)
        hopper = item_id(service, co, "Z-HOPPER", CheckKind.HOPPER_RESIDUE)
        first = service.submit_evidence(co, hopper, {"value": 0.4}, "U-OP1",
                                        device_id="TERM-01", device_seq=7, now=T0)
        replay = service.submit_evidence(co, hopper, {"value": 0.4}, "U-OP1",
                                         device_id="TERM-01", device_seq=7,
                                         now=T0 + MIN)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["evidence_id"], first["evidence_id"])
        self.assertEqual(len(store.of_type("evidence_submitted")), 1)

    def test_same_device_seq_different_content_rejected(self):
        """同设备同序号但内容不同: 序号冲突, 明确拒绝。"""
        _, _, service = make_service("v2")
        co = open_high_risk(service, T0)
        hopper = item_id(service, co, "Z-HOPPER", CheckKind.HOPPER_RESIDUE)
        service.submit_evidence(co, hopper, {"value": 0.4}, "U-OP1",
                                device_id="TERM-01", device_seq=7, now=T0)
        with self.assertRaises(DomainRejection) as ctx:
            service.submit_evidence(co, hopper, {"value": 0.5}, "U-OP1",
                                    device_id="TERM-01", device_seq=7, now=T0 + MIN)
        self.assertEqual(ctx.exception.reason, "device_sequence_conflict")

    def test_rejected_submission_leaves_no_evidence_and_allows_retry(self):
        """无效计量(非残留类超限)拒绝后不留提交记录, 同设备消息可修正重传。"""
        _, store, service = make_service("v2")
        co = open_high_risk(service, T0)
        dosing = item_id(service, co, "Z-FILL", CheckKind.DOSING_CALIBRATION)
        with self.assertRaises(DomainRejection) as ctx:
            service.submit_evidence(co, dosing, {"value": 0.9}, "U-OP1",
                                    device_id="TERM-03", device_seq=2, now=T0)
        self.assertEqual(ctx.exception.reason, "measurement_out_of_limit")
        self.assertEqual(store.of_type("evidence_submitted"), [])
        item = service.find_item(co, "Z-FILL", CheckKind.DOSING_CALIBRATION)
        self.assertTrue(item.pending)
        # 修正后重传(同设备同序号同内容之外的合法新值会冲突, 换序号重传)
        service.submit_evidence(co, dosing, {"value": 0.3}, "U-OP1",
                                device_id="TERM-03", device_seq=3, now=T0 + MIN)
        self.assertEqual(item.result, CheckResult.CLEAR)


class ReceiptAppendOnlyTest(unittest.TestCase):
    RECEIPT = {"receipt_id": "RC-9001", "device_id": "ROBOT-01", "device_seq": 41,
               "line": "L1", "kind": "mold_change", "mold_id": "M-02",
               "program_version": "v7", "recipe_version": "R-200",
               "emitted_at": "2026-09-19T08:05:00"}

    def test_receipt_replay_idempotent_and_conflict_rejected(self):
        _, store, service = make_service("v2")
        first = service.record_receipt(dict(self.RECEIPT), now=T0)
        replay = service.record_receipt(dict(self.RECEIPT), now=T0 + MIN)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["receipt_id"], first["receipt_id"])
        # 同设备同序号但内容被篡改: 拒绝, 原回执不变
        tampered = dict(self.RECEIPT, receipt_id="RC-9999", mold_id="M-99")
        with self.assertRaises(DomainRejection) as ctx:
            service.record_receipt(tampered, now=T0 + 2 * MIN)
        self.assertEqual(ctx.exception.reason, "receipt_conflict")
        self.assertEqual(service.receipts["RC-9001"]["mold_id"], "M-02")
        self.assertNotIn("RC-9999", service.receipts)
        self.assertEqual(len(store.of_type("receipt_recorded")), 1)


if __name__ == "__main__":
    unittest.main()
