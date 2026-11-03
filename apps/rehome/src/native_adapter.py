"""Experimental, synthetic-only Codex 0.162.1 selective history adapter.

This is NOT an installer or production migration utility.  It never reads auth,
copies a whole user database, starts a model, or writes an existing profile.
Only explicit user and assistant text survives the portable representation.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

CODEX_VERSION = "0.162.1"
FORMAT = "agent-workspace-synthetic-history"
FORMAT_VERSION = 1
MARKER = ".agent-workspace-synthetic.json"
TERMINAL_TURN_STATUSES = frozenset(("completed", "failed", "interrupted"))

STATE_SQL = {
    "projects": """CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}', position INTEGER NOT NULL,
        created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)""",
    "project_roots": """CREATE TABLE project_roots (project_id TEXT NOT NULL
        REFERENCES projects(id) ON DELETE CASCADE, position INTEGER NOT NULL,
        path TEXT NOT NULL, PRIMARY KEY(project_id,position))""",
    "thread_sections": """CREATE TABLE thread_sections (id TEXT PRIMARY KEY,
        name TEXT NOT NULL, appearance TEXT)""",
    "thread_spawn_edges": """CREATE TABLE thread_spawn_edges
        (parent_thread_id TEXT NOT NULL, child_thread_id TEXT NOT NULL PRIMARY KEY,
        status TEXT NOT NULL)""",
    "threads": """CREATE TABLE threads (id TEXT PRIMARY KEY,
        rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL, source TEXT NOT NULL,
        model_provider TEXT NOT NULL, cwd TEXT NOT NULL, title TEXT NOT NULL,
        sandbox_policy TEXT NOT NULL, approval_mode TEXT NOT NULL,
        tokens_used INTEGER NOT NULL DEFAULT 0, has_user_event INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0, archived_at INTEGER,
        git_sha TEXT, git_branch TEXT, git_origin_url TEXT,
        cli_version TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '',
        agent_nickname TEXT, agent_role TEXT, memory_mode TEXT NOT NULL DEFAULT 'enabled',
        model TEXT, reasoning_effort TEXT, agent_path TEXT,
        created_at_ms INTEGER, updated_at_ms INTEGER, thread_source TEXT,
        preview TEXT NOT NULL DEFAULT '', recency_at INTEGER NOT NULL DEFAULT 0,
        recency_at_ms INTEGER NOT NULL DEFAULT 0, history_mode TEXT NOT NULL DEFAULT 'legacy',
        name TEXT, is_pinned INTEGER NOT NULL DEFAULT 0,
        thread_section_id TEXT REFERENCES thread_sections(id) ON DELETE SET NULL,
        section_position INTEGER, section_entered_at_ms INTEGER,
        project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
        originator TEXT, daybreak_enabled BOOLEAN, creator_user_id TEXT,
        creator_account_id TEXT)""",
}
HISTORY_SQL = {
    "thread_history_projection_state": """CREATE TABLE thread_history_projection_state
        (thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER NOT NULL,
        next_rollout_ordinal INTEGER NOT NULL)""",
    "thread_items": """CREATE TABLE thread_items (thread_id TEXT NOT NULL,
        turn_id TEXT NOT NULL, item_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL,
        created_at_ms INTEGER NOT NULL, item_json TEXT NOT NULL,
        item_type TEXT NOT NULL DEFAULT '', updated_at_ordinal INTEGER NOT NULL DEFAULT 0,
        started_at_ms INTEGER, completed_at_ms INTEGER,
        PRIMARY KEY(thread_id,turn_id,item_id))""",
    "thread_realtime_items": """CREATE TABLE thread_realtime_items
        (thread_id TEXT NOT NULL, item_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL,
        created_at_ms INTEGER NOT NULL, item_type TEXT NOT NULL, item_json TEXT NOT NULL,
        PRIMARY KEY(thread_id,item_id))""",
    "thread_turns": """CREATE TABLE thread_turns (thread_id TEXT NOT NULL,
        turn_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL, status TEXT NOT NULL,
        error_json TEXT, started_at INTEGER, completed_at INTEGER, duration_ms INTEGER,
        first_user_item_id TEXT, final_agent_item_id TEXT, rollout_byte_offset INTEGER,
        rollout_end_ordinal INTEGER, rollout_end_byte_offset INTEGER, root_turn_id TEXT,
        PRIMARY KEY(thread_id,turn_id))""",
}


class AdapterError(ValueError):
    pass


def _json(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _root(path, *, exists=True):
    p = Path(path).absolute()
    # Fail closed on any .codex ancestor, including a fake marker in production.
    if any(part.lower() == ".codex" for part in p.parts):
        raise AdapterError("Production .codex paths are prohibited")
    for ancestor in (p, *p.parents):
        if ancestor.exists() and (ancestor.is_symlink() or
            (hasattr(ancestor, "is_junction") and ancestor.is_junction())):
            raise AdapterError("Symlink/junction profiles are prohibited")
    if exists and not p.is_dir():
        raise AdapterError("Synthetic profile does not exist")
    return p


def _marker(root):
    p = _root(root)
    try:
        mark = json.loads((p / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdapterError("Explicit synthetic-only profile marker required") from exc
    if mark.get("kind") != "synthetic-only" or mark.get("schema_version") != 1:
        raise AdapterError("Invalid synthetic-only profile marker")
    if mark.get("codex_version", CODEX_VERSION) != CODEX_VERSION:
        raise AdapterError("Codex version mismatch")
    if (p / "auth.json").exists():
        raise AdapterError("Profiles containing authorization are prohibited")
    return p


def mark_synthetic_profile(root):
    """Mark an explicitly isolated, auth-free native bootstrap profile."""
    p = _root(root)
    if (p / "auth.json").exists():
        raise AdapterError("Do not mark profiles containing authorization")
    old = p / MARKER
    if old.exists():
        _marker(p)
    old.write_text(_json({"kind": "synthetic-only", "schema_version": 1,
                          "codex_version": CODEX_VERSION}), encoding="utf-8")
    return p


@contextmanager
def _connect(path, *, readonly=False):
    if path.is_symlink():
        raise AdapterError("Database symlinks are prohibited")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) if readonly else sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _expected_columns(sql):
    with closing(sqlite3.connect(":memory:")) as conn:
        for statement in sql.values():
            conn.execute(statement)
        return {table: [(r[1], r[2], r[3], r[5]) for r in conn.execute(f'PRAGMA table_info("{table}")')]
                for table in sql}


EXPECTED_STATE = _expected_columns(STATE_SQL)
EXPECTED_HISTORY = _expected_columns(HISTORY_SQL)


def _schema_gate(conn, expected):
    actual = {}
    for table, columns in expected.items():
        got = [(r[1], r[2], r[3], r[5]) for r in conn.execute(f'PRAGMA table_info("{table}")')]
        if got != columns:
            raise AdapterError(f"Unsupported Codex {CODEX_VERSION} schema: {table}")
        actual[table] = got
    return actual


def _fingerprint():
    return hashlib.sha256(_json({"state": EXPECTED_STATE,
                                "history": EXPECTED_HISTORY}).encode()).hexdigest()


SCHEMA_FINGERPRINT = _fingerprint()

_HISTORY_CLEANUP_TRIGGER = """CREATE TRIGGER thread_realtime_items_projection_cleanup
    AFTER DELETE ON thread_history_projection_state
