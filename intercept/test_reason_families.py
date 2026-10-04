import unittest

from intercept.governed.gateway import LIVE_DENIALS
from intercept.governed.reason_families import _FAMILIES, REASON_FAMILIES, classify

# Codes produced by intercept/policy/runs.py Policy.evaluate besides ALLOW.
POLICY_CODES = {"UNKNOWN_RUN", "REPLAY", "ADMISSION_CAPACITY", "TOOL_DENIED", "TASK_SCOPE",
                "APPROVAL_NOT_IMPLEMENTED", "AUDITOR_BLOCK", "BUDGET_EXHAUSTED"}


class ReasonFamilyTests(unittest.TestCase):
    def test_every_emitted_deny_code_has_a_family(self):
        for code in LIVE_DENIALS | POLICY_CODES | {"DOMAIN_BLOCKLISTED", "SIGNATURE_MATCH", "CASE_OUT_OF_SCOPE"}:
            with self.subTest(code=code):
                self.assertNotEqual(classify(code), "UNCLASSIFIED")

    def test_known_examples_and_unknown_codes(self):
        self.assertEqual(classify("DOMAIN_BLOCKLISTED"), "EGRESS")
        self.assertEqual(classify("SCREENING_NOT_COMPLETE"), "PREREQUISITE")
        self.assertEqual(classify("MODEL_SIGNATURE_DENIED"), "SUPPLY_CHAIN")
        self.assertEqual(classify("SOMETHING_NEW"), "UNCLASSIFIED")
        self.assertEqual(classify(None), "UNCLASSIFIED")

    def test_each_code_belongs_to_one_family(self):
        self.assertEqual(len(REASON_FAMILIES), sum(len(codes) for codes in _FAMILIES.values()))


if __name__ == "__main__":
    unittest.main()
