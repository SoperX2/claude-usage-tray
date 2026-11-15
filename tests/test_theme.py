"""Tester för menyvalet Widgetens utseende. Kör med: python -m unittest discover tests"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["CLAUDE_USAGE_LANG"] = "sv"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tray as app  # noqa: E402


class ThemeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settings.json"
        patcher = mock.patch.object(app, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_light_and_dark_ignore_windows(self):
        never = mock.Mock(side_effect=AssertionError("Windows ska inte frågas"))
        self.assertTrue(app.is_light("light", never))
        self.assertFalse(app.is_light("dark", never))

    def test_system_asks_windows(self):
        self.assertTrue(app.is_light("system", lambda: True))
        self.assertFalse(app.is_light("system", lambda: False))

    def test_widget_follows_apps_theme(self):
        # Mörkt aktivitetsfält men ljusa appar, vilket går att välja i Windows
        a = app.App.__new__(app.App)
        a.settings = {"theme": "system"}
        with mock.patch.object(app, "taskbar_is_light", return_value=False), \
                mock.patch.object(app, "apps_are_light", return_value=True):
            self.assertTrue(a._widget_light())
            a.settings["theme"] = "dark"
            self.assertFalse(a._widget_light())

    def test_tray_icons_always_follow_taskbar(self):
        # Valet gäller bara widgeten: ljust val och mörkt aktivitetsfält ger ändå ljusa (vita) ikoner
        a = app.App.__new__(app.App)
        a.settings = {"theme": "light"}
        a.stop_evt = mock.Mock(is_set=lambda: False)
        a.data = {"session": (50.0, None), "week": None}
        a.error = None
        a.updated = None
        a.ui_queue = mock.Mock()
        a.icons = {"session": mock.Mock(), "week": mock.Mock()}
        for taskbar_light in (False, True):
            with mock.patch.object(app, "taskbar_is_light", return_value=taskbar_light), \
                    mock.patch.object(app, "apps_are_light", return_value=not taskbar_light), \
                    mock.patch.object(app, "make_gauge") as gauge:
                a._render()
            self.assertEqual({c.kwargs["light"] for c in gauge.call_args_list}, {taskbar_light})

    def test_default_is_system(self):
        self.assertEqual(app.load_settings()["theme"], "system")

    def test_saved_choice_is_kept(self):
        self.path.write_text(json.dumps({"theme": "light"}), encoding="utf-8")
        self.assertEqual(app.load_settings()["theme"], "light")

    def test_unknown_choice_falls_back_to_system(self):
        self.path.write_text(json.dumps({"theme": "lila"}), encoding="utf-8")
        self.assertEqual(app.load_settings()["theme"], "system")

    def test_tray_menu_has_appearance_submenu(self):
        a = app.App.__new__(app.App)
        a.settings = app.load_settings()
        a.data = {"session": None, "week": None}
        a.error = None
        a.updated = None
        a.ui_queue = mock.Mock()
        menu = a._build_menu()
        sub = next(item for item in menu.items if item.text == "Widgetens utseende").submenu
        self.assertEqual([item.text for item in sub.items], ["Följ Windows", "Ljust", "Mörkt"])
        self.assertEqual([item.checked for item in sub.items], [True, False, False])
        self.assertTrue(all(item.radio for item in sub.items))


if __name__ == "__main__":
    unittest.main()
