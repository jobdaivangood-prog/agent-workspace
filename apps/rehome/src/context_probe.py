"""Prove restored synthetic messages reach the model request without a model.

A short-lived receiver bound only to 127.0.0.1 accepts one Responses payload,
keeps it in memory, and returns intentional HTTP 400. No API key is required,
no real provider is configured, and no request payload is written to disk.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import threading

from protocol_probe import CodexProbe, MARKER, ProbeError, SUPPORTED_VERSION, SUPPORTED_RUNTIMES
from snapshot import _safe_path


PROVIDER = "synthetic_context_receiver"
MODEL = "synthetic-context-probe"
STOP_MESSAGE = "synthetic_probe_stop"
MAX_BODY = 4 * 1024 * 1024


def _guard_profile(profile_path) -> Path:
    path = Path(os.path.abspath(profile_path))
    if any(part.casefold() == ".codex" for part in path.parts):
        raise ProbeError("Production .codex paths are prohibited")
    _safe_path(path)
    try:
        marker = json.loads((path / MARKER).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise ProbeError("A valid synthetic profile marker is required") from None
    if marker.get("kind") != "synthetic-only" or marker.get("schema_version") != 1:
        raise ProbeError("Only explicitly synthetic profiles may be probed")
    if marker.get("codex_version", SUPPORTED_VERSION) != SUPPORTED_VERSION:
        raise ProbeError("Synthetic profile version mismatch")
    for name in (MARKER, "state_5.sqlite", "thread_history_1.sqlite"):
        if (path / name).exists():
            _safe_path(path / name)
    if (path / "auth.json").exists():
        raise ProbeError("Profiles containing authorization are prohibited")
    if (path / "config.toml").exists():
        raise ProbeError("Context probe requires an unconfigured synthetic profile")
    return path


class _Receiver:
    """Ephemeral loopback-only HTTP receiver; never logs headers or payload."""
    def __init__(self):
        self.payloads = []
        self.authorization_seen = False
        self.bad_requests = 0
        self.event = threading.Event()
        self.lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _reply(self, status, body):
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded)
                self.wfile.flush()

            def do_POST(self):
                # Reject credential-bearing traffic without keeping its value.
                if self.headers.get("Authorization"):
                    with receiver.lock:
                        receiver.authorization_seen = True
                        receiver.bad_requests += 1
                    self._reply(400, {"error": {"message": "authorization_prohibited"}})
                    receiver.event.set()
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    length = -1
                if self.path != "/v1/responses" or not 0 < length <= MAX_BODY:
                    with receiver.lock:
                        receiver.bad_requests += 1
                    self._reply(400, {"error": {"message": "synthetic_request_rejected"}})
                    receiver.event.set()
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    with receiver.lock:
                        receiver.bad_requests += 1
                    self._reply(400, {"error": {"message": "synthetic_invalid_json"}})
                    receiver.event.set()
                    return
                with receiver.lock:
                    receiver.payloads.append(payload)
                self._reply(400, {"error": {"type": STOP_MESSAGE, "code": STOP_MESSAGE,
                                           "message": STOP_MESSAGE}})
                receiver.event.set()

            def do_GET(self):
                with receiver.lock:
                    receiver.bad_requests += 1
                self._reply(404, {"error": {"message": "synthetic_endpoint_only"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.timeout = 5
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class _LocalContextProbe(CodexProbe):
    """Separate synthetic write probe; does not change the history-read whitelist."""
    def __init__(self, executable, profile, *, timeout=20, environment=None, supported_runtimes=None):
        self.profile = _guard_profile_for_launch(profile)
        self.executable = Path(executable).resolve()
        self.timeout, self.counter = timeout, 0
        self.requests = []
        self.inbox = queue.Queue()
        self.process = None
        env = {name: os.environ[name] for name in
               ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP") if name in os.environ}
        if environment is None:
            user = self.profile / "context-probe-user"
            user.mkdir(exist_ok=False)
            env.update({"CODEX_HOME": str(self.profile), "HOME": str(user), "USERPROFILE": str(user),
                        "APPDATA": str(user / "AppData" / "Roaming"),
                        "LOCALAPPDATA": str(user / "AppData" / "Local")})
        else:
            # Explicit opt-in for a receiver-local synthetic stage. The normal
            # probe whitelist and its default environment stay unchanged.
            allowed = {"systemroot", "windir", "path", "pathext", "temp", "tmp",
                       "codex_home", "home", "userprofile", "appdata", "localappdata"}
            if not isinstance(environment, dict) or any(not isinstance(k, str) or k.casefold() not in allowed or not isinstance(v, str) for k, v in environment.items()):
                raise ProbeError("Unexpected synthetic environment field")
            user = self.profile.parent
            expected = {"CODEX_HOME": self.profile, "HOME": user, "USERPROFILE": user,
                        "APPDATA": user / "AppData/Roaming", "LOCALAPPDATA": user / "AppData/Local",
                        "TEMP": user / "Temp", "TMP": user / "Temp"}
            if any(environment.get(key) != str(path) for key, path in expected.items()):
                raise ProbeError("Receiver probe environment is not isolated")
            env = dict(environment)
        version = subprocess.run([str(self.executable), "--version"], env=env,
                                 capture_output=True, text=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True)
        version_text = version.stdout.strip()
        runtime_version = version_text.removeprefix("codex-cli ")
        admitted = SUPPORTED_RUNTIMES if supported_runtimes is None else frozenset(supported_runtimes)
        if not version_text.startswith("codex-cli ") or runtime_version not in admitted:
            raise ProbeError("Unsupported Codex executable for context probe")
        self.runtime_version = runtime_version
        self.process = subprocess.Popen(
            [str(self.executable), "app-server", "--listen", "stdio://", "-c", "analytics.enabled=false"],
            cwd=self.profile, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        def read():
            try:
                for line in self.process.stdout:
                    if len(line) > 8 * 1024 * 1024:
                        self.inbox.put(ProbeError("Oversized synthetic protocol message"))
                        return
                    self.inbox.put(json.loads(line))
            except Exception as error:
                self.inbox.put(error)
            finally:
                self.inbox.put(ProbeError("Synthetic context process closed"))
        threading.Thread(target=read, daemon=True).start()
        try:
            self._request("initialize", {"clientInfo": {
                "name": "agent_workspace_context_probe", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False}})
            self._write({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise

    def resume_and_probe(self, thread_id):
        self._request("thread/resume", {"threadId": thread_id, "excludeTurns": True,
                                       "modelProvider": PROVIDER, "model": MODEL})
        return self._request("turn/start", {"threadId": thread_id, "input": [{
            "type": "text", "text": "Synthetic context inspection only.", "text_elements": []}],
            "model": MODEL})


def _guard_profile_for_launch(profile) -> Path:
    """The generated loopback-only config must already exist at launch."""
    path = Path(os.path.abspath(profile))
    if any(part.casefold() == ".codex" for part in path.parts):
        raise ProbeError("Production profile paths are prohibited")
    _safe_path(path)
    marker = json.loads((path / MARKER).read_text(encoding="utf-8"))
    if marker.get("kind") != "synthetic-only" or marker.get("schema_version") != 1:
        raise ProbeError("Synthetic profile required")
    if (path / "auth.json").exists():
        raise ProbeError("Authorization prohibited")
    return path


def _config(port):
    return (f'model = "{MODEL}"\nmodel_provider = "{PROVIDER}"\n'
            'check_for_update_on_startup = false\nweb_search = "disabled"\n'
            '[analytics]\nenabled = false\n[feedback]\nenabled = false\n'
            f'[model_providers.{PROVIDER}]\nname = "Synthetic loopback receiver"\n'
            f'base_url = "http://127.0.0.1:{port}/v1"\nwire_api = "responses"\n'
            'requires_openai_auth = false\nsupports_websockets = false\nrequest_max_retries = 0\n')


def _summarize(payload, expected_user_text, expected_assistant_text):
    inputs = payload.get("input", [])
    if not isinstance(inputs, list):
        raise ProbeError("Unexpected Responses input format")
    visible = {"user": [], "assistant": []}
    nonmessage = []
    attachments = 0
    for item in inputs:
        if not isinstance(item, dict):
            nonmessage.append("invalid")
            continue
        kind = item.get("type", "message" if "role" in item else "unknown")
        if kind != "message":
            nonmessage.append(kind)
        content = item.get("content", [])
        if isinstance(content, str):
            if item.get("role") in visible:
                visible[item["role"]].append(content)
            continue
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                attachments += 1
            elif block.get("type") in ("input_text", "output_text", "text"):
                if item.get("role") in visible and isinstance(block.get("text"), str):
                    visible[item["role"]].append(block["text"])
            else:
                attachments += 1
    return {"prior_user_message_present": any(expected_user_text in text for text in visible["user"]),
            "prior_assistant_message_present": any(expected_assistant_text in text for text in visible["assistant"]),
            "input_items": len(inputs), "user_text_blocks": len(visible["user"]),
            "assistant_text_blocks": len(visible["assistant"]),
            "reasoning_items": nonmessage.count("reasoning"),
            "tool_input_items": sum(kind in {"function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"} for kind in nonmessage),
            "other_nonmessage_items": sum(kind not in {"reasoning", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"} for kind in nonmessage),
            "attachment_blocks": attachments}


def probe_context(executable, profile_path, thread_id, expected_user_text, expected_assistant_text):
    """Inspect one restored synthetic chat's actual request to a local fake provider.

    This deliberately starts one failing synthetic turn, never a real model.
    The profile is test-only and is mutated by that failed turn. The temporary
    provider configuration is removed before returning. Full payload is never
    part of the report and is never logged.
    """
    profile = _guard_profile(profile_path)
    if not all(isinstance(value, str) and value for value in (thread_id, expected_user_text, expected_assistant_text)):
        raise ProbeError("Synthetic thread and expected messages must be nonempty strings")
    config = profile / "config.toml"
    with _Receiver() as receiver:
        generated = _config(receiver.port).encode("utf-8")
        with config.open("xb") as stream:
            stream.write(generated)
        try:
            with _LocalContextProbe(executable, profile) as probe:
                probe.resume_and_probe(thread_id)
                if not receiver.event.wait(timeout=20):
                    raise ProbeError("No local Responses request was captured before timeout")
                commands = list(probe.requests)
            if receiver.authorization_seen or receiver.bad_requests or len(receiver.payloads) != 1:
                raise ProbeError("Synthetic receiver saw unexpected traffic or credentials")
            summary = _summarize(receiver.payloads[0], expected_user_text, expected_assistant_text)
            passed = (summary["prior_user_message_present"] and summary["prior_assistant_message_present"]
                      and not any(summary[key] for key in ("reasoning_items", "tool_input_items", "other_nonmessage_items", "attachment_blocks")))
            return {"status": "passed" if passed else "failed", "synthetic_only": True,
                    "codex_version": getattr(probe, "runtime_version", SUPPORTED_VERSION),
                    "storage_adapter_version": SUPPORTED_VERSION, **summary,
                    "requests_captured": len(receiver.payloads), "intentional_http_status": 400,
                    "receiver": "127.0.0.1", "authorization_header_seen": False,
                    "model_turn_started": True, "real_model_invoked": False,
                    "api_credentials_used": False, "payload_persisted": False,
                    "protocol_requests": commands, "real_profile_changed": False}
        finally:
            if config.exists() and hashlib.sha256(config.read_bytes()).digest() == hashlib.sha256(generated).digest():
                config.unlink()
