"""开线令牌规则。"""
import unittest

from app.models import TokenState
from app.service import DomainRejection

from helpers import MIN, T0, make_service
from test_rules import _fully_cleared

RECEIPT_OK = {"receipt_id": "RC-9001", "device_id": "ROBOT-01", "device_seq": 41,
              "line": "L1", "kind": "mold_change", "mold_id": "M-02",
              "program_version": "v7", "recipe_version": "R-200"}


def _released(service, ttl=30 * MIN):
    """走完放行全流程, 返回 (批次号, 令牌号)。"""
    co = _fully_cleared(service)
    service.record_receipt(dict(RECEIPT_OK), now=T0 + 40 * MIN)
    service.verify_mold_program(co, "RC-9001", "U-MECH", now=T0 + 41 * MIN)
    service.submit_first_article(co, {"weight_g": 100, "seal_temp_c": 160,
                                      "code_grade": "A"}, "U-OP1", now=T0 + 42 * MIN)
    service.approve_first_article(co, "U-QA", now=T0 + 43 * MIN)
    token_id = service.issue_token(co, now=T0 + 44 * MIN, ttl_seconds=ttl)
    return co, token_id


class TokenIssueTest(unittest.TestCase):
    def test_token_binds_line_product_recipe_and_expiry(self):
        _, _, service = make_service("v2")
        co, token_id = _released(service)
        token = service.tokens[token_id]
        self.assertEqual(token.line, "L1")
        self.assertEqual(token.product_id, "P-PLAIN")
        self.assertEqual(token.recipe_version, "R-200")
        self.assertEqual(token.expires_at - token.issued_at, 30 * MIN)
        self.assertEqual(service.changeovers[co].state.value, "released")

    def test_gates_block_issuance(self):
        """缺任何一道放行条件都不得签发令牌。"""
        _, _, service = make_service("v2")
        co = _fully_cleared(service)  # 未做模具校验与首件
        with self.assertRaises(DomainRejection) as ctx:
            service.issue_token(co, now=T0 + 40 * MIN)
        self.assertEqual(ctx.exception.reason, "release_gates_incomplete")
        gates = ctx.exception.details["gates"]
        self.assertTrue(gates["clearance_passed"])
        self.assertFalse(gates["mold_program_consistent"])
        self.assertFalse(gates["first_article_approved"])


class TokenUseTest(unittest.TestCase):
    def test_expired_token_rejected(self):
        """令牌过期: 明确拒绝并标记 expired。"""
        _, _, service = make_service("v2")
        _, token_id = _released(service, ttl=30 * MIN)
        with self.assertRaises(DomainRejection) as ctx:
            service.consume_token(token_id, "L1", "P-PLAIN", "R-200",
                                  now=T0 + 44 * MIN + 31 * MIN)
        self.assertEqual(ctx.exception.reason, "token_expired")
        self.assertEqual(service.tokens[token_id].state, TokenState.EXPIRED)

    def test_binding_mismatch_rejected(self):
        """令牌绑定产线/产品/配方版本, 不符即拒。"""
        _, _, service = make_service("v2")
        _, token_id = _released(service)
        with self.assertRaises(DomainRejection) as ctx:
            service.consume_token(token_id, "L1", "P-ALLERGEN", "R-100",
                                  now=T0 + 45 * MIN)
        self.assertEqual(ctx.exception.reason, "token_binding_mismatch")

    def test_double_consume_rejected(self):
        _, _, service = make_service("v2")
        _, token_id = _released(service)
        service.consume_token(token_id, "L1", "P-PLAIN", "R-200", now=T0 + 45 * MIN)
        self.assertEqual(service.tokens[token_id].state, TokenState.CONSUMED)
        with self.assertRaises(DomainRejection) as ctx:
            service.consume_token(token_id, "L1", "P-PLAIN", "R-200",
                                  now=T0 + 46 * MIN)
        self.assertEqual(ctx.exception.reason, "token_not_usable")

    def test_sweep_marks_expired(self):
        _, _, service = make_service("v2")
        _, token_id = _released(service, ttl=30 * MIN)
        expired = service.sweep_tokens(now=T0 + 44 * MIN + 31 * MIN)
        self.assertEqual(expired, [token_id])
        self.assertEqual(service.tokens[token_id].state, TokenState.EXPIRED)

    def test_cancel_revokes_active_token(self):
        _, _, service = make_service("v2")
        co, token_id = _released(service)
        service.cancel_changeover(co, "计划变更", now=T0 + 50 * MIN)
        self.assertEqual(service.tokens[token_id].state, TokenState.REVOKED)
        self.assertEqual(service.tokens[token_id].revoke_reason, "计划变更")


if __name__ == "__main__":
    unittest.main()
