"""Independent wait worker for the transfer prototype (Windows, Python stdlib).

This module never terminates another process, starts Codex, changes a profile,
or performs a real restore.  The probe worker writes a receipt after the exact
process identities have exited.  It is deliberately useful as a small building
block for a later restore worker.

DETACHED_PROCESS detaches the console; it does not escape an enclosing Windows
Job object.  A surrogate-parent test is not evidence that closing the actual
Codex application will preserve a worker launched by that application's job.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid
from typing import Iterable


class RunnerError(RuntimeError):
    """A wait/launch operation could not safely establish its preconditions."""


class IdentityMismatch(RunnerError):
    """The PID belongs to a different process instance."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_time_ticks: int

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise RunnerError("Process PID must be a positive integer")
        if type(self.creation_time_ticks) is not int or self.creation_time_ticks <= 0:
            raise RunnerError("Process creation time must be a positive integer")


def _kernel32():
    if os.name != "nt":
        raise RunnerError("This prototype requires Windows")
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    dll.GetProcessTimes.restype = wintypes.BOOL
    dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    dll.WaitForSingleObject.restype = wintypes.DWORD
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    return dll


def _open_process(pid: int, *, allow_missing: bool):
    dll = _kernel32()
    # Query identity and wait on the same process handle, avoiding PID reuse.
    handle = dll.OpenProcess(0x1000 | 0x00100000, False, pid)
    if not handle:
        code = ctypes.get_last_error()
        if allow_missing and code == 87:  # ERROR_INVALID_PARAMETER: no such PID.
            return dll, None
        raise RunnerError(f"Cannot inspect process {pid} (Windows error {code})")
    return dll, handle


def _creation_ticks(dll, handle) -> int:
    creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not dll.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
        raise RunnerError(f"Cannot read process identity (Windows error {ctypes.get_last_error()})")
    return (creation.dwHighDateTime << 32) | creation.dwLowDateTime


def capture_process_identity(pid: int) -> ProcessIdentity:
    """Capture an existing process's PID and exact Windows creation time."""
    if type(pid) is not int or pid <= 0:
        raise RunnerError("Process PID must be a positive integer")
    dll, handle = _open_process(pid, allow_missing=False)
    try:
        return ProcessIdentity(pid, _creation_ticks(dll, handle))
    finally:
        dll.CloseHandle(handle)


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunnerError("Timeout must be a finite number")
    if not math.isfinite(value) or not 0 < value <= 86400:
        raise RunnerError("Timeout must be greater than zero and at most one day")
    return float(value)


