from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
import zipfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "clipboard"
REPO = Path(__file__).resolve().parents[3]
JS_FILES = [
    "auth-gate.js",
    "site-switch.js",
    "clipboard-client.js",
    "clipboard-client-local.js",
    "archive.js",
    "archive-output.js",
    "downloads.js",
    "destructive.js",
    "bulk.js",
    "toolbar.js",
    "view.js",
    "compose.js",
    "app.js",
]
CSS_FILES = ["theme.css", "styles.css", "components.css", "toolbar.css"]
FRONTEND_FILES = ["index.html", *CSS_FILES, *JS_FILES]
GONE = ["storage.js", "selection-menu.js", "selection-menu.css", "compact-layout.css"]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class Parser(HTMLParser):
    pass


class StopLineTests(unittest.TestCase):
    def test_every_frontend_file_stays_below_the_stop_line(self) -> None:
        for name in FRONTEND_FILES:
            lines = read(name).splitlines()
            self.assertLess(len(lines), 300, f"{name} crossed the 300-line stop line")

    def test_superseded_files_are_gone(self) -> None:
        for name in GONE:
            self.assertFalse((ROOT / name).exists(), f"{name} should have been deleted")

    def test_javascript_syntax(self) -> None:
        for name in JS_FILES:
            subprocess.run(
                ["node", "--check", str(ROOT / name)], check=True, capture_output=True, text=True, encoding="utf-8"
            )


class LayoutContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = read("index.html")
        Parser().feed(self.html)

    def test_required_markers(self) -> None:
        for marker in [
            'data-auth-state="pending"',
            'src="./auth-gate.js"',
            'src="./clipboard-client.js"',
            'src="./clipboard-client-local.js"',
            'href="./theme.css"',
            'id="mode-plate"',
            'id="bulk-trigger"',
            'id="bulk-panel"',
            'id="search-input"',
            'id="theme-toggle"',
        ]:
            self.assertIn(marker, self.html)

    def test_no_pagination_survives(self) -> None:
        for marker in ['id="prev-page"', 'id="next-page"', 'id="page-label"', "上一页", "下一页"]:
            self.assertNotIn(marker, self.html)

    def test_the_count_chip_is_the_only_bulk_entry(self) -> None:
        self.assertNotIn('id="selection-menu"', self.html)
        self.assertNotIn('class="selection-bar"', self.html)
        self.assertNotIn('id="clip-total"', self.html)

    def test_bulk_panel_offers_exactly_three_actions(self) -> None:
        start = self.html.index('<div id="bulk-panel"')
        end = self.html.index("</div>", self.html.index("bulk-destroy"))
        panel = self.html[start:end]
        self.assertEqual(panel.count("<button"), 3)
        for label in ["全部复制", "全部下载", "全部焚毁"]:
            self.assertIn(label, panel)

    def test_the_plate_shows_one_character_and_toggles(self) -> None:
        plate = re.search(r'<button id="mode-plate".*?</button>', self.html, re.S).group(0)
        body = re.sub(r"<[^>]+>", "", plate).strip()
        self.assertEqual(body, "临")
        self.assertIn('aria-pressed="false"', plate)
        # The star is produced by script on toggle, never rendered alongside it.
        self.assertNotIn("★", plate)
        self.assertIn('plate.textContent = favorite ? "★" : "临"', read("toolbar.js"))

    def test_only_one_site_face_is_rendered(self) -> None:
        self.assertEqual(self.html.count("clip brain"), 1)
        self.assertNotIn("mastodon</span>", self.html)
        self.assertIn('class="lockup" href="/"', self.html)

    def test_search_carries_no_prompt_text(self) -> None:
        search = re.search(r'<input id="search-input".*?>', self.html, re.S).group(0)
        self.assertNotIn("placeholder", search)
        self.assertIn("Alt Space", self.html)

    def test_cards_show_a_countdown_but_no_creation_date(self) -> None:
        template = self.html[self.html.index('<template id="clip-template"') :]
        self.assertIn('class="countdown"', template)
        self.assertIn('class="ttl"', template)
        self.assertNotIn("clip-created", template)
        self.assertNotIn("<time", template)

    def test_no_copy_button_because_double_click_copies(self) -> None:
        self.assertNotIn('class="copy-button"', self.html)
        self.assertIn('clipList.addEventListener("dblclick"', read("app.js"))


class ThemeContractTests(unittest.TestCase):
    def test_both_theme_token_sets_exist(self) -> None:
        theme = read("theme.css")
        self.assertIn(":root {", theme)
        self.assertIn(':root[data-theme="dark"]', theme)
        light = theme[theme.index(":root {") : theme.index(':root[data-theme="dark"]')]
        dark = theme[theme.index(':root[data-theme="dark"]') :]
        for token in ["--bg", "--text", "--surface", "--line", "--teal", "--coral"]:
            self.assertIn(token, light, f"{token} missing from the light set")
            self.assertIn(token, dark, f"{token} missing from the dark set")

    def test_dark_mode_avoids_pure_black_and_pure_white(self) -> None:
        dark = read("theme.css")
        dark = dark[dark.index(':root[data-theme="dark"]') :]
        self.assertNotIn("#000", dark)
        self.assertNotIn("#FFFFFF", dark.upper().replace("#FFFDF6", ""))

    def test_bulk_chip_differs_between_rest_and_expanded(self) -> None:
        """A previous draft made dark mode yellow at rest, so opening it
        changed nothing at all."""
        toolbar = read("toolbar.css")
        rest = toolbar[toolbar.index(".count-chip {") : toolbar.index(".count-chip:hover")]
        expanded = toolbar[toolbar.index(".count-chip:hover") : toolbar.index(".bulk-panel {")]
        self.assertIn("background: var(--input)", rest)
        self.assertIn("background: var(--yellow)", expanded)


