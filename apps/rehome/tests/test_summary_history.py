"""Deterministic tests for the lossy, agent-authored summary history contract."""

import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

import native_adapter as native
import rehome_backup_module as backup
import rehome_display_metadata as display
import rehome_result_links as links
import rehome_summary_history as summaries
import rehome_review as review
import snapshot


def _sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _project(pid="p1", name="Project"):
    return {"id": pid, "name": name, "created_at_ms": 1000, "updated_at_ms": 2000}


def _chat(cid, *, title=None, project_id="p1", archived=False, pinned=False, created=3000, updated=9000):
    return {"canonical_chat_id": cid, "title": title or ("Title " + cid), "project_id": project_id,
            "archived": archived, "is_pinned": pinned, "created_at_ms": created, "updated_at_ms": updated}


def _handoff(cid, coverage="complete"):
    return {"canonical_chat_id": cid, "task_record_id": "record_" + cid,
            "task_revision_id": "revision_" + cid,
            "source_cursor": {"last_turn_id": "turn_" + cid, "visible_sha256": _sha("visible " + cid)},
            "coverage": coverage, "goal": "Перенести проверенную сводку " + cid,
            "outcome": "Подготовлена сводка.", "decisions": [{"text": "Решение", "reason": "Основание"}],
            "constraints": ["Только доступная история"], "unfinished": ["Проверить результат"],
            "next_steps": ["Продолжить проверку"]}


def _source(projects=None, chats=None, handoffs=None, results=None):
    chats = chats if chats is not None else [_chat("c1")]
    return {"workspace_id": "workspace_1", "created_at_ms": 11000,
            "catalog": {"projects": projects if projects is not None else [_project()], "chats": chats},
            "handoffs": handoffs if handoffs is not None else [_handoff(c["canonical_chat_id"]) for c in chats],
            "results": results if results is not None else []}


