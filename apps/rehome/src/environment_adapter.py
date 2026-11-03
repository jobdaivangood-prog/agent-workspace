"""Synthetic-only environment staging composed with the existing backup engine.

Reuses its bounded source inventory, secret filter, path validator and exclusion
sets. Never calls create_backup, pruning, vault or password UI. Production
installation/merge and account authentication are deliberately absent.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath, PurePosixPath
import re
import sys
import uuid

from snapshot import _safe_path, _hash_file, _read_json, _fingerprint, _content_state


FORMAT = "agent-workspace-environment/1"
BACKUP_FORMAT = 'rehome-backup-environment/1'
MARKER = ".agent-workspace-synthetic.json"
MANIFEST = "environment.json"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.@+/-]{1,200}$")
_SIMPLE = re.compile(r"^[A-Za-z0-9_.+-]{1,128}$")
_URL = re.compile(r"https?://[^\s<>\"'`]+", re.I)
_SECRET_KEY = re.compile(r"token|password|passwd|secret|api.?key|authorization|cookie|credential", re.I)
_DEPS = {"node_modules", ".venv", "venv", "npm-cache", "ocr"}
_ABSOLUTE_TEXT = re.compile(r'''(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/][^\s"'`<>]+|/(?:opt|usr|home|mnt|Volumes|Users|var|srv|etc|tmp)/[^\s"'`<>]+)''')
_COMPONENT_POLICY = {
    "user-skills": ("skills", "codex/skills", False),
    "agents-skills": ("skills", "home/.agents/skills", False),
    "plugin-packages": ("plugin-cache", None, False),
    "skill-health-data": ("data", "home/Documents/Codex/skill-health-data", False),
    "agent-reach": ("tool", "home/Documents/Codex/agent-reach", False),
    "engineering-runtime": ("runtime", "home/Documents/Codex/engineering-runtime", False),
    "codex-rules": ("rules", "codex/rules", False),
    "codex-agents": ("agents", "codex/agents", False),
    "home-agent-definitions": ("agents", "home/.agents/agents", False),
    "codex-instructions": ("instructions", "codex/AGENTS.md", True),
}
_REFERENCE_COMPONENTS = (
    (re.compile(r"project-\d+-(?:agents|codex)-skills"), "project-reference"),
    (re.compile(r"custom-plugin-\d+"), "custom-plugin"),
    (re.compile(r"agent-config-\d+"), "agent-reference"),
    (re.compile(r"engineering-context-\d+"), "context"),
)
_DENIED_BASENAMES = {"auth.json", "credentials.json", "secrets.json", "token.json", "tokens.json",
                     "cookies.json", "cookies.txt", ".npmrc", ".pypirc"}


class EnvironmentError(ValueError):
    pass


def _component_policy(component_id):
    if component_id in _COMPONENT_POLICY:
        return _COMPONENT_POLICY[component_id]
    for pattern, kind in _REFERENCE_COMPONENTS:
        if isinstance(component_id, str) and pattern.fullmatch(component_id):
            return kind, None, False
    raise EnvironmentError("Unknown environment component")


def _expected_target(component_id, relative):
    _, root, single_file = _component_policy(component_id)
    if root is None:
        return None
    if single_file:
        if relative != "AGENTS.md":
            raise EnvironmentError("Instruction component must contain only AGENTS.md")
        return root
    return root + "/" + relative


def _denied_resource_name(relative):
    name = relative.split("/")[-1].casefold()
    return (name in _DENIED_BASENAMES or
            name.startswith(".env") and not name.endswith((".example", ".sample", ".template")))


def load_existing_engine(engine_path):
    """Import the explicit existing builder without running its CLI or writing pyc."""
    source = _safe_path(Path(engine_path))
    prompt = _safe_path(source.parent / "password_prompt.py")
    old_bytecode = sys.dont_write_bytecode
    old_prompt = sys.modules.get("password_prompt")
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("agent_workspace_existing_password_prompt", prompt)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["password_prompt"] = module
        spec = importlib.util.spec_from_file_location("agent_workspace_existing_backup_engine", source)
        engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(engine)
    finally:
        sys.dont_write_bytecode = old_bytecode
        if old_prompt is None:
            sys.modules.pop("password_prompt", None)
        else:
            sys.modules["password_prompt"] = old_prompt
    required = ("sources", "sensitive", "safe_name", "scrub_config", "SKIP_DIRS", "MAX_FILE", "MAX_TOTAL")
    if any(not hasattr(engine, name) for name in required):
        raise EnvironmentError("Existing backup engine lacks the expected reusable helpers")
    return engine


def _synthetic(path, *, require_marker=True):
    root = Path(os.path.abspath(path))
    if any(part.casefold() == ".codex" for part in root.parts):
        raise EnvironmentError("Production .codex paths are prohibited")
    _safe_path(root, allow_missing=not require_marker)
    if require_marker:
        try:
            mark = _read_json(root / MARKER)
        except (OSError, ValueError):
            raise EnvironmentError("An explicit synthetic profile marker is required") from None
        if mark.get("kind") != "synthetic-only" or mark.get("schema_version") != 1:
            raise EnvironmentError("Invalid synthetic profile marker")
        if (root / "auth.json").exists():
            raise EnvironmentError("Profiles with authorization are prohibited")
    return root


def _write_json(path, value):
    _safe_path(path, allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write((json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _name(value, fallback):
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) and ":" not in value and not value.startswith("/") and ".." not in value.split("/") else fallback


def _absolute(value):
    return isinstance(value, str) and (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())


def _safe_config(cfg):
    """Selected values only. URLs, opaque arguments and secret values become refs."""
    selected, bindings, secrets, omitted = {}, [], [], []
    def reference(label, *, local=False, kind="value"):
        key = "binding-" + hashlib.sha256(label.encode()).hexdigest()[:20]
        if local:
            bindings.append({"id": key, "field": label, "kind": kind, "requires_local_setup": True})
            return {"local_binding": key}
        secrets.append({"id": key, "field": label, "requires_reentry": True})
        return {"secret_ref": key}
    for key in ("model", "model_provider", "personality"):
        value = cfg.get(key)
        if isinstance(value, str) and _SIMPLE.fullmatch(value):
            selected[key] = value
        elif value is not None:
            omitted.append(key)
    enums = {"model_reasoning_effort": {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"},
             "model_verbosity": {"low", "medium", "high"}}
    for key, choices in enums.items():
        if cfg.get(key) in choices:
            selected[key] = cfg[key]
        elif key in cfg:
            omitted.append(key)
    mcp = {}
    for index, (raw_name, data) in enumerate(cfg.get("mcp_servers", {}).items()):
        if not isinstance(data, dict):
            continue
        name = _name(raw_name, f"server-{index}")
        server = {"requires_local_setup": True}
        for key in ("enabled", "required"):
            if type(data.get(key)) is bool:
                server[key] = data[key]
        for key in ("startup_timeout_sec", "tool_timeout_sec"):
            if type(data.get(key)) in (int, float) and 0 <= data[key] <= 3600:
                server[key] = data[key]
        if "command" in data:
            command = data["command"]
            server["command"] = command if isinstance(command, str) and _SIMPLE.fullmatch(command) else reference(f"mcp.{name}.command", local=_absolute(command), kind="executable")
        if "cwd" in data:
            server["cwd"] = reference(f"mcp.{name}.cwd", local=True, kind="working_directory")
        if "url" in data:
            server["url"] = reference(f"mcp.{name}.url")
        if isinstance(data.get("args"), list):
            server["args"] = [reference(f"mcp.{name}.args.{i}", local=_absolute(value), kind="argument_path") for i, value in enumerate(data["args"])]
        for key in ("env", "http_headers"):
            if isinstance(data.get(key), dict):
                server[key] = {_name(raw, f"field-{i}"): reference(f"mcp.{name}.{key}.{_name(raw, f'field-{i}')}") for i, raw in enumerate(data[key])}
        if data.get("auth") in ("oauth", "chatgpt"):
            server["auth"] = data["auth"]
        mcp[name] = server
    plugins = {}
    for index, (raw, data) in enumerate(cfg.get("plugins", {}).items()):
        name = _name(raw, f"plugin-{index}")
        plugins[name] = {"install_required": True}
        if isinstance(data, dict) and type(data.get("enabled")) is bool:
            plugins[name]["enabled"] = data["enabled"]
    agents = {}
    if isinstance(cfg.get("agents"), dict):
        for index, (raw, data) in enumerate(cfg["agents"].items()):
            if not isinstance(data, dict):
                continue
            name = _name(raw, f"agent-{index}")
            agent = {}
            if "config_file" in data:
                agent["config_file"] = reference(f"agents.{name}.config_file", local=True, kind="agent_definition")
            agents[name] = agent
    selected.update({"mcp_servers": mcp, "plugins": plugins, "agents": agents})
    for key in ('check_for_update_on_startup', 'hide_agent_reasoning'):
        if type(cfg.get(key)) is bool:
            selected[key] = cfg[key]
        elif key in cfg:
            omitted.append(key)
    desktop = cfg.get('desktop', {})
    if isinstance(desktop, dict):
        shell = desktop.get('integratedTerminalShell')
        if shell in {'commandPrompt','powershell','powershell7','windowsPowerShell'}:
            selected['desktop'] = {'integratedTerminalShell': shell}
        omitted.extend('desktop.' + _name(k,'unrecognized-field') for k in desktop if k != 'integratedTerminalShell' or 'desktop' not in selected)
    unknown = set(cfg) - {"model", "model_provider", "personality", "model_reasoning_effort", "model_verbosity", "mcp_servers", "plugins", "agents",'desktop','check_for_update_on_startup','hide_agent_reasoning'}
    omitted.extend(_name(key, "unrecognized-field") for key in sorted(unknown))
    return {"selected": selected, "local_bindings": bindings, "secret_refs": secrets, "omitted_settings": omitted,
            "active_config_written": False, "authorization_exported": False}


def _credential_url(data):
    from urllib.parse import urlsplit
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    for match in _URL.finditer(text):
        try:
            url = urlsplit(match.group())
        except ValueError:
            return True
        # Query/fragment may hide credentials; do not guess from their names.
        if url.username is not None or url.password is not None or url.query or url.fragment:
            return True
    return False


def _tokenize_roots(data, home, codex_home):
    if b"\x00" in data:
        return data, []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, []
    tokens = []
    for label, root in (("CODEX_HOME", codex_home), ("HOME", home)):
        variants = [(str(root).replace("\\", "\\\\"), f"${{{label}_JSON}}"),
                    (root.as_posix(), f"${{{label}_POSIX}}"), (str(root), f"${{{label}_NATIVE}}")]
        for old, replacement in variants:
            # A is a complete path prefix; it must never match Alice or A-old.
            # Both raw and JSON-escaped '\\' separators are covered here.
            pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(old) + r'''(?=$|[\\/\s"'`,;\)\]\}])''',
                                 re.I if os.name == "nt" else 0)
            if old and pattern.search(text):
                text = pattern.sub(lambda _: replacement, text)
                tokens.append(replacement)
    return text.encode("utf-8"), sorted(set(tokens))


def _tokenize_unknown_paths(data, component, relative):
    """Do not carry executable references to another machine's unknown paths."""
    if b"\x00" in data:
        return data, []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, []
    refs = []
    values = sorted({match.group().rstrip(",;)]}") for match in _ABSOLUTE_TEXT.finditer(text)}, key=len, reverse=True)
    for index, value in enumerate(values):
        label = f"resource.{component}.{relative}.{index}"
        identifier = "binding-" + hashlib.sha256(label.encode()).hexdigest()[:20]
        token = "${LOCAL_BINDING_" + identifier + "}"
        text = text.replace(value, token)
        refs.append({"id": identifier, "field": label, "kind": "resource_path", "requires_local_setup": True})
    return text.encode("utf-8"), refs