BEGIN
    DELETE FROM thread_realtime_items WHERE thread_id = OLD.thread_id;
END"""

_APPROVED_TRIGGERS = {"thread_realtime_items_projection_cleanup": _HISTORY_CLEANUP_TRIGGER}
for _field in ("created_at", "updated_at"):
    _name = f"threads_{_field}_ms_after_insert"
    _APPROVED_TRIGGERS[_name] = f"""CREATE TRIGGER {_name}
AFTER INSERT ON threads
WHEN NEW.{_field}_ms IS NULL
BEGIN
    UPDATE threads
    SET {_field}_ms = NEW.{_field} * 1000
    WHERE id = NEW.id;
END"""
    _name = f"threads_{_field}_ms_after_update"
    _APPROVED_TRIGGERS[_name] = f"""CREATE TRIGGER {_name}
AFTER UPDATE OF {_field} ON threads
WHEN NEW.{_field} != OLD.{_field}
 AND NEW.{_field}_ms IS OLD.{_field}_ms
BEGIN
    UPDATE threads
    SET {_field}_ms = NEW.{_field} * 1000
    WHERE id = NEW.id;
END"""
_APPROVED_TRIGGERS["threads_recency_at_after_insert"] = """CREATE TRIGGER threads_recency_at_after_insert
AFTER INSERT ON threads
WHEN NEW.recency_at_ms = 0
BEGIN
    UPDATE threads
    SET recency_at = NEW.updated_at,
        recency_at_ms = COALESCE(NEW.updated_at_ms, NEW.updated_at * 1000)
    WHERE id = NEW.id;
