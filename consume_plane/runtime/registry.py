"""Loaded plugins and subscription matching."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..model.actions import AgentAction
from ..ports.plugin import SetupContext, Subscription
from .config import ConsumePlaneConfig
from .loader import PluginLoadError, discover, load_handler, validate_plugin_class

log = logging.getLogger("consume_plane.registry")


@dataclass
class LoadedPlugin:
    instance: Any
    name: str
    version: str
    method: str
    subscription: Subscription
    origin: str
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def log(self) -> logging.Logger:
        return logging.getLogger(f"consume_plane.plugin.{self.name}")


class Registry:
    def __init__(self, plugins: Iterable[LoadedPlugin]):
        self.plugins = list(plugins)

    def matching(self, action: AgentAction) -> list[LoadedPlugin]:
        return [p for p in self.plugins if p.subscription.matches(action)]

    def get(self, name: str) -> LoadedPlugin | None:
        return next((p for p in self.plugins if p.name == name), None)

    async def teardown_all(self) -> None:
        for p in self.plugins:
            teardown = getattr(p.instance, "teardown", None)
            if teardown is None:
                continue
            try:
                await teardown()
            except Exception:
                p.log.exception("teardown failed")


async def load_registry(cfg: ConsumePlaneConfig, extra: Iterable[type] = ()) -> Registry:
    """Discover drop-in files, import configured handlers, validate, instantiate and set up.

    `extra` lets an embedding process (or a test) register plugin classes directly.
    With on_plugin_load_error=fail every problem is collected and raised together.
    """
    errors: list[PluginLoadError] = []
    candidates: dict[str, tuple[type, str]] = {}

    def add(cls: type, origin: str) -> None:
        if cls.name in candidates:
            errors.append(PluginLoadError(
                f"{origin}: duplicate plugin name '{cls.name}' (already loaded from {candidates[cls.name][1]})"))
        else:
            candidates[cls.name] = (cls, origin)

    found, discover_errors = discover(cfg.plugin_dirs)
    errors.extend(discover_errors)
    for cls, origin in found:
        add(cls, origin)
    for cls in extra:
        try:
            add(validate_plugin_class(cls, "embedded"), "embedded")
        except PluginLoadError as e:
            errors.append(e)
    for name, entry in cfg.plugins.items():
        if entry.handler:
            try:
                cls = load_handler(entry.handler)
            except PluginLoadError as e:
                errors.append(e)
                continue
            if cls.name != name:
                errors.append(PluginLoadError(f"plugins.{name}: handler class is named '{cls.name}'"))
                continue
            add(cls, entry.handler)
        elif name not in candidates and entry.enabled:
            errors.append(PluginLoadError(f"plugins.{name}: no handler and not found in plugin_dirs"))

    loaded = []
    for name, (cls, origin) in candidates.items():
        entry = cfg.plugins.get(name)
        if entry is not None and not entry.enabled:
            log.info("plugin %s disabled by config", name)
            continue
        config = dict(entry.config) if entry is not None else {}
        plugin = LoadedPlugin(None, name, cls.version, cls.method, cls.subscription, origin, config)
        try:
            plugin.instance = cls()
            setup = getattr(plugin.instance, "setup", None)
            if setup is not None:
                await setup(SetupContext(name, config, plugin.log))
        except Exception as e:
            errors.append(PluginLoadError(f"{origin}: setup of '{name}' failed: {type(e).__name__}: {e}"))
            continue
        loaded.append(plugin)

    if errors:
        if cfg.on_plugin_load_error == "fail":
            raise PluginLoadError("\n".join(str(e) for e in errors))
        for e in errors:
            log.warning("skipping plugin: %s", e)
    log.info("loaded plugins: %s", ", ".join(f"{p.name}@{p.version}" for p in loaded) or "none")
    return Registry(loaded)
