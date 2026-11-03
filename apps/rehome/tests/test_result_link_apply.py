"""End-to-end local apply coverage for portable result links."""

import copy
import json
from pathlib import Path
import unittest

import native_adapter as native
import rehome_apply_transaction as transaction
import rehome_backup_module as backup
import rehome_native_apply as apply
import rehome_native_staging as staging
import rehome_result_links as links
import snapshot
from tests import test_native_apply as fixtures


class ResultLinkApplyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeApplyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.links_stage = self._make_linked_stage()

    def _make_linked_stage(self):
        fixture = self.fixture.fixture.fixture
        original_candidate = backup._read(Path(self.fixture.stage["job"]) / "Package" / snapshot.CHAT_BUNDLE_PATH)
        candidate = copy.deepcopy(original_candidate)
        thread = next(t for t in candidate["transfer"]["threads"] if t["id"] == fixture.tid)
        thread["archived"] = True
        assistant = next(m for turn in thread["turns"] for m in turn["messages"] if m["role"] == "assistant")
        source_href = fixture.result.resolve().as_posix()
        assistant["text"] += f"\n[finished result](<{source_href}>)"

        relative = fixture.relative
        binding = {relative: {"path": str(fixture.result.resolve())}}
        manifest = links.make_manifest(candidate, binding)
        descriptor = backup._read(Path(self.fixture.stage["job"]) / "Package" / backup.DESCRIPTOR)
        descriptor.update(format="rehome-backup/2", result_links_path=links.NAME)

        linked_root = self.fixture.root / "backup-linked"
        descriptor_path = self.fixture.root / "linked-ReHome.json"
        manifest_path = self.fixture.root / "linked-ResultLinks.json"
        snapshot._write_json(descriptor_path, descriptor)
        snapshot._write_json(manifest_path, manifest)
        payload = {
            relative: fixture.result,
            backup.DESCRIPTOR: descriptor_path,
            links.NAME: manifest_path,
        }
        created = snapshot.create_snapshot(linked_root, version=1, device_id="synthetic-test",
                                           results=payload, chat_bundle=candidate)
        backup.verify_backup(linked_root, expected_digest=created["manifest_digest"])

        stage = staging.prepare_native(
            linked_root,
            self.fixture.root / "StagingLinked",
            expected_digest=created["manifest_digest"],
            runtime_version=backup.SUPPORTED_RUNTIME,
            schema_template=self.fixture.fixture.template,
            template_pins=self.fixture.fixture.pins,
            project_map=self.fixture.fixture.mapping,
        )
        return {
            "backup": linked_root,
            "backup_digest": created["manifest_digest"],
            "candidate": candidate,
            "original_candidate": original_candidate,
            "relative": relative,
            "source_text": assistant["text"],
            "source_href": source_href,
            "source_path": fixture.result,
            "stage": stage,
        }

    def _prepare_apply(self):
        staged = self.links_stage["stage"]
        return apply.prepare(
            staged["job"], self.fixture.root / "ApplyLinked",
            expected_digest=self.links_stage["backup_digest"],
            expected_stage_digest=staged["stage_digest"],
            target_profile=self.fixture.target,
            projects_root=self.fixture.root / "Restored Projects",
        )

    def test_linked_result_survives_apply_and_rollback_without_changing_source(self):
        staged = self.links_stage
        original_package_path = Path(self.fixture.stage["job"]) / "Package" / snapshot.CHAT_BUNDLE_PATH
        original_package_bytes = original_package_path.read_bytes()
        original_message_text = next(
            m["text"] for t in staged["original_candidate"]["transfer"]["threads"]
            if t["id"] == self.fixture.fixture.fixture.tid
            for turn in t["turns"] for m in turn["messages"] if m["role"] == "assistant"
        )

        result = self._prepare_apply()
        candidate_root = Path(result["candidate"])
        with native._connect(candidate_root / "Profile" / "state_5.sqlite", readonly=True) as db:
            row = db.execute("SELECT rollout_path, archived FROM threads WHERE id=?",
                             (self.fixture.fixture.fixture.tid,)).fetchone()
        self.assertEqual(1, row["archived"])
        rollout = Path(row["rollout_path"])
        self.assertTrue(rollout.is_relative_to(self.fixture.target))
        candidate_rollout = candidate_root / "Profile" / rollout.relative_to(self.fixture.target)
        session_text = candidate_rollout.read_text(encoding="utf-8")
        restored_root = Path(json.loads((candidate_root / apply.PLAN).read_text(encoding="utf-8"))[
            "project_roots"][self.fixture.fixture.fixture.pid])
        expected_result = restored_root / "Results" / "final.py"
        candidate_result = candidate_root / "Projects" / self.fixture.fixture.fixture.pid / "Results" / "final.py"
        self.assertEqual(staged["source_path"].read_bytes(), candidate_result.read_bytes())
        self.assertEqual(snapshot._hash_file(staged["source_path"]), snapshot._hash_file(candidate_result))
        self.assertIn(f"<{expected_result.as_uri()}>", session_text)

        prepared = transaction.prepare(candidate_root, self.fixture.root / "TransactionLinked",
                                       expected_apply_digest=result["apply_digest"], guard=lambda: None)
        transaction.activate(prepared["job"], transaction_digest=prepared["transaction_digest"], guard=lambda: None)
        apply.verify_installed(candidate_root)
        self.assertEqual(session_text, rollout.read_text(encoding="utf-8"))
        self.assertEqual(staged["source_path"].read_bytes(), expected_result.read_bytes())
        self.assertEqual(snapshot._hash_file(staged["source_path"]), snapshot._hash_file(expected_result))
        self.assertEqual(staged["source_path"].read_bytes(), expected_result.read_bytes())
        rolled_back = transaction.rollback(prepared["job"], transaction_digest=prepared["transaction_digest"],
                                           guard=lambda: None)
        self.assertEqual("rolled_back", rolled_back["status"])
        self.assertEqual(b"ORIGINAL_STATE", (self.fixture.target / "state_5.sqlite").read_bytes())

        package_candidate = backup._read(original_package_path)
        package_message = next(
            m["text"] for t in package_candidate["transfer"]["threads"]
            if t["id"] == self.fixture.fixture.fixture.tid
            for turn in t["turns"] for m in turn["messages"] if m["role"] == "assistant"
        )
        self.assertEqual(original_package_bytes, original_package_path.read_bytes())
        self.assertEqual(original_message_text, package_message)
        self.assertEqual(staged["source_text"], next(
            m["text"] for t in staged["candidate"]["transfer"]["threads"]
            if t["id"] == self.fixture.fixture.fixture.tid
            for turn in t["turns"] for m in turn["messages"] if m["role"] == "assistant"
        ))

    def test_tampered_relocation_ledger_fails_even_after_apply_ready_is_resealed(self):
        result = self._prepare_apply()
        root = Path(result["candidate"])
        (root / "Relocations.json").write_text(native._json([{"tampered": True}]), encoding="utf-8")
        (root / apply.READY).write_text(native._json({
            "format": "rehome-native-apply-ready/1",
            "files": apply._inventory(root),
        }), encoding="utf-8")
        resealed_digest = snapshot._hash_file(root / apply.READY)[0]
        with self.assertRaisesRegex(ValueError, "Result relocation ledger differs"):
            apply.verify(root, expected_apply_digest=resealed_digest)


if __name__ == "__main__":
    unittest.main()
