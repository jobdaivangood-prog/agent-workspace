"""Pure tests for portable result-link discovery and relocation."""

import copy
import unittest

import rehome_result_links as links


def candidate_with(*messages):
    return {
        "transfer": {
            "projects": [{"id": "p", "name": "Project"}, {"id": "other", "name": "Other"}],
            "threads": [{
                "id": "t",
                "project_id": "p",
                "archived": True,
                "turns": [{
                    "id": "u",
                    "messages": list(messages),
                }],
            }],
        },
    }


def message(ident, text, role="assistant"):
    return {"id": ident, "role": role, "phase": "final_answer", "text": text}


class ResultLinkTests(unittest.TestCase):
    def test_extracts_supported_local_destinations_and_excludes_code_and_other_links(self):
        text = (
            "[Windows](<C:/Users/A/report one.pdf>) [slash drive](/C:/Users/B/report.pdf) "
            "[encoded](file:///C:/Users/C/report%20one.pdf) [POSIX](/mnt/data/out.pdf) "
            "[sandbox](sandbox:/mnt/data/final.pdf). "
            "[relative](docs/report.pdf) [web](https://example.test/a) "
            "[UNC](\\\\server\\share\\report.pdf)\n"
            "`C:/inline/code.pdf`\n"
            "```text\n[code](C:/fenced/code.pdf)\n```"
        )
        found = links.extract(text)
        hrefs = [item["href"] for item in found]
        self.assertEqual(5, len(found))
        self.assertEqual(5, len(set(hrefs)))
        for item in found:
            span = text[item["start"]:item["end"]]
            self.assertIn(span, (item["href"], f"<{item['href']}>"))
            self.assertTrue(item["path"])
        for excluded in ("docs/report.pdf", "https://example.test/a", "\\\\server\\share\\report.pdf",
                         "C:/inline/code.pdf", "C:/fenced/code.pdf"):
            self.assertNotIn(excluded, hrefs)

    def test_extract_spans_include_angle_brackets_when_destination_is_wrapped(self):
        text = "See [report](<C:/Users/A/report (final).pdf>)."
        found = links.extract(text)
        self.assertEqual(1, len(found))
        item = found[0]
        self.assertEqual("<C:/Users/A/report (final).pdf>", text[item["start"]:item["end"]])

    def test_multiline_inline_code_and_fence_language_lines_do_not_expose_links(self):
        text = (
            "``\n[hidden inline](C:/hidden/inline.py)\n``\n"
            "```markdown\n"
            "```python\n[hidden fenced](C:/hidden/fenced.py)\n"
            "```\n"
            "[visible](C:/visible/result.py)"
        )
        found = links.extract(text)
        self.assertEqual(["C:/visible/result.py"], [item["href"] for item in found])

    def test_normalize_href_casefolds_windows_paths(self):
        self.assertEqual(links.normalize_href("C:/Users/A/Report.PDF"),
                         links.normalize_href("c:\\users\\a\\report.pdf"))
        self.assertIsNone(links.normalize_href("https://example.test/report.pdf"))
        self.assertIsNone(links.normalize_href("relative/report.pdf"))
        self.assertIsNone(links.normalize_href("\\\\server\\share\\report.pdf"))

    def test_source_identity_keeps_literal_percent_distinct_from_decoded_uri(self):
        candidate = candidate_with(message(
            "m", "[literal percent](C:/out/a%20b.pdf) [space](<C:/out/a b.pdf>) "
            "[encoded literal percent](file:///C:/out/a%2520b.pdf)"))
        percent = "Projects/p/Results/percent/a.pdf"
        space = "Projects/p/Results/space/a.pdf"
        manifest = links.make_manifest(candidate, {
            percent: {"path": "C:/out/a%20b.pdf"},
            space: {"path": "C:/out/a b.pdf"},
        })
        self.assertEqual([percent, space, percent],
                         [item["relative"] for item in manifest["occurrences"]])
        self.assertNotEqual(links.source_key("C:/out/a%20b.pdf"), links.source_key("C:/out/a b.pdf"))
        self.assertEqual(links.source_key("C:/out/a%20b.pdf"),
                         links.normalize_href("file:///C:/out/a%2520b.pdf"))

    def test_explicit_alias_maps_missing_old_source_to_selected_file(self):
        candidate = candidate_with(message("m", "[old result](C:/legacy/old-name.py)"))
        relative = "Projects/p/Results/current.py"
        manifest = links.make_manifest(candidate, {
            relative: {"path": "C:/selected/current.py", "aliases": ["C:/legacy/old-name.py"]},
        })
        self.assertEqual([relative], [item["relative"] for item in manifest["occurrences"]])
        self.assertEqual(["C:/legacy/old-name.py"], manifest["files"][0]["aliases"])

    def test_projectless_result_uses_reserved_project_binding(self):
        candidate = candidate_with(message("m", "[result](C:/selected/projectless.py)"))
        candidate["transfer"]["threads"][0]["project_id"] = None
        relative = "Projects/__projectless__/Results/projectless.py"
        manifest = links.make_manifest(candidate, {
            relative: {"path": "C:/selected/projectless.py"},
        })
        self.assertEqual([relative], [item["relative"] for item in manifest["occurrences"]])

    def test_collect_scans_assistant_only_and_preserves_occurrence_identity(self):
        candidate = candidate_with(
            message("m1", "[one](C:/src/a.pdf) [again](C:/src/a.pdf)"),
            message("m2", "[user](C:/src/a.pdf)", role="user"),
        )
        occurrences = links.collect(candidate)
        self.assertEqual(2, len(occurrences))
        self.assertEqual(["m1", "m1"], [item["message_id"] for item in occurrences])
        self.assertEqual(["t", "t"], [item["thread_id"] for item in occurrences])
        self.assertEqual(["u", "u"], [item["turn_id"] for item in occurrences])
        for item in occurrences:
            for key in ("project_id", "text_sha256", "start", "end", "href", "path", "key"):
                self.assertIn(key, item)

    def test_manifest_binds_exact_source_and_project_and_handles_duplicate_basenames(self):
        candidate = candidate_with(message(
            "m", "[first](C:/old/one/report.pdf) [second](C:/old/two/report.pdf) "
            "[unselected](C:/old/three/report.pdf)"))
        bindings = {
            "Projects/p/Results/first/report.pdf": {"path": "C:/old/one/report.pdf"},
            "Projects/p/Results/second/report.pdf": {"path": "C:/old/two/report.pdf"},
            "Projects/other/Results/third/report.pdf": {"path": "C:/old/three/report.pdf"},
        }
        manifest = links.make_manifest(candidate, bindings)
        self.assertEqual("rehome-result-links/1", manifest["format"])
        self.assertEqual(2, len(manifest["occurrences"]))
        self.assertEqual({"Projects/p/Results/first/report.pdf",
                          "Projects/p/Results/second/report.pdf"},
                         {item["relative"] for item in manifest["occurrences"]})
        self.assertIsNone(links.validate(manifest, candidate, set(bindings)))

    def test_manifest_leaves_missing_or_unselected_sources_unbound(self):
        candidate = candidate_with(message("m", "[not bound](C:/old/missing.pdf)"))
        manifest = links.make_manifest(candidate, {})
        self.assertEqual([], manifest["files"])
        self.assertEqual([], manifest["occurrences"])
        self.assertIsNone(links.validate(manifest, candidate, set()))

    def test_validate_rejects_occurrence_href_and_structure_tampering(self):
        candidate = candidate_with(message("m", "[result](C:/old/result.pdf)"))
        relative = "Projects/p/Results/one/result.pdf"
        manifest = links.make_manifest(candidate, {relative: {"path": "C:/old/result.pdf"}})
        result_paths = {relative}
        self.assertIsNone(links.validate(manifest, candidate, result_paths))

        for change in ("href", "relative", "text_sha256"):
            tampered = copy.deepcopy(manifest)
            tampered["occurrences"][0][change] = "tampered"
            with self.subTest(change=change), self.assertRaises(ValueError):
                links.validate(tampered, candidate, result_paths)
        tampered = copy.deepcopy(manifest)
        tampered["format"] = "rehome-result-links/unknown"
        with self.assertRaises(ValueError):
            links.validate(tampered, candidate, result_paths)
        with self.assertRaises(ValueError):
            links.validate(manifest, candidate, set())

    def test_derive_rewrites_only_bound_assistant_href_and_preserves_candidate(self):
        original = candidate_with(
            message("m", "Before [result](C:/old/result one.pdf) and https://example.test/a"),
            message("user", "[same](C:/old/result one.pdf)", role="user"),
        )
        candidate = copy.deepcopy(original)
        relative = "Projects/p/Results/one/result one.pdf"
        manifest = links.make_manifest(candidate, {
            relative: {"path": "C:/old/result one.pdf"},
        })
        derived, ledger = links.derive(candidate, manifest, {"p": "/new/project root"})
        assistant_text = derived["transfer"]["threads"][0]["turns"][0]["messages"][0]["text"]
        user_text = derived["transfer"]["threads"][0]["turns"][0]["messages"][1]["text"]
        self.assertIn("</new/project root/Results/one/result one.pdf>", assistant_text)
        self.assertIn("https://example.test/a", assistant_text)
        self.assertEqual(original["transfer"]["threads"][0]["turns"][0]["messages"][0]["text"],
                         candidate["transfer"]["threads"][0]["turns"][0]["messages"][0]["text"])
        self.assertEqual(original["transfer"]["threads"][0]["turns"][0]["messages"][1]["text"], user_text)
        self.assertEqual(original["transfer"]["threads"][0]["archived"],
                         derived["transfer"]["threads"][0]["archived"])
        self.assertEqual(1, len(ledger))
        self.assertEqual(relative, ledger[0]["relative"])


if __name__ == "__main__":
    unittest.main()
