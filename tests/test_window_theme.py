"""Tester för dialogernas tema (apply_window_theme). Kör med: python -m unittest discover tests"""

import os
import sys
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

os.environ["CLAUDE_USAGE_LANG"] = "sv"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tray as app  # noqa: E402


class WindowThemeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.win = tk.Toplevel(self.root)
        self.win.withdraw()
        frame = tk.Frame(self.win)
        frame.pack()
        self.label = tk.Label(frame, text="Text")
        self.entry = tk.Entry(frame)
        self.button = tk.Button(frame, text="OK")
        for w in (self.label, self.entry, self.button):
            w.pack()
        self.light_defaults = (self.label.cget("bg"), self.entry.cget("bg"), self.button.cget("bg"))

    def apply(self, apps_light: bool):
        with mock.patch.object(app, "apps_are_light", return_value=apps_light), \
                mock.patch.object(app, "set_dark_title_bar") as title_bar:
            app.apply_window_theme(self.win)
        return title_bar

    def test_dark_mode_colours_everything(self):
        title_bar = self.apply(apps_light=False)
        title_bar.assert_called_once_with(self.win, True)
        c = app.DARK_WINDOW
        self.assertEqual(self.win.cget("bg"), c["bg"])
        self.assertEqual((self.label.cget("bg"), self.label.cget("fg")), (c["bg"], c["fg"]))
        self.assertEqual((self.entry.cget("bg"), self.entry.cget("fg")), (c["field"], c["fg"]))
        self.assertEqual(self.entry.cget("insertbackground"), c["fg"])     # markören syns
        self.assertEqual((self.button.cget("bg"), self.button.cget("fg")), (c["button"], c["fg"]))

    def test_light_mode_is_unchanged(self):
        title_bar = self.apply(apps_light=True)
        title_bar.assert_called_once_with(self.win, False)
        self.assertEqual(
            (self.label.cget("bg"), self.entry.cget("bg"), self.button.cget("bg")), self.light_defaults)

    def test_error_is_logged_not_raised(self):
        with mock.patch.object(app, "apps_are_light", side_effect=OSError("registret")), \
                mock.patch.object(app, "log") as log:
            app.apply_window_theme(self.win)
        log.assert_called_once()

    def test_title_bar_falls_back_to_old_attribute(self):
        calls = []

        def dwm(_hwnd, attribute, _value, _size):
            calls.append(attribute)
            return 0 if attribute == 19 else -2147024809     # E_INVALIDARG för 20
        with mock.patch.object(app.dwmapi, "DwmSetWindowAttribute", side_effect=dwm):
            app.set_dark_title_bar(self.win, True)
        self.assertEqual(calls, [20, 19])


if __name__ == "__main__":
    unittest.main()
