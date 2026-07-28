from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "clipboard"
JS_FILES = [
    "storage.js",
    "archive.js",
    "downloads.js",
    "bulk.js",
    "selection-menu.js",
    "view.js",
    "app.js",
]
CSS_FILES = ["styles.css", "components.css", "selection-menu.css"]
FRONTEND_FILES = ["index.html", *CSS_FILES, *JS_FILES]


class Parser(HTMLParser):
    pass


class ClipBrainContractTests(unittest.TestCase):
    def test_frontend_files_stay_below_stop_line(self) -> None:
        for name in FRONTEND_FILES:
            lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
            self.assertLess(len(lines), 300, f"{name} crossed the 300-line stop line")

    def test_javascript_syntax(self) -> None:
        for name in JS_FILES:
            subprocess.run(
                ["node", "--check", str(ROOT / name)],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_html_contract(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        Parser().feed(html)
        for marker in [
            'href="/"',
            'href="/clipboard/"',
            'id="selection-menu"',
            'id="selection-trigger"',
            'id="bulk-actions"',
            'id="bulk-action"',
            'id="bulk-copy-label"',
            'id="bulk-download-label"',
            'id="bulk-destroy"',
            'id="select-page"',
            'href="./selection-menu.css"',
            'src="./selection-menu.js"',
        ]:
            self.assertIn(marker, html)
        action_start = html.index('<div id="bulk-actions"')
        action_end = html.index("</div>", action_start)
        self.assertEqual(html[action_start:action_end].count("<button"), 2)
        self.assertNotIn('id="download-selected"', html)

    def test_delayed_selection_menu_contract(self) -> None:
        script = (ROOT / "selection-menu.js").read_text(encoding="utf-8")
        for marker in [
            "OPEN_DELAY_MS = 260",
            "CLOSE_DELAY_MS = 220",
            'addEventListener("pointerenter"',
            'addEventListener("pointerleave"',
            "setProgress",
            "clearProgress",
            'downloadLabel.classList.add("is-active")',
        ]:
            self.assertIn(marker, script)

    def test_no_animation_contract(self) -> None:
        css = "".join(
            (ROOT / name).read_text(encoding="utf-8") for name in CSS_FILES
        ).lower()
        self.assertNotIn("transition:", css)
        self.assertNotIn("animation:", css)

    def test_streaming_zip_is_readable_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "smoke.zip"
            run = subprocess.run(
                ["node", str(Path(__file__).with_name("archive-smoke.mjs")), str(output)],
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(run.stdout)
            self.assertTrue(result["strictLimitPassed"])
            self.assertLess(result["totalBytes"], 1024**3)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                self.assertEqual(len(names), 3)
                self.assertTrue(any(name.endswith("/text.txt") for name in names))
                self.assertTrue(all("/../" not in name for name in names))
                report = next(name for name in names if name.endswith("/report.txt"))
                self.assertEqual(archive.read(report), b"abc")

    def test_bulk_action_contract(self) -> None:
        run = subprocess.run(
            ["node", str(Path(__file__).with_name("bulk-smoke.mjs"))],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(run.stdout)
        self.assertFalse(result["hasFilesTextOnly"])
        self.assertTrue(result["hasFilesWithFile"])
        self.assertEqual(result["removedIds"], ["a", "b"])
        self.assertTrue(result["strictLimitPassed"])


if __name__ == "__main__":
    unittest.main()
