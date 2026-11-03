import hashlib
import importlib
import json
from pathlib import Path
import shutil
import unittest
import uuid

import native_adapter as native
import rehome_backup_workflow as backup_workflow
import rehome_history_filter as filtering
import rehome_history_lineage as lineage
import rehome_journal as journal
import rehome_rollout_resolver as resolver
import rehome_review as review


def _lineage_fixture():
    # Load the shared fixture lazily so unittest does not rediscover its tests
    # from this module.
    return importlib.import_module("tests.test_history_lineage").LineageTests


class BackupHeadTests(unittest.TestCase):
    """Regression coverage for the explicit verbatim current-head backup route."""

    def setUp(self):
        _lineage_fixture().setUp(self)

    def tearDown(self):
        _lineage_fixture().tearDown(self)

    def path(self, tid):
        return _lineage_fixture().path(self, tid)

    def make_journal(self, tid, texts, dependency=None):
        return _lineage_fixture().make_journal(self, tid, texts, dependency)

    def change_header(self, tid, change):
        return _lineage_fixture().change_header(self, tid, change)

    def global_state(self):
        path = self.profile / ".codex-global-state.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    def physicalize_head(self, tid, physical_id=None):
        physical_id = physical_id or str(uuid.uuid4())
        old = self.path(tid)
        new = old.with_name(old.name.rsplit(tid, 1)[0] + physical_id + ".jsonl")
        old.rename(new)
        with native._connect(self.profile / "state_5.sqlite") as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(new), tid))
        with native._connect(self.profile / "thread_history_1.sqlite") as db:
            for table in ("thread_items", "thread_turns", "thread_history_projection_state"):
                db.execute("UPDATE " + table + " SET thread_id=? WHERE thread_id=?", (physical_id, tid))
        return physical_id, new

    def clone_history_key(self, source_id, target_id):
        with native._connect(self.profile / "thread_history_1.sqlite") as db:
            for table in ("thread_turns", "thread_items", "thread_history_projection_state"):
                rows = list(db.execute("SELECT * FROM " + table + " WHERE thread_id=?", (source_id,)))
                if not rows:
                    continue
                columns = [r[1] for r in db.execute("PRAGMA table_info(" + table + ")")]
                placeholders = ",".join("?" for _ in columns)
                for row in rows:
                    db.execute("INSERT INTO " + table + " (" + ",".join(columns) + ") VALUES(" + placeholders + ")",
                               (target_id, *tuple(row)[1:]))

    def watched_source_digest(self):
        paths = [self.profile / "state_5.sqlite", self.profile / "thread_history_1.sqlite",
                 self.profile / ".codex-global-state.json"]
        paths.extend(self.profile.rglob("rollout-*.jsonl"))
        return {str(path.relative_to(self.profile)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in paths if path.is_file()}

    def test_current_physical_head_preserves_logical_transfer_id_and_backup_evidence(self):
        physical_id, head_path = self.physicalize_head(self.parent)
        before = self.watched_source_digest()

        result = lineage.export_candidate(
            self.profile, [self.parent], text_policy=filtering.VERBATIM_TEXT,
            resolve_current_storage=True)
        review.validate_backup_history(result)

        self.assertEqual("rehome-backup-history/1", result["format"])
        self.assertEqual(filtering.VERBATIM_TEXT, result["text_policy"])
        self.assertEqual([self.parent], result["selected_thread_ids"])
        self.assertEqual(self.parent, result["transfer"]["threads"][0]["id"])
        binding = result["lineage"][self.parent][0]["storage_binding"]
        self.assertEqual(physical_id, binding["physical_rollout_id"])
        self.assertEqual(self.parent, binding["logical_session_id"])
        self.assertTrue(head_path.is_file())
        texts = [message["text"] for turn in result["transfer"]["threads"][0]["turns"]
                 for message in turn["messages"]]
        self.assertIn("PARENT_INHERITED", texts)
        self.assertIn("PARENT_LATER_EXCLUDED", texts)
        self.assertEqual(before, self.watched_source_digest())

    def test_current_head_requires_resolvable_ui_project_assignment(self):
        self.physicalize_head(self.parent)
        global_path, glob = self.global_state()
        glob.setdefault("thread-project-assignments", {})[self.parent] = {
            "projectKind": "local", "projectId": "missing-legacy-project"}
        with native._connect(self.profile / "state_5.sqlite") as db:
            db.execute("UPDATE threads SET project_id=NULL WHERE id=?", (self.parent,))
        global_path.write_text(json.dumps(glob), encoding="utf-8")

        with self.assertRaisesRegex(lineage.source.SourceError, "Legacy project metadata missing"):
            lineage.export_candidate(self.profile, [self.parent], text_policy=filtering.VERBATIM_TEXT,
                                     resolve_current_storage=True)

    def test_current_head_rejects_conflicting_sql_and_ui_project_assignments(self):
        self.physicalize_head(self.parent)
        global_path, glob = self.global_state()
        other_project = str(uuid.uuid4())
        with native._connect(self.profile / "state_5.sqlite") as db:
            sql_project = db.execute("SELECT project_id FROM threads WHERE id=?", (self.parent,)).fetchone()[0]
            db.execute("INSERT INTO projects VALUES(?,?,?,?,?,?)",
                       (other_project, "Other synthetic project", "{}", 1, 0, 0))
        glob.setdefault("thread-project-assignments", {})[self.parent] = {
            "projectKind": "local", "projectId": other_project}
        global_path.write_text(json.dumps(glob), encoding="utf-8")
        self.assertNotEqual(sql_project, other_project)

        with self.assertRaisesRegex(lineage.source.SourceError, "SQL/UI project assignment conflict"):
            lineage.export_candidate(self.profile, [self.parent], text_policy=filtering.VERBATIM_TEXT,
                                     resolve_current_storage=True)

    def test_missing_physical_head_is_rejected(self):
        _, head_path = self.physicalize_head(self.parent)
        head_path.unlink()

        with self.assertRaisesRegex(journal.JournalError, "Journal is missing"):
            lineage.export_candidate(self.profile, [self.parent], text_policy=filtering.VERBATIM_TEXT,
                                     resolve_current_storage=True)

    def test_duplicate_physical_head_is_rejected(self):
        physical_id, head_path = self.physicalize_head(self.parent)
        archive = self.profile / "archived_sessions"
        archive.mkdir(exist_ok=True)
        duplicate = archive / head_path.name
        shutil.copyfile(head_path, duplicate)

        with self.assertRaises(resolver.RolloutResolutionError) as caught:
            lineage.export_candidate(self.profile, [self.parent], text_policy=filtering.VERBATIM_TEXT,
                                     resolve_current_storage=True)
        self.assertEqual("resolver_ambiguous_physical_rollout", caught.exception.code)
        self.assertTrue(physical_id)

    def test_wrong_physical_head_owner_is_rejected(self):
        _, head_path = self.physicalize_head(self.parent)
        records = [json.loads(line) for line in head_path.read_text(encoding="utf-8").splitlines()]
        records[0]["payload"]["id"] = str(uuid.uuid4())
        data = "".join(json.dumps(row) + "\n" for row in records).encode("utf-8")
        head_path.write_bytes(data)
        with self.assertRaises(resolver.RolloutResolutionError) as caught:
            lineage.export_candidate(self.profile, [self.parent], text_policy=filtering.VERBATIM_TEXT,
                                     resolve_current_storage=True)
        self.assertEqual("resolver_logical_owner_missing", caught.exception.code)

    def test_missing_physical_projection_is_rejected(self):
        physical_id, _ = self.physicalize_head(self.parent)
        wrong_id = str(uuid.uuid4())
        with native._connect(self.profile / "thread_history_1.sqlite") as db:
            for table in ("thread_items", "thread_turns", "thread_history_projection_state"):
                db.execute("UPDATE " + table + " SET thread_id=? WHERE thread_id=?", (wrong_id, physical_id))

        with self.assertRaises(resolver.RolloutResolutionError) as caught:
            lineage.export_candidate(self.profile, [self.parent], text_policy=filtering.VERBATIM_TEXT,
                                     resolve_current_storage=True)
        self.assertEqual("resolver_projection_missing", caught.exception.code)

    def test_stable_source_copies_old_physical_dependency_without_current_pointer(self):
        old_physical = str(uuid.uuid4())
        parent_current = str(uuid.uuid4())
        original = self.path(self.parent)
        old_path = original.with_name(original.name.rsplit(self.parent, 1)[0] + old_physical + ".jsonl")
        original.rename(old_path)
        with native._connect(self.profile / "state_5.sqlite") as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(old_path), self.parent))
        with native._connect(self.profile / "thread_history_1.sqlite") as db:
            for table in ("thread_items", "thread_turns", "thread_history_projection_state"):
                db.execute("UPDATE " + table + " SET thread_id=? WHERE thread_id=?", (old_physical, self.parent))

        current_path = old_path.with_name(old_path.name.rsplit(old_physical, 1)[0] + parent_current + ".jsonl")
        shutil.copyfile(old_path, current_path)
        self.clone_history_key(old_physical, parent_current)
        with native._connect(self.profile / "state_5.sqlite") as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(current_path), self.parent))
        self.change_header(self.child, lambda meta: meta.update(
            cli_version="0.165.0-alpha.1.2",
            history_base={**meta["history_base"], "thread_id": old_physical},
            forked_from_id=old_physical))

        destination = self.root / "stable-source"
        frozen = backup_workflow.stable_source(self.profile, destination, lambda: None)
        copied_old = frozen / old_path.relative_to(self.profile)
        with native._connect(frozen / "state_5.sqlite", readonly=True) as db:
            pointers = {Path(row[0]) for row in db.execute("SELECT rollout_path FROM threads")}
        self.assertTrue(copied_old.is_file())
        self.assertNotIn(copied_old, pointers)
        result = lineage.export_candidate(frozen, [self.child])
        self.assertEqual(2, len(result["transfer"]["threads"][0]["turns"]))

    def test_backup_record_limit_accepts_large_nonvisible_record_only_on_backup_route(self):
        path = self.path(self.parent)
        projection = self.profile / "thread_history_1.sqlite"
        with native._connect(projection, readonly=True) as db:
            next_ordinal = db.execute(
                "SELECT next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?",
                (self.parent,)).fetchone()[0]
        large = "R" * (9 * 1024 * 1024)
        service_event = {
            "type": "event_msg", "ordinal": next_ordinal,
            "payload": {"type": "item_completed", "item": {"type": "Reasoning", "content": large}},
        }
        with path.open("ab") as stream:
            stream.write((json.dumps(service_event) + "\n").encode("utf-8"))
        with native._connect(projection) as db:
            db.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=? WHERE thread_id=?",
                       (path.stat().st_size, next_ordinal + 1, self.parent))

        with self.assertRaisesRegex(lineage.LineageError, "record limit"):
            lineage.export_candidate(self.profile, [self.parent])

        physical_id, current_path = self.physicalize_head(self.parent)
        before = self.watched_source_digest()
        result = lineage.export_candidate(
            self.profile, [self.parent], text_policy=filtering.VERBATIM_TEXT,
            resolve_current_storage=True)

        self.assertEqual(self.parent, result["transfer"]["threads"][0]["id"])
        self.assertEqual(physical_id, result["lineage"][self.parent][0]["storage_binding"]["physical_rollout_id"])
        self.assertEqual(2, len(result["transfer"]["threads"][0]["turns"]))
        self.assertNotIn(large[:100], json.dumps(result))
        self.assertEqual(current_path, self.path(self.parent))
        self.assertEqual(before, self.watched_source_digest())

    def test_backup_record_limit_still_rejects_records_over_sixteen_mib(self):
        path=self.path(self.parent)
        with native._connect(self.profile/'thread_history_1.sqlite') as db:
            ordinal=db.execute('SELECT next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?',(self.parent,)).fetchone()[0]
            record={'type':'event_msg','ordinal':ordinal,'payload':{'type':'item_completed','item':{'type':'Reasoning','content':'R'*(17*1024*1024)}}}
            with path.open('ab') as stream:stream.write((json.dumps(record)+'\n').encode())
            db.execute('UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=? WHERE thread_id=?',(path.stat().st_size,ordinal+1,self.parent))
        with self.assertRaisesRegex(lineage.LineageError,'record limit'):
            lineage.export_candidate(self.profile,[self.parent],text_policy=filtering.VERBATIM_TEXT,resolve_current_storage=True)


if __name__ == "__main__":
    unittest.main()
