"""Finish/Start coordinator coverage using synthetic native profiles only."""

import copy
import hashlib
import json
from pathlib import Path
import queue
import shutil
import unittest
from unittest.mock import patch

import native_adapter as native
import rehome_backup_workflow as workflow
import rehome_publications as publications
import rehome_session_source as source_reader
import rehome_sessions as sessions
import rehome_summary_history as summaries
import snapshot
from tests import test_native_staging as staging_fixtures
from tests import test_summary_history as summary_fixtures


class QuietGuard:
    def assert_quiet(self, **kwargs):
        return None


class SessionCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = staging_fixtures.NativeStagingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.profile = self.fixture.fixture.profile
        self.workspace_id = "session-workspace-test"
        self.settings_path = self.root / "session-settings.json"
        self.settings = self._settings("source-one", self.profile)
        snapshot._write_json(self.settings_path, self.settings)

    def _settings(self, label, profile):
        home = Path(profile).parent
        local = self.root / (label + "-local")
        backup_root = self.root / "publications"
        projects = self.root / (label + "-projects")
        local.mkdir(parents=True, exist_ok=True)
        backup_root.mkdir(parents=True, exist_ok=True)
        runtime = self.root / "synthetic-runtime" / "bin" / "codex.exe"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_bytes(b"synthetic runtime placeholder")
        (runtime.parent.parent / "ChatGPT.exe").write_bytes(b"synthetic desktop placeholder")
        return {"format": "rehome-two-action-settings/1", "source_home": str(home),
                "source_profile": str(profile), "local_root": str(local),
                "backup_root": str(backup_root), "projects_root": str(projects),
                "runtime_executable": str(runtime), "runtime_version": "0.166.0-alpha.5.1",
                "runtime_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
                "schema_template": str(self.fixture.template), "template_pins": self.fixture.pins,
                "session_workspace_id": self.workspace_id, "session_policy": publications.POLICY,
                "session_device_id": "synthetic-device", "backup_root_selected": True}

    def _read_inspection(self):
        with patch.object(workflow, "_runtime"):
            return sessions.inspect(self.settings)

    def _read_profile_inspection(self, settings):
        with patch.object(workflow, "_runtime"):
            return sessions.inspect(settings)

    def _input_source(self, inspection, *, include_result=False, projectless_result=False):
        catalog = copy.deepcopy(inspection["inspection"]["catalog"])
        source = summary_fixtures._source(projects=catalog["projects"], chats=catalog["chats"])
        source["workspace_id"] = self.workspace_id
        boundaries = {row["canonical_chat_id"]: row for row in inspection["inspection"]["boundaries"]}
        for handoff in source["handoffs"]:
            boundary = boundaries[handoff["canonical_chat_id"]]
            handoff["source_cursor"] = {"last_turn_id": boundary["last_turn_id"] or "no-visible-turns",
                                         "visible_sha256": boundary["visible_sha256"]}
            if boundary["item_count"] == 0:
                handoff["coverage"] = "partial"
        if include_result:
            chat = next(c for c in catalog["chats"] if (c["project_id"] is None) == projectless_result)
            project_id = chat["project_id"]
            result_path = self.fixture.fixture.result
            source["results"] = [{"result_id": "selected-result", "version_id": "selected-version",
                "project_id": project_id, "chat_ids": [chat["canonical_chat_id"]],
                "name": result_path.name, "source_path": str(result_path),
                "relative_path": "Projects/%s/Results/%s" % (project_id or "__projectless__", result_path.name),
                "sha256": snapshot._hash_file(result_path)[0], "bytes": result_path.stat().st_size}]
        return source

    def _prepare(self, *, include_result=True, projectless_result=False, unavailable_result=False,
                 settings=None, settings_path=None):
        settings = settings or self.settings
        settings_path = settings_path or self.settings_path
        inspected = self._read_profile_inspection(settings)
        source = self._input_source(inspected, include_result=include_result,
                                    projectless_result=projectless_result)
        if unavailable_result and source["results"]:
            source["results"][0]["sha256"] = None
            source["results"][0]["bytes"] = None
        request_input = {"inspection": inspected["inspection"], "source": source,
                         "include_environment": False}
        prepared = sessions.prepare(settings, settings_path, request_input)
        return inspected, source, prepared

    def _continue_source(self, accepted_source, inspection, *, outcome=None, target_chat=None):
        source = copy.deepcopy(accepted_source)
        source["catalog"] = copy.deepcopy(inspection["inspection"]["catalog"])
        boundaries = {row["canonical_chat_id"]: row for row in inspection["inspection"]["boundaries"]}
        for handoff in source["handoffs"]:
            boundary = boundaries[handoff["canonical_chat_id"]]
            handoff["source_cursor"] = {"last_turn_id": boundary["last_turn_id"] or "no-visible-turns",
                                         "visible_sha256": boundary["visible_sha256"]}
        if target_chat is not None and outcome is not None:
            next(h for h in source["handoffs"] if h["canonical_chat_id"] == target_chat)["outcome"] = outcome
        return source

    def _append_completed_assistant_turn(self, profile, native_id, text):
        timestamp = "2026-01-02T03:04:05Z"
        with native._connect(profile / "thread_history_1.sqlite") as history:
            projection = history.execute("SELECT next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?",
                                         (native_id,)).fetchone()
            start = projection[0]
            turn_id, message_id = "synthetic-new-outcome-turn", "synthetic-new-outcome-message"
            response = {"type":"message", "id":message_id, "role":"assistant",
                        "content":[{"type":"output_text","text":text}], "phase":"final_answer"}
            completed = {"type":"AgentMessage", "id":message_id, "content":[{"type":"Text","text":text}],
                         "phase":"FinalAnswer", "memory_citation":None, "delivery":None, "questions":None}
            records = [
                {"timestamp":timestamp,"ordinal":start,"type":"event_msg",
                 "payload":{"type":"task_started","turn_id":turn_id}},
                {"timestamp":timestamp,"ordinal":start+1,"type":"response_item","payload":response},
                {"timestamp":timestamp,"ordinal":start+2,"type":"event_msg",
                 "payload":{"type":"item_completed","thread_id":native_id,"turn_id":turn_id,"item":completed}},
                {"timestamp":timestamp,"ordinal":start+3,"type":"event_msg",
                 "payload":{"type":"task_complete","turn_id":turn_id,"last_agent_message":text}},
            ]
            with native._connect(profile / "state_5.sqlite", readonly=True) as thread:
                journal_path = Path(thread.execute("SELECT rollout_path FROM threads WHERE id=?", (native_id,)).fetchone()[0])
            with journal_path.open("ab") as stream:
                for record in records:
                    stream.write((native._json(record) + "\n").encode("utf-8"))
            turn_end = start + 3
            history.execute("INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,status,final_agent_item_id,rollout_end_ordinal,root_turn_id) "
                            "VALUES(?,?,?,?,?,?,?)", (native_id,turn_id,start,"completed",message_id,turn_end,turn_id))
            item = {"type":"agentMessage", "id":message_id, "text":text, "phase":"final_answer",
                    "memoryCitation":None, "delivery":None, "questions":None}
            history.execute("INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type,updated_at_ordinal) "
                            "VALUES(?,?,?,?,?,?,?,?)", (native_id,turn_id,message_id,start+2,16000,
                            native._json(item),"agentMessage",start+2))
            history.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=? WHERE thread_id=?",
                            (journal_path.stat().st_size,turn_end+1,native_id))
        with native._connect(profile / "state_5.sqlite") as state:
            state.execute("UPDATE threads SET updated_at_ms=16000,updated_at=16 WHERE id=?", (native_id,))

    def test_prepare_rejects_forged_catalog_and_observation_boundary(self):
        inspected = self._read_inspection()
        source = self._input_source(inspected, include_result=False)
        good = {"inspection": inspected["inspection"], "source": source, "include_environment": False}

        forged_catalog = copy.deepcopy(good)
        forged_catalog["source"]["catalog"]["chats"][0]["title"] += " forged"
        with self.assertRaisesRegex(ValueError, "catalog"):
            sessions.prepare(self.settings, self.settings_path, forged_catalog)

        forged_boundary = copy.deepcopy(good)
        forged_boundary["inspection"]["boundaries"][0]["visible_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            sessions.prepare(self.settings, self.settings_path, forged_boundary)

    def test_repeat_finish_reuses_publication_but_changed_result_publishes(self):
        _,_,request=self._prepare(include_result=True)
        with patch.object(workflow,'_runtime'),patch.object(sessions.trial_guard,'wait_for_quiet',return_value=QuietGuard()):
            first=sessions.finish(request['request'],request['request_sha256'],close_apps=False)
            second=sessions.finish(request['request'],request['request_sha256'],close_apps=False)
            self.assertEqual(second['publication']['status'],'unchanged')
            self.assertEqual(first['publication']['publication']['publication_id'],second['publication']['publication']['publication_id'])
            self.fixture.fixture.result.write_bytes(b'new finished bytes')
            third=sessions.finish(request['request'],request['request_sha256'],close_apps=False)
            self.assertEqual(third['publication']['status'],'published')
            self.assertNotEqual(first['publication']['publication']['publication_id'],third['publication']['publication']['publication_id'])

    def test_raw_journal_tail_without_projection_is_reported(self):
        inspected=self._read_inspection()['inspection']
        row=next(row for row in inspected['boundaries'] if row['journal'])
        journal_path=self.profile/row['journal']['path']
        with journal_path.open('ab') as stream:
            stream.write(b'{"type":"event_msg","ordinal":99999,"payload":{"type":"task_started","turn_id":"new"}}\n')
        tails=source_reader.verify_boundaries(self.profile,inspected)
        self.assertTrue(any(t['chat_id']==row['canonical_chat_id'] and t['reason']=='journal_changed_or_projection_not_proven' for t in tails))

    def test_manual_summary_restore_rejected_before_wait_or_map_change(self):
        _,_,request=self._prepare(include_result=True)
        with patch.object(workflow,'_runtime'),patch.object(sessions.trial_guard,'wait_for_quiet',return_value=QuietGuard()):
            finished=sessions.finish(request['request'],request['request_sha256'],close_apps=False)
        with patch.object(workflow,'_runtime'),patch.object(workflow,'_wait') as wait,patch.object(workflow.transaction,'prepare') as prepare:
            with self.assertRaises(workflow.WorkflowError) as error:
                workflow.restore_backup(self.settings,finished['publication']['package'],self.root/'cancel',queue.Queue())
            self.assertEqual(error.exception.code,'summary_requires_session_start')
            wait.assert_not_called();prepare.assert_not_called()
        self.assertFalse((Path(self.settings['local_root'])/'SessionMappings').exists())

    def test_start_cannot_select_while_another_thread_owns_profile(self):
        from concurrent.futures import ThreadPoolExecutor
        with workflow.ProfileLock(self.profile),ThreadPoolExecutor(max_workers=1) as pool,patch.object(publications,'scan') as scan:
            future=pool.submit(sessions.start,self.settings,close_apps=False,reopen=False)
            with self.assertRaises(workflow.WorkflowError):future.result(timeout=10)
            scan.assert_not_called()

    def test_finish_publishes_report_skips_missing_result_and_leaves_profile_unchanged(self):
        _, source, prepared = self._prepare(include_result=True, unavailable_result=True)
        request_path = Path(prepared["request"])
        request = snapshot._read_json(request_path)
        selected = Path(request["source"]["results"][0]["source_path"])
        before = {name: snapshot._hash_file(self.profile / name)[0]
                  for name in ("state_5.sqlite", "thread_history_1.sqlite", ".codex-global-state.json")}
        selected.unlink()

        with patch.object(workflow, "_runtime"), \
             patch.object(sessions, "request_close") as close_apps, \
             patch.object(sessions.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()):
            finished = sessions.finish(prepared["request"], prepared["request_sha256"], close_apps=True)

        close_apps.assert_called_once()
        self.assertIn(finished["status"], ("complete_with_omissions", "incomplete"))
        publication = finished["publication"]
        self.assertEqual(publication["status"], "published")
        report = publication["publication"]["report"]
        selected_rows = [row for row in report["items"] if row["category"] == "results"]
        self.assertEqual(len(selected_rows), 1)
        self.assertEqual(selected_rows[0]["status"], "skipped")
        self.assertEqual(selected_rows[0]["reason"], "result_missing_unreadable_or_excluded")
        self.assertEqual({name: snapshot._hash_file(self.profile / name)[0] for name in before}, before)
        self.assertFalse((self.profile / summaries.IDENTITY_FILE).exists())

    def test_start_same_publisher_is_noop_then_other_synthetic_profile_restores_and_rolls_back(self):
        _, _, prepared = self._prepare(include_result=False)
        with patch.object(workflow, "_runtime"), \
             patch.object(sessions, "request_close"), \
             patch.object(sessions.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()):
            published = sessions.finish(prepared["request"], prepared["request_sha256"], close_apps=False)
        digest = published["publication"]["publication"]["package_manifest_sha256"]
        original_db = snapshot._hash_file(self.profile / "state_5.sqlite")[0]

        with patch.object(workflow, "_runtime"), patch.object(sessions, "request_close") as close_apps, \
             patch.object(sessions.subprocess, "Popen") as reopen:
            unchanged = sessions.start(self.settings, close_apps=True, reopen=True)
        self.assertEqual(unchanged["status"], "unchanged")
        close_apps.assert_not_called()
        reopen.assert_not_called()
        self.assertEqual(snapshot._hash_file(self.profile / "state_5.sqlite")[0], original_db)

        other_profile = self.root / "other-home" / "synthetic-profile"
        other_profile.parent.mkdir()
        native.create_synthetic_profile(other_profile, seed=False)
        other_settings = self._settings("source-two", other_profile)
        other_settings["backup_root"] = self.settings["backup_root"]
        other_settings["session_workspace_id"] = self.workspace_id
        other_settings["session_policy"] = publications.POLICY
        with patch.object(workflow, "_runtime"), patch.object(sessions, "request_close") as close_other, \
             patch.object(sessions.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(sessions.subprocess, "Popen") as reopen_other:
            restored = sessions.start(other_settings, close_apps=True, reopen=True)
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(restored["publication"]["publication"]["package_manifest_sha256"], digest)
        self.assertTrue((other_profile / summaries.IDENTITY_FILE).exists())
        close_other.assert_called_once()
        reopen_other.assert_called_once()

        with patch.object(workflow, "_runtime"), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()):
            rolled_back = workflow.rollback(other_settings, self.root / "rollback-cancel", queue.Queue())
        self.assertEqual(rolled_back["status"], "rolled_back")
        self.assertFalse((other_profile / summaries.IDENTITY_FILE).exists())

    def test_two_publisher_receiver_cycles_preserve_new_summary_and_result_then_rollback(self):
        with native._connect(self.profile / "state_5.sqlite") as state:
            source_native_id = state.execute("SELECT id FROM threads ORDER BY id LIMIT 1").fetchone()[0]
            state.execute("UPDATE threads SET project_id=NULL WHERE id=?", (source_native_id,))
        global_path = self.profile / ".codex-global-state.json"
        global_state = json.loads(global_path.read_text(encoding="utf-8"))
        global_state.get("thread-project-assignments", {}).pop(source_native_id, None)
        global_path.write_text(native._json(global_state), encoding="utf-8")
        with native._connect(self.profile / "state_5.sqlite", readonly=True) as state:
            original_ids = {row[0] for row in state.execute("SELECT id FROM threads")}
        original_state_hash = snapshot._hash_file(self.profile / "state_5.sqlite")[0]
        _, source_p1, request_p1 = self._prepare(include_result=True, projectless_result=True)
        self.assertIsNone(source_p1["results"][0]["project_id"])
        self.assertIn("Projects/__projectless__/Results/", source_p1["results"][0]["relative_path"])
        with patch.object(workflow, "_runtime"), patch.object(sessions, "request_close"), \
             patch.object(sessions.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()):
            finished_p1 = sessions.finish(request_p1["request"], request_p1["request_sha256"], close_apps=False)
        p1 = finished_p1["publication"]

        inventory_p1 = self._read_profile_inspection(self.settings)
        source_p2 = self._continue_source(snapshot._read_json(Path(p1["package"]) / snapshot.CHAT_BUNDLE_PATH)["source"],
            inventory_p1, target_chat=source_p1["results"][0]["chat_ids"][0], outcome="A checkpoint P2")
        request_p2 = sessions.prepare(self.settings, self.settings_path,
            {"inspection":inventory_p1["inspection"],"source":source_p2,"include_environment":False})
        with patch.object(workflow, "_runtime"), patch.object(sessions.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()):
            finished_p2 = sessions.finish(request_p2["request"], request_p2["request_sha256"], close_apps=False)
        p2 = finished_p2["publication"]
        p2_digest = p2["publication"]["package_manifest_sha256"]

        p2_folder = Path(p2["path"])
        detached_p2 = self.root / "detached-P2-publication"
        p2_folder.rename(detached_p2)
        with patch.object(workflow, "_runtime"), patch.object(sessions, "request_close") as close_old, \
             patch.object(workflow, "restore_backup") as restore_old:
            stale_start = sessions.start(self.settings, close_apps=True, reopen=False)
        self.assertEqual(stale_start["status"], "unchanged")
        close_old.assert_not_called()
        restore_old.assert_not_called()
        self.assertFalse((self.profile / summaries.IDENTITY_FILE).exists())
        detached_p2.rename(p2_folder)

        receiver_profile = self.root / "receiver-home" / "profile"
        receiver_profile.parent.mkdir()
        shutil.copytree(self.fixture.template, receiver_profile)
        receiver_settings = self._settings("receiver", receiver_profile)
        receiver_settings["backup_root"] = self.settings["backup_root"]
        receiver_settings_path = self.root / "receiver-settings.json"
        snapshot._write_json(receiver_settings_path, receiver_settings)
        real_restore = workflow.restore_backup
        def restore_then_fail_after_activation(*args, **kwargs):
            real_restore(*args, **kwargs)
            raise RuntimeError("synthetic post-activation interruption")
        with patch.object(workflow, "_runtime"), patch.object(sessions, "request_close") as close_b, \
             patch.object(sessions.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(sessions.subprocess, "Popen") as reopen_b, \
             patch.object(workflow, "restore_backup", side_effect=restore_then_fail_after_activation):
            with self.assertRaises(workflow.WorkflowError):
                sessions.start(receiver_settings, close_apps=True, reopen=True)
        close_b.assert_called_once()
        reopen_b.assert_not_called()
        self.assertTrue((receiver_profile / summaries.IDENTITY_FILE).exists())
        mapping_file = Path(receiver_settings["local_root"]) / "SessionMappings" / (p2_digest + ".json")
        self.assertTrue(mapping_file.is_file())

        restored_result = next(Path(receiver_settings["projects_root"]).rglob(source_p1["results"][0]["name"]))
        original_result_bytes = Path(source_p1["results"][0]["source_path"]).read_bytes()
        self.assertEqual(restored_result.read_bytes(), original_result_bytes)
        receiver_inventory = self._read_profile_inspection(receiver_settings)
        accepted = receiver_inventory["accepted_summary_source"]
        self.assertIsNotNone(accepted)
        target_chat = source_p1["results"][0]["chat_ids"][0]
        accepted_handoff = next(h for h in accepted["handoffs"] if h["canonical_chat_id"] == target_chat)
        self.assertEqual(accepted_handoff["outcome"], "A checkpoint P2")
        self.assertEqual(receiver_inventory["local_result_bindings"]["selected-result"]["source_path"],str(restored_result))

        identity_receipt = json.loads((receiver_profile / summaries.IDENTITY_FILE).read_text(encoding="utf-8"))
        native_id = next(row["native_chat_id"] for row in identity_receipt["identity_map"]
                         if row["canonical_chat_id"] == target_chat)
        self._append_completed_assistant_turn(receiver_profile, native_id, "Synthetic new outcome")

        receiver_inspection = self._read_profile_inspection(receiver_settings)
        self.assertEqual({row["canonical_chat_id"] for row in receiver_inspection["inspection"]["catalog"]["chats"]},
                         {row["canonical_chat_id"] for row in source_p1["catalog"]["chats"]})
        self.assertIn(target_chat, {row["canonical_chat_id"] for row in receiver_inspection["inspection"]["catalog"]["chats"]})
        self.assertEqual(len({row["canonical_chat_id"] for row in receiver_inspection["inspection"]["catalog"]["chats"]}),
                         len(receiver_inspection["inspection"]["catalog"]["chats"]))

        continuing_source = self._continue_source(accepted, receiver_inspection,
            target_chat=target_chat, outcome="B appended assistant outcome")
        continuing_handoff = next(h for h in continuing_source["handoffs"] if h["canonical_chat_id"] == target_chat)
        prior_decisions = copy.deepcopy(continuing_handoff["decisions"])
        continuing_handoff["decisions"].append({"text": "Продолжить по обновлённому итогy",
                                                  "reason": "Добавлено после новой записи assistant"})
        self.assertEqual(receiver_inspection['local_result_bindings']['selected-result']['source_path'],str(restored_result))
        self.assertEqual(continuing_handoff["decisions"][:len(prior_decisions)], prior_decisions)
        continuation_input = {"inspection": receiver_inspection["inspection"], "source": continuing_source,
                              "include_environment": False}
        receiver_request = sessions.prepare(receiver_settings, receiver_settings_path, continuation_input)
        self.assertEqual(snapshot._read_json(Path(receiver_request['request']))['source']['results'][0]['source_path'],str(restored_result))
        with patch.object(workflow, "_runtime"), patch.object(sessions, "request_close"), \
             patch.object(sessions.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()):
            receiver_finished = sessions.finish(receiver_request["request"], receiver_request["request_sha256"],
                                                close_apps=False)
        self.assertEqual(receiver_finished["publication"]["status"], "published")
        second_package = Path(receiver_finished["publication"]["package"])
        second_candidate = snapshot._read_json(second_package / snapshot.CHAT_BUNDLE_PATH)
        second_source = second_candidate["source"]
        final_handoff = next(h for h in second_source["handoffs"] if h["canonical_chat_id"] == target_chat)
        self.assertEqual(final_handoff["outcome"], continuing_handoff["outcome"])
        self.assertEqual(final_handoff["decisions"][:len(prior_decisions)], prior_decisions)
        self.assertEqual(len(second_source["results"]), 1)
        self.assertEqual(second_source["results"][0]["result_id"], source_p1["results"][0]["result_id"])

        with patch.object(workflow, "_runtime"), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()):
            receiver_rollback = workflow.rollback(receiver_settings, self.root / "receiver-cycle-rollback", queue.Queue())
        self.assertEqual(receiver_rollback["status"], "rolled_back")
        self.assertFalse((receiver_profile / summaries.IDENTITY_FILE).exists())
        self.assertTrue(mapping_file.is_file(), "The receiver mapping survives rollback")
        returned_receiver_inventory = self._read_profile_inspection(receiver_settings)
        self.assertIsNotNone(returned_receiver_inventory["accepted_summary_source"])
        self.assertEqual(next(h for h in returned_receiver_inventory["accepted_summary_source"]["handoffs"]
                              if h["canonical_chat_id"] == target_chat)["outcome"],
                         "B appended assistant outcome")

        with patch.object(workflow, "_runtime"), patch.object(sessions, "request_close"), \
             patch.object(sessions.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(sessions.subprocess, "Popen"):
            source_restored = sessions.start(self.settings, close_apps=True, reopen=True)
        self.assertEqual(source_restored["status"], "restored")
        second_digest = source_restored["publication"]["publication"]["package_manifest_sha256"]
        source_mapping = Path(self.settings["local_root"]) / "SessionMappings" / (second_digest + ".json")
        self.assertTrue(source_mapping.is_file())
        with patch.object(workflow, "_runtime"):
            final_inspection = source_reader.inspect(self.profile, workspace_id=self.workspace_id,
                previous_candidate=sessions._previous(self.settings))
        final_ids = [row["canonical_chat_id"] for row in final_inspection["catalog"]["chats"]]
        self.assertEqual(set(final_ids), {row["canonical_chat_id"] for row in source_p1["catalog"]["chats"]})
        self.assertEqual(len(final_ids), len(set(final_ids)))
        final_package = Path(self.settings["local_root"]) / "SessionPackages" / second_digest
        final_candidate = snapshot._read_json(final_package / snapshot.CHAT_BUNDLE_PATH)
        final_results = final_candidate["source"]["results"]
        self.assertEqual(len(final_results), 1)
        self.assertEqual(len({row["result_id"] for row in final_results}), len(final_results))
        installed_result = next(Path(self.settings["projects_root"]).rglob(final_results[0]["name"]))
        self.assertEqual(installed_result.read_bytes(), original_result_bytes)

        with patch.object(workflow, "_runtime"), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()):
            rollback = workflow.rollback(self.settings, self.root / "cycle-rollback", queue.Queue())
        self.assertEqual(rollback["status"], "rolled_back")
        self.assertEqual(snapshot._hash_file(self.profile / "state_5.sqlite")[0], original_state_hash)
        self.assertFalse((self.profile / summaries.IDENTITY_FILE).exists())
        self.assertTrue(source_mapping.is_file(), "The pinned result mapping survives rollback")

        returned_inventory = self._read_profile_inspection(self.settings)
        prior_returned = returned_inventory["accepted_summary_source"]
        self.assertIsNotNone(prior_returned)
        returned_handoff = next(h for h in prior_returned["handoffs"] if h["canonical_chat_id"] == target_chat)
        self.assertEqual(returned_handoff["outcome"], "A checkpoint P2")
        returned_source = self._continue_source(prior_returned, returned_inventory,
            target_chat=target_chat, outcome="A completed after rollback")
        returned_request = sessions.prepare(self.settings, self.settings_path,
            {"inspection":returned_inventory["inspection"],"source":returned_source,"include_environment":False})
        with patch.object(workflow, "_runtime"), patch.object(sessions.trial_guard, "wait_for_quiet", return_value=QuietGuard()), \
             patch.object(workflow.trial_guard, "wait_for_quiet", return_value=QuietGuard()):
            returned_finished = sessions.finish(returned_request["request"], returned_request["request_sha256"],close_apps=False)
        self.assertEqual(returned_finished["publication"]["status"], "published")
        returned_candidate = snapshot._read_json(Path(returned_finished["publication"]["package"]) / snapshot.CHAT_BUNDLE_PATH)
        returned_summary = next(h for h in returned_candidate["source"]["handoffs"] if h["canonical_chat_id"] == target_chat)
        self.assertEqual(returned_summary["outcome"], "A completed after rollback")



if __name__ == "__main__":
    unittest.main()
