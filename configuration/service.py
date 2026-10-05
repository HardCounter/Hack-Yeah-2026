"""Cross-process JSON config transactions, separate from evidence databases.

One atomic state file contains editable configs and the selected immutable snapshot.
OS file locks serialize writers across API workers and gateway processes.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl

from pydantic import ValidationError

from configuration.models import ConfigState, PolicyConfig

ROOT = Path(__file__).resolve().parents[1]
PRESET_NAMES = ("lenient", "standard", "strict")
MAX_CONFIG_BYTES = 64_000
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
MESSAGES = {
    "bad_request": "malformed configuration request",
    "config_not_found": "configuration not found",
    "method_not_allowed": "method not allowed",
    "config_revision_conflict": "configuration revision changed",
    "config_too_large": "configuration request exceeds size limit",
    "unsupported_media_type": "application/json required",
    "invalid_config": "invalid configuration",
    "config_unavailable": "configuration storage unavailable",
    "config_writes_disabled": "configuration writes are disabled until an admin token is configured",
    "admin_token_required": "a valid admin token is required to change configuration",
    "origin_not_allowed": "browser origin is not allowed to change configuration",
    "rate_limit_exceeded": "configuration write rate limit exceeded; retry in a minute",
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
                 "auditors", "id", "type", "config", "patterns", "action", "classes", "domains", "intercept",
                 "trajectory_risk", "velocity_guard", "window_s", "max_calls", "feedback", "enabled",
                 "pattern_match", "fields",
                 "allow_agent_scope", "max_ttl_s", "max_signals_per_session_per_minute", "allowed_actions",
                 "semantic_guard", "block_threshold", "approve_threshold", "alert_threshold", "on_error"}
        fields = [".".join(str(part) if isinstance(part, int) or part in known else "field"
                           for part in error["loc"]) or "config" for error in exc.errors()][:20]
        raise ConfigError(422, "invalid_config", fields) from None


def _private_open(path, flags):
    """Open private metadata without following its final symlink/reparse point."""
    if not WINDOWS:
        return os.open(path, flags | os.O_NOFOLLOW, 0o600)
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    access = 0x40000000 | (0x80000000 if flags & os.O_RDWR else 0)  # GENERIC_WRITE/READ
    handle = create(str(path), access, 3, None, 1 if flags & os.O_EXCL else 4,
                    0x00200000, None)  # shared read/write; CREATE_NEW/OPEN_ALWAYS; OPEN_REPARSE_POINT
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # The CRT handle adapter defaults to read/write; its supported flags do
        # not include O_RDWR/O_WRONLY. Never inherit the config lock in children.
        fd = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise
    try:
        if os.fstat(fd).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(errno.ELOOP, "configuration metadata is a reparse point")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _try_lock(fd):
    if WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd):
    if WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _replace_file(source, destination):
    if not WINDOWS:
        os.replace(source, destination)
        return
    # Windows cannot fsync a directory through os.open. A same-volume native
    # write-through rename follows the temporary file's successful os.fsync.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel.MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    if not move(str(source), str(destination), 0x1 | 0x8):  # REPLACE_EXISTING | WRITE_THROUGH
        raise ctypes.WinError(ctypes.get_last_error())


def _sync_directory(directory):
    if WINDOWS:
        return  # _replace_file performs the Windows durability operation
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ConfigService:
    def __init__(self, directory=None, *, presets_dir=None, read_only=False):
        self.directory = Path(directory or os.environ.get("CONFIG_DIR", ROOT / "var" / "config")).resolve()
        self.presets_dir = Path(presets_dir or ROOT / "config" / "presets")
        self.path = self.directory / "state.json"
        self.initialized_path = self.directory / ".initialized"
        self.read_only = read_only

    def _mark_initialized(self):
        if self.initialized_path.exists():
            return
        if WINDOWS:
            fd, temporary = tempfile.mkstemp(prefix=".config-initialized-", dir=self.directory)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(b"1\n")
                    out.flush()
                    os.fsync(out.fileno())
                _replace_file(temporary, self.initialized_path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return
        fd = _private_open(self.initialized_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL)
        with os.fdopen(fd, "wb") as out:
            out.write(b"1\n")
            out.flush()
            os.fsync(out.fileno())
        _sync_directory(self.directory)

    @contextmanager
    def _transaction(self):
        try:
            if self.read_only:
                # Writers replace state.json atomically, so reads need no writable lock file.
                yield self._load()
                return
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = _private_open(self.directory / ".config.lock", os.O_CREAT | os.O_RDWR)
            with os.fdopen(fd, "r+b") as lock:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        _try_lock(lock.fileno())
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                        if time.monotonic() >= deadline:
                            raise ConfigError(503, "config_unavailable")
                        time.sleep(0.01)
                try:
                    yield self._load()
                finally:
                    _unlock(lock.fileno())
        except (OSError, ValueError, KeyError, TypeError, ValidationError):
            raise ConfigError(503, "config_unavailable") from None

    def _load(self):
        if self.path.is_symlink():
            raise ConfigError(503, "config_unavailable")
        if not self.path.exists():
            if self.read_only:
                raise ConfigError(503, "config_unavailable")
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
            raw = parse_json(data)
            upgraded = self._upgrade(raw)
            state = ConfigState.model_validate(raw).model_dump(mode="json", exclude_unset=True)
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
            if upgraded:
                if self.read_only:
                    raise ConfigError(503, "config_unavailable")  # the management service performs migration
                self._write(state)
            if not self.read_only:
                self._mark_initialized()
            return state
        except ConfigError:
            raise ConfigError(503, "config_unavailable") from None

    def _upgrade(self, raw):
        """A config stored under an older schema is replaced by the shipped preset, so a schema change
        cannot leave the service unavailable. Configs that still validate are kept as saved."""
        def outdated(config):
            try:
                validate_policy(config)
                return False
            except ConfigError:
                return True

        changed = False
        for name in PRESET_NAMES:
            if outdated(raw["configs"][name]["config"]):
                config = validate_policy(parse_json((self.presets_dir / f"{name}.json").read_bytes()))
                raw["configs"][name] = {"config": config, "revision": revision(config), "updated_at": now()}
                changed = True
        selected = raw["selection"]
        if outdated(selected["config"]):
            entry = raw["configs"][selected["name"]]
            raw["selection"] = {"name": selected["name"], "revision": entry["revision"],
                                "selected_at": now(), "config": entry["config"]}
            changed = True
        return changed

    def _write(self, state):
        if self.read_only:
            raise ConfigError(405, "method_not_allowed")
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
            _replace_file(temporary, self.path)
            replaced = True
            _sync_directory(self.directory)
        except OSError:
            # A directory-fsync failure after rename must not activate unacknowledged edits.
            if replaced:
                if backup is not None:
                    _replace_file(backup, self.path)
                else:
                    self.path.unlink(missing_ok=True)
                try:
                    _sync_directory(self.directory)
                except OSError:
                    pass  # report unavailable even when the filesystem cannot sync the rollback
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
            active = state["selection"]
            return [{"name": name, "preset": True, "description": entry["config"]["description"],
                     "revision": entry["revision"], "selected": name == active["name"],
                     "active_revision": active["revision"] if name == active["name"] else None,
                     "requires_selection": name != active["name"] or entry["revision"] != active["revision"]}
                    for name, entry in sorted(state["configs"].items())]

    def get_config(self, name):
        self._name(name)
        with self._transaction() as state:
            return state["configs"][name]

    def get_active_config(self, name):
        self._name(name)
        snapshot = self.snapshot_for_intercept()
        if snapshot["name"] != name:
            raise ConfigError(409, "config_revision_conflict")
        return snapshot

    def update(self, name, value):
        if self.read_only:
            raise ConfigError(405, "method_not_allowed")
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
        if self.read_only:
            raise ConfigError(405, "method_not_allowed")
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
