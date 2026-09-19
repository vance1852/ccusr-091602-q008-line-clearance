"""领域枚举与 domain_contract.json 的一致性。"""
import json
import unittest
from pathlib import Path

from app.models import (
    ChangeoverState,
    CheckKind,
    CheckResult,
    EvidenceKind,
    RiskLevel,
    TokenState,
)

CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "domain_contract.json").read_text(encoding="utf-8"))


class ContractConsistencyTest(unittest.TestCase):
    def test_states_match_contract(self):
        self.assertEqual({s.value for s in ChangeoverState},
                         set(CONTRACT["changeover_states"]))
        self.assertEqual({s.value for s in CheckResult}, set(CONTRACT["check_results"]))
        self.assertEqual({s.value for s in TokenState}, set(CONTRACT["token_states"]))

    def test_vocab_matches_contract(self):
        self.assertEqual({s.value for s in RiskLevel}, set(CONTRACT["risk_levels"]))
        self.assertEqual({s.value for s in CheckKind}, set(CONTRACT["check_kinds"]))
        self.assertEqual({s.value for s in EvidenceKind},
                         set(CONTRACT["evidence_kinds"]))


if __name__ == "__main__":
    unittest.main()
