"""Selected config reaches actual enforcement; live sessions remain pinned."""
import asyncio
import json

from configuration.service import ConfigService
from intercept.service.local import LocalService
from simulation.governed import POLICY_PATH


def test_selected_policy_changes_new_gateway_sessions_only(tmp_path):
    from simulation import agent  # sets up synthetic generator imports
    import generate
    generate.build(tmp_path / "base")
    configs = ConfigService(tmp_path / "configs")
    lenient = configs.get_config("lenient")
    configs.select("lenient", lenient["revision"])

    async def scenario():
        def create():
            return LocalService(bank_path=tmp_path / "base" / "bank.db", runs_dir=tmp_path / "runs",
                                app_id="APP-0001", contract_id="contract_config_test", config_service=configs)
        old, new = create(), create()
        try:
            await old.handle_request("/v1/runs/bind", {"session_id": "ses_config_old", "contract_id": "contract_config_test"})
            initial_revision = old.runtime.contract.policy_version
            read = {"session_id": "ses_config_old", "call_id": "first", "tool": "read_application", "arguments": {"app_id": "APP-0001"}}
            assert (await old.handle_request("/v1/tools/execute", read))["decision"] == "ALLOW"

            edited = configs.get_config("lenient")["config"]
            edited["allowed_tools"].remove("read_application")
            edited["budget"]["tokens"] = 1234
            edited["intercept"]["feedback"]["enabled"] = False  # stored-only; consume plane must not change
            saved = configs.update("lenient", edited)
            assert configs.snapshot_for_intercept()["revision"].removeprefix("sha256:") == initial_revision
            configs.select("lenient", saved["revision"])

            read["call_id"] = "second"
            assert (await old.handle_request("/v1/tools/execute", read))["decision"] == "ALLOW"
            assert old.runtime.contract.policy_version == initial_revision
            assert old.runtime.contract.budget.tokens == 50000

            # This service was constructed BEFORE selection; binding still loads the latest snapshot.
            await new.handle_request("/v1/runs/bind", {"session_id": "ses_config_new", "contract_id": "contract_config_test"})
            assert new.runtime.contract.policy_version == saved["revision"].removeprefix("sha256:")
            assert new.runtime.contract.budget.tokens == 1234
            assert new.runtime.prompt_gateway.max_output_tokens == 2048
            assert new.runtime.prompt_gateway.allowed_models == frozenset(edited["allowed_models"])
            read.update(session_id="ses_config_new", call_id="third")
            assert (await new.handle_request("/v1/tools/execute", read))["decision"] == "BLOCK"
            assert new.runtime.consumer_config == json.loads(POLICY_PATH.read_text())["consumer"]
            assert old.runtime.consumer_config == new.runtime.consumer_config
            assert "velocity-guard" not in [plugin.name for plugin in new.runtime.manager.registry.plugins]
        finally:
            await old.close()
            await new.close()
    asyncio.run(scenario())