def _read_stable(path, max_file):
    path = _safe_path(path)
    before = path.lstat()
    if not path.is_file() or before.st_size > max_file:
        raise EnvironmentError("Source is not a regular bounded file")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if _fingerprint(before) != _fingerprint(opened):
            raise EnvironmentError("Environment source changed before reading")
        data = stream.read(max_file + 1)
        after = os.fstat(stream.fileno())
    if len(data) > max_file or _fingerprint(before) != _fingerprint(after) or _content_state(opened) != _content_state(after) or _content_state(before) != _content_state(path.lstat()):
        raise EnvironmentError("Environment source changed during reading")
    return data


def _backup_sensitive(engine, path, data):
    """Supplement legacy text-only heuristics with the same patterns in bytes.

    Binary resources remain exact when admitted. This is bounded detection of
    recognizable credentials, not a guarantee about arbitrary secret values.
    """
    if engine.sensitive(path, data) or _credential_url(data):
        return True
    for sample in (data, data.replace(b'\x00', b'')):
        if engine.KEY_PATTERN.search(sample):
            return True
        for match in engine.ASSIGNED_SECRET.finditer(sample):
            if engine.sensitive(path, match.group(0).decode('utf-8', errors='replace').encode('utf-8')):
                return True
        if _credential_url(sample):
            return True
    return False


