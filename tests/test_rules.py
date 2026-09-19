"""领域规则单元测试：明确拒绝、矩阵版本快照、只追加与离线补传、令牌生命周期。"""
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app import errors
from app.catalog import CheckItemDef, EquipmentExpectation, Product, ZoneDef
from app.matrix import MatrixRegistry, MatrixRule, MatrixVersion
from app.models import (
    ChangeoverState,
    CheckResult,
    TokenState,
)
from app.service import ChangeoverService

T0 = datetime(2026, 9, 19, 8, 0, 0)


def t(minutes):
    return T0 + timedelta(minutes=minutes)


def make_service(matrix=None, token_ttl=timedelta(minutes=30)):
    products = {
        "P-ALG": Product("P-ALG", "含致敏原产品", True),
        "P-PLAIN": Product("P-PLAIN", "普通产品", False),
    }
    zones = {
        "Z1": ZoneDef("Z1", "操作区", "operator", (
            CheckItemDef("M1", "Z1", "计量项", "measurement", "operator",
                         unit="g", limit=0.0),
            CheckItemDef("S1", "Z1", "扫码项", "scan", "operator",
                         expected_batch="current"),
        )),
        "Z2": ZoneDef("Z2", "设备区", "technician", (
            CheckItemDef("MOLD", "Z2", "模具回执", "machine", "technician"),
        )),
    }
    if matrix is None:
        matrix = MatrixRegistry([
            MatrixVersion("1.0", datetime(2026, 1, 1), (
                MatrixRule(True, False, "high", ("Z1", "Z2")),
            )),
        ])
    expectations = {
        ("L1", "R1"): EquipmentExpectation("L1", "R1", "P-PLAIN",
                                           {"MOLD": {"mold_id": "MOLD-X"}}),
    }
    return ChangeoverService(
        products=products, zones=zones, matrix=matrix,
        expectations=expectations, token_ttl=token_ttl,
    )


def prepare(service, now=T0):
    return service.prepare_changeover(
        line_id="L1", from_product_id="P-ALG", to_product_id="P-PLAIN",
        previous_batch="B-OLD", new_batch="B-NEW", recipe_version="R1", now=now,
    )


def drive_to_release(service, co, start=T0):
    """沿最小路径把工单推进到放行，返回令牌。"""
    service.begin_checking(co.changeover_id, start)
    service.append_machine_receipt(
        co.changeover_id, "MOLD", device_id="D1", device_seq=1,
        payload={"mold_id": "MOLD-X"}, occurred_at=start,
    )
    service.record_measurement(co.changeover_id, "M1", value=0.0,
                               actor="op", occurred_at=start)
    service.confirm_item(co.changeover_id, "M1", signer="op",
                         role="operator", now=start)
    service.record_scan(co.changeover_id, "S1", code="NEW-1",
                        code_batch="B-NEW", actor="op", occurred_at=start)
    service.confirm_item(co.changeover_id, "S1", signer="op",
                         role="operator", now=start)
    service.confirm_item(co.changeover_id, "MOLD", signer="tech",
                         role="technician", now=start)
    service.proceed_to_first_article(co.changeover_id, start)
    service.submit_first_article(co.changeover_id, measurements={"w": 1.0},
                                 actor="op", now=start)
    service.approve_first_article(co.changeover_id, approver="qa",
                                  role="qa", now=start)
    return service.release(co.changeover_id, now=start)


