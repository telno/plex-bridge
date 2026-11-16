#!/usr/bin/env python3
"""Tests for ops/render-report.py:  python3 ops/test_render_report.py"""
import importlib.util, os, unittest

spec = importlib.util.spec_from_file_location("render_report", os.path.join(os.path.dirname(os.path.abspath(__file__)), "render-report.py"))
R = importlib.util.module_from_spec(spec); spec.loader.exec_module(R)

SAMPLE = """# Plex bridge daily review 2026-01-02

STATUS: fixed

Job `ab12` failed: **exit 187**, see [docs](https://example.com/x).

- first item
- second with `code`

| Job | Exit |
|---|---|
| ab12 | 187 |

```
git log --oneline
```

## Digest

Jobs: 3
Health: ok
"""


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.text, self.runs = R.render(SAMPLE)
        self.paras = self.text.split("\n")

    def run_for(self, needle, whole=True):
        n = next(k for k, p in enumerate(self.paras, 1) if needle in p)
        return [r for r in self.runs if r[0] == n and (r[1] == 0) == whole]

    def test_header(self):
        self.assertEqual(self.paras[0], "Plex bridge daily review 2026-01-02")
        self.assertEqual(self.paras[1], "FIXED")
        self.assertEqual(self.runs[0][3:], (R.BOLD, 20, "-"))
        self.assertEqual(self.runs[1][3:], (R.BOLD, 14, R.STATUS["fixed"]))
        self.assertNotIn("STATUS:", self.text)

    def test_markdown_syntax_is_gone(self):
        for junk in ("**", "`", "```", "|---", "## ", "]("):
            self.assertNotIn(junk, self.text)
        self.assertIn("Job ab12 failed: exit 187, see docs (https://example.com/x).", self.paras)
        self.assertIn("  •  first item", self.paras)
        self.assertIn("DIGEST", self.paras)
        self.assertIn("Jobs: 3", self.paras); self.assertIn("Health: ok", self.paras)

    def test_inline_spans_point_at_the_right_characters(self):
        line = "Job ab12 failed: exit 187, see docs (https://example.com/x)."
        spans = {line[a - 1:b]: font for _, a, b, font, _, _ in self.run_for("Job ab12 failed", whole=False)}
        self.assertEqual(spans, {"ab12": R.MONO, "exit 187": R.BOLD})
        item = next(p for p in self.paras if "second with" in p)
        (_, a, b, font, _, _), = self.run_for("second with", whole=False)
        self.assertEqual((item[a - 1:b], font), ("code", R.MONO))

    def test_table_and_code_are_fixed_width(self):
        self.assertEqual(self.run_for("Job    Exit")[0][3], R.MONOBOLD)
        self.assertEqual(self.run_for("ab12   187")[0][3], R.MONO)
        self.assertEqual(self.run_for("git log --oneline")[0][3], R.MONO)

    def test_every_run_targets_a_non_empty_paragraph(self):
        for n, a, b, *_ in self.runs:
            self.assertTrue(self.paras[n - 1].strip())
            self.assertLessEqual(b, len(self.paras[n - 1]))

    def test_status_variants(self):
        self.assertEqual(R.render("# t\n\n**STATUS: all clear**\n\nok")[0].split("\n")[1], "ALL CLEAR")
        self.assertEqual(R.render("just text")[0].split("\n")[1], "NO STATUS")
        # only the first status line is the headline; the digest's own "Status:" line stays in the body
        self.assertIn("Status: clean", R.render("STATUS: fixed\n\n**Status: clean**")[0])


if __name__ == "__main__":
    unittest.main()