END"""


def _schema_object_allowed(row):
    if row["type"] in ("table", "index"):
        return True
    normalize = lambda text: " ".join(text.split()).casefold()
    return (row["type"] == "trigger" and row["name"] in _APPROVED_TRIGGERS
            and normalize(row["sql"]) == normalize(_APPROVED_TRIGGERS[row["name"]]))


def _bootstrap(root):
    root.mkdir()
    mark_synthetic_profile(root)
    for name, statements in (("state_5.sqlite", STATE_SQL),
                             ("thread_history_1.sqlite", HISTORY_SQL)):
        with _connect(root / name) as conn:
            for statement in statements.values():
                conn.execute(statement)


def _clone_schema(template, root, *, synthetic_marker=True, expected_state=None):
    """Copy schema and migration metadata only from isolated native bootstrap."""
    source = _marker(template)
    root.mkdir()
    if synthetic_marker:
        mark_synthetic_profile(root)
    for filename, expected in (("state_5.sqlite", EXPECTED_STATE if expected_state is None else expected_state),
                                ("thread_history_1.sqlite", EXPECTED_HISTORY)):
        with _connect(source / filename, readonly=True) as src:
            _schema_gate(src, expected)
            if filename.startswith("state"):
                if src.execute("SELECT COUNT(*) FROM threads WHERE has_user_event<>0").fetchone()[0]:
                    raise AdapterError("Schema template contains user threads")
                if src.execute("SELECT COUNT(*) FROM projects").fetchone()[0]:
                    raise AdapterError("Schema template contains projects")
            else:
                if any(src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                       for t in ("thread_items", "thread_turns", "thread_realtime_items")):
                    raise AdapterError("Schema template contains history")
            rows = src.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                               "AND name NOT LIKE 'sqlite_%' ORDER BY CASE type "
                               "WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name").fetchall()
            with _connect(root / filename) as dst:
                for row in rows:
                    if not _schema_object_allowed(row):
                        raise AdapterError("Unexpected native schema object")
                    dst.execute(row["sql"])
                if any(row["name"] == "_sqlx_migrations" for row in rows):
                    migrations = src.execute("SELECT * FROM _sqlx_migrations").fetchall()
                    if migrations:
                        placeholders = ",".join("?" for _ in migrations[0])
                        dst.executemany(f"INSERT INTO _sqlx_migrations VALUES({placeholders})", migrations)


def seed_history_schema_from_reference(template_root, reference_db):
    """Read ONLY DDL and SQLx migration metadata, never reference history rows.

    An explicit reference may be the installed history database. Its contents
    are not exported: only sqlite_master and _sqlx_migrations are queried.
    This helper creates a new, empty synthetic history schema for native tests.
    """
    target = _marker(template_root) / "thread_history_1.sqlite"
    if target.exists():
        raise AdapterError("Synthetic history schema already exists")
    reference = Path(reference_db).absolute()
    if reference.is_symlink():
        raise AdapterError("Schema reference symlink prohibited")
    temp = target.with_name(".history-schema-" + uuid.uuid4().hex + ".sqlite")
    try:
        with _connect(reference, readonly=True) as src:
            _schema_gate(src, EXPECTED_HISTORY)
            schema = src.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY CASE type "
                                 "WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name").fetchall()
            with _connect(temp) as dst:
                for row in schema:
                    if not _schema_object_allowed(row):
                        raise AdapterError("Unexpected history schema object")
                    dst.execute(row["sql"])
                if any(row["name"] == "_sqlx_migrations" for row in schema):
                    migrations = src.execute("SELECT * FROM _sqlx_migrations").fetchall()
                    if migrations:
                        placeholders = ",".join("?" for _ in migrations[0])
                        dst.executemany(f"INSERT INTO _sqlx_migrations VALUES({placeholders})", migrations)
        if target.exists():
            raise AdapterError("History schema destination appeared")
        temp.rename(target)
    finally:
        if temp.exists():
            temp.unlink()
    return target


def _safe_visible(item):
    """Whitelist reconstruction, never a delete-known-bad-fields filter."""
    kind = item.get("type")
    ident = item.get("id")
    if not isinstance(ident, str) or not ident:
        raise AdapterError("Visible item lacks an ID")
    if kind == "userMessage":
        texts, omitted = [], 0
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            else:
                omitted += 1
        return {"id": ident, "role": "user", "text": "\n".join(texts),
                "omitted_attachments": omitted}
    if kind == "agentMessage":
        if not isinstance(item.get("text"), str):
            raise AdapterError("Assistant message lacks text")
        phase = item.get("phase")
        if phase not in (None, "commentary", "final_answer"):
            raise AdapterError("Unknown assistant message phase")
        return {"id": ident, "role": "assistant", "text": item["text"], "phase": phase}
    return None


def export_profile(profile_root, thread_ids=None):
    """Export selected synthetic threads, children, projects, and visible text."""
    root = _marker(profile_root)
    with _connect(root / "state_5.sqlite", readonly=True) as state, \
         _connect(root / "thread_history_1.sqlite", readonly=True) as hist:
        _schema_gate(state, EXPECTED_STATE)
        _schema_gate(hist, EXPECTED_HISTORY)
        rows = {r["id"]: dict(r) for r in state.execute("SELECT * FROM threads")}
        selected = set(rows) if thread_ids is None else set(thread_ids)
        if not selected or not selected <= rows.keys():
            raise AdapterError("No threads selected or unknown thread ID")
        edges = [dict(r) for r in state.execute("SELECT * FROM thread_spawn_edges")]
        while True:
            expanded = selected | {r["child_thread_id"] for r in edges
                                   if r["parent_thread_id"] in selected}
            if expanded == selected:
                break
            if not expanded <= rows.keys():
                raise AdapterError("Missing child thread")
            selected = expanded
        global_path = root / ".codex-global-state.json"
        glob = json.loads(global_path.read_text(encoding="utf-8")) if global_path.exists() else {}
        assignments = glob.get("thread-project-assignments", {})
        projects = {r["id"]: dict(r) for r in state.execute("SELECT * FROM projects")}
        sections = {r["id"]: dict(r) for r in state.execute("SELECT * FROM thread_sections")}
        threads, used_projects, used_sections, omitted = [], set(), set(), 0
        for tid in sorted(selected):
            row = rows[tid]
            assignment = assignments.get(tid)
            if isinstance(assignment, dict):
                if assignment.get("projectKind") != "local":
                    raise AdapterError("Only local project assignments can be restored")
                assignment = assignment.get("projectId")
            pid = row["project_id"] or assignment
            if pid is not None and pid not in projects:
                raise AdapterError("Project membership needs an explicit legacy-ID mapping")
            sid = row["thread_section_id"]
            if pid:
                used_projects.add(pid)
            if sid:
                if sid not in sections:
                    raise AdapterError("Missing thread section")
                used_sections.add(sid)
            turns = []
            for turn in hist.execute("SELECT * FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal", (tid,)):
                if turn["status"] not in TERMINAL_TURN_STATUSES:
                    raise AdapterError("Only terminal synthetic turns may be exported")
                messages = []
                for record in hist.execute("SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=? ORDER BY rollout_ordinal", (tid, turn["turn_id"])):
                    visible = _safe_visible(json.loads(record[0]))
                    if visible is None:
                        omitted += 1
                    else:
                        messages.append(visible)
                turns.append({"id": turn["turn_id"], "status": turn["status"], "messages": messages})
            threads.append({"id": tid, "title": row["title"], "name": row["name"],
                            "project_id": pid, "section_id": sid,
                            "is_pinned": bool(row["is_pinned"]),
                            "archived": bool(row["archived"]), "turns": turns})
        result = {"format": FORMAT, "format_version": FORMAT_VERSION,
                  "codex_version": CODEX_VERSION, "schema_fingerprint": SCHEMA_FINGERPRINT,
                  "synthetic_only": True,
                  "projects": [{"id": pid, "name": projects[pid]["name"]}
                               for pid in sorted(used_projects)],
                  "sections": [{"id": sid, "name": sections[sid]["name"]}
                               for sid in sorted(used_sections)],
                  "threads": threads,
                  "spawn_edges": [{"parent": e["parent_thread_id"], "child": e["child_thread_id"]}
                                  for e in edges if e["parent_thread_id"] in selected and e["child_thread_id"] in selected],
                  "omitted_nonmessage_items": omitted}
        _validate_bundle(result)
        return result


def _exact(obj, keys, label):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise AdapterError(f"Unknown or missing fields: {label}")


def _ident(value):
    if (not isinstance(value, str) or not value or len(value) > 256 or
        value in (".", "..") or value.endswith((".", " ")) or
        re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or
        re.match(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", value, re.IGNORECASE)):
        raise AdapterError("Invalid identifier")


def _thread_ident(value):
    _ident(value)
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise AdapterError("Thread ID must be a canonical UUID") from exc
    if canonical != value:
        raise AdapterError("Thread ID must be a canonical UUID")


def _validate_bundle(bundle):
    return _validate_transfer(bundle, synthetic_only=True)


def _validate_transfer(bundle, *, synthetic_only):
    """Shared structure; callers must explicitly select their provenance gate."""
    if type(synthetic_only) is not bool:
        raise AdapterError('Explicit transfer provenance required')
    _exact(bundle, {"format", "format_version", "codex_version", "schema_fingerprint", "synthetic_only", "projects", "sections", "threads", "spawn_edges", "omitted_nonmessage_items"}, "bundle")
    if (bundle["format"] != FORMAT or bundle["format_version"] != FORMAT_VERSION or
        bundle["codex_version"] != CODEX_VERSION or bundle["schema_fingerprint"] != SCHEMA_FINGERPRINT or
        bundle["synthetic_only"] is not synthetic_only):
        raise AdapterError("Version/schema/synthetic gate rejected bundle")
    for key in ("projects", "sections", "threads", "spawn_edges"):
        if not isinstance(bundle[key], list):
            raise AdapterError("Expected list: " + key)
    identities = {}
    for key in ("projects", "sections"):
        ids = set()
        for obj in bundle[key]:
            _exact(obj, {"id", "name"}, key)
            _ident(obj["id"])
            if obj["id"] in ids or not isinstance(obj["name"], str):
                raise AdapterError("Duplicate ID or invalid name")
            ids.add(obj["id"])
        identities[key] = ids
    tids, path_ids = set(), set()
    for thread in bundle["threads"]:
        _exact(thread, {"id", "title", "name", "project_id", "section_id", "is_pinned", "archived", "turns"}, "thread")
        _thread_ident(thread["id"])
        if thread["id"] in tids or thread["id"].casefold() in path_ids:
            raise AdapterError("Thread ID collision")
        tids.add(thread["id"])
        path_ids.add(thread["id"].casefold())
        if not isinstance(thread["title"], str) or not (thread["name"] is None or isinstance(thread["name"], str)):
            raise AdapterError("Invalid thread title/name")
        if type(thread["archived"]) is not bool or type(thread["is_pinned"]) is not bool:
            raise AdapterError("Invalid thread flags")
        for field, collection in (("project_id", "projects"), ("section_id", "sections")):
            if thread[field] is not None and thread[field] not in identities[collection]:
                raise AdapterError("Unresolved thread membership")
        turnids, itemids = set(), set()
        if not isinstance(thread["turns"], list):
            raise AdapterError("Invalid turn list")
        for turn in thread["turns"]:
            fields = {"id", "messages", "status"} if "status" in turn else {"id", "messages"}
            _exact(turn, fields, "turn")
            if turn.get("status", "completed") not in TERMINAL_TURN_STATUSES:
                raise AdapterError("Only terminal turn statuses may be imported")
            _ident(turn["id"])
            if turn["id"] in turnids:
                raise AdapterError("Turn ID collision")
            turnids.add(turn["id"])
            if not isinstance(turn["messages"], list):
                raise AdapterError("Invalid message list")
            for item in turn["messages"]:
                if not isinstance(item, dict):
                    raise AdapterError("Invalid message")
                role = item.get("role")
                fields = {"id", "role", "text", "omitted_attachments"} if role == "user" else {"id", "role", "text", "phase"}
                _exact(item, fields, "message")
                _ident(item["id"])
                if item["id"] in itemids:
                    raise AdapterError("Item ID collision")
                itemids.add(item["id"])
                if role not in ("user", "assistant") or not isinstance(item["text"], str):
                    raise AdapterError("Invalid visible message")
                if role == "assistant" and item["phase"] not in (None, "commentary", "final_answer"):
                    raise AdapterError("Invalid assistant phase")
                if role == "user" and (type(item["omitted_attachments"]) is not int or item["omitted_attachments"] < 0):
                    raise AdapterError("Invalid omitted-attachment count")
    if not tids:
        raise AdapterError("Empty bundle")
    children = set()
    for edge in bundle["spawn_edges"]:
        _exact(edge, {"parent", "child"}, "spawn edge")
        if edge["parent"] not in tids or edge["child"] not in tids or edge["parent"] == edge["child"] or edge["child"] in children:
            raise AdapterError("Invalid spawn edge")
        children.add(edge["child"])
    if type(bundle["omitted_nonmessage_items"]) is not int or bundle["omitted_nonmessage_items"] < 0:
        raise AdapterError("Invalid omitted item count")


def _destination_roots(bundle, project_roots):
    result = {}
    needed = {p["id"] for p in bundle["projects"]}
    if any(t["project_id"] is None for t in bundle["threads"]):
        needed.add("__projectless__")
    for pid in needed:
        value = project_roots.get(pid)
        values = [value] if isinstance(value, (str, os.PathLike)) else value
        if not isinstance(values, list) or not values:
            raise AdapterError("Destination project root missing: " + pid)
        roots = []
        for raw in values:
            path = Path(raw)
            if not path.is_absolute():
                raise AdapterError("Destination roots must be absolute")
            _root(path, exists=False)
            roots.append(str(path))
        result[pid] = roots
    return result


def _native_item(message, *, append_attachment_notice=True):
    if message["role"] == "user":
        text = message["text"]
        if message["omitted_attachments"] and append_attachment_notice:
            text += "\n[Source attachment omitted from transfer; request the original if needed.]"
        return {"type": "userMessage", "id": message["id"], "clientId": None,
                "content": [{"type": "text", "text": text, "text_elements": []}]}
    return {"type": "agentMessage", "id": message["id"], "text": message["text"],
            "phase": message["phase"], "memoryCitation": None, "delivery": None,
            "questions": None}


def import_profile(bundle, destination_root, project_roots, *, schema_template=None):
    """Build a NEW synthetic profile; database writes are one ATTACH transaction.

    Existing destinations are always rejected, even when IDs do not collide.
    The whole staged directory is published only after database/file checks.
    A native-empty schema_template preserves actual SQLx migration metadata;
    without it the static test schema is not a claimed native profile.
    """
    _validate_bundle(bundle)
    return _write_profile(bundle, destination_root, project_roots, schema_template=schema_template,
                          synthetic_only=True, runtime_version=CODEX_VERSION)


def _write_profile(bundle, destination_root, project_roots, *, schema_template,
                   synthetic_only, runtime_version,display_metadata=None):
    """Internal writer, used after the public caller's contract/target checks."""
    _validate_transfer(bundle, synthetic_only=synthetic_only)
    if display_metadata is not None:
        if synthetic_only:raise AdapterError('Display metadata is confined to backup imports')
        import rehome_display_metadata
        rehome_display_metadata.validate(bundle,display_metadata)
    if not synthetic_only and schema_template is None:
        raise AdapterError('Native backup staging requires an explicit schema template')
    expected_state = EXPECTED_STATE
    if not synthetic_only:
        # Current-runtime schema is deliberately confined to the backup writer.
        # Public synthetic wrappers retain their original strict schema gate.
        import readonly_source
        if runtime_version != readonly_source.CURRENT_SCHEMA_VERSION:
            raise AdapterError('Unsupported native backup runtime')
        expected_state, _ = readonly_source._schema(runtime_version)
    roots = _destination_roots(bundle, project_roots)
    dest = _root(destination_root, exists=False)
    if dest.exists():
        raise AdapterError("Destination exists; merge/overwrite is prohibited")
    if not dest.parent.is_dir():
        raise AdapterError("Destination parent must exist")
    prefix = '.synthetic-import-' if synthetic_only else '.backup-import-'
    originator = 'synthetic-transfer' if synthetic_only else 'rehome-backup-staging'
    stage = dest.parent / (prefix + uuid.uuid4().hex)
    try:
        _clone_schema(schema_template, stage, synthetic_marker=synthetic_only,
                      expected_state=expected_state) if schema_template is not None else _bootstrap(stage)
        with _connect(stage / "state_5.sqlite") as conn:
            _schema_gate(conn, expected_state)
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("ATTACH DATABASE ? AS history", (str(stage / "thread_history_1.sqlite"),))
            conn.execute("PRAGMA history.journal_mode=DELETE")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for pos, project in enumerate(bundle["projects"]):
                    conn.execute("INSERT INTO projects VALUES(?,?,?,?,?,?)",
                                 (project["id"], project["name"], "{}", pos, 0, 0))
                    for i, path in enumerate(roots[project["id"]]):
                        conn.execute("INSERT INTO project_roots VALUES(?,?,?)", (project["id"], i, path))
                for section in bundle["sections"]:
                    conn.execute("INSERT INTO thread_sections VALUES(?,?,?)", (section["id"], section["name"], None))
                for position, thread in enumerate(bundle["threads"]):
                    tid, pid = thread["id"], thread["project_id"]
                    cwd = roots[pid or "__projectless__"][0]
                    rollout_rel = Path("archived_sessions" if thread["archived"] else "sessions") / ("rollout-1970-01-01T00-00-00-" + tid + ".jsonl")
                    rollout_final = dest / rollout_rel
                    first = next((m["text"] for t in thread["turns"] for m in t["messages"] if m["role"] == "user"), "")
                    columns = ["id", "rollout_path", "created_at", "updated_at", "source", "model_provider", "cwd", "title", "sandbox_policy", "approval_mode", "has_user_event", "archived", "cli_version", "first_user_message", "history_mode", "name", "is_pinned", "thread_section_id", "section_position", "project_id", "preview", "originator"]
                    values = [tid, str(rollout_final), 0, 0, "cli", "openai", cwd, thread["title"],
                              _json({"type": "read-only"}), "untrusted", int(bool(first)), int(thread["archived"]),
                              runtime_version, first, "paginated", thread["name"], int(thread["is_pinned"]),
                              thread["section_id"], position, pid, first[:300], originator]
                    conn.execute("INSERT INTO threads(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in values) + ")", values)
                    journal = stage / rollout_rel
                    journal.parent.mkdir(exist_ok=True)
                    header = {"timestamp": "1970-01-01T00:00:00Z", "ordinal": 0, "type": "session_meta", "payload": {
                        "id": tid, "timestamp": "1970-01-01T00:00:00Z", "cwd": cwd,
                        "originator": originator, "cli_version": runtime_version,
                        "source": "cli", "model_provider": "openai", "history_mode": "paginated"}}
                    journal_records = [header]
                    ordinal = 1
                    for turn in thread["turns"]:
                        start = ordinal
                        first_user = next((m["id"] for m in turn["messages"] if m["role"] == "user"), None)
                        final_agent = next((m["id"] for m in reversed(turn["messages"]) if m["role"] == "assistant" and m["phase"] == "final_answer"), None)
                        for message in turn["messages"]:
                            native = _native_item(message, append_attachment_notice=synthetic_only)
                            prompt_item = {"type": "message", "id": message["id"],
                                           "role": message["role"], "content": [{
                                               "type": "input_text" if message["role"] == "user" else "output_text",
                                               "text": native["content"][0]["text"] if message["role"] == "user" else native["text"]}]}
                            if message["role"] == "assistant":
                                prompt_item["phase"] = message["phase"]
                            journal_records.append({"timestamp": "1970-01-01T00:00:00Z",
                                                    "ordinal": ordinal, "type": "response_item", "payload": prompt_item})
                            conn.execute("INSERT INTO history.thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal) VALUES(?,?,?,?,?,?,?,?)",
                                         (tid, turn["id"], message["id"], ordinal, 0, _json(native), native["type"], ordinal))
                            ordinal += 1
                        if not turn["messages"]:
                            ordinal += 1
                        conn.execute("INSERT INTO history.thread_turns(thread_id,turn_id,rollout_ordinal,status,started_at,completed_at,duration_ms,first_user_item_id,final_agent_item_id,rollout_end_ordinal,root_turn_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                     (tid, turn["id"], start, turn.get("status", "completed"), 0, 0, 0, first_user, final_agent, ordinal - 1, turn["id"]))
                    journal.write_text("".join(_json(record) + "\n" for record in journal_records), encoding="utf-8")
                    conn.execute("INSERT INTO history.thread_history_projection_state VALUES(?,?,?)", (tid, journal.stat().st_size, ordinal))
                for edge in bundle["spawn_edges"]:
                    conn.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)", (edge["parent"], edge["child"], "completed"))
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise AdapterError("Foreign-key validation failed")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        local_host_key = "local:" + str(dest)
        global_state = {
            "local-projects": {p["id"]: {"id": p["id"], "name": p["name"],
                "rootPaths": roots[p["id"]], "createdAt": 0, "updatedAt": 0} for p in bundle["projects"]},
            "project-order": [p["id"] for p in bundle["projects"]],
            "thread-project-assignments": {
                t["id"]: {"projectKind": "local", "projectId": t["project_id"]}
                for t in bundle["threads"] if t["project_id"]},
            "thread-projectless-output-directories": {
                t["id"]: roots["__projectless__"][0]
                for t in bundle["threads"] if t["project_id"] is None},
            "thread-workspace-root-hints": {
                t["id"]: roots[t["project_id"] or "__projectless__"][0]
                for t in bundle["threads"]},
            "thread-project-membership-host-ids": {t["id"]: "local" for t in bundle["threads"]},
            "app-server-project-id-by-legacy-project-id-by-host": {
                local_host_key: {p["id"]: p["id"] for p in bundle["projects"]}},
            "app-server-projects-migration-by-host": {
                local_host_key: {"version": 1, "projectsMigrated": True,
                                 "threadAssignmentsMigrated": True,
                                 "pendingThreadAssignmentIds": []}},
        }
        (stage / ".codex-global-state.json").write_text(_json(global_state), encoding="utf-8")
        (stage / "session_index.jsonl").write_text("".join(_json({"id": t["id"], "thread_name": t["name"] or t["title"], "updated_at": "1970-01-01T00:00:00Z"}) + "\n" for t in bundle["threads"]), encoding="utf-8")
        report = {"synthetic_only": synthetic_only, "codex_version": runtime_version,
                  "projects": len(bundle["projects"]), "threads": len(bundle["threads"]),
                  "native_schema_template": schema_template is not None,
                  "destination": str(dest), "schema_fingerprint": SCHEMA_FINGERPRINT}
        (stage / "import-report.json").write_text(_json(report), encoding="utf-8")
        if display_metadata is not None:
            rehome_display_metadata.apply_to_profile(stage,dest,bundle,display_metadata)
        # Another actor must not create the target after the initial check.
        if dest.exists():
            raise AdapterError("Destination appeared during import")
        stage.rename(dest)
        return report
    finally:
        if stage.exists():
            # This exact, freshly generated staging path is owned by this call.
            if stage.parent != dest.parent or not stage.name.startswith(prefix):
                raise AdapterError("Unexpected staging path")
            shutil.rmtree(stage)


