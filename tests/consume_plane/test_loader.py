import asyncio
import textwrap
from pathlib import Path

import pytest

from consume_plane.runtime.config import ConfigError, ConsumePlaneConfig, PluginEntry, parse_config
from consume_plane.runtime.loader import PluginLoadError
from consume_plane.runtime.registry import load_registry

REPO = Path(__file__).resolve().parents[2]

GOOD = """
from consume_plane.sdk import Subscription

class {cls}:
    name = "{name}"
    version = "1.0.0"
    method = "deterministic"
    subscription = Subscription(kinds=frozenset({{"tool_use"}}))

    async def setup(self, ctx):
        self.limit = ctx.config.get("limit", 1)

    async def handle(self, action, ctx):
        pass

PLUGINS = [{cls}]
"""


def write_plugin(directory: Path, filename: str, body: str) -> None:
    (directory / filename).write_text(textwrap.dedent(body))


def load(cfg):
    return asyncio.run(load_registry(cfg))


def test_drop_in_file_is_discovered_and_configured(tmp_path):
    write_plugin(tmp_path, "my_plugin.py", GOOD.format(cls="Mine", name="my-plugin"))
    write_plugin(tmp_path, "_helper.py", "raise RuntimeError('must not be imported')")
    cfg = ConsumePlaneConfig(plugin_dirs=[str(tmp_path)],
                             plugins={"my-plugin": PluginEntry("my-plugin", config={"limit": 7})})
    registry = load(cfg)
    assert [p.name for p in registry.plugins] == ["my-plugin"]
    assert registry.plugins[0].instance.limit == 7


def test_interception_plugins_cannot_be_loaded_as_consumers():
    with pytest.raises(PluginLoadError, match="subscription"):
        load(ConsumePlaneConfig(plugin_dirs=[str(REPO / "plugins")]))


def test_module_class_handler_loads(tmp_path, monkeypatch):
    write_plugin(tmp_path, "pkg_plugin.py", GOOD.format(cls="Pkg", name="pkg-plugin"))
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = load(ConsumePlaneConfig(plugins={"pkg-plugin": PluginEntry("pkg-plugin", handler="pkg_plugin:Pkg")}))
    assert registry.get("pkg-plugin") is not None


def test_disabled_plugin_is_not_loaded(tmp_path):
    write_plugin(tmp_path, "my_plugin.py", GOOD.format(cls="Mine", name="my-plugin"))
    cfg = ConsumePlaneConfig(plugin_dirs=[str(tmp_path)],
                             plugins={"my-plugin": PluginEntry("my-plugin", enabled=False)})
    assert load(cfg).plugins == []


@pytest.mark.parametrize("body, message", [
    ("x = 1\n", "PLUGINS list"),
    (GOOD.format(cls="Mine", name="Bad_Name"), "kebab-case"),
    (GOOD.format(cls="Mine", name="ok").replace("async def handle", "def handle"), "async def handle"),
    (GOOD.format(cls="Mine", name="ok").replace("(self, action, ctx)", "(self, action)"), "exactly"),
    (GOOD.format(cls="Mine", name="ok").replace('method = "deterministic"', 'method = "magic"'), "method"),
    ("import does_not_exist\n", "import failed"),
])
def test_invalid_plugin_files_fail_with_clear_error(tmp_path, body, message):
    write_plugin(tmp_path, "broken.py", body)
    with pytest.raises(PluginLoadError, match=message):
        load(ConsumePlaneConfig(plugin_dirs=[str(tmp_path)]))


def test_duplicate_names_fail(tmp_path):
    write_plugin(tmp_path, "a.py", GOOD.format(cls="A", name="same"))
    write_plugin(tmp_path, "b.py", GOOD.format(cls="B", name="same"))
    with pytest.raises(PluginLoadError, match="duplicate plugin name 'same'"):
        load(ConsumePlaneConfig(plugin_dirs=[str(tmp_path)]))


def test_configured_plugin_that_does_not_exist_fails():
    with pytest.raises(PluginLoadError, match="not found in plugin_dirs"):
        load(ConsumePlaneConfig(plugins={"ghost": PluginEntry("ghost")}))


def test_skip_mode_keeps_good_plugins(tmp_path):
    write_plugin(tmp_path, "good.py", GOOD.format(cls="Good", name="good"))
    write_plugin(tmp_path, "broken.py", "x = 1\n")
    registry = load(ConsumePlaneConfig(plugin_dirs=[str(tmp_path)], on_plugin_load_error="skip"))
    assert [p.name for p in registry.plugins] == ["good"]


def test_failing_setup_is_a_load_error(tmp_path):
    write_plugin(tmp_path, "boom.py", GOOD.format(cls="Boom", name="boom").replace(
        'self.limit = ctx.config.get("limit", 1)', 'raise ValueError("bad config")'))
    with pytest.raises(PluginLoadError, match="setup of 'boom' failed"):
        load(ConsumePlaneConfig(plugin_dirs=[str(tmp_path)]))


def test_config_rejects_unknown_keys_and_resolves_paths(tmp_path):
    with pytest.raises(ConfigError, match="unknown keys"):
        parse_config({"consume_plane": {"partitionz": 2}})
    with pytest.raises(ConfigError, match="unknown actions"):
        parse_config({"consume_plane": {"feedback": {"allowed_actions": {"x": ["NUKE"]}}}})
    cfg = parse_config({"consume_plane": {"plugin_dirs": ["plugins"]}}, tmp_path)
    assert cfg.plugin_dirs == [str(tmp_path / "plugins")]


def test_repository_config_file_parses():
    from consume_plane.runtime.config import load_config
    cfg = load_config(REPO / "consume_plane.yaml")
    assert cfg.plugin_dirs == []
    assert "velocity-guard" not in cfg.plugins
    assert "velocity-guard" not in cfg.feedback.allowed_actions
