import pytest

from conftest import wire_event
from contracts.action import PromptPayload, SessionPayload, ToolUsePayload, UnknownPayload
from contracts.wire import DecodeError, decode_event


def test_tool_call_decodes_to_tool_use():
    a = decode_event(wire_event(3, tool="create_client", args={"app_id": "APP-0001"}))
    assert a.kind == "tool_use" and a.seq == 3 and a.executed
    assert isinstance(a.payload, ToolUsePayload)
    assert a.payload.tool == "create_client" and a.payload.args == {"app_id": "APP-0001"}
    assert a.payload.result.trust == "untrusted"
    assert a.gateway.final == "ALLOW" and a.policy_version == "v1"
    assert a.ts.tzinfo is not None


def test_mcp_tool_maps_to_tool_use_and_llm_call_to_prompt():
    assert decode_event(wire_event(1, action_type="mcp_tool") | {"action_details": {"name": "x"}}).kind == "tool_use"
    prompt = decode_event(wire_event(2, action_type="llm_call"))
    assert prompt.kind == "prompt" and isinstance(prompt.payload, PromptPayload)


def test_session_event_without_gateway_takes_policy_version_from_payload():
    a = decode_event(wire_event(0, action_type="session", interception_metadata=None))
    assert isinstance(a.payload, SessionPayload) and a.gateway is None and a.policy_version == "v1"


def test_unknown_action_type_is_kept_not_dropped():
    a = decode_event(wire_event(1, action_type="a2a_message"))
    assert a.kind == "a2a_message" and not a.known_kind and isinstance(a.payload, UnknownPayload)


@pytest.mark.parametrize("mutate, message", [
    (lambda e: e.pop("seq"), "seq"),
    (lambda e: e.update(seq=-1), "seq"),
    (lambda e: e.update(schema_version="1.0"), "schema_version"),
    (lambda e: e.update(status="ok"), "status"),
    (lambda e: e.update(ts="yesterday"), "timestamp"),
    (lambda e: e["action_details"].update(result={"success": True}), "content references"),
    (lambda e: e.pop("session_id"), "session_id"),
])
def test_invalid_events_are_rejected(mutate, message):
    e = wire_event(1)
    mutate(e)
    with pytest.raises(DecodeError, match=message):
        decode_event(e)