def _supplemental_specs(home, codex_home, cfg):
    specs = [dict(id="codex-rules", source=codex_home / "rules", kind="rules", target="codex/rules"),
             dict(id="codex-agents", source=codex_home / "agents", kind="agents", target="codex/agents"),
             dict(id="home-agent-definitions", source=home / ".agents" / "agents", kind="agents", target="home/.agents/agents"),
             dict(id="codex-instructions", source=codex_home / "AGENTS.md", kind="instructions", target="codex/AGENTS.md")]
    for index, (name, agent) in enumerate(cfg.get("agents", {}).items() if isinstance(cfg.get("agents"), dict) else []):
        if isinstance(agent, dict) and isinstance(agent.get("config_file"), str):
            raw = Path(agent["config_file"])
            path = raw if raw.is_absolute() else codex_home / raw
            # Ordinary codex/agents definitions are already covered above.
            if not path.is_relative_to(codex_home / "agents"):
                specs.append(dict(id=f"agent-config-{index}", source=path, kind="agent-reference", target=None))
    return specs


def export_environment(home, codex_home, destination, *, engine_path):
    """Stage only a marked synthetic environment in a NEW sibling directory."""
    return _export_environment(home, codex_home, destination, engine_path=engine_path, synthetic_only=True)


def export_backup_environment(home, codex_home, destination, *, engine_path):
    """Read explicit real resource roots; prepare a NEW inert portable package."""
    return _export_environment(home, codex_home, destination, engine_path=engine_path, synthetic_only=False)


