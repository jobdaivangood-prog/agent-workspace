"""Local Codex protocol checks against explicitly marked synthetic profiles.

Never starts a model turn, copies credentials, or touches a real Codex profile.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from snapshot import _safe_path

MARKER = ".agent-workspace-synthetic.json"
SUPPORTED_VERSION = "0.162.1"
SUPPORTED_RUNTIMES = frozenset({"0.162.1", "0.164.0-alpha.11.1"})
READ_METHODS = {"thread/list", "thread/read", "thread/turns/list", "thread/items/list"}


class ProbeError(RuntimeError):
    pass


def validate_profile_path(root: Path, *, require_marker: bool = True) -> Path:
    root = Path(os.path.abspath(root))
    if any(part.casefold() == ".codex" for part in root.parts):
        raise ProbeError("Real .codex profile paths and their descendants are prohibited")
    _safe_path(root, allow_missing=not require_marker)
    if not require_marker:
        return root
    try:
        marker_file = _safe_path(root / MARKER)
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProbeError("A valid synthetic-only marker is required") from error
    if not isinstance(marker, dict) or marker.get("kind") != "synthetic-only" or marker.get("schema_version") != 1:
        raise ProbeError("A valid synthetic-only marker is required")
    if marker.get("codex_version", SUPPORTED_VERSION) != SUPPORTED_VERSION:
        raise ProbeError("Synthetic profile version mismatch")
    if (root / "auth.json").exists() or (root / "config.toml").exists():
        raise ProbeError("Probe profiles must not contain authorization or pre-existing configuration")
    return root


def mark_synthetic(root: Path) -> Path:
    root = validate_profile_path(root, require_marker=False)
    root.mkdir(parents=True, exist_ok=False)
    (root / MARKER).write_text(json.dumps({"kind": "synthetic-only", "schema_version": 1}), encoding="utf-8")
    return root


class CodexProbe:
    """Short-lived built-in Codex interface; this is not an MCP server."""

    def __init__(self, executable: Path, profile: Path, *, timeout: float = 25):
        self.profile = validate_profile_path(profile)
        self.executable = Path(executable).resolve()
        version = subprocess.run(
            [str(self.executable), "--version"], capture_output=True, text=True,
            timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True,
        ).stdout.strip()
        runtime_version = version.removeprefix("codex-cli ")
        if not version.startswith("codex-cli ") or runtime_version not in SUPPORTED_RUNTIMES:
            raise ProbeError(f"Unsupported Codex executable: {version}")
        self.runtime_version = runtime_version
        # Intentionally omit API keys and credential variables from the child environment.
        names = ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "USERPROFILE", "HOME",
                 "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP")
        env = {k: os.environ[k] for k in names if k in os.environ}
        env["CODEX_HOME"] = str(self.profile)
        self.process = subprocess.Popen(
            [str(self.executable), "app-server", "--listen", "stdio://",
             "-c", "analytics.enabled=false"],
            cwd=self.profile, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.timeout, self.counter = timeout, 0
        self.inbox: queue.Queue = queue.Queue()
        self.requests: list[str] = []

        def read():
            try:
                for line in self.process.stdout:
                    if len(line) > 8 * 1024 * 1024:
                        self.inbox.put(ProbeError("Oversized protocol response"))
                        return
                    self.inbox.put(json.loads(line))
            except Exception as error:
                self.inbox.put(error)
            finally:
                self.inbox.put(ProbeError("Codex probe process closed"))

        threading.Thread(target=read, daemon=True).start()
        try:
            self._request("initialize", {"clientInfo": {
                "name": "agent_workspace_synthetic_probe", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False}})
            self._write({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise

    def _write(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _request(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.requests.append(method)
        self._write({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                message = self.inbox.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if isinstance(message, Exception):
                raise ProbeError(str(message))
            if "method" in message and "id" in message:
                self._write({"id": message["id"], "error": {
                    "code": -32601, "message": "Synthetic probe refuses server callbacks"}})
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise ProbeError(f"{method}: {message['error'].get('message', 'RPC error')}")
                return message.get("result")
        raise ProbeError(f"Timeout during {method}")

    def read(self, method, **params):
        if method not in READ_METHODS:
            raise ProbeError("The probe permits only history reads")
        return self._request(method, params)

    def seed_empty_thread(self, project_root: Path):
        """Create synthetic metadata, without sending turn/start or any model request."""
        project_root = Path(project_root).resolve()
        if not project_root.is_relative_to(self.profile):
            raise ProbeError("Synthetic project must be inside the marked profile")
        project_root.mkdir(parents=True, exist_ok=True)
        return self._request("thread/start", {
            "cwd": str(project_root), "ephemeral": False,
        })

    def close(self):
        if not getattr(self, "process", None):
            return
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            # This process belongs solely to this synthetic test, never the user's app.
            self.process.terminate()
            self.process.wait(timeout=4)
        if self.process.stdout:
            self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
