"""Plugin discovery and protocol validation (docs/consumer-plane.md section 8.1)."""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import re
import sys
from pathlib import Path

from ..ports.plugin import Subscription

NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
EXT_PACKAGE = "consume_plane_ext"


class PluginLoadError(Exception):
    pass


def validate_plugin_class(cls: object, origin: str) -> type:
    """Check structural conformance with ConsumerPlugin; raise PluginLoadError naming the problem."""
    where = f"{origin}: {getattr(cls, '__name__', cls)!s}"
    if not inspect.isclass(cls):
        raise PluginLoadError(f"{where}: PLUGINS entries must be classes")
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise PluginLoadError(f"{where}: 'name' must be a kebab-case string, got {name!r}")
    if not isinstance(getattr(cls, "version", None), str):
        raise PluginLoadError(f"{where}: 'version' must be a string")
    if getattr(cls, "method", None) not in ("deterministic", "semantic"):
        raise PluginLoadError(f"{where}: 'method' must be 'deterministic' or 'semantic'")
    if not isinstance(getattr(cls, "subscription", None), Subscription):
        raise PluginLoadError(f"{where}: 'subscription' must be a consume_plane.sdk.Subscription")
    handle = getattr(cls, "handle", None)
    if not inspect.iscoroutinefunction(handle):
        raise PluginLoadError(f"{where}: 'handle' must be 'async def handle(self, action, ctx)'")
    if len(inspect.signature(handle).parameters) != 3:
        raise PluginLoadError(f"{where}: 'handle' must take exactly (self, action, ctx)")
    for hook in ("setup", "teardown"):
        fn = getattr(cls, hook, None)
        if fn is not None and not inspect.iscoroutinefunction(fn):
            raise PluginLoadError(f"{where}: '{hook}' must be 'async def' when defined")
    return cls


def load_file(path: Path) -> list[type]:
    module_name = f"{EXT_PACKAGE}.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"{path}: cannot import")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(module_name, None)
        raise PluginLoadError(f"{path}: import failed: {type(e).__name__}") from None
    plugins = getattr(module, "PLUGINS", None)
    if not isinstance(plugins, (list, tuple)):
        raise PluginLoadError(f"{path}: module must define a PLUGINS list")
    return [validate_plugin_class(cls, str(path)) for cls in plugins]


def discover(plugin_dirs: list[str]) -> tuple[list[tuple[type, str]], list[PluginLoadError]]:
    """Return (class, origin) for each plugin in the drop-in directories, plus per-file errors."""
    found, errors = [], []
    for d in plugin_dirs:
        directory = Path(d)
        if not directory.is_dir():
            errors.append(PluginLoadError(f"{directory}: plugin directory does not exist"))
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                found.extend((cls, str(path)) for cls in load_file(path))
            except PluginLoadError as e:
                errors.append(e)
    return found, errors


def load_handler(handler: str) -> type:
    module_name, sep, attr = handler.partition(":")
    if not sep or not attr:
        raise PluginLoadError(f"{handler}: handler must look like 'package.module:Class'")
    try:
        module = importlib.import_module(module_name)
    except Exception as e:
        raise PluginLoadError(f"{handler}: import failed: {type(e).__name__}") from None
    cls = getattr(module, attr, None)
    if cls is None:
        raise PluginLoadError(f"{handler}: '{attr}' not found in {module_name}")
    return validate_plugin_class(cls, handler)
