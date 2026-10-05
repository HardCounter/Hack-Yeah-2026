"""Portable config locking and native write durability; actual OS paths run in CI."""
import ctypes
import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from configuration import service as storage


def test_native_file_lock_excludes_another_open_and_releases(tmp_path):
    path = tmp_path / ".config.lock"
    first = storage._private_open(path, os.O_CREAT | os.O_RDWR)
    second = storage._private_open(path, os.O_CREAT | os.O_RDWR)
    try:
        storage._try_lock(first)
        try:
            with pytest.raises(OSError):
                storage._try_lock(second)
        finally:
            storage._unlock(first)
        storage._try_lock(second)
        storage._unlock(second)
    finally:
        os.close(second)
        os.close(first)


def test_transaction_releases_lock_after_failed_work(tmp_path):
    service = storage.ConfigService(tmp_path)
    with pytest.raises(RuntimeError):
        with service._transaction():
            raise RuntimeError("work failed")
    assert storage.ConfigService(tmp_path).snapshot_for_intercept()["name"] == "standard"


def test_storage_errors_do_not_retry_as_lock_contention(tmp_path, monkeypatch):
    calls = []
    def broken(_fd):
        calls.append(True)
        raise OSError(errno.EIO, "disk failed")
    monkeypatch.setattr(storage, "_try_lock", broken)
    with pytest.raises(storage.ConfigError) as error:
        storage.ConfigService(tmp_path).snapshot_for_intercept()
    assert error.value.code == "config_unavailable" and len(calls) == 1


def test_lock_file_symlink_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / "unrelated"
    target.write_bytes(b"unchanged")
    try:
        (tmp_path / ".config.lock").symlink_to(target)
    except OSError:
        if os.name == "nt":
            pytest.skip("Windows runner does not grant symlink creation privileges")
        raise
    with pytest.raises(storage.ConfigError):
        storage.ConfigService(tmp_path).snapshot_for_intercept()
    assert target.read_bytes() == b"unchanged"


@pytest.mark.skipif(os.name == "nt", reason="Windows durability uses native replacement")
def test_posix_directory_sync_does_not_follow_symlinks(tmp_path):
    directory = tmp_path / "actual"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(OSError):
        storage._sync_directory(link)


def test_windows_lock_and_unlock_always_use_the_same_byte(tmp_path, monkeypatch):
    calls = []
    fd = os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0,
                           locking=lambda handle, mode, count: calls.append((os.lseek(handle, 0, os.SEEK_CUR), mode, count)))
    monkeypatch.setattr(storage, "WINDOWS", True)
    monkeypatch.setattr(storage, "msvcrt", fake, raising=False)
    try:
        os.lseek(fd, 42, os.SEEK_SET)
        storage._try_lock(fd)
        os.lseek(fd, 73, os.SEEK_SET)
        storage._unlock(fd)
    finally:
        os.close(fd)
    assert calls == [(0, 2, 1), (0, 0, 1)]


def test_windows_replace_requests_write_through_without_cross_volume_copy(monkeypatch):
    calls = []
    class Move:
        def __call__(self, source, destination, flags):
            calls.append((source, destination, flags))
            return True
    monkeypatch.setattr(storage, "WINDOWS", True)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: SimpleNamespace(MoveFileExW=Move()), raising=False)
    storage._replace_file(Path("temporary"), Path("state.json"))
    assert calls == [("temporary", "state.json", 0x1 | 0x8)]


def test_windows_native_replace_error_is_reported(monkeypatch):
    class Move:
        def __call__(self, *_args):
            return False
    monkeypatch.setattr(storage, "WINDOWS", True)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: SimpleNamespace(MoveFileExW=Move()), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(errno.EACCES, "native failure"), raising=False)
    with pytest.raises(OSError):
        storage._replace_file("temporary", "state.json")
