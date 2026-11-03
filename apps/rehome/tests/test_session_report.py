import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from rehome_session_report import Timings, make_report, render_html


def row(category="chats", ident="c1", name="Chat", project_id=None, chat_id=None):
    return {"category": category, "id": ident, "name": name, "project_id": project_id, "chat_id": chat_id}


def outcome(item, status="included", reason=None):
    return {"category": item["category"], "id": item["id"], "status": status, "reason": reason}


class SessionReportTests(unittest.TestCase):
    def test_category_totals_and_report_status(self):
        inventory = [row("projects", "p1"), row("chats", "c1", chat_id="c1"), row("results", "r1"), row("skills", "s1")]
        outcomes = [outcome(inventory[0]), outcome(inventory[1], "skipped", "filtered"),
                    outcome(inventory[2], "error", "failed"), outcome(inventory[3], "pending", "waiting")]
        report = make_report(inventory, outcomes, [{"name": "scan", "elapsed_seconds": 0.25, "status": "passed"}])
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["totals"]["chats"], {"discovered": 1, "included": 0, "skipped": 1, "error": 0, "pending": 0})
        self.assertEqual(report["totals"]["results"]["error"], 1)
        self.assertEqual(report["totals"]["skills"]["pending"], 1)
        self.assertEqual(len(report["items"]), len(inventory))

    def test_status_requires_all_rows_and_no_omissions(self):
        item = row()
        self.assertEqual(make_report([item], [outcome(item)], [])["status"], "complete")
        self.assertEqual(make_report([item], [outcome(item, "skipped", "filtered")], [])["status"], "complete_with_omissions")
        self.assertEqual(make_report([item], [outcome(item, "error", "failed")], [])["status"], "complete_with_omissions")
        self.assertEqual(make_report([item], [outcome(item, "pending", "waiting")], [])["status"], "incomplete")

    def test_invalid_inventory_outcome_mappings_rejected(self):
        item = row()
        with self.assertRaises(ValueError):
            make_report([item, item], [outcome(item)], [])
        with self.assertRaises(ValueError):
            make_report([item], [], [])
        with self.assertRaises(ValueError):
            make_report([item], [outcome(item), outcome(item)], [])
        with self.assertRaises(ValueError):
            make_report([item], [outcome(row(ident="other"))], [])
        malformed = dict(item, unexpected="secret")
        with self.assertRaises(ValueError):
            make_report([malformed], [outcome(item)], [])

    def test_html_escapes_external_strings_and_has_only_controlled_chat_link(self):
        unsafe_chat_id = 'x" onmouseover="evil'
        chat = row(ident=unsafe_chat_id, name="Чат")
        item = row("results", "r1", name='<script>alert("x")</script>', chat_id=unsafe_chat_id)
        report = make_report([chat, item], [outcome(chat), outcome(item, "skipped", '<img src=x onerror="x">')],
                             [{"name": '<b>timing</b>', "elapsed_seconds": 0.1, "status": "passed"}])
        html = render_html(report)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img", html)
        self.assertIn("href=\"#chat-x%22%20onmouseover%3D%22evil\"", html)
        self.assertIn("id=\"chat-x%22%20onmouseover%3D%22evil\"", html)
        self.assertIn("Проекты", html)
        self.assertIn("Завершено с пропусками", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<script>alert", html)
        self.assertIn("Обнаружено", html)
        self.assertIn("Ожидают", html)

    def test_report_does_not_copy_unexpected_identifiers(self):
        item = row()
        data = make_report([item], [outcome(item)], [])
        self.assertEqual(set(data["items"][0]), {"category", "id", "name", "project_id", "chat_id", "status", "reason"})
        with self.assertRaises(ValueError):
            make_report([item], [outcome(item)], [{"name": "ok", "elapsed_seconds": 1, "status": "passed", "token": "secret"}])

    def test_non_included_outcomes_require_a_reason(self):
        item = row()
        for status in ("skipped", "error", "pending"):
            for reason in (None, "", "  "):
                with self.subTest(status=status, reason=reason), self.assertRaises(ValueError):
                    make_report([item], [outcome(item, status, reason)], [])

    def test_non_finite_timings_are_rejected(self):
        item = row()
        for duration in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                make_report([item], [outcome(item)],
                            [{"name": "scan", "elapsed_seconds": duration, "status": "passed"}])

    def test_links_require_existing_chat_and_reuse_single_target(self):
        chat = row(ident="c1", name="Чат")
        linked_a = row("results", "r1", chat_id="c1")
        linked_b = row("results", "r2", chat_id="c1")
        orphan = row("results", "r3", chat_id="missing")
        inventory = [chat, linked_a, linked_b, orphan]
        html = render_html(make_report(inventory, [outcome(item) for item in inventory], []))
        self.assertEqual(html.count('id="chat-c1"'), 1)
        self.assertEqual(html.count('href="#chat-c1"'), 2)
        self.assertNotIn('href="#chat-missing"', html)
        self.assertIn("Чаты", html)
        self.assertIn("Результаты", html)

    def test_timings_records_success_and_exception_then_reraises(self):
        timings = Timings()
        with timings.measure("ok"):
            pass
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with timings.measure("broken"):
                raise RuntimeError("boom")
        self.assertEqual([x["status"] for x in timings.entries], ["passed", "failed"])
        self.assertTrue(all(x["elapsed_seconds"] >= 0 for x in timings.entries))


if __name__ == "__main__":
    unittest.main()
