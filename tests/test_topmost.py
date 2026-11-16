"""Tester för att widgeten ligger överst. Kör med: python -m unittest discover tests"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ["CLAUDE_USAGE_LANG"] = "sv"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tray as app  # noqa: E402

TOP = app.WS_EX_TOPMOST


class CoveringWindowTests(unittest.TestCase):
    def find(self, styles: dict, above: list):
        """styles: hwnd -> exstyle. above: fönstren ovanför widgeten (hwnd 1), närmast först."""
        chain = dict(zip([1] + above, above + [None]))
        with mock.patch.object(app.user32, "GetWindowLongPtrW", side_effect=lambda h, _i: styles[h]), \
                mock.patch.object(app.user32, "GetWindow", side_effect=lambda h, _c: chain[h]), \
                mock.patch.object(app.user32, "IsWindowVisible", return_value=True):
            return app.covering_window(1)

    def test_on_top(self):
        self.assertIsNone(self.find({1: TOP}, []))

    def test_other_topmost_windows_above_are_fine(self):
        self.assertIsNone(self.find({1: TOP, 2: TOP, 3: TOP}, [2, 3]))

    def test_normal_window_above_is_reported(self):
        self.assertEqual(self.find({1: TOP, 2: TOP, 3: 0}, [2, 3]), 3)

    def test_lost_topmost_flag_is_reported(self):
        self.assertEqual(self.find({1: 0}, []), 1)


class SetTopmostTests(unittest.TestCase):
    def test_never_activates_moves_or_resizes(self):
        with mock.patch.object(app.user32, "SetWindowPos") as swp:
            app.set_topmost(42)
        swp.assert_called_once_with(
            42, app.HWND_TOPMOST, 0, 0, 0, 0, app.SWP_NOMOVE | app.SWP_NOSIZE | app.SWP_NOACTIVATE)


class TickTests(unittest.TestCase):
    def make_widget(self, cover):
        w = app.Widget.__new__(app.Widget)
        w.win = mock.Mock()
        w.win.state.return_value = "normal"
        w.win.wm_frame.return_value = "0x1"
        w.last_cover = None
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(app, "covering_window", return_value=cover).start()
        mock.patch.object(app, "window_class", return_value="Chrome_WidgetWin_1").start()
        self.set_topmost = mock.patch.object(app, "set_topmost").start()
        self.log = mock.patch.object(app, "log").start()
        return w

    def test_covered_widget_is_raised_and_logged_once(self):
        w = self.make_widget(cover=99)
        with mock.patch.object(app, "exclusive_fullscreen", return_value=False):
            w._topmost_tick()
            w._topmost_tick()
        self.assertEqual(self.set_topmost.call_count, 2)
        self.log.assert_called_once()
        self.assertIn("Chrome_WidgetWin_1", self.log.call_args.args[0])
        w.win.after.assert_called_with(app.TOPMOST_CHECK_MS, w._topmost_tick)

    def test_nothing_happens_when_on_top(self):
        w = self.make_widget(cover=None)
        with mock.patch.object(app, "exclusive_fullscreen", return_value=False):
            w._topmost_tick()
        self.set_topmost.assert_not_called()

    def test_exclusive_fullscreen_is_left_alone(self):
        w = self.make_widget(cover=99)
        with mock.patch.object(app, "exclusive_fullscreen", return_value=True):
            w._topmost_tick()
        self.set_topmost.assert_not_called()

    def test_hidden_widget_is_left_alone(self):
        w = self.make_widget(cover=99)
        w.win.state.return_value = "withdrawn"
        w._topmost_tick()
        self.set_topmost.assert_not_called()

    def test_error_is_logged_and_timer_continues(self):
        w = self.make_widget(cover=99)
        with mock.patch.object(app, "exclusive_fullscreen", side_effect=OSError("boom")):
            w._topmost_tick()
        self.log.assert_called_once()
        w.win.after.assert_called_once()


class ShowOffScreenTests(unittest.TestCase):
    def show(self, on_screen: bool, x: int, y: int):
        w = app.Widget.__new__(app.Widget)
        w.x, w.y, w.scale = x, y, 1.0
        w.win = mock.Mock()
        for name in ("_layout", "_sync_scale", "_apply_border", "keep_on_top"):
            setattr(w, name, mock.Mock())
        with mock.patch.object(app, "is_on_a_monitor", return_value=on_screen), \
                mock.patch.object(app.Widget, "_default_position", return_value=(100, 200)), \
                mock.patch.object(app, "style_as_tool_window"), \
                mock.patch.object(app, "round_window_corners"), \
                mock.patch.object(app, "log"):
            w.show()
        return w

    def test_widget_outside_all_screens_moves_to_primary(self):
        w = self.show(on_screen=False, x=-5000, y=-5000)
        self.assertEqual((w.x, w.y), (100, 200))
        w._layout.assert_called_once()
        w.keep_on_top.assert_called_once()

    def test_widget_on_a_screen_stays(self):
        w = self.show(on_screen=True, x=10, y=20)
        self.assertEqual((w.x, w.y), (10, 20))
        w._layout.assert_not_called()
        w.keep_on_top.assert_called_once()


if __name__ == "__main__":
    unittest.main()
