"""Read-only source evidence for Codex 0.162.1; never an apply permission.

Production profiles may be read by this module. It never opens them for writes,
reads auth/config, writes a backup, or invokes Codex/model operations. Callers
must choose thread IDs explicitly. The native adapter remains synthetic-only.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import sqlite3
import stat

import native_adapter as native
import rehome_history_filter as history_filter
import rehome_history_boundary as history_boundary

CURRENT_SCHEMA_VERSION = '0.166.0-alpha.5.1'


def _schema(version):
    if version == native.CODEX_VERSION:
        return native.EXPECTED_STATE, native.SCHEMA_FINGERPRINT
    if version != CURRENT_SCHEMA_VERSION:
        raise SourceError('Unsupported source schema version')
    state = copy.deepcopy(native.EXPECTED_STATE)
    state['threads'].append(('attached_thread_parent_id', 'TEXT', 0, 0))
    return state, _digest({'state': state, 'history': native.EXPECTED_HISTORY})

INVENTORY_FORMAT = "agent-workspace-readonly-inventory"
EXPORT_FORMAT = "agent-workspace-readonly-source"


class SourceError(ValueError):
    pass


class SourceChangedError(SourceError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else _json(value).encode()).hexdigest()


def _source_root(root):
    p = Path(root).absolute()
    if not p.is_dir():
        raise SourceError("Source profile is not a directory")
    for part in (p, *p.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise SourceError("Ambiguous symlink/junction source profile")
    for filename in ("state_5.sqlite", "thread_history_1.sqlite"):
        db = p / filename
        if not db.is_file() or db.is_symlink():
            raise SourceError("Required native database missing or symlinked: " + filename)
        # Read-only WAL cannot bootstrap a missing shared-memory file safely.
        if db.with_name(db.name + "-wal").exists() and not db.with_name(db.name + "-shm").exists():
            raise SourceError("Read-only WAL access requires existing shared-memory metadata")
    return p


def _open_ro(path):
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _source_identity(root):
    """Physical directory/database identities, without paths or mutable times.

    SQLite data_version belongs to an opened database connection. It does not
    prove that the same database still occupies the selected filesystem path.
    This additional binding detects ordinary directory/database replacement.
    """
    root = Path(root)
    identities = {}
    for key, path, directory in (('directory', root, True),
                                 ('state_5.sqlite', root / 'state_5.sqlite', False),
                                 ('thread_history_1.sqlite', root / 'thread_history_1.sqlite', False)):
        info = path.lstat()
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_kind(info.st_mode) or not info.st_ino:
            raise SourceError('Source physical identity is unavailable')
        identities[key] = {'device': info.st_dev, 'inode': info.st_ino}
    return identities


def _global_bytes(root):
    p = root / ".codex-global-state.json"
    if p.is_symlink():
        raise SourceError("Global metadata symlink prohibited")
    return p.read_bytes() if p.exists() else b"{}"


class _AttachedSchema:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql):
        if not sql.startswith("PRAGMA table_info("):
            raise SourceError("Unexpected schema query")
        return self.conn.execute(sql.replace("PRAGMA table_info(", "PRAGMA history.table_info(", 1))


@contextmanager
def _snapshot(profile_root, schema_version=native.CODEX_VERSION):
    root = _source_root(profile_root)
    identity = _source_identity(root)
    def ensure_same_source():
        try:
            observed = _source_identity(root)
        except (OSError, SourceError):
            raise SourceChangedError('Source instance disappeared or changed during read') from None
        if observed != identity:
            raise SourceChangedError('Source directory or database was replaced during read')
    conn = state_observer = history_observer = None
    try:
        state_observer = _open_ro(root / "state_5.sqlite")
        ensure_same_source()
        history_observer = _open_ro(root / "thread_history_1.sqlite")
        ensure_same_source()
        before = (state_observer.execute("PRAGMA data_version").fetchone()[0],
                  history_observer.execute("PRAGMA data_version").fetchone()[0])
        global_raw = _global_bytes(root)
        try:
            glob = json.loads(global_raw)
        except ValueError as exc:
            raise SourceError("Invalid global project metadata JSON") from exc
        if not isinstance(glob, dict):
            raise SourceError("Global project metadata must be an object")
        conn = _open_ro(root / "state_5.sqlite")
        ensure_same_source()
        conn.execute("ATTACH DATABASE ? AS history", ((root / "thread_history_1.sqlite").as_uri() + "?mode=ro",))
        ensure_same_source()
        conn.execute("BEGIN")
        native._schema_gate(conn, _schema(schema_version)[0])
        native._schema_gate(_AttachedSchema(conn), native.EXPECTED_HISTORY)
        # Touch both attached stores inside this read transaction. A separate
        # observer detects commits during the operation, including WAL writers.
        conn.execute("SELECT COUNT(*) FROM threads").fetchone()
        conn.execute("SELECT COUNT(*) FROM history.thread_turns").fetchone()
        ensure_same_source()
        yield conn, glob, root
        ensure_same_source()
        after = (state_observer.execute("PRAGMA data_version").fetchone()[0],
                 history_observer.execute("PRAGMA data_version").fetchone()[0])
        if before != after or _global_bytes(root) != global_raw:
            raise SourceChangedError("Source changed during read; retry from inventory")
        ensure_same_source()
    finally:
        if conn is not None:
            if conn.in_transaction:
                conn.rollback()
            conn.close()
        if state_observer is not None:
            state_observer.close()
        if history_observer is not None:
            history_observer.close()


def _normal_path(value):
    if not isinstance(value, str) or not value:
        raise SourceError("Invalid project root metadata")
    return ntpath.normcase(ntpath.normpath(value)) if re.match(r"^[A-Za-z]:[\\/]", value) else os.path.normcase(os.path.normpath(value))


def _host_mapping(glob, root):
    by_host = glob.get("app-server-project-id-by-legacy-project-id-by-host", {})
    if not isinstance(by_host, dict):
        raise SourceError("Invalid host migration mapping")
    matches = []
    for host, mapping in by_host.items():
        if isinstance(host, str) and host.startswith("local:") and _normal_path(host[6:]) == _normal_path(str(root)):
            if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()):
                raise SourceError("Invalid legacy-to-native project mapping")
            matches.append(mapping)
    if len(matches) > 1:
        raise SourceError("Ambiguous current-host migration mapping")
    return matches[0] if matches else {}


def _project_tables(conn):
    projects = {r["id"]: {"id": r["id"], "name": r["name"], "roots": []}
                for r in conn.execute("SELECT id,name FROM projects")}
    for row in conn.execute("SELECT project_id,path FROM project_roots ORDER BY project_id,position"):
        if row["project_id"] not in projects:
            raise SourceError("Orphan project root")
        projects[row["project_id"]]["roots"].append(row["path"])
    return projects


def _resolve_legacy(legacy_id, legacy, projects, migration):
    if not isinstance(legacy_id, str):
        raise SourceError("Invalid legacy project ID")
    if legacy_id in migration:
        target = migration[legacy_id]
        if target not in projects:
            raise SourceError("Migration points to a missing native project")
        if legacy_id in projects and target != legacy_id:
            raise SourceError("Native identity conflicts with migration mapping")
        return target, "migration"
    if legacy_id in projects:
        return legacy_id, "native-id"
    row = legacy.get(legacy_id)
    if not isinstance(row, dict) or not isinstance(row.get("name"), str):
        raise SourceError("Legacy project metadata missing")
    roots = row.get("rootPaths", [])
    if not isinstance(roots, list):
        raise SourceError("Invalid legacy project roots")
    normalized = sorted(_normal_path(p) for p in roots)
    candidates = [pid for pid, project in projects.items()
                  if project["name"] == row["name"] and
                  (not roots or sorted(_normal_path(p) for p in project["roots"]) == normalized)]
    if len(candidates) != 1:
        raise SourceError("Ambiguous or absent project mapping by roots/name")
    return candidates[0], "roots+name" if roots else "unique-name"


def _thread_project(row, glob, projects, migration):
    assignments = glob.get("thread-project-assignments", {})
    hosts = glob.get("thread-project-membership-host-ids", {})
    legacy = glob.get("local-projects", {})
    if not all(isinstance(value, dict) for value in (assignments, hosts, legacy)):
        raise SourceError("Invalid project assignment metadata")
    if hosts.get(row["id"], "local") != "local":
        raise SourceError("Remote-host project membership requires another source")
    assigned = assignments.get(row["id"])
    ui_id, route = None, None
    if assigned is not None:
        if isinstance(assigned, dict):
            if assigned.get("projectKind") != "local":
                raise SourceError("Non-local project membership requires another source")
            legacy_id = assigned.get("projectId")
        elif isinstance(assigned, str):
            # Earlier synthetic prototypes used direct string assignments.
            legacy_id = assigned
        else:
            raise SourceError("Invalid UI project assignment")
        ui_id, route = _resolve_legacy(legacy_id, legacy, projects, migration)
    sql_id = row["project_id"]
    if sql_id is not None and sql_id not in projects:
        raise SourceError("Thread references a missing native project")
    if sql_id is not None and ui_id is not None and sql_id != ui_id:
        raise SourceError("SQL/UI project assignment conflict")
    return sql_id or ui_id, "sql" if sql_id else route or "projectless"


def _metadata(conn, glob, root, schema_version=native.CODEX_VERSION):
    projects = _project_tables(conn)
    migration = _host_mapping(glob, root)
    rows = [dict(r) for r in conn.execute("SELECT id,title,name,project_id,thread_section_id,is_pinned,archived,updated_at_ms,updated_at,cli_version,history_mode,has_user_event FROM threads ORDER BY id")]
    grouped = {}
    for row in conn.execute("SELECT thread_id,status,COUNT(*) AS count FROM history.thread_turns GROUP BY thread_id,status"):
        grouped.setdefault(row["thread_id"], {})[row["status"]] = row["count"]
    public = []
    for row in rows:
        entry = {"id": row["id"], "title": row["title"], "name": row["name"],
                 "archived": bool(row["archived"]), "project_id": None,
                 "mapping": None, "status": "ready", "reason": None,
                 "turn_status_counts": grouped.get(row["id"], {})}
        try:
            native._thread_ident(row["id"])
            if schema_version == CURRENT_SCHEMA_VERSION:
                attached = conn.execute('SELECT attached_thread_parent_id FROM threads WHERE id=?', (row['id'],)).fetchone()[0]
                if attached is not None:
                    raise SourceError('Attached/forked parent history needs lineage verification')
            if row["history_mode"] != "paginated":
                raise SourceError("Legacy history requires a separate source adapter")
            if row["has_user_event"] and not entry["turn_status_counts"]:
                raise SourceError("User thread has no available paginated turn history")
            entry["project_id"], entry["mapping"] = _thread_project(row, glob, projects, migration)
        except (SourceError, native.AdapterError) as exc:
            entry["status"], entry["reason"] = "blocked", str(exc)
        unknown = set(entry["turn_status_counts"]) - native.TERMINAL_TURN_STATUSES - {"inProgress"}
        if entry["status"] == "ready" and unknown:
            entry["status"] = "blocked"
            entry["reason"] = "Unknown native turn status: " + ",".join(sorted(unknown))
        if entry["status"] == "ready" and "inProgress" in entry["turn_status_counts"]:
            entry["status"] = "in_progress"
            entry["reason"] = "Active inProgress turns will be reported and deferred"
        public.append(entry)
    token = _digest({"schema_version": schema_version, "root": _normal_path(str(root)), "state_metadata": rows,
                     "projects": projects, "ui_metadata": glob, "turn_counts": grouped})
    return {"format": INVENTORY_FORMAT, "format_version": 1,
            "codex_version": schema_version,
            "schema_fingerprint": _schema(schema_version)[1],
            "inventory_token": token, "read_only": True,
            "projects": [{"id": p["id"], "name": p["name"], "root_count": len(p["roots"])}
                         for p in projects.values()], "threads": public}, rows, projects


def inventory(profile_root, *, schema_version=native.CODEX_VERSION):
    """Return metadata only, with stable IDs and per-thread mapping blockers."""
    with _snapshot(profile_root, schema_version) as (conn, glob, root):
        report, _, _ = _metadata(conn, glob, root, schema_version)
        return report


def export_selected(profile_root, thread_ids, *, expected_inventory_token=None,
                    schema_version=native.CODEX_VERSION, exclusions=None):
    """Read selected visible history; return evidence, never an import command.

    This function writes no files. Active turns are explicitly reported in
    blocked_turns. Their texts are not queried. Terminal failed/interrupted
    statuses and visible messages are preserved; error_json is never selected.
    Hidden/tool item_json is
    never selected. Source attachments are omitted by whitelist reconstruction.
    """
    if not isinstance(thread_ids, (list, tuple)) or not thread_ids or any(not isinstance(t, str) for t in thread_ids):
        raise SourceError("Select an explicit nonempty list of inventory thread IDs")
    if len(set(thread_ids)) != len(thread_ids):
        raise SourceError("Duplicate selected thread IDs")
    strict = schema_version == CURRENT_SCHEMA_VERSION
    if exclusions is not None and (not strict or not isinstance(exclusions, dict)):
        raise SourceError('Exclusions require the current filtered reader')
    exclusions = {} if exclusions is None else exclusions
    used_exclusions, filtering, boundaries = set(), [], []
    with _snapshot(profile_root, schema_version) as (conn, glob, root):
        report, rows, projects = _metadata(conn, glob, root, schema_version)
        if expected_inventory_token is not None and expected_inventory_token != report["inventory_token"]:
            raise SourceChangedError("Inventory token changed; review fresh inventory")
        by_id = {r["id"]: r for r in rows}
        entries = {e["id"]: e for e in report["threads"]}
        if not set(thread_ids) <= by_id.keys():
            raise SourceError("Selected thread is absent from inventory")
        for tid in thread_ids:
            if entries[tid]["status"] == "blocked":
                raise SourceError("Selected thread is blocked: " + entries[tid]["reason"])
        sections = {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM thread_sections")}
        used_projects, used_sections, threads, blocked_turns = set(), set(), [], []
        omitted = 0
        for tid in thread_ids:
            row, entry = by_id[tid], entries[tid]
            if strict:
                try:
                    boundaries.append(history_boundary.check(conn, root, tid))
                except (history_boundary.BoundaryError, OSError) as exc:
                    raise SourceError('Source history boundary not verified: ' + str(exc)) from None
            pid, sid = entry["project_id"], row["thread_section_id"]
            if pid is not None:
                used_projects.add(pid)
            if sid is not None:
                if sid not in sections:
                    raise SourceError("Missing thread section")
                used_sections.add(sid)
            turns = []
            for turn in conn.execute("SELECT turn_id,status FROM history.thread_turns WHERE thread_id=? ORDER BY rollout_ordinal", (tid,)):
                if turn["status"] == "inProgress":
                    blocked_turns.append({"thread_id": tid, "turn_id": turn["turn_id"],
                                          "status": turn["status"], "reason": "active_turn_deferred"})
                    continue
                if turn["status"] not in native.TERMINAL_TURN_STATUSES:
                    raise SourceError("Unknown selected turn status")
                messages = []
                omitted += conn.execute("SELECT COUNT(*) FROM history.thread_items WHERE thread_id=? AND turn_id=? AND item_type NOT IN ('userMessage','agentMessage')", (tid, turn["turn_id"])).fetchone()[0]
                for item in conn.execute("SELECT item_id,item_type,item_json FROM history.thread_items WHERE thread_id=? AND turn_id=? AND item_type IN ('userMessage','agentMessage') ORDER BY rollout_ordinal", (tid, turn["turn_id"])):
                    try:
                        decoded = json.loads(item['item_json'], object_pairs_hook=history_filter.unique_object)
                        if not isinstance(decoded, dict) or decoded.get('type') != item['item_type'] or decoded.get('id') != item['item_id']:
                            raise SourceError('Visible message identity/type mismatch')
                        if strict:
                            key = tid + '/' + turn['turn_id'] + '/' + item['item_id']
                            visible, evidence = history_filter.visible_message(decoded, exclusions.get(key))
                            filtering.append({'thread_id': tid, 'turn_id': turn['turn_id'], **evidence})
                            if key in exclusions:
                                used_exclusions.add(key)
                        else:
                            visible = native._safe_visible(decoded)
                    except (ValueError, TypeError) as exc:
                        raise SourceError("Invalid selected visible message") from exc
                    if visible is None:
                        raise SourceError("Item type discriminator mismatch")
                    messages.append(visible)
                turns.append({"id": turn["turn_id"], "status": turn["status"], "messages": messages})
            threads.append({"id": tid, "title": row["title"], "name": row["name"],
                            "project_id": pid, "section_id": sid, "is_pinned": bool(row["is_pinned"]),
                            "archived": bool(row["archived"]), "turns": turns})
        selected = set(thread_ids)
        edges, excluded_children = [], []
        for edge in conn.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"):
            if edge["parent_thread_id"] in selected:
                if edge["child_thread_id"] in selected:
                    edges.append({"parent": edge["parent_thread_id"], "child": edge["child_thread_id"]})
                else:
                    excluded_children.append(edge["child_thread_id"])
        transfer = {"format": native.FORMAT, "format_version": native.FORMAT_VERSION,
                    "codex_version": native.CODEX_VERSION, "schema_fingerprint": native.SCHEMA_FINGERPRINT,
                    "synthetic_only": False,
                    "projects": [{"id": pid, "name": projects[pid]["name"]} for pid in sorted(used_projects)],
                    "sections": [{"id": sid, "name": sections[sid]} for sid in sorted(used_sections)],
                    "threads": threads, "spawn_edges": edges, "omitted_nonmessage_items": omitted}
        validation_copy = copy.deepcopy(transfer)
        validation_copy["synthetic_only"] = True
        # Reuse only the pure structural validator. Neither returned object is
        # marked synthetic, and native apply must reject both of them.
        native._validate_bundle(validation_copy)
        if set(exclusions) != used_exclusions:
            raise SourceError('Unused exclusions: source selection or history changed')
        if strict:
            # Detect a journal update after an earlier per-thread check too.
            if boundaries != [history_boundary.check(conn, root, tid) for tid in thread_ids]:
                raise SourceChangedError('Source journals changed during export')
            for obj in transfer['projects'] + transfer['sections']:
                history_filter.check_text(obj['name'])
            for obj in transfer['threads']:
                history_filter.check_text(obj['title'])
                if obj['name'] is not None:
                    history_filter.check_text(obj['name'])
        return {"format": EXPORT_FORMAT, "format_version": 1, "read_only": True,
                "synthetic_only": False, "codex_version": schema_version,
                "source_snapshot": {"inventory_token": report["inventory_token"],
                                    "schema_fingerprint": _schema(schema_version)[1],
                                    "selected_thread_ids": list(thread_ids),
                                    "consistency": "read-transactions-plus-observed-no-commits"},
                "status": "partial" if blocked_turns else "complete",
                "blocked_turns": blocked_turns,
                "excluded_child_thread_ids": sorted(set(excluded_children)),
                "filtering": filtering,
                "journal_boundaries": boundaries,
                "publication_ready": False,
                "coverage": "all selected projected rows; journal boundaries verified for current independent threads, semantic projection equivalence not asserted",
                "transfer": transfer}
