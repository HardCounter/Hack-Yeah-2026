"""Governed runtime (Layer 1 of the integrated pipeline): synchronous decisions for one trusted run.

- `gateway.GovernedGateway`  tool calls: contract/scope/identity checks, auditors, dispatch, evidence
- `prompts.PromptGateway`    model requests: model allowlist, token reservation, input/output inspection
- `baseline`                 protected bank baseline captured at session start (verifier input)
- `registry`                 trusted adapter to the synthetic tool registry + in-process feedback channel

Every decision is persisted through `persistence.events.build_action_event`.
"""


class GovernedError(ValueError):
    """Invalid trusted binding or action shape."""