def wait_for_processes(
    identities: Iterable[ProcessIdentity],
    *,
    timeout_seconds: float,
    cancel_path: Path | None = None,
    poll_seconds: float = 0.1,
) -> dict:
    """Wait without killing anything; return exited/cancelled/timed_out.

    A missing PID is already exited.  A reused PID is an identity mismatch and
    fails closed, so no caller should continue to restoration in that case.
    Handles are held throughout the wait rather than polling PID existence.
    """
    timeout_seconds = _timeout(timeout_seconds)
    if not 0.01 <= poll_seconds <= 1:
        raise RunnerError("Poll interval must be between 0.01 and 1 second")
    expected = tuple(identities)
    if not expected or len(expected) > 100:
        raise RunnerError("Expected between 1 and 100 process identities")
    if len({identity.pid for identity in expected}) != len(expected):
        raise RunnerError("Duplicate process PID")
    opened = []
    started = time.monotonic()
    try:
        for identity in expected:
            dll, handle = _open_process(identity.pid, allow_missing=True)
            if handle is None:
                continue
            opened.append((dll, handle))
            if _creation_ticks(dll, handle) != identity.creation_time_ticks:
                raise IdentityMismatch(f"Process {identity.pid} identity changed")
        while True:
            if cancel_path is not None and cancel_path.exists():
                return {"status": "cancelled", "waited_seconds": round(time.monotonic() - started, 3)}
            pending = []
            for dll, handle in opened:
                result = dll.WaitForSingleObject(handle, 0)
                if result == 0x00000102:  # WAIT_TIMEOUT, still running.
                    pending.append((dll, handle))
                elif result != 0:  # WAIT_OBJECT_0 is the only other success.
                    raise RunnerError(f"Cannot wait for process (Windows error {ctypes.get_last_error()})")
            if not pending:
                return {"status": "exited", "waited_seconds": round(time.monotonic() - started, 3)}
            elapsed = time.monotonic() - started
            if elapsed >= timeout_seconds:
                return {"status": "timed_out", "waited_seconds": round(elapsed, 3)}
            time.sleep(min(poll_seconds, timeout_seconds - elapsed))
    finally:
        for dll, handle in opened:
            dll.CloseHandle(handle)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _write_atomic(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _check_plain_path(path: Path) -> None:
    # Refuse junction/symlink parents rather than letting a test job escape.
    for component in (path, *path.parents):
        if not component.exists():
            continue
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RunnerError("Reparse points are not allowed in a worker job path")


def launch_detached_probe(
    job_directory: Path,
    identities: Iterable[ProcessIdentity],
    *,
    timeout_seconds: float = 300,
    purpose: str = "surrogate-parent",
) -> dict:
    """Start an independent console-detached worker in a NEW local job folder.

    No breakaway-from-job flags, services, scheduler tasks, shell commands, or
    registry changes are used.  The returned PID is launch evidence, not proof
    that work completed.  Read receipt.json for the terminal outcome.
    """
    if os.name != "nt":
        raise RunnerError("This prototype requires Windows")
    if purpose != "surrogate-parent":
        raise RunnerError("Only the synthetic surrogate-parent probe is implemented")
    timeout_seconds = _timeout(timeout_seconds)
    expected = tuple(identities)
    if not expected or len(expected) > 100:
        raise RunnerError("Expected between 1 and 100 process identities")
    if len({item.pid for item in expected}) != len(expected):
        raise RunnerError("Duplicate process PID")
    job_directory = Path(job_directory).absolute()
    _check_plain_path(job_directory)
    if not job_directory.parent.is_dir():
        raise RunnerError("The worker job parent directory must exist")
    job_directory.mkdir(exist_ok=False)
    request = {
        "schema_version": 1,
        "purpose": purpose,
        "wait_for": [asdict(identity) for identity in expected],
        "timeout_seconds": timeout_seconds,
    }
    request_path = job_directory / "request.json"
    _write_atomic(request_path, request)
    request_digest = hashlib.sha256(request_path.read_bytes()).hexdigest()
    try:
        worker = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--worker", str(request_path), "--digest", request_digest],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            cwd=str(job_directory),
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except OSError as exc:
        _write_atomic(job_directory / "receipt.json", {"status": "launch_failed", "error_type": type(exc).__name__, "schema_version": 1})
        raise RunnerError("Could not launch the detached worker") from exc
    return {
        "status": "launched",
        "worker_pid": worker.pid,
        "job_directory": str(job_directory),
        "request_sha256": request_digest,
        "completion_receipt": str(job_directory / "receipt.json"),
        "scope": "surrogate-parent only; enclosing Windows Job inheritance unchanged",
    }


def run_probe_worker(request_path: Path, expected_digest: str) -> int:
    """Run a fixed safe probe action; never accept arbitrary executable actions."""
    request_path = Path(request_path).absolute()
    job_directory = request_path.parent
    try:
        _check_plain_path(request_path)
        if request_path.name != "request.json" or not request_path.is_file():
            raise RunnerError("Expected a regular request.json in its job directory")
        if request_path.stat().st_size > 65536:
            raise RunnerError("Worker request is too large")
        raw = request_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise RunnerError("Worker request changed after launch")
        request = json.loads(raw)
        if type(request) is not dict or set(request) != {"schema_version", "purpose", "wait_for", "timeout_seconds"}:
            raise RunnerError("Unsupported worker request fields")
        if request["schema_version"] != 1 or request["purpose"] != "surrogate-parent":
            raise RunnerError("Unsupported worker request")
        if type(request["wait_for"]) is not list:
            raise RunnerError("wait_for must be an array")
        identities = [ProcessIdentity(**item) for item in request["wait_for"]]
        outcome = wait_for_processes(
            identities,
            timeout_seconds=request["timeout_seconds"],
            cancel_path=job_directory / "cancel",
        )
        ready = outcome["status"] == "exited"
        if ready:
            with (job_directory / "restored-probe.txt").open("x", encoding="utf-8") as stream:
                stream.write("Synthetic probe completed after surrogate parent exit. No Codex profile was restored.\n")
        receipt = {
            "schema_version": 1,
            "status": "probe_completed" if ready else outcome["status"],
            "verification_scope": "surrogate-parent tested; actual Codex close not tested",
            "worker_pid": os.getpid(),
            "request_sha256": expected_digest,
            "wait_result": outcome,
            "restoration_performed": False,
        }
        _write_atomic(job_directory / "receipt.json", receipt)
        return 0 if ready else 2
    except (OSError, ValueError, TypeError, RunnerError) as exc:
        # Keep the receipt small, without configuration, command output, or data.
        try:
            _write_atomic(job_directory / "receipt.json", {
                "schema_version": 1,
                "status": "failed",
                "error_type": type(exc).__name__,
                "restoration_performed": False,
            })
        except OSError:
            pass
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic independent restore-worker probe")
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--digest", required=True)
    arguments = parser.parse_args(argv)
    return run_probe_worker(arguments.worker, arguments.digest)


if __name__ == "__main__":
    raise SystemExit(main())
