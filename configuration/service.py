"""Cross-process JSON config transactions, separate from evidence databases.

One atomic state file contains editable configs and the selected immutable snapshot.
flock serializes writers/readers across API workers and gateway processes on Linux.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from pydantic import ValidationError

from configuration.models import ConfigState, PolicyConfig

ROOT = Path(__file__).resolve().parents[1]
PRESET_NAMES = ("lenient", "standard", "strict")
MAX_CONFIG_BYTES = 64_000
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
MESSAGES = {
    "bad_request": "malformed configuration request",
    "unauthorized": "management authentication required",
    "config_not_found": "configuration not found",
    "method_not_allowed": "method not allowed",
    "config_revision_conflict": "configuration revision changed",
    "config_too_large": "configuration request exceeds size limit",
    "unsupported_media_type": "application/json required",
    "invalid_config": "invalid configuration",
    "config_unavailable": "configuration storage unavailable",
}


class ConfigError(Exception):
    def __init__(self, status, code, fields=()):
        self.status, self.code = status, code
        self.fields = list(fields)
        super().__init__(code)

    def body(self):
        return {"error": {"code": self.code, "message": MESSAGES[self.code],
                          "details": {"fields": self.fields} if self.fields else {}}}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def revision(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def parse_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite number")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ConfigError(400, "bad_request") from None


def validate_policy(value):
    try:
        # New optional plugin fields must not alter hashes of already-pinned snapshots.
        return PolicyConfig.model_validate(value).model_dump(mode="json", exclude_unset=True)
    except ValidationError as exc:
        # Only model field paths, never submitted values or validator exception prose.
        known = {"name", "description", "allowed_tools", "admin_tools", "budget", "tokens", "tool_calls",
                 "cost_usd", "allowed_models", "max_output_tokens", "require_approval", "feed_version",
                 "controls", "email_recipients", "egress_hosts", "model_source_hosts", "allowed_model_suffixes",
                 "auditors", "id", "type", "config", "patterns", "action", "classes", "intercept",
                 "trajectory_risk", "velocity_guard", "window_s", "max_calls", "feedback", "enabled",
                 "pattern_match", "fields",
                 "allow_agent_scope", "max_ttl_s", "max_signals_per_session_per_minute", "allowed_actions",
                 "semantic_guard", "block_threshold", "approve_threshold", "alert_threshold", "on_error"}
        fields = [".".join(str(part) if isinstance(part, int) or part in known else "field"
                           for part in error["loc"]) or "config" for error in exc.errors()][:20]
        raise ConfigError(422, "invalid_config", fields) from None


class ConfigService:
    def __init__(self, directory=None, *, presets_dir=None):
        self.directory = Path(directory or os.environ.get("CONFIG_DIR", ROOT / "var" / "config")).resolve()
        self.presets_dir = Path(presets_dir or ROOT / "config" / "presets")
        self.path = self.directory / "state.json"
        self.initialized_path = self.directory / ".initialized"

    def _mark_initialized(self):
        if self.initialized_path.exists():
            return
        fd = os.open(self.initialized_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(b"1\n")
            out.flush()
            os.fsync(out.fileno())
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextmanager
    def _transaction(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.directory / ".config.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as lock:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise ConfigError(503, "config_unavailable")
                        time.sleep(0.01)
                yield self._load()
        except (OSError, ValueError, KeyError, TypeError, ValidationError):
            raise ConfigError(503, "config_unavailable") from None

    def _load(self):
        if self.path.is_symlink():
            raise ConfigError(503, "config_unavailable")
        if not self.path.exists():
            if self.initialized_path.exists():
                raise ConfigError(503, "config_unavailable")  # deleted state must not reset policy
            configs = {}
            for name in PRESET_NAMES:
                try:
                    config = validate_policy(parse_json((self.presets_dir / f"{name}.json").read_bytes()))
                except ConfigError:
                    raise ConfigError(503, "config_unavailable") from None
                if config["name"] != name:
                    raise ConfigError(503, "config_unavailable")
                configs[name] = {"config": config, "revision": revision(config), "updated_at": now()}
            entry = configs["standard"]
            state = {"schema_version": 1, "configs": configs,
                     "selection": {"name": "standard", "revision": entry["revision"],
                                   "selected_at": now(), "config": entry["config"]}, "changes": []}
            self._write(state)
            self._mark_initialized()
            return state
        try:
            with self.path.open("rb") as source:
                data = source.read(1_000_001)
            if len(data) > 1_000_000:
                raise ValueError("state too large")
            state = ConfigState.model_validate(parse_json(data)).model_dump(mode="json", exclude_unset=True)
            if state["schema_version"] != 1 or set(state["configs"]) != set(PRESET_NAMES):
                raise ValueError("invalid state")
            for name, entry in state["configs"].items():
                config = validate_policy(entry["config"])
                if config["name"] != name or revision(config) != entry["revision"]:
                    raise ValueError("invalid stored config")
            selected = state["selection"]
            if (selected["name"] not in PRESET_NAMES or
                    selected["config"]["name"] != selected["name"] or
                    revision(validate_policy(selected["config"])) != selected["revision"]):
                raise ValueError("invalid selected snapshot")
            self._mark_initialized()
            return state
        except ConfigError:
            raise ConfigError(503, "config_unavailable") from None

    def _write(self, state):
        data = canonical(state)
        if len(data) > 1_000_000:
            raise ConfigError(503, "config_unavailable")
        fd, temporary = tempfile.mkstemp(prefix=".config-", dir=self.directory)
        backup = None
        replaced = False
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            if self.path.exists():
                backup_fd, backup = tempfile.mkstemp(prefix=".config-backup-", dir=self.directory)
                os.close(backup_fd)
                Path(backup).unlink()
                os.link(self.path, backup, follow_symlinks=False)
            os.replace(temporary, self.path)
            replaced = True
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # A directory-fsync failure after rename must not activate unacknowledged edits.
            if replaced:
                if backup is not None:
                    os.replace(backup, self.path)
                else:
                    self.path.unlink(missing_ok=True)
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass  # report unavailable even when the filesystem cannot sync the rollback
                finally:
                    os.close(directory_fd)
            raise
        finally:
            Path(temporary).unlink(missing_ok=True)
            if backup is not None:
                Path(backup).unlink(missing_ok=True)

    def _name(self, name):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ConfigError(400, "bad_request")
        if name not in PRESET_NAMES:
            raise ConfigError(404, "config_not_found")

    def list_configs(self):
        with self._transaction() as state:
            return [{"name": name, "preset": True, "description": entry["config"]["description"],
                     "revision": entry["revision"]} for name, entry in sorted(state["configs"].items())]

    def get_config(self, name):
        self._name(name)
        with self._transaction() as state:
            return state["configs"][name]

    def update(self, name, value):
        self._name(name)
        config = validate_policy(value)
        if config["name"] != name:
            raise ConfigError(422, "invalid_config", ["name"])
        rev = revision(config)
        with self._transaction() as state:
            entry = state["configs"][name]
            if rev != entry["revision"]:
                entry = {"config": config, "revision": rev, "updated_at": now()}
                state["configs"][name] = entry
                state["changes"] = (state["changes"] + [{"operation": "update", "name": name,
                                                        "revision": rev, "ts": entry["updated_at"]}])[-1000:]
                self._write(state)
            active = state["selection"]
            return {"name": name, "revision": rev, "updated_at": entry["updated_at"],
                    "selected": name == active["name"], "active_revision": active["revision"],
                    "requires_selection": name != active["name"] or rev != active["revision"]}

    def select(self, name, expected_revision):
        self._name(name)
        with self._transaction() as state:
            entry = state["configs"][name]
            if entry["revision"] != expected_revision:
                raise ConfigError(409, "config_revision_conflict")
            active = state["selection"]
            if active["name"] != name or active["revision"] != expected_revision:
                active = {"name": name, "revision": expected_revision, "selected_at": now(),
                          "config": entry["config"]}
                state["selection"] = active
                state["changes"] = (state["changes"] + [{"operation": "select", "name": name,
                                                        "revision": expected_revision, "ts": active["selected_at"]}])[-1000:]
                self._write(state)
            return {key: active[key] for key in ("name", "revision", "selected_at")} | {"effective_for": "new_sessions"}

    def snapshot_for_intercept(self):
        with self._transaction() as state:
            return state["selection"]
