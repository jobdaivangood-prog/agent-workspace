"""Pinned enrichment tests using only the synthetic backup fixture."""

import copy
import queue
from pathlib import Path
import unittest

import rehome_backup_module as backup
import rehome_backup_workflow as workflow
import rehome_result_links as links
import rehome_review_sources as managed
import snapshot
from tests import test_backup_module as fixtures


class ResultEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BackupModuleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.old_backup = self.fixture.root / "backup-no-results"
        self.old_receipt = backup.create_backup(
            self.fixture.profile,
            [self.fixture.tid],
            self.old_backup,
            ready_files={},
            runtime_version=backup.SUPPORTED_RUNTIME,
            device_id="synthetic-test",
        )
        self.old_history = self.old_backup / snapshot.CHAT_BUNDLE_PATH
        self.original_history_bytes = self.old_history.read_bytes()
        self.original_history_hash = snapshot._hash_file(self.old_history)
        self.new_backup = self.fixture.root / "backup-enriched"
        self.ready_files = {
            self.fixture.relative: {
                "path": str(self.fixture.result),
                "classification": managed.CLASSIFICATION,
            }
        }

    def test_add_results_enriches_new_copy_and_preserves_zero_result_history(self):
        result = backup.add_results(
            self.old_backup,
            self.new_backup,
            ready_files=self.ready_files,
            profile=self.fixture.profile,
            expected_digest=self.old_receipt["manifest_digest"],
        )

        self.assertEqual(self.original_history_bytes, self.old_history.read_bytes())
        self.assertEqual(self.original_history_hash, snapshot._hash_file(self.old_history))
        enriched_history = self.new_backup / snapshot.CHAT_BUNDLE_PATH
        self.assertEqual(self.original_history_bytes, enriched_history.read_bytes())
        self.assertEqual(self.original_history_hash, snapshot._hash_file(enriched_history))

        descriptor = backup._read(self.new_backup / backup.DESCRIPTOR)
        self.assertEqual([self.fixture.relative], descriptor["result_paths"])
        self.assertEqual("rehome-backup/2", descriptor["format"])
        self.assertEqual(self.fixture.result.read_bytes(),
                         (self.new_backup / self.fixture.relative).read_bytes())
        self.assertEqual(result["manifest_digest"],
                         backup.verify_backup(self.new_backup)["manifest_digest"])

    def test_add_results_rejects_wrong_source_pin_before_creating_copy(self):
        with self.assertRaises(backup.BackupError):
            backup.add_results(
                self.old_backup,
                self.fixture.root / "backup-bad-pin",
                ready_files=self.ready_files,
                profile=self.fixture.profile,
                expected_digest="0" * 64,
            )
        self.assertFalse((self.fixture.root / "backup-bad-pin").exists())

    def test_add_results_rejects_nested_destination_without_changing_source(self):
        before = snapshot._hash_file(self.old_history)
        profile_before = sorted(
            (path.relative_to(self.fixture.profile).as_posix(),
             snapshot._hash_file(path) if path.is_file() else None)
            for path in self.fixture.profile.rglob("*")
        )
        for nested in (self.old_backup / "nested-new-copy", self.fixture.profile / "nested-new-copy"):
            with self.subTest(destination=nested), self.assertRaises(backup.BackupError):
                backup.add_results(
                    self.old_backup,
                    nested,
                    ready_files=self.ready_files,
                    profile=self.fixture.profile,
                    expected_digest=self.old_receipt["manifest_digest"],
                )
            self.assertFalse(nested.exists())
            self.assertEqual(before, snapshot._hash_file(self.old_history))
            self.assertEqual(self.old_receipt["manifest_digest"],
                             backup.verify_backup(self.old_backup,
                                                  expected_digest=self.old_receipt["manifest_digest"])["manifest_digest"])
        profile_after = sorted(
            (path.relative_to(self.fixture.profile).as_posix(),
             snapshot._hash_file(path) if path.is_file() else None)
            for path in self.fixture.profile.rglob("*")
        )
        self.assertEqual(profile_before, profile_after)

    def test_repeated_enrichment_merges_old_and_new_alias_occurrences(self):
        old_alias = "C:/previous/old-result.py"
        new_alias = "C:/previous/new-result.py"
        base_candidate = copy.deepcopy(backup._read(self.old_backup / snapshot.CHAT_BUNDLE_PATH))
        assistant = next(m for thread in base_candidate["transfer"]["threads"]
                         for turn in thread["turns"] for m in turn["messages"] if m["role"] == "assistant")
        assistant["text"] += (
            f"\n[old name]({old_alias}) [new name]({new_alias}) "
            f"[current source](<{self.fixture.result.resolve().as_posix()}>)"
        )
        alias_source = self.fixture.root / "backup-alias-source"
        alias_receipt = snapshot.create_snapshot(
            alias_source,
            version=1,
            device_id="synthetic-test",
            results={backup.DESCRIPTOR: self.old_backup / backup.DESCRIPTOR},
            chat_bundle=base_candidate,
        )

        first_backup = self.fixture.root / "backup-first-alias"
        first = backup.add_results(
            alias_source,
            first_backup,
            ready_files={self.fixture.relative: {
                "path": str(self.fixture.result),
                "classification": managed.CLASSIFICATION,
                "aliases": [old_alias],
            }},
            profile=self.fixture.profile,
            expected_digest=alias_receipt["manifest_digest"],
        )
        second_backup = self.fixture.root / "backup-second-alias"
        second = backup.add_results(
            first_backup,
            second_backup,
            ready_files={self.fixture.relative: {
                "path": str(self.fixture.result),
                "classification": managed.CLASSIFICATION,
                "aliases": [new_alias],
            }},
            profile=self.fixture.profile,
            expected_digest=first["manifest_digest"],
        )

        backup.verify_backup(second_backup, expected_digest=second["manifest_digest"])
        manifest = backup._read(second_backup / links.NAME)
        file_entry = next(item for item in manifest["files"] if item["relative"] == self.fixture.relative)
        self.assertTrue({old_alias, new_alias}.issubset(set(file_entry["aliases"])))
        occurrence_paths = {item["path"] for item in manifest["occurrences"]}
        self.assertEqual({old_alias.casefold(), new_alias.casefold(),
                          self.fixture.result.resolve().as_posix().casefold()},
                         {path.casefold() for path in occurrence_paths})
        self.assertEqual(3, len(manifest["occurrences"]))

    def test_discovery_then_enrichment_handles_available_missing_and_projectless_links(self):
        available = self.fixture.root / "discovery source" / "final result.py"
        available.parent.mkdir()
        available.write_text("print('synthetic discovered result')\n", encoding="utf-8")
        manually_selected = self.fixture.root / "manual replacement" / "replacement.py"
        manually_selected.parent.mkdir()
        manually_selected.write_text("print('synthetic manual replacement')\n", encoding="utf-8")
        missing = self.fixture.root / "not-present" / "missing.py"

        base = self.fixture.root / "backup-discovery-base"
        base_receipt = backup.create_backup(
            self.fixture.profile,
            [self.fixture.tid],
            base,
            ready_files={},
            runtime_version=backup.SUPPORTED_RUNTIME,
            device_id="synthetic-test",
        )
        candidate = copy.deepcopy(backup._read(base / snapshot.CHAT_BUNDLE_PATH))
        thread = next(t for t in candidate["transfer"]["threads"] if t["id"] == self.fixture.tid)
        thread["project_id"] = None
        assistant = next(m for turn in thread["turns"] for m in turn["messages"]
                         if m["role"] == "assistant")
        available_href = available.resolve().as_posix()
        missing_href = missing.resolve().as_posix()
        assistant["text"] += (
            f"\n[available one](<{available_href}>)"
            f" [available repeat](<{available_href}>)"
            f" [missing](<{missing_href}>)"
        )

        linked_source = self.fixture.root / "backup-discovery-source"
        source_receipt = snapshot.create_snapshot(
            linked_source,
            version=1,
            device_id="synthetic-test",
            results={backup.DESCRIPTOR: base / backup.DESCRIPTOR},
            chat_bundle=candidate,
        )
        source_history = linked_source / snapshot.CHAT_BUNDLE_PATH
        source_history_bytes = source_history.read_bytes()
        source_history_hash = snapshot._hash_file(source_history)

        discovery = workflow.discover_results(
            {"source_profile": str(self.fixture.profile)}, linked_source)
        self.assertEqual(source_receipt["manifest_digest"], discovery["manifest_digest"])
        self.assertEqual(2, len(discovery["rows"]))
        by_path = {row["path"]: row for row in discovery["rows"]}
        self.assertEqual({available_href, missing_href}, set(by_path))
        self.assertEqual("available", by_path[available_href]["status"])
        self.assertEqual("missing", by_path[missing_href]["status"])
        self.assertEqual("__projectless__", by_path[available_href]["project_id"])
        self.assertEqual("Без проекта", by_path[available_href]["project_name"])
        self.assertEqual(source_history_bytes, source_history.read_bytes())
        self.assertEqual(source_history_hash, snapshot._hash_file(source_history))

        selected = by_path[available_href]
        missing_row = by_path[missing_href]
        output = self.fixture.root / "enriched-output"
        output.mkdir()
        settings = {
            "source_profile": str(self.fixture.profile),
            "backup_root": str(output),
            "finished_results": [{
                "project_id": selected["project_id"],
                "path": selected["path"],
                "display_name": selected["display_name"],
                "classification": managed.CLASSIFICATION,
            }, {
                "project_id": missing_row["project_id"],
                "path": str(manually_selected),
                "display_name": missing_row["display_name"],
                "classification": managed.CLASSIFICATION,
                "aliases": [missing_href],
            }],
        }
        result = workflow.enrich_backup(settings, linked_source, self.fixture.root / "cancel-sentinel",
                                        queue.Queue(), expected_digest=discovery["manifest_digest"])
        enriched = Path(result["backup"])
        verified = backup.verify_backup(enriched, expected_digest=result["manifest_digest"])
        self.assertEqual(2, verified["counts"]["results"])
        descriptor = backup._read(enriched / backup.DESCRIPTOR)
        self.assertEqual(2, len(descriptor["result_paths"]))
        self.assertTrue(all(path.startswith("Projects/__projectless__/Results/")
                            for path in descriptor["result_paths"]))
        source_paths = {links.source_key(item["source_path"]): item["relative"]
                        for item in backup._read(enriched / links.NAME)["files"]}
        self.assertEqual({links.source_key(str(available)), links.source_key(str(manually_selected))},
                         set(source_paths))
        self.assertEqual(available.read_bytes(),
                         (enriched / source_paths[links.source_key(str(available))]).read_bytes())
        self.assertEqual(manually_selected.read_bytes(),
                         (enriched / source_paths[links.source_key(str(manually_selected))]).read_bytes())

        enriched_candidate = backup._read(enriched / snapshot.CHAT_BUNDLE_PATH)
        result_manifest = backup._read(enriched / links.NAME)
        self.assertEqual(3, len(result_manifest["occurrences"]))
        alias_occurrence = next(item for item in result_manifest["occurrences"]
                                if item["path"] == missing_href)
        self.assertEqual(source_paths[links.source_key(str(manually_selected))], alias_occurrence["relative"])
        relocated_root = self.fixture.root / "relocated project"
        derived, _ledger = links.derive(enriched_candidate, result_manifest,
                                        {"__projectless__": str(relocated_root)})
        derived_text = next(m["text"] for t in derived["transfer"]["threads"]
                            if t["id"] == self.fixture.tid
                            for turn in t["turns"] for m in turn["messages"] if m["role"] == "assistant")
        expected_target = relocated_root / Path(*source_paths[links.source_key(str(manually_selected))].split("/")[2:])
        self.assertIn(f"<{expected_target.as_uri()}>", derived_text)
        self.assertEqual(source_history_bytes, source_history.read_bytes())


if __name__ == "__main__":
    unittest.main()
