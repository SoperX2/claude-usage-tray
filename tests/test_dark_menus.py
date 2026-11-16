"""Tester för mörka menyer. Kör med: python -m unittest discover tests"""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ["CLAUDE_USAGE_LANG"] = "sv"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tray as app  # noqa: E402


class DarkMenuTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(app, "_flush_menu_themes", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def enable(self, build: int, windll=None):
        with mock.patch.object(app.sys, "getwindowsversion", return_value=SimpleNamespace(build=build)), \
                mock.patch.object(app.ctypes, "WinDLL", windll or mock.MagicMock()) as dll, \
                mock.patch.object(app, "log") as log:
            app.enable_dark_menus()
        return dll, log

    def test_calls_both_functions_by_ordinal(self):
        dll, log = self.enable(22631)
        uxtheme = dll.return_value
        uxtheme.__getitem__.assert_any_call(135)
        uxtheme.__getitem__.assert_any_call(136)
        uxtheme.__getitem__.return_value.assert_any_call(1)     # AllowDark
        log.assert_not_called()
        self.assertIsNotNone(app._flush_menu_themes)

    def test_too_old_windows_is_skipped(self):
        dll, log = self.enable(17763)       # Windows 10 1809
        dll.assert_not_called()
        log.assert_called_once()
        self.assertIsNone(app._flush_menu_themes)

    def test_missing_function_logs_and_continues(self):
        broken = mock.MagicMock()
        broken.return_value.__getitem__.side_effect = AttributeError("function ordinal 135 not found")
        _dll, log = self.enable(22631, broken)
        log.assert_called_once()
        self.assertIn("förblir ljusa", log.call_args.args[0])
        self.assertIsNone(app._flush_menu_themes)
        app.flush_menu_themes()     # gör inget och kastar inget

    def test_failing_flush_only_logs(self):
        app._flush_menu_themes = mock.Mock(side_effect=OSError("boom"))
        with mock.patch.object(app, "log") as log:
            app.flush_menu_themes()
        log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
