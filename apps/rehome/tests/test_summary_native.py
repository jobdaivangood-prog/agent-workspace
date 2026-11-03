"""Synthetic end-to-end staging, activation, source inspection and rollback."""

import hashlib
import json
from pathlib import Path
import unittest

import native_adapter as native
import rehome_apply_transaction as transaction
import rehome_backup_module as backup
import rehome_native_apply as apply
import rehome_native_staging as staging
import rehome_session_source as session_source
import rehome_summary_history as summaries
import snapshot
from tests import test_native_staging as staging_fixtures
from tests import test_summary_history as summary_fixtures


class SummaryNativeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = staging_fixtures.NativeStagingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def _install(self, label, *, old_identity=None):
        result_file = self.root / (label + "-result.bin")
        result_file.write_bytes(("mapped bytes " + label).encode("utf-8"))
        result = {"result_id": "result1", "version_id": "version1", "project_id": "p1",
                  "chat_ids": ["c1"], "name": result_file.name, "source_path": str(result_file),
                  "relative_path": "Projects/p1/Results/" + result_file.name,
                  "sha256": hashlib.sha256(result_file.read_bytes()).hexdigest(),
                  "bytes": result_file.stat().st_size}
        source = summary_fixtures._source(projects=[summary_fixtures._project("p1", "Summary project")],
                                          chats=[summary_fixtures._chat("c1", title="Summary chat")],
                                          results=[result])
        package = self.root / (label + "-summary-package")
        verified = backup.create_summary_backup(source, package, source_profile=self.fixture.fixture.profile)
        package_digest = verified["manifest_digest"]

        project_map = {"p1": "Projects/Summary project"}
        stage_result = staging.prepare_native(package, self.root / (label + "-staging"),
            expected_digest=package_digest, runtime_version=backup.SUPPORTED_RUNTIME,
            schema_template=self.fixture.template, template_pins=self.fixture.pins,
            project_map=project_map)
        target_home = self.root / (label + "-synthetic-home")
        target_profile = target_home / ".codex"
        target_profile.mkdir(parents=True)
        if old_identity is not None:
            (target_profile / summaries.IDENTITY_FILE).write_bytes(old_identity)
        candidate = apply.prepare(stage_result["job"], self.root / (label + "-apply"),
            expected_digest=package_digest, expected_stage_digest=stage_result["stage_digest"],
            target_profile=target_profile, target_home=target_home,
            projects_root=target_home / "Projects")
        receipt = transaction.prepare(candidate["candidate"], self.root / (label + "-transaction"),
                                      expected_apply_digest=candidate["apply_digest"], guard=lambda: None)
        active = transaction.activate(receipt["job"], transaction_digest=receipt["transaction_digest"],
                                      guard=lambda: None)
        return {"source": source, "package": package, "package_digest": package_digest,
                "candidate": backup._read(package / snapshot.CHAT_BUNDLE_PATH),
                "target": target_profile, "projects_root": target_home / "Projects",
                "apply": candidate, "transaction": receipt, "active": active,
                "result_file": result_file, "result": result}

    def test_summary_staging_activation_identity_install_source_boundaries_and_rollback(self):
        original_receipt = b'{"previous":"identity receipt"}\n'
        installed = self._install("summary", old_identity=original_receipt)
        target = installed["target"]
        apply.verify_installed(installed["apply"]["candidate"])

        plan = backup._read(Path(installed["apply"]["candidate"]) / apply.PLAN)
        mapped_file = Path(plan["project_roots"]["p1"]) / "Results" / installed["result_file"].name
        self.assertEqual(mapped_file.read_bytes(), installed["result_file"].read_bytes())
        self.assertEqual(installed["active"]["status"], "restored")

        receipt = json.loads((target / summaries.IDENTITY_FILE).read_text(encoding="utf-8"))
        self.assertEqual(receipt, summaries.identity_receipt(installed["candidate"], installed["package_digest"]))
        with native._connect(target / "state_5.sqlite", readonly=True) as state:
            native_ids = {row[0] for row in state.execute("SELECT id FROM threads")}
        self.assertEqual(native_ids, {row["native_chat_id"] for row in installed["candidate"]["identity_map"]})
        with native._connect(target / "thread_history_1.sqlite", readonly=True) as history:
            item_types = [row[0] for row in history.execute("SELECT item_type FROM thread_items")]
        self.assertEqual(item_types, ["agentMessage"])

        inspected = session_source.inspect(target, workspace_id="workspace_1",
            previous_candidate=(installed["candidate"], installed["package_digest"]))
        self.assertEqual([chat["canonical_chat_id"] for chat in inspected["catalog"]["chats"]], ["c1"])
        self.assertTrue(all(row["journal"]["projection_verified"] for row in inspected["boundaries"]))
        self.assertEqual(session_source.verify_boundaries(target, inspected), [])

        native_id = receipt["identity_map"][0]["native_chat_id"]
        with native._connect(target / "thread_history_1.sqlite") as history:
            row = history.execute("SELECT turn_id,rollout_ordinal FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1",
                                  (native_id,)).fetchone()
            turn_id, ordinal = row[0], row[1] + 1
            appended = {"type": "userMessage", "id": "synthetic-appended-tail",
                        "content": [{"type": "text", "text": "synthetic tail"}]}
            history.execute("INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal) "
                            "VALUES(?,?,?,?,?,?,?,?)", (native_id, turn_id, appended["id"], ordinal, 12000,
                            native._json(appended), "userMessage", ordinal))
        tails = session_source.verify_boundaries(target, inspected)
        self.assertEqual(tails, [{"chat_id": "c1", "reason": "messages_after_summary_boundary", "messages": 1}])

        with native._connect(target / "thread_history_1.sqlite") as history:
            original = history.execute("SELECT item_id,item_json FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal LIMIT 1",
                                       (native_id,)).fetchone()
            changed = json.loads(original[1])
            changed["text"] += " edited"
            history.execute("UPDATE thread_items SET item_json=? WHERE thread_id=? AND item_id=?",
                            (native._json(changed), native_id, original[0]))
        with self.assertRaisesRegex(ValueError, "history changed"):
            session_source.verify_boundaries(target, inspected)

        rolled_back = transaction.rollback(installed["transaction"]["job"],
            transaction_digest=installed["transaction"]["transaction_digest"], guard=lambda: None)
        self.assertEqual(rolled_back["status"], "rolled_back")
        self.assertEqual((target / summaries.IDENTITY_FILE).read_bytes(), original_receipt)

    def test_rollback_removes_identity_receipt_when_target_had_none(self):
        installed = self._install("summary-no-old-identity")
        target = installed["target"]
        self.assertTrue((target / summaries.IDENTITY_FILE).is_file())
        result = transaction.rollback(installed["transaction"]["job"],
            transaction_digest=installed["transaction"]["transaction_digest"], guard=lambda: None)
        self.assertEqual(result["status"], "rolled_back")
        self.assertFalse((target / summaries.IDENTITY_FILE).exists())


if __name__ == "__main__":
    unittest.main()