def create_synthetic_profile(profile_root, *, seed=True):
    """Unit-test fixture only.  Never a copy of the installed user's profile."""
    root = _root(profile_root, exists=False)
    if root.exists():
        raise AdapterError("Fixture destination already exists")
    _bootstrap(root)
    if not seed:
        return root
    pid = "00000000-0000-4000-8000-000000000001"
    tid = "00000000-0000-4000-8000-000000000002"
    turn = "00000000-0000-4000-8000-000000000003"
    with _connect(root / "state_5.sqlite") as conn:
        conn.execute("INSERT INTO projects VALUES(?,?,?,?,?,?)", (pid, "Synthetic project", "{}", 0, 0, 0))
        conn.execute("INSERT INTO project_roots VALUES(?,?,?)", (pid, 0, "C:/synthetic-source-only"))
        conn.execute("INSERT INTO threads(id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode,has_user_event,history_mode,project_id,cli_version,name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (tid, str(root / "synthetic.jsonl"), 0, 0, "cli", "openai", "C:/synthetic-source-only", "Synthetic chat", '{"type":"read-only"}', "untrusted", 1, "paginated", pid, CODEX_VERSION, "Synthetic chat"))
    with _connect(root / "thread_history_1.sqlite") as conn:
        items = [
            {"type": "userMessage", "id": "synthetic-user-1", "clientId": None, "content": [
                {"type": "text", "text": "Create a synthetic report.", "text_elements": []},
                {"type": "image", "url": "DO_NOT_TRANSFER_SOURCE_ATTACHMENT"}]},
            {"type": "reasoning", "id": "synthetic-hidden-1", "text": "DO_NOT_TRANSFER_REASONING"},
            {"type": "commandExecution", "id": "synthetic-tool-1", "output": "DO_NOT_TRANSFER_TOOL_OUTPUT"},
            {"type": "agentMessage", "id": "synthetic-agent-1", "text": "Synthetic report ready.", "phase": "final_answer", "memoryCitation": None, "delivery": None, "questions": None},
        ]
        for ordinal, item in enumerate(items, 1):
            conn.execute("INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal) VALUES(?,?,?,?,?,?,?,?)", (tid, turn, item["id"], ordinal, 0, _json(item), item["type"], ordinal))
        conn.execute("INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,status,first_user_item_id,final_agent_item_id,root_turn_id) VALUES(?,?,?,?,?,?,?)", (tid, turn, 1, "completed", "synthetic-user-1", "synthetic-agent-1", turn))
    return root