class BehaviourContractTests(unittest.TestCase):
    def test_no_native_confirm_or_animation(self) -> None:
        scripts = "".join(read(name) for name in JS_FILES)
        css = "".join(read(name) for name in CSS_FILES).lower()
        self.assertNotIn("window.confirm", scripts)
        self.assertNotIn("transition:", css)
        self.assertNotIn("animation:", css)

    def test_scrollbars_are_hidden_and_regions_still_scroll(self) -> None:
        styles = read("styles.css")
        self.assertIn("scrollbar-width: none", styles)
        self.assertIn("::-webkit-scrollbar { width: 0", styles)
        self.assertIn("overflow-y: auto", styles)
        self.assertIn("overflow-y: auto", read("components.css"))

    def test_backend_mode_never_uses_indexeddb(self) -> None:
        backend = read("clipboard-client.js")
        self.assertNotIn("indexedDB", backend)
        self.assertIn('if (window.ClipAuth.isLocalDemo()) return;', backend)
        local = read("clipboard-client-local.js")
        self.assertIn("if (!window.ClipAuth.isLocalDemo()) return;", local)
        self.assertIn("indexedDB", local)

    def test_the_page_never_persists_a_token(self) -> None:
        scripts = "".join(read(name) for name in JS_FILES)
        for raw in scripts.splitlines():
            line = raw.strip()
            if line.startswith("//") or line.startswith("*"):
                continue
            if "localStorage" in line or "sessionStorage" in line:
                self.assertIn("THEME_KEY", line, f"only the theme may be persisted: {line}")

    def test_api_failure_shows_an_offline_state_instead_of_a_stale_list(self) -> None:
        app = read("app.js")
        self.assertIn("state.offline = true", app)
        self.assertIn("state.clips = []", app)
        self.assertIn('id="offline-state"', read("index.html"))

    def test_bulk_actions_target_everything_only_when_nothing_is_ticked(self) -> None:
        app = read("app.js")
        target = app[app.index("function targets()") : app.index("function deriveTopics")]
        self.assertIn("if (!state.selected.size) return state.clips;", target)
        self.assertIn("state.selected.has(clip.id)", target)

    def test_delete_many_always_names_its_ids(self) -> None:
        self.assertIn("entry_ids: ids", read("clipboard-client.js"))


class RouteContractTests(unittest.TestCase):
    def test_site_routes_and_csp(self) -> None:
        gate = read("auth-gate.js")
        switch = read("site-switch.js")
        compose = (REPO / "compose.yml").read_text(encoding="utf-8")
        nginx = (REPO / "nginx" / "default.conf").read_text(encoding="utf-8")
        self.assertIn('fetch("/",', gate)
        self.assertIn('window.location.replace("/auth/sign_in")', gate)
        self.assertIn('window.location.port === "4173"', gate)
        self.assertIn('const TARGET = "/clipboard/"', switch)
        self.assertIn("./demos/clip-brain/clipboard:/srv/clip-brain:ro", compose)
        self.assertIn("location = /clipboard", nginx)
        self.assertIn("location ^~ /clipboard/", nginx)
        self.assertIn("location ^~ /clipboard-api/", nginx)
        self.assertIn("frame-ancestors 'none'", nginx)
        self.assertNotIn("/clipboard/share", nginx)

    def test_clipboard_api_body_cap_exceeds_the_entry_limit(self) -> None:
        nginx = (REPO / "nginx" / "default.conf").read_text(encoding="utf-8")
        block = nginx[nginx.index("location ^~ /clipboard-api/") :]
        block = block[: block.index("\n  }")]
        size = block.split("client_max_body_size", 1)[1].split(";", 1)[0].strip()
        self.assertTrue(size.endswith("m"))
        self.assertGreater(int(size[:-1]) * 1024**2, 1024**3)


class ArchiveTests(unittest.TestCase):
    def test_streaming_zip_is_readable_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "smoke.zip"
            run = subprocess.run(
                ["node", str(Path(__file__).with_name("archive-smoke.mjs")), str(output)],
                check=True, capture_output=True, text=True, encoding="utf-8",
            )
            result = json.loads(run.stdout)
            self.assertTrue(result["strictLimitPassed"])
            self.assertLess(result["totalBytes"], 1024**3)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                self.assertEqual(len(names), 3)
                self.assertTrue(all("/../" not in name for name in names))
                report = next(name for name in names if name.endswith("/report.txt"))
                self.assertEqual(archive.read(report), b"abc")

    def test_archive_delivery_contract(self) -> None:
        run = subprocess.run(
            ["node", str(Path(__file__).with_name("archive-output-smoke.mjs"))],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        result = json.loads(run.stdout)
        self.assertEqual(result["browserDelivery"], "browser")
        self.assertEqual(result["directDelivery"], "direct")
        self.assertEqual(result["threshold"], 256 * 1024**2)

    def test_bulk_action_contract(self) -> None:
        run = subprocess.run(
            ["node", str(Path(__file__).with_name("bulk-smoke.mjs"))],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        result = json.loads(run.stdout)
        self.assertFalse(result["hasFilesTextOnly"])
        self.assertTrue(result["hasFilesWithFile"])
        self.assertEqual(result["removedIds"], ["a", "b"])
        self.assertTrue(result["strictLimitPassed"])


if __name__ == "__main__":
    unittest.main()