def _export_environment(home, codex_home, destination, *, engine_path, synthetic_only):
    if synthetic_only:
        home = _synthetic(home, require_marker=False)
        codex_home = _synthetic(codex_home)
        destination = _synthetic(destination, require_marker=False)
    else:
        from rehome_review_sources import _local
        home, codex_home = _local(home), _local(codex_home)
        destination = _local(destination, missing=True)
        if any(part.casefold() == '.codex' for part in destination.parts):
            raise EnvironmentError('Live environment output prohibited')
    if not codex_home.is_relative_to(home):
        raise EnvironmentError("Codex resource profile must be inside explicit home")
    if destination.exists() or (synthetic_only and destination.is_relative_to(home)):
        raise EnvironmentError("Use a new staging directory outside source resources")
    engine = load_existing_engine(engine_path)
    source_bindings = {}
    if synthetic_only:
        specs, cfg = engine.sources(home, codex_home)
    else:
        from rehome_environment_sources import guarded_sources
        specs, cfg, source_bindings = guarded_sources(engine, home, codex_home)
    specs += _supplemental_specs(home, codex_home, cfg)
    if not synthetic_only:
        from rehome_review_sources import _local
        for spec in specs:
            source = _local(spec['source'], missing=True)
            if destination.is_relative_to(source) or source.is_relative_to(destination):
                raise EnvironmentError('Environment output overlaps selected source resources')
    # Fixed policy controls installation locations; mutable inventory metadata
    # cannot turn a reference package into an active profile file.
    for spec in specs:
        kind, target_root, _ = _component_policy(spec["id"])
        if kind != spec["kind"]:
            raise EnvironmentError("Existing source component kind does not match policy")
        spec["target"] = target_root
    if synthetic_only and (codex_home / "config.toml").exists():
        # Reuse the legacy scrubber as an additional check; its output is never
        # exported because opaque URL/argument protection must be stricter.
        engine.scrub_config((codex_home / "config.toml").read_text("utf-8-sig"))
    config = _safe_config(cfg)
    discovered, directory_bindings = {}, {}
    if not synthetic_only:
        from rehome_environment_sources import discover_resources
        discovered, directory_bindings = discover_resources(specs, home, engine)
    inventory = {"format": FORMAT if synthetic_only else BACKUP_FORMAT, "synthetic_only": synthetic_only,
                 "created_at": datetime.now(timezone.utc).isoformat(),
                 "engine_sha256": hashlib.sha256(Path(engine_path).read_bytes()).hexdigest(),
                 "components": [], "files": [], "skills": [], "plugins": [], "agents": [],
                 "excluded": [], "dependencies": [], "config": config,
                 "limits": ["Synthetic profiles only" if synthetic_only else 'Real resources; preparation only; no activation',
                            "Plugin caches are reference snapshots, not installed plugins",
                            "Dependencies and authentication require separate local setup",
                            "Secret filtering reuses legacy heuristics; arbitrary secrets cannot be guaranteed absent"]}
    if not synthetic_only:
        inventory['token_namespace'] = uuid.uuid4().hex
    destination.mkdir(parents=True)
    seen = set()
    total = 0
    skill_candidates = []
    candidate_count = 0
    for spec in specs:
        source = Path(os.path.abspath(spec["source"]))
        component = {"id": spec["id"], "kind": spec["kind"], "present": source.exists(),
                     "file_count": 0, "total_bytes": 0,
                     "restore_mode": "reference-only" if spec["target"] is None else "copy"}
        inventory["components"].append(component)
        if not source.is_relative_to(home):
            inventory["excluded"].append({"component": spec["id"], "path": "", "reason": "outside_synthetic_home" if synthetic_only else 'outside_explicit_home'})
            continue
        if not source.exists():
            continue
        _safe_path(source)
        candidates = []
        if not synthetic_only:
            candidates, exclusions = discovered[spec['id']]
            inventory['excluded'].extend(exclusions)
            inventory['dependencies'].extend(e.copy() for e in exclusions if e['reason'] == 'dependency_excluded')
        elif source.is_file():
            candidates.append((source, source.name))
        else:
            for directory, dirs, files in os.walk(source, followlinks=False):
                current = _safe_path(Path(directory))
                excluded_dirs = {".git", "__pycache__"} if spec["kind"] == "plugin-cache" else set(engine.SKIP_DIRS)
                if spec["kind"] == "tool":
                    excluded_dirs |= {"npm", "tools", "state"}
                if spec["kind"] == "runtime":
                    excluded_dirs.add("ocr")
                keep = []
                for name in dirs:
                    relative = (current / name).relative_to(source).as_posix()
                    try:
                        _safe_path(current / name)
                        linked = False
                    except ValueError:
                        linked = True
                    if name in excluded_dirs or linked:
                        reason = "link_or_reparse" if linked else "dependency_excluded" if name in _DEPS else "temporary_or_local_state"
                        item = {"component": spec["id"], "path": relative, "reason": reason}
                        inventory["excluded"].append(item)
                        if reason == "dependency_excluded":
                            inventory["dependencies"].append(item.copy())
                    else:
                        keep.append(name)
                dirs[:] = keep
                candidates.extend((current / name, (current / name).relative_to(source).as_posix()) for name in sorted(files))
        for path, relative in candidates:
            candidate_count += 1
            if candidate_count > 100000:
                raise EnvironmentError('Environment file count limit exceeded')
            record = {"component": spec["id"], "path": relative}
            if _denied_resource_name(relative):
                inventory['excluded'].append({**record, 'reason': 'sensitive_content_omitted'})
                continue
            try:
                engine.safe_name(relative)
                if not synthetic_only and _safe_path(path).stat().st_nlink != 1:
                    raise EnvironmentError('Hard-linked resource prohibited')
                data = _read_stable(path, engine.MAX_FILE)
            except (ValueError, OSError):
                inventory["excluded"].append({**record, "reason": "unsafe_link_missing_or_large_file"})
                continue
            if not synthetic_only:
                binding = (hashlib.sha256(data).hexdigest(), len(data))
                if _hash_file(path) != binding:
                    raise EnvironmentError('Environment resource changed after reading')
                source_bindings[path] = binding
            sensitive = (engine.sensitive(path, data) or _credential_url(data)) if synthetic_only else _backup_sensitive(engine, path, data)
            if path.name == 'SKILL.md':
                skill_candidates.append((spec['id'], relative, b'' if sensitive and not synthetic_only else data))
            if sensitive:
                inventory["excluded"].append({**record, "reason": "sensitive_content_omitted"})
                continue
            raw_data = data
            rebindings = []
            if synthetic_only:
                data, tokens = _tokenize_roots(data, home, codex_home)
                data, unresolved = _tokenize_unknown_paths(data, spec['id'], relative)
            else:
                from rehome_environment_contract import project_resource
                data, rebindings, unresolved = project_resource(data, home, codex_home, spec['id'], relative, inventory['token_namespace'])
                tokens = [r['token'] for r in rebindings]
            if unresolved:
                inventory["config"]["local_bindings"].extend(unresolved)
                inventory["dependencies"].append({**record, "reason": "unresolved_resource_paths", "bindings": [item["id"] for item in unresolved]})
            payload = engine.safe_name(f"payload/{spec['id']}/{relative}")
            if payload.casefold() in seen:
                raise EnvironmentError("Environment payload path collision")
            seen.add(payload.casefold())
            total += len(data)
            if total > engine.MAX_TOTAL:
                raise EnvironmentError("Environment exceeds the existing engine size limit")
            output = destination.joinpath(*payload.split("/"))
            _safe_path(output, allow_missing=True)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as stream:
                stream.write(data)
            target = _expected_target(spec["id"], relative)
            if target is not None:
                engine.safe_name(target)
            inventory["files"].append({"path": payload, "component": spec["id"], "relative_path": relative,
                                       "target": target, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "tokens": tokens,
                                       "unresolved_bindings": [item["id"] for item in unresolved]})
            if not synthetic_only:
                inventory['files'][-1].update(original_sha256=hashlib.sha256(raw_data).hexdigest(),
                                              original_size=len(raw_data), rebindings=rebindings)
            component["file_count"] += 1
            component["total_bytes"] += len(data)
            if spec["kind"] in {"agents", "agent-reference", "instructions", "rules"}:
                inventory["agents"].append({"component": spec["id"], "path": relative, "kind": spec["kind"]})
            if spec["kind"] in {"plugin-cache", "custom-plugin"} and (relative.endswith((".codex-plugin/plugin.json", ".claude-plugin/plugin.json")) or relative == "plugin.json"):
                try:
                    meta = json.loads(raw_data)
                except (ValueError, UnicodeError):
                    meta = {}
                name = _name(meta.get("name", meta.get("id")), "unidentified-plugin")
                version = meta.get("version")
                inventory["plugins"].append({"id": name, "version": version if isinstance(version, str) and _SIMPLE.fullmatch(version) else None,
                                             "component": spec["id"], "manifest_path": payload,
                                             "restore_mode": "reference-only", "install_required": True})
    described = {(component,relative) for component,relative,_ in skill_candidates}
    for item in inventory['excluded']:
        if item['path'].split('/')[-1] == 'SKILL.md' and (item['component'],item['path']) not in described:
            skill_candidates.append((item['component'],item['path'],b''))
            described.add((item['component'],item['path']))
    for component, relative, data in skill_candidates:
        folder = relative.rsplit("/", 1)[0] if "/" in relative else ""
        prefix = folder + "/" if folder else ""
        files = [entry for entry in inventory["files"] if entry["component"] == component and entry["relative_path"].startswith(prefix)]
        try:
            match = re.search(r"(?m)^name:\s*['\"]?([^'\"\r\n]+)", data.decode("utf-8-sig"))
        except UnicodeDecodeError:
            match = None
        name = _name(match.group(1).strip() if match else None, folder.split("/")[-1] or "unnamed-skill")
        exclusions = [entry for entry in inventory["excluded"] if entry["component"] == component and entry["path"].startswith(prefix)]
        kind, target_root, _ = _component_policy(component)
        discoverable = kind == "skills" and target_root in {"codex/skills", "home/.agents/skills"}
        status = ("reference-only" if not discoverable else
                  "requires_local_setup" if exclusions or any(entry["unresolved_bindings"] for entry in files) else "copied")
        inventory["skills"].append({"name": name, "component": component, "path": folder,
                                     "file_count": len(files), "resource_dirs": sorted({entry["relative_path"][len(prefix):].split("/")[0] for entry in files if "/" in entry["relative_path"][len(prefix):]}),
                                     "restore_mode": "copy" if discoverable else "reference-only",
                                     "discoverable_after_copy": discoverable,
                                     "status": status})
    for name, data in config["selected"]["plugins"].items():
        if not any(item["id"] == name for item in inventory["plugins"]):
            inventory["plugins"].append({"id": name, "version": None, "component": None, "manifest_path": None,
                                         "restore_mode": "reinstall-required", "install_required": True})
    if isinstance(cfg.get("agents"), dict):
        for index, (name, agent) in enumerate(cfg["agents"].items()):
            if isinstance(agent, dict) and isinstance(agent.get("config_file"), str):
                path = Path(agent["config_file"])
                path = path if path.is_absolute() else codex_home / path
                if not path.exists():
                    inventory["dependencies"].append({"component": "agent-definition", "path": _name(name, f"agent-{index}"), "reason": "agent_definition_missing"})
    inventory["file_count"] = len(inventory["files"])
    inventory["total_bytes"] = total
    inventory["skill_counts"] = {"discovered": len(inventory["skills"]),
                                 "copy_ready": sum(item["status"] == "copied" for item in inventory["skills"]),
                                 "requires_local_setup": sum(item["status"] == "requires_local_setup" for item in inventory["skills"]),
                                 "reference_only": sum(item["status"] == "reference-only" for item in inventory["skills"])}
    if not synthetic_only:
        from rehome_environment_sources import verify_discovery, verify_metadata_bindings
        verify_metadata_bindings(source_bindings)
        verify_discovery(directory_bindings)
    _write_json(destination / MANIFEST, inventory)
    (verify_environment if synthetic_only else verify_backup_environment)(destination)
    return inventory