class SummaryHistoryTests(unittest.TestCase):
    def test_build_and_validate_are_deterministic(self):
        source = _source()
        first = summaries.build(source)
        second = summaries.build(source)
        self.assertEqual(first, second)
        self.assertEqual(summaries.validate(first), {"projects": 1, "threads": 1, "turns": 1, "messages": 1, "partial_summaries": 0})

    def test_native_identity_uses_canonical_id_and_survives_title_change(self):
        source = _source()
        initial = summaries.build(source)
        source["catalog"]["chats"][0]["title"] = "A completely different title"
        renamed = summaries.build(source)
        self.assertEqual(initial["identity_map"][0]["native_chat_id"], renamed["identity_map"][0]["native_chat_id"])
        self.assertEqual(initial["identity_map"][0]["native_chat_id"],
                         summaries.native_id("workspace_1", "c1"))

    def test_projects_empty_chats_archive_pin_and_projectless_are_preserved(self):
        chats = [_chat("c1", archived=True, pinned=True),
                 _chat("c2", project_id=None, archived=False, pinned=True, created=4000, updated=8000)]
        source = _source(projects=[_project(), _project("p2", "Empty project")], chats=chats)
        built = summaries.build(source)
        self.assertEqual([p["id"] for p in built["transfer"]["projects"]], ["p1", "p2"])
        self.assertEqual([t["project_id"] for t in built["transfer"]["threads"]], ["p1", None])
        self.assertEqual([t["archived"] for t in built["transfer"]["threads"]], [True, False])
        self.assertEqual([t["is_pinned"] for t in built["transfer"]["threads"]], [True, True])

    def test_projection_has_exactly_one_assistant_summary_and_original_dates(self):
        source = _source()
        built = summaries.build(source)
        thread = built["transfer"]["threads"][0]
        messages = [message for turn in thread["turns"] for message in turn["messages"]]
        self.assertEqual(len(thread["turns"]), 1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "assistant")
        self.assertIn("Перенесённая сводка ReHome", messages[0]["text"])
        self.assertNotIn("user", [message["role"] for message in messages])
        self.assertEqual(built["display_metadata"]["threads"][thread["id"]]["values"]["created_at_ms"], 3000)
        self.assertEqual(built["display_metadata"]["threads"][thread["id"]]["values"]["updated_at_ms"], 9000)
        self.assertEqual(built["display_metadata"]["threads"][thread["id"]]["turns"][thread["turns"][0]["id"]]
                         ["items"][messages[0]["id"]]["created_at_ms"], 11000)

    def test_forged_transfer_handoff_digest_and_old_verbatim_policy_are_rejected(self):
        built = summaries.build(_source())
        forged_transfer = copy.deepcopy(built)
        forged_transfer["transfer"]["threads"][0]["title"] = "Forged"
        with self.assertRaises(ValueError):
            summaries.validate(forged_transfer)
        forged_digest = copy.deepcopy(built)
        forged_digest["source_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            summaries.validate(forged_digest)
        forged_handoff = copy.deepcopy(built)
        forged_handoff["source"]["handoffs"][0]["source_cursor"]["visible_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            summaries.validate(forged_handoff)
        with self.assertRaises(ValueError):
            review.validate_backup_history(built)

    def test_source_links_keep_literal_percent_sequences_distinct_from_spaces(self):
        paths = ("C:/source/literal%20name.dat", "C:/source/literal name.dat")
        results = []
        bindings = {}
        for index, path in enumerate(paths, 1):
            relative = "Projects/p1/Results/file%d.dat" % index
            row = {"result_id": "result%d" % index, "version_id": "v%d" % index,
                   "project_id": "p1", "chat_ids": ["c1"], "name": "file%d.dat" % index,
                   "source_path": path, "relative_path": relative,
                   "sha256": str(index) * 64, "bytes": 1}
            results.append(row)
            bindings[relative] = {"path": path}
        candidate = summaries.build(_source(results=results))
        content = candidate["transfer"]["threads"][0]["turns"][0]["messages"][0]["text"]
        manifest = links.make_manifest(candidate, bindings)
        self.assertIn("literal%2520name.dat", content)
        self.assertIn("literal%20name.dat", content)
        self.assertEqual(len(manifest["files"]), 2)
        self.assertEqual({links.source_key(entry["source_path"]) for entry in manifest["files"]},
                         {links.source_key(path) for path in paths})
        self.assertNotEqual(links.source_key(paths[0]), links.source_key(paths[1]))

    def test_only_one_selected_version_per_logical_result(self):
        results = [
            {"result_id": "result1", "version_id": "v1", "project_id": "p1", "chat_ids": ["c1"],
             "name": "file", "source_path": "C:/files/a.bin", "relative_path": "Projects/p1/Results/a.bin",
             "sha256": "0" * 64, "bytes": 0},
            {"result_id": "result1", "version_id": "v2", "project_id": "p1", "chat_ids": ["c1"],
             "name": "file", "source_path": "C:/files/b.bin", "relative_path": "Projects/p1/Results/b.bin",
             "sha256": "1" * 64, "bytes": 0},
        ]
        with self.assertRaises(ValueError):
            summaries.build(_source(results=results))

    def test_partial_summary_is_explicitly_marked(self):
        built = summaries.build(_source(handoffs=[_handoff("c1", "partial")]))
        summary = built["transfer"]["threads"][0]["turns"][0]["messages"][0]["text"]
        self.assertIn("неполной доступной истории", summary)
        self.assertEqual(summaries.validate(built)["partial_summaries"], 1)

    def test_summary_package_roundtrip_pins_sources_and_rejects_missing_or_substituted_result(self):
        with tempfile.TemporaryDirectory(prefix="summary-history-test-") as temp:
            root = Path(temp)
            source_file = root / "source.bin"
            source_file.write_bytes(b"selected result bytes")
            digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
            result = {"result_id": "result1", "version_id": "v1", "project_id": "p1", "chat_ids": ["c1"],
                      "name": "source.bin", "source_path": str(source_file),
                      "relative_path": "Projects/p1/Results/source.bin", "sha256": digest,
                      "bytes": source_file.stat().st_size}
            source = _source(results=[result])
            source_profile = native.create_synthetic_profile(root / "synthetic-source-profile", seed=False)
            package = root / "package"
            verified = backup.create_summary_backup(source, package, source_profile=source_profile)
            self.assertEqual(verified["text_policy"], summaries.POLICY)
            self.assertEqual(verified["counts"]["messages"], 1)
            pinned = verified["manifest_digest"]
            self.assertEqual(backup.verify_backup(package, expected_digest=pinned)["status"], "backup_verified")

            payload_path = package / "Projects/p1/Results/source.bin"
            substituted = root / "substituted"
            snapshot.copy_snapshot(package, substituted, expected_digest=pinned)
            (substituted / "Projects/p1/Results/source.bin").write_bytes(b"substitute")
            with self.assertRaises(ValueError):
                backup.verify_backup(substituted, expected_digest=pinned)

            payload_path.unlink()
            with self.assertRaises(ValueError):
                backup.verify_backup(package, expected_digest=pinned)

    def test_summary_result_rejects_codex_database_protected_profile_path_and_hardlink(self):
        with tempfile.TemporaryDirectory(prefix="summary-result-protection-") as temp:
            root = Path(temp)
            profile = native.create_synthetic_profile(root / "synthetic-profile", seed=False)
            outside = root / "ordinary-result.bin"
            outside.write_bytes(b"synthetic finished bytes")

            def source_for(path):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                row = {"result_id": "protected-result", "version_id": "v1", "project_id": "p1",
                       "chat_ids": ["c1"], "name": path.name, "source_path": str(path),
                       "relative_path": "Projects/p1/Results/" + path.name,
                       "sha256": digest, "bytes": path.stat().st_size}
                return _source(results=[row])

            codex_db = root / ".codex" / "state_5.sqlite"
            codex_db.parent.mkdir()
            codex_db.write_bytes(b"synthetic profile database")
            profile_db = profile / "state_5.sqlite"
            hardlink = root / "hardlinked-result.bin"
            os.link(outside, hardlink)
            for index, path in enumerate((codex_db, profile_db, hardlink)):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    backup.create_summary_backup(source_for(path), root / ("rejected-%d" % index),
                                                 source_profile=profile)


if __name__ == "__main__":
    unittest.main()
