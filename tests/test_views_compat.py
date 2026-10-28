"""Guards the AstrBot plugin view compatibility contract.

AstrBot 4.24.2 introduced plugin views but scanned only ``pages/`` and injected
only ``window.AstrBotPluginPage``. AstrBot 4.29 renamed the feature to Views:
it scans ``views/`` first but keeps ``pages/`` as an alias, and it injects
``window.AstrBotPluginView`` while keeping the old global as an alias.

Shipping ``pages/`` alone therefore works on 4.24.2 through 4.29.x; shipping
``views/`` alone breaks on 4.24.2 - 4.28. These are filesystem-only assertions
so they run without AstrBot installed.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEW_NAME = "settings"
ENTRY_FILE = "index.html"
BRIDGE_NAMES = ("AstrBotPluginPage", "AstrBotPluginView")

# 4.24.2-4.28 hard-code "pages"; 4.29+ prefer "views" then fall back to "pages".
SCANNED_DIR_NAMES = ("views", "pages")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _view_dirs() -> list[Path]:
    return [
        ROOT / name / VIEW_NAME
        for name in SCANNED_DIR_NAMES
        if (ROOT / name / VIEW_NAME / ENTRY_FILE).is_file()
    ]


class ViewDirectoryLayoutTest(unittest.TestCase):
    def test_exactly_one_scanned_directory_ships_the_view(self) -> None:
        """One copy is enough: pages/ is the only name every release accepts."""
        present = [
            name
            for name in SCANNED_DIR_NAMES
            if (ROOT / name / VIEW_NAME / ENTRY_FILE).is_file()
        ]
        self.assertEqual(
            present,
            ["pages"],
            "the view must live in pages/ only; a duplicate views/ copy would "
            "have to be kept in sync by hand and is not needed because every "
            "4.24.2+ release accepts pages/",
        )

    def test_view_assets_are_present(self) -> None:
        page_root = ROOT / "pages" / VIEW_NAME
        for asset in ("index.html", "app.js", "style.css"):
            with self.subTest(asset=asset):
                self.assertTrue((page_root / asset).is_file())

    def test_no_extra_explorable_entries(self) -> None:
        """AstrBot treats every sub-directory with index.html as a view."""
        stray = [
            child.name
            for child in (ROOT / "pages").iterdir()
            if child.is_dir()
            and child.name != VIEW_NAME
            and (child / ENTRY_FILE).is_file()
        ]
        self.assertEqual(stray, [], f"unexpected extra views: {stray}")


class BridgeBootstrapTest(unittest.TestCase):
    def test_bootstrap_accepts_both_bridge_globals(self) -> None:
        source = _read(ROOT / "pages" / VIEW_NAME / "app.js")
        for name in BRIDGE_NAMES:
            with self.subTest(bridge=name):
                self.assertIn(
                    f"window.{name}",
                    source,
                    f"app.js never reads window.{name}",
                )

    def test_bootstrap_has_same_origin_fallback(self) -> None:
        source = _read(ROOT / "pages" / VIEW_NAME / "app.js")
        self.assertIn("/api/plug/", source)

    def test_relative_asset_urls_only(self) -> None:
        """4.29 serves assets through path tokens; hard-coded URLs break it."""
        html = _read(ROOT / "pages" / VIEW_NAME / "index.html")
        refs = re.findall(r'(?:src|href)="([^"]+)"', html)
        self.assertTrue(refs, "index.html references no assets")
        for ref in refs:
            with self.subTest(ref=ref):
                self.assertFalse(ref.startswith(("http://", "https://")), ref)
                self.assertFalse(ref.startswith("/"), ref)
                self.assertNotIn("asset_token", ref)

    def test_no_hard_coded_asset_token(self) -> None:
        for asset in ("index.html", "app.js"):
            with self.subTest(asset=asset):
                self.assertNotIn("asset_token", _read(ROOT / "pages" / VIEW_NAME / asset))


class ViewI18nTest(unittest.TestCase):
    def test_locale_files_declare_view_metadata(self) -> None:
        i18n_dir = ROOT / ".astrbot-plugin" / "i18n"
        locales = sorted(i18n_dir.glob("*.json"))
        self.assertTrue(locales, "no locale files found")
        for locale in locales:
            with self.subTest(locale=locale.name):
                data = json.loads(_read(locale))
                # 4.24.2-4.28 read pages.<name>.title only.
                self.assertIsInstance(
                    data.get("pages", {}).get(VIEW_NAME),
                    dict,
                    f"{locale.name} is missing pages.{VIEW_NAME}",
                )
                # 4.29 prefers views.<name>.title; both keys keep titles stable.
                self.assertIsInstance(
                    data.get("views", {}).get(VIEW_NAME),
                    dict,
                    f"{locale.name} is missing views.{VIEW_NAME}",
                )

    def test_view_titles_agree_between_keys(self) -> None:
        i18n_dir = ROOT / ".astrbot-plugin" / "i18n"
        for locale in sorted(i18n_dir.glob("*.json")):
            with self.subTest(locale=locale.name):
                data = json.loads(_read(locale))
                self.assertEqual(
                    data["views"][VIEW_NAME].get("title"),
                    data["pages"][VIEW_NAME].get("title"),
                )


class MetadataTest(unittest.TestCase):
    def test_version_was_bumped(self) -> None:
        text = _read(ROOT / "metadata.yaml")
        version = re.search(r"^version:\s*(\S+)", text, re.MULTILINE)
        self.assertIsNotNone(version)
        self.assertNotEqual(version.group(1), "1.3.1")

    def test_astrbot_version_unchanged(self) -> None:
        """The declared floor is the maintainer's call; this change leaves it."""
        text = _read(ROOT / "metadata.yaml")
        spec = re.search(r'^astrbot_version:\s*"?([^"\n]+)"?', text, re.MULTILINE)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.group(1).strip(), ">=4.16")


if __name__ == "__main__":
    unittest.main()