class ContractEnumTest(unittest.TestCase):
    def test_enums_match_domain_contract(self):
        contract = json.loads(
            Path("domain_contract.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(contract["changeover_states"]),
            {s.value for s in ChangeoverState},
        )
        self.assertEqual(
            set(contract["check_results"]),
            {r.value for r in CheckResult},
        )
        self.assertEqual(
            set(contract["token_states"]),
            {s.value for s in TokenState},
        )


class SigningRuleTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.co = prepare(self.service)
        self.service.begin_checking(self.co.changeover_id, T0)

    def test_cross_post_signing_rejected(self):
        self.service.record_measurement(
            self.co.changeover_id, "M1", value=0.0, actor="op", occurred_at=T0
        )
        with self.assertRaises(errors.CrossPostSigningError):
            self.service.confirm_item(
                self.co.changeover_id, "M1", signer="tech", role="technician", now=T0
            )

    def test_confirm_requires_evidence(self):
        with self.assertRaises(errors.EvidenceMissingError):
            self.service.confirm_item(
                self.co.changeover_id, "M1", signer="op", role="operator", now=T0
            )

    def test_not_applicable_requires_note_and_counts_as_done(self):
        with self.assertRaises(errors.EvidenceMissingError):
            self.service.confirm_item(
                self.co.changeover_id, "S1", signer="op", role="operator",
                now=T0, result=CheckResult.NOT_APPLICABLE,
            )
        self.service.confirm_item(
            self.co.changeover_id, "S1", signer="op", role="operator",
            now=T0, result=CheckResult.NOT_APPLICABLE, note="本批无贴标",
        )
        self.assertIs(self.co.item_result("S1"), CheckResult.NOT_APPLICABLE)
        self.assertNotIn("S1", self.co.pending_items())

    def test_first_article_approval_requires_qa(self):
        drive_to_release_almost = self.co
        # 推到首件阶段
        self.service.append_machine_receipt(
            self.co.changeover_id, "MOLD", device_id="D1", device_seq=1,
            payload={"mold_id": "MOLD-X"}, occurred_at=T0,
        )
        self.service.record_measurement(
            self.co.changeover_id, "M1", value=0.0, actor="op", occurred_at=T0
        )
        self.service.confirm_item(self.co.changeover_id, "M1",
                                  signer="op", role="operator", now=T0)
        self.service.record_scan(self.co.changeover_id, "S1", code="N1",
                                 code_batch="B-NEW", actor="op", occurred_at=T0)
        self.service.confirm_item(self.co.changeover_id, "S1",
                                  signer="op", role="operator", now=T0)
        self.service.confirm_item(self.co.changeover_id, "MOLD",
                                  signer="tech", role="technician", now=T0)
        self.service.proceed_to_first_article(self.co.changeover_id, T0)
        self.service.submit_first_article(
            self.co.changeover_id, measurements={"w": 1.0}, actor="op", now=T0
        )
        with self.assertRaises(errors.CrossPostSigningError):
            self.service.approve_first_article(
                self.co.changeover_id, approver="tech", role="technician", now=T0
            )


class LabelRuleTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.co = prepare(self.service)
        self.service.begin_checking(self.co.changeover_id, T0)

    def test_stale_label_rejected(self):
        with self.assertRaises(errors.StaleLabelError):
            self.service.record_scan(
                self.co.changeover_id, "S1", code="LBL-9",
                code_batch="B-OLD", actor="op", occurred_at=T0,
            )

    def test_duplicate_scan_rejected(self):
        self.service.record_scan(
            self.co.changeover_id, "S1", code="LBL-1",
            code_batch="B-NEW", actor="op", occurred_at=T0,
        )
        with self.assertRaises(errors.DuplicateScanError):
            self.service.record_scan(
                self.co.changeover_id, "S1", code="LBL-1",
                code_batch="B-NEW", actor="op", occurred_at=t(1),
            )


class ResidueAndBackfillTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.co = prepare(self.service)
        self.service.begin_checking(self.co.changeover_id, T0)

    def test_over_limit_measurement_triggers_reclean_and_invalidates_zone(self):
        self.service.record_measurement(
            self.co.changeover_id, "M1", value=0.0, actor="op", occurred_at=T0
        )
        self.service.confirm_item(self.co.changeover_id, "M1",
                                  signer="op", role="operator", now=T0)
        self.service.record_scan(self.co.changeover_id, "S1", code="N1",
                                 code_batch="B-NEW", actor="op", occurred_at=T0)
        self.service.confirm_item(self.co.changeover_id, "S1",
                                  signer="op", role="operator", now=T0)
        self.service.record_measurement(
            self.co.changeover_id, "M1", value=3.0, actor="op", occurred_at=t(5)
        )
        self.assertIs(self.co.state, ChangeoverState.RECLEANING)
        # 同区域两个签字都失效，且只有本区域受影响
        invalidated = {inv.item_id for inv in self.co.invalidations}
        self.assertEqual(invalidated, {"M1", "S1"})
        self.assertIs(self.co.item_result("M1"), CheckResult.RESIDUE_FOUND)

    def test_confirm_blocked_while_residue_effective(self):
        self.service.record_measurement(
            self.co.changeover_id, "M1", value=2.0, actor="op", occurred_at=T0
        )
        self.assertIs(self.co.state, ChangeoverState.RECLEANING)
        with self.assertRaises(errors.ResidueUnresolvedError):
            self.service.complete_recleaning(self.co.changeover_id, t(1))

    def test_offline_backfill_does_not_overwrite_later_check(self):
        self.service.record_measurement(
            self.co.changeover_id, "M1", value=0.0, actor="op", occurred_at=t(10)
        )
        self.service.confirm_item(self.co.changeover_id, "M1",
                                  signer="op", role="operator", now=t(11))
        # 离线终端补传一条更早发生的超标记录：只追加，不覆盖
        self.service.record_measurement(
            self.co.changeover_id, "M1", value=9.9, actor="op",
            occurred_at=t(5), recorded_at=t(20),
            device_id="TERM-1", device_seq=3,
        )
        self.assertIs(self.co.state, ChangeoverState.CHECKING)
        self.assertIs(self.co.item_result("M1"), CheckResult.CLEAR)
        self.assertEqual(len(self.co.events_for("M1")), 2)  # 旧记录仍在日志中

    def test_device_resend_is_idempotent(self):
        first = self.service.append_machine_receipt(
            self.co.changeover_id, "MOLD", device_id="D1", device_seq=1,
            payload={"mold_id": "MOLD-X"}, occurred_at=T0,
        )
        again = self.service.append_machine_receipt(
            self.co.changeover_id, "MOLD", device_id="D1", device_seq=1,
            payload={"mold_id": "MOLD-X"}, occurred_at=T0, recorded_at=t(30),
        )
        self.assertIs(first, again)
        self.assertEqual(len(self.co.events_for("MOLD")), 1)


class MatrixVersionTest(unittest.TestCase):
    def test_clearance_uses_matrix_version_at_prepare_time(self):
        matrix = MatrixRegistry([
            MatrixVersion("1.0", datetime(2026, 1, 1), (
                MatrixRule(True, False, "high", ("Z1",)),
            )),
            MatrixVersion("2.0", datetime(2026, 9, 1), (
                MatrixRule(True, False, "high", ("Z1", "Z2")),
            )),
        ])
        service = make_service(matrix=matrix)
        # 2.0 生效前建单：范围只有 Z1，即使放行时 2.0 已生效也不追加
        old = prepare(service, now=datetime(2026, 5, 1, 8, 0))
        self.assertEqual(old.matrix_version, "1.0")
        self.assertEqual({i.item_id for i in old.scope}, {"M1", "S1"})
        # 2.0 生效后建单：范围包含 Z2
        new = prepare(service, now=T0)
        self.assertEqual(new.matrix_version, "2.0")
        self.assertEqual({i.item_id for i in new.scope}, {"M1", "S1", "MOLD"})


class ReleaseAndTokenTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.co = prepare(self.service)

    def test_release_blocked_on_mold_mismatch(self):
        self.service.begin_checking(self.co.changeover_id, T0)
        self.service.append_machine_receipt(
            self.co.changeover_id, "MOLD", device_id="D1", device_seq=1,
            payload={"mold_id": "MOLD-WRONG"}, occurred_at=T0,
        )
        self.service.record_measurement(self.co.changeover_id, "M1", value=0.0,
                                        actor="op", occurred_at=T0)
        self.service.confirm_item(self.co.changeover_id, "M1",
                                  signer="op", role="operator", now=T0)
        self.service.record_scan(self.co.changeover_id, "S1", code="N1",
                                 code_batch="B-NEW", actor="op", occurred_at=T0)
        self.service.confirm_item(self.co.changeover_id, "S1",
                                  signer="op", role="operator", now=T0)
        self.service.confirm_item(self.co.changeover_id, "MOLD",
                                  signer="tech", role="technician", now=T0)
        self.service.proceed_to_first_article(self.co.changeover_id, T0)
        self.service.submit_first_article(self.co.changeover_id,
                                          measurements={"w": 1.0}, actor="op", now=T0)
        self.service.approve_first_article(self.co.changeover_id,
                                           approver="qa", role="qa", now=T0)
        with self.assertRaises(errors.ReleaseBlockedError):
            self.service.release(self.co.changeover_id, now=T0)

    def test_release_requires_first_article_approval(self):
        self.service.begin_checking(self.co.changeover_id, T0)
        self.service.append_machine_receipt(
            self.co.changeover_id, "MOLD", device_id="D1", device_seq=1,
            payload={"mold_id": "MOLD-X"}, occurred_at=T0,
        )
        self.service.record_measurement(self.co.changeover_id, "M1", value=0.0,
                                        actor="op", occurred_at=T0)
        self.service.confirm_item(self.co.changeover_id, "M1",
                                  signer="op", role="operator", now=T0)
        self.service.record_scan(self.co.changeover_id, "S1", code="N1",
                                 code_batch="B-NEW", actor="op", occurred_at=T0)
        self.service.confirm_item(self.co.changeover_id, "S1",
                                  signer="op", role="operator", now=T0)
        self.service.confirm_item(self.co.changeover_id, "MOLD",
                                  signer="tech", role="technician", now=T0)
        self.service.proceed_to_first_article(self.co.changeover_id, T0)
        with self.assertRaises(errors.ReleaseBlockedError):
            self.service.release(self.co.changeover_id, now=T0)

    def test_token_expired_rejected(self):
        token = drive_to_release(self.service, self.co)
        with self.assertRaises(errors.TokenExpiredError):
            self.service.consume_token(
                token.token_id, line_id="L1", product_id="P-PLAIN",
                recipe_version="R1", now=t(31),
            )
        self.assertIs(token.state, TokenState.EXPIRED)

    def test_token_mismatch_rejected(self):
        token = drive_to_release(self.service, self.co)
        with self.assertRaises(errors.TokenMismatchError):
            self.service.consume_token(
                token.token_id, line_id="L2", product_id="P-PLAIN",
                recipe_version="R1", now=t(1),
            )
        self.assertIs(token.state, TokenState.ISSUED)  # 未核销

    def test_token_double_consume_rejected(self):
        token = drive_to_release(self.service, self.co)
        self.service.consume_token(
            token.token_id, line_id="L1", product_id="P-PLAIN",
            recipe_version="R1", now=t(1),
        )
        with self.assertRaises(errors.TokenStateError):
            self.service.consume_token(
                token.token_id, line_id="L1", product_id="P-PLAIN",
                recipe_version="R1", now=t(2),
            )

    def test_late_residue_after_release_revokes_token(self):
        token = drive_to_release(self.service, self.co)
        # 离线终端补传一条发生在放行前、但晚于合格称重的超标记录
        self.service.record_measurement(
            self.co.changeover_id, "M1", value=7.0, actor="op",
            occurred_at=t(1), recorded_at=t(10),
            device_id="TERM-9", device_seq=1,
        )
        self.assertIs(self.co.state, ChangeoverState.RECLEANING)
        self.assertIs(token.state, TokenState.REVOKED)


if __name__ == "__main__":
    unittest.main()