def verify_environment(staging):
    return _verify_environment(staging, synthetic_only=True)


def verify_backup_environment(staging):
    return _verify_environment(staging, synthetic_only=False)


def _verify_environment(staging, *, synthetic_only):
    if synthetic_only:
        root = _synthetic(staging, require_marker=False)
    else:
        from rehome_review_sources import _local
        root = _local(staging)
    inventory = _read_json(root / MANIFEST)
    if inventory.get("format") != (FORMAT if synthetic_only else BACKUP_FORMAT) or inventory.get("synthetic_only") is not synthetic_only:
        raise EnvironmentError("Unsupported environment inventory")
    if not isinstance(inventory.get("files"), list):
        raise EnvironmentError("Missing environment file inventory")
    components = {}
    if not isinstance(inventory.get("components"), list):
        raise EnvironmentError("Missing environment component inventory")
    for component in inventory["components"]:
        if not isinstance(component, dict) or component.get("id") in components:
            raise EnvironmentError("Duplicate or invalid environment component")
        kind, target_root, _ = _component_policy(component.get("id"))
        expected_mode = "copy" if target_root is not None else "reference-only"
        if component.get("kind") != kind or component.get("restore_mode") != expected_mode:
            raise EnvironmentError("Environment component policy differs from its declared mode")
        components[component["id"]] = component
    expected = {MANIFEST}
    seen = set()
    total = 0
    restore_paths = []
    from snapshot import _validate_relative, _check_collisions
    for entry in inventory["files"]:
        if not isinstance(entry, dict) or type(entry.get("size")) is not int or entry["size"] < 0:
            raise EnvironmentError("Invalid environment file entry")
        if not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise EnvironmentError("Invalid environment file checksum")
        _validate_relative(entry["path"])
        _validate_relative(entry["relative_path"])
        if not isinstance(entry.get("component"), str) or not _SIMPLE.fullmatch(entry["component"]):
            raise EnvironmentError("Invalid environment component identifier")
        if entry["component"] not in components:
            raise EnvironmentError("Payload references an unknown component")
        if entry["path"] != f"payload/{entry['component']}/{entry['relative_path']}":
            raise EnvironmentError("Environment payload path does not match its component")
        if not entry["path"].startswith("payload/"):
            raise EnvironmentError("Environment payload must be under payload/")
        if entry["path"].casefold() in seen:
            raise EnvironmentError("Environment paths collide")
        seen.add(entry["path"].casefold())
        if _denied_resource_name(entry["relative_path"]):
            raise EnvironmentError("Authorization and secret resource basenames are prohibited")
        target = _expected_target(entry["component"], entry["relative_path"])
        if entry.get("target") != target:
            raise EnvironmentError("Environment target does not match fixed component policy")
        if target is not None:
            _validate_relative(entry["target"])
            if target.casefold() in {"codex/config.toml", "codex/auth.json", "home/.codex/config.toml", "home/.codex/auth.json"}:
                raise EnvironmentError("Active configuration and authorization are prohibited")
        restore_paths.append(target if target is not None else f"references/{entry['component']}/{entry['relative_path']}")
    # All output locations are checked before restore creates its stage folder.
    _check_collisions(restore_paths)
    for entry in inventory["files"]:
        checksum, size = _hash_file(root.joinpath(*entry["path"].split("/")))
        if size != entry["size"] or checksum != entry["sha256"]:
            raise EnvironmentError("Environment payload is incomplete or changed")
        expected.add(entry["path"])
        total += size
    actual = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            _safe_path(Path(directory) / name)
        actual.update((Path(directory) / name).relative_to(root).as_posix() for name in files)
    if actual != expected or inventory.get("file_count") != len(seen) or inventory.get("total_bytes") != total:
        raise EnvironmentError("Environment inventory does not match received files")
    if not synthetic_only:
        from rehome_environment_contract import validate_manifest
        validate_manifest(inventory, root)
    return inventory


