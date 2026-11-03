"""Read-only process gate for a reviewed local directory-switch trial.

This watches named Codex/ChatGPT processes and the union of their observed
descendants. It does not prove absence of every possible file writer. It never
terminates or launches processes, edits environment variables or changes files.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import ntpath
import os
from pathlib import Path
import re
import threading
import time

from detached_runner import ProcessIdentity, RunnerError, capture_process_identity


WRITER_NAMES = {"codex.exe", "chatgpt.exe"}
OVERRIDE_NAMES = ("CODEX_HOME", "CODEX_SQLITE_HOME", "CODEX_ELECTRON_USER_DATA_PATH")
QUIET_SECONDS = 2.0
SCOPE = "named Codex/ChatGPT and observed descendants"


class GuardError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _process_api():
    if os.name != "nt":
        raise GuardError("UNSUPPORTED_OS", "The process gate requires Windows")
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    dll.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(dll, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        function.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    return dll


def enumerate_processes() -> list[dict]:
    """Return the full Toolhelp inventory, rejecting truncated enumeration.

    Only PID, parent PID and executable base name are read. Command lines,
    environments, credentials and profile contents are never inspected here.
    """
    dll = _process_api()
    handle = dll.CreateToolhelp32Snapshot(0x00000002, 0)
    if not handle or handle == wintypes.HANDLE(-1).value:
        raise GuardError("ENUMERATION_FAILED", f"Process snapshot failed (Windows code {ctypes.get_last_error()})")
    try:
        row = _PROCESSENTRY32W()
        row.dwSize = ctypes.sizeof(row)
        ctypes.set_last_error(0)
        success = dll.Process32FirstW(handle, ctypes.byref(row))
        if not success:
            error = ctypes.get_last_error()
            if error == 18:  # ERROR_NO_MORE_FILES: a complete empty snapshot.
                return []
            raise GuardError("ENUMERATION_FAILED", f"First process entry failed (Windows code {error})")
        result = []
        while True:
            result.append({"pid": int(row.th32ProcessID), "parent_pid": int(row.th32ParentProcessID), "name": row.szExeFile})
            ctypes.set_last_error(0)
            if not dll.Process32NextW(handle, ctypes.byref(row)):
                error = ctypes.get_last_error()
                if error != 18:
                    raise GuardError("ENUMERATION_FAILED", f"Process enumeration was incomplete (Windows code {error})")
                return result
    finally:
        dll.CloseHandle(handle)


def _indexed(rows) -> dict[int, dict]:
    if not isinstance(rows, list):
        raise GuardError("INVALID_INVENTORY", "Process inventory must be a complete list")
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"pid", "parent_pid", "name"}
                or type(row["pid"]) is not int or row["pid"] < 0
                or type(row["parent_pid"]) is not int or row["parent_pid"] < 0
                or not isinstance(row["name"], str) or not row["name"] or len(row["name"]) > 260
                or "\x00" in row["name"]):
            raise GuardError("INVALID_INVENTORY", "Invalid process metadata")
        if row["pid"] in result or (row["pid"] != 0 and row["pid"] == row["parent_pid"]):
            raise GuardError("INVALID_INVENTORY", "Duplicate PID or self-parent process metadata")
        result[row["pid"]] = dict(row)
    return result


def _normalize_windows_path(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value) or '"' in value:
        raise GuardError("ENVIRONMENT_OVERRIDE", "An ambiguous profile directory override is present")
    value = value.replace("/", "\\")
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    drive, tail = ntpath.splitdrive(value)
    if not drive or not tail.startswith("\\"):
        raise GuardError("ENVIRONMENT_OVERRIDE", "Relative profile directory overrides are prohibited")
    return ntpath.normcase(ntpath.normpath(value))


def _default_directories() -> dict[str, str]:
    home, roaming = os.environ.get("USERPROFILE"), os.environ.get("APPDATA")
    if not home or not roaming:
        raise GuardError("ENVIRONMENT_UNAVAILABLE", "Default user profile directories are unavailable")
    return {"CODEX_HOME": ntpath.join(home, ".codex"),
            "CODEX_SQLITE_HOME": ntpath.join(home, ".codex"),
            "CODEX_ELECTRON_USER_DATA_PATH": ntpath.join(roaming, "Codex")}


def _expand_registry_value(value: str) -> str:
    # Expand only these ordinary Windows path variables, never arbitrary secrets.
    allowed = {name.casefold(): os.environ.get(name) for name in
               ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "SystemDrive")}

    def replace(match):
        result = allowed.get(match.group(1).casefold())
        if not result:
            raise GuardError("ENVIRONMENT_UNAVAILABLE", "A registry profile override cannot be resolved safely")
        return result

    return re.sub(r"%([^%]+)%", replace, value)


def read_profile_overrides() -> dict[str, dict[str, str | None]]:
    """Read only the three profile-related values in process/user/machine scope."""
    if os.name != "nt":
        raise GuardError("UNSUPPORTED_OS", "Environment preflight requires Windows")
    import winreg
    result = {"process": {name: os.environ.get(name) for name in OVERRIDE_NAMES}}
    locations = (("user", winreg.HKEY_CURRENT_USER, "Environment"),
                 ("machine", winreg.HKEY_LOCAL_MACHINE,
                  r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"))
    for scope, hive, path in locations:
        values = {name: None for name in OVERRIDE_NAMES}
        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as key:
                for name in OVERRIDE_NAMES:
                    try:
                        value, kind = winreg.QueryValueEx(key, name)
                    except FileNotFoundError:
                        continue
                    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
                        raise GuardError("ENVIRONMENT_OVERRIDE", "A profile override has an unsupported registry type")
                    values[name] = _expand_registry_value(value) if kind == winreg.REG_EXPAND_SZ else value
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise GuardError("ENVIRONMENT_UNAVAILABLE", f"Cannot inspect {scope} profile overrides") from exc
        result[scope] = values
    return result


class QuietGuard:
    """In-memory gate; receipts alone are never authorization to rename files."""
    def __init__(self, *, expected_defaults=None, check_environment=True):
        if type(check_environment) is not bool:
            raise GuardError("INVALID_ARGUMENT", "check_environment must be a boolean")
        self.check_environment = check_environment
        self.expected_defaults = expected_defaults
        if expected_defaults is not None:
            if not isinstance(expected_defaults, dict) or set(expected_defaults) != set(OVERRIDE_NAMES):
                raise GuardError("INVALID_ARGUMENT", "Explicit defaults must specify exactly three profile paths")
            self.expected_defaults = {name: _normalize_windows_path(value) for name, value in expected_defaults.items()}
        self._known: dict[int, ProcessIdentity] = {}
        self._uncertain: set[int] = set()
        self._observed: set[int] = set()
        self._ready = False
        self._quiet_since = None
        self._sequence = 0
        self._lock = threading.Lock()

    def _environment(self, check_environment=None):
        enabled = self.check_environment if check_environment is None else check_environment
        if type(enabled) is not bool:
            raise GuardError("INVALID_ARGUMENT", "check_environment must be a boolean")
        if not enabled:
            return
        defaults = self.expected_defaults or {name: _normalize_windows_path(value) for name, value in _default_directories().items()}
        values = read_profile_overrides()
        if not isinstance(values, dict) or set(values) != {"process", "user", "machine"}:
            raise GuardError("ENVIRONMENT_UNAVAILABLE", "Incomplete profile override preflight")
        for scope, entries in values.items():
            if not isinstance(entries, dict) or set(entries) != set(OVERRIDE_NAMES):
                raise GuardError("ENVIRONMENT_UNAVAILABLE", "Incomplete profile override preflight")
            for name, value in entries.items():
                if value is not None and _normalize_windows_path(value) != defaults[name]:
                    raise GuardError("ENVIRONMENT_OVERRIDE", f"{scope} {name} differs from the reviewed default directory")

    def _poll(self) -> dict:
        indexed = _indexed(enumerate_processes())
        alive = set(indexed) - {0}
        self._uncertain.intersection_update(alive)
        cache: dict[int, ProcessIdentity | None] = {}

        def identity(pid):
            if pid in cache:
                return cache[pid]
            try:
                found = capture_process_identity(pid)
            except (OSError, RunnerError):
                # A process can exit between the snapshot and identity query.
                # Keep waiting until another complete inventory confirms absence.
                cache[pid] = None
                self._uncertain.add(pid)
                return None
            if not isinstance(found, ProcessIdentity) or found.pid != pid:
                raise GuardError("INVALID_IDENTITY", "Process identity response did not match its PID")
            if pid in self._known and found != self._known[pid]:
                raise GuardError("PID_REUSED", f"PID {pid} belongs to a different process instance")
            cache[pid] = found
            return found

        named = {pid for pid, row in indexed.items() if pid and row["name"].casefold() in WRITER_NAMES}
        for pid in (set(self._known) & alive) | named | self._uncertain:
            found = identity(pid)
            if found is not None:
                self._known[pid] = found
            self._observed.add(pid)
        children: dict[int, list[int]] = {}
        for pid, row in indexed.items():
            if pid:
                children.setdefault(row["parent_pid"], []).append(pid)
        # Retain historical parents: helpers can outlive a captured main process
        # or first appear in the next snapshot after that parent has exited.
        pending_parents = list(set(self._known) | named | self._uncertain)
        visited = set()
        while pending_parents:
            parent = pending_parents.pop()
            if parent in visited:
                continue
            visited.add(parent)
            parent_identity = self._known.get(parent)
            for child in children.get(parent, []):
                child_identity = identity(child)
                if parent_identity is not None and child_identity is not None and child_identity.creation_time_ticks < parent_identity.creation_time_ticks:
                    # The recorded parent PID was reused before this snapshot.
                    # An older generic node is not this newer writer's child.
                    continue
                if child_identity is not None:
                    self._known[child] = child_identity
                if parent_identity is None:
                    self._uncertain.add(child)
                self._observed.add(child)
                pending_parents.append(child)
        pending = (set(self._known) & alive) | self._uncertain | named
        self._sequence += 1
        return {"pending_count": len(pending), "pending_pids": sorted(pending),
                "uncertain_pids": sorted(self._uncertain), "observed_process_count": len(self._observed),
                "scope": SCOPE, "all_possible_writers_checked": False,
                "inventory_sequence": self._sequence}

    def assert_quiet(self, *, check_environment=None) -> dict:
        """Recheck immediately before EACH directory rename; never a saved boolean."""
        with self._lock:
            if not self._ready:
                raise GuardError("NOT_READY", "The complete quiet-period wait has not succeeded")
            self._environment(check_environment)
            result = self._poll()
            if result["pending_count"]:
                self._ready = False
                self._quiet_since = None
                raise GuardError("NOT_QUIET", "Codex or an observed descendant appeared again")
            return {"schema": 1, "status": "ready", "quiet_seconds": QUIET_SECONDS, **result}


def wait_for_quiet(cancel_path, timeout_seconds=3600, on_update=None, *,
                   expected_defaults=None, check_environment=True, poll_seconds=.1) -> QuietGuard:
    """Wait voluntarily for known writers, then require two seconds of quiet.

    Rollback may set check_environment=False so a later environment change never
    removes the process-gated path back to the original directories.
    """
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 86400
            or isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float))
            or not math.isfinite(poll_seconds) or not .01 <= poll_seconds <= 1):
        raise GuardError("INVALID_ARGUMENT", "A finite timeout and bounded poll interval are required")
    if on_update is not None and not callable(on_update):
        raise GuardError("INVALID_ARGUMENT", "on_update must be callable")
    cancel = Path(cancel_path) if cancel_path is not None else None
    guard = QuietGuard(expected_defaults=expected_defaults, check_environment=check_environment)
    started = time.monotonic()
    while True:
        if cancel is not None and cancel.exists():
            raise GuardError("CANCELLED", "Process wait was cancelled")
        guard._environment()
        result = guard._poll()
        now = time.monotonic()
        if now - started > timeout_seconds:
            raise GuardError("TIMED_OUT", "Known Codex writers did not become quiet before the timeout")
        if result["pending_count"]:
            guard._quiet_since = None
            quiet = 0.0
            status = "waiting"
        else:
            if guard._quiet_since is None:
                guard._quiet_since = now
            quiet = max(0.0, now - guard._quiet_since)
            status = "ready" if quiet >= QUIET_SECONDS else "quiet_period"
        update = {"status": status, "quiet_seconds": round(min(quiet, QUIET_SECONDS), 3), **result}
        if on_update is not None:
            on_update(update)
        if status == "ready":
            if cancel is not None and cancel.exists():
                raise GuardError("CANCELLED", "Process wait was cancelled")
            guard._environment()
            guard._ready = True
            # Close the gap between the final progress update and handoff.
            guard.assert_quiet()
            return guard
        elapsed = now - started
        if elapsed >= timeout_seconds:
            raise GuardError("TIMED_OUT", "Known Codex writers did not become quiet before the timeout")
        time.sleep(min(float(poll_seconds), timeout_seconds - elapsed))
