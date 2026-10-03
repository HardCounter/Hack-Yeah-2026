import asyncio

from conftest import Harness, action
from consume_plane.plugins.gateway_violations import GatewayViolations


def test_gateway_scope_deny_emits_fixed_code_without_arguments():
    denied = action(
        1, status="blocked", tool="create_client", side_effect="write",
        args={"app_id": "APP-9999", "name": "Sensitive Person"},
        interception_metadata={
            "final_decision": "BLOCK", "policy_version": "v1",
            "auditor_decisions": [{"auditor": "task-contract", "decision": "BLOCK",
                                   "rule_id": "TASK_SCOPE"}],
        },
    )
    harness = Harness([denied], [GatewayViolations], poll_timeout_s=0.01, retry_backoff_s=0)
    asyncio.run(harness.run())
    finding = harness.findings()[0]

    assert finding.rule_id == "gateway.task_scope_deny"
    assert finding.evidence_event_ids == (denied.event_id,)
    assert finding.details["reason_code"] == "TASK_SCOPE"
    assert "APP-9999" not in str(finding.to_dict())
    assert "Sensitive Person" not in str(finding.to_dict())