def restore_environment(staging, destination):
    """Copy verified resources to a NEW synthetic user/profile; do not activate config."""
    return _restore_environment(staging, destination, synthetic_only=True)


def restore_backup_environment(staging, destination):
    """Materialize real resources into a NEW inert user layout; no installation."""
    return _restore_environment(staging, destination, synthetic_only=False)


def _restore_environment(staging, destination, *, synthetic_only):
    inventory = (verify_environment if synthetic_only else verify_backup_environment)(staging)
    source = _safe_path(Path(staging))
    destination = _synthetic(destination, require_marker=False)
    if destination.exists() or destination.is_relative_to(source):
        raise EnvironmentError("Environment restore destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # No longer than EnvironmentMaterials: a long temporary basename used to
    # break otherwise valid restores on Windows with legacy path limits.
    stage = destination.parent / (".er-" + uuid.uuid4().hex[:16])
    import rehome_windows_paths as windows_paths
    for entry in inventory['files']:
        target = entry['target']
        if target is None:
            relative = Path('references') / entry['component'] / entry['relative_path']
        else:
            scope, rest = target.split('/', 1)
            relative = (Path('home/codex-profile') if scope == 'codex' else Path('home')) / rest
        windows_paths.check(stage / relative)
        windows_paths.check(destination / relative)
    for relative in ('home/codex-profile/config.candidate.json', 'rehome-environment-materials.json'):
        windows_paths.check(stage / relative)
        windows_paths.check(destination / relative)
    stage.mkdir()
    home = stage / "home"
    codex = home / "codex-profile"
    codex.mkdir(parents=True)
    final_home = destination / "home"
    final_codex = final_home / "codex-profile"
    restored = 0
    for entry in inventory["files"]:
        target = entry["target"]
        if target is None:
            output = stage / "references" / entry["component"] / entry["relative_path"]
        else:
            scope, relative = target.split("/", 1)
            output = (codex if scope == "codex" else home).joinpath(*relative.split("/"))
        _safe_path(output, allow_missing=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        data = _read_stable(source.joinpath(*entry["path"].split("/")), entry["size"])
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise EnvironmentError("Environment payload changed after verification")
        if not synthetic_only:
            from rehome_environment_contract import resource_bytes
            data = resource_bytes(data, entry, inventory['token_namespace'], home=final_home, codex_home=final_codex)
        elif entry["tokens"]:
            text = data.decode("utf-8")
            for label, root in (("CODEX_HOME", final_codex), ("HOME", final_home)):
                for suffix, value in (("JSON", str(root).replace("\\", "\\\\")), ("POSIX", root.as_posix()), ("NATIVE", str(root))):
                    text = text.replace(f"${{{label}_{suffix}}}", value)
            data = text.encode("utf-8")
        with output.open("xb") as stream:
            stream.write(data)
        restored += 1
    if synthetic_only:
        _write_json(codex / MARKER, {"kind": "synthetic-only", "schema_version": 1})
        _write_json(stage / MARKER, {"kind": "synthetic-only", "schema_version": 1})
    else:
        _write_json(stage / 'rehome-environment-materials.json', {'format': 'rehome-environment-materials/1',
            'synthetic_only': False, 'profile_applied': False, 'environment_activated': False})
    _write_json(codex / "config.candidate.json", inventory["config"])
    if destination.exists():
        raise EnvironmentError("Destination appeared during environment preparation")
    stage.rename(destination)
    return {"status": "prepared", "synthetic_only": synthetic_only, "destination": str(destination),
            "home": str(final_home), "codex_home": str(final_codex), "files_restored": restored,
            "skills": sum(item.get("status") == "copied" and item.get("restore_mode") == "copy" for item in inventory["skills"]),
            "skills_discovered": len(inventory["skills"]),
            "skills_reference_only": sum(item.get("restore_mode") == "reference-only" for item in inventory["skills"]),
            "skill_discovery_verified": False, "plugin_installation_verified": False,
            "active_config_written": False, "authorization_exported": False,
            "requires_local_setup": bool(inventory["config"]["local_bindings"] or inventory["config"]["secret_refs"] or inventory["dependencies"] or inventory["plugins"])}
