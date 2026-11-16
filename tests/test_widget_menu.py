"""Tester för den gemensamma menyn och widgetens högerklick. Kör med: python -m unittest discover tests"""

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ["CLAUDE_USAGE_LANG"] = "sv"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tray as app  # noqa: E402


def make_app(**settings) -> app.App:
    a = app.App.__new__(app.App)
    a.settings = {"theme": "system", "visible": True, "locked": False, "notifications": True, **settings}
    a.data = {"session": None, "week": None}
    a.error = None
    a.updated = None
    a.ui_queue = mock.Mock()
    a.icons = {}
    a.ready = {k: threading.Event() for k in app.App.KEYS}
    a.ready["session"].set()
    a.menu_waiting = False
    a.stop_evt = threading.Event()
    return a


class SharedMenuTests(unittest.TestCase):
    def texts(self, a):
        """Raderna som syns i menyn (pystray hoppar över rader med visible=False)."""
        return [item.text for item in a._build_menu() if item.text]

    def test_one_menu_with_hide_widget_and_lock(self):
        texts = self.texts(make_app())
        self.assertIn("Dölj widget", texts)
        self.assertNotIn("Visa widget", texts)
        self.assertIn("Lås position", texts)
        self.assertNotIn("Stäng widget", texts)

    def test_widget_line_follows_visibility(self):
        texts = self.texts(make_app(visible=False))
        self.assertIn("Visa widget", texts)
        self.assertNotIn("Dölj widget", texts)

    def test_lock_check_shows_current_value(self):
        a = make_app()
        lock = next(item for item in a._build_menu().items if item.text == "Lås position")
        self.assertFalse(lock.checked)
        a.settings["locked"] = True
        self.assertTrue(lock.checked)       # läses när menyn visas, inte när den byggs


class LeftClickTests(unittest.TestCase):
    def left_click(self, a):
        """Samma anrop som pystray gör vid vänsterklick, sedan körs det som köades till Tk-tråden."""
        a.widget = mock.Mock()
        a._build_menu()(mock.Mock())
        a.ui_queue.put.assert_called_once()
        a.ui_queue.put.call_args.args[0]()
        return a.widget

    def test_show_widget_is_the_default_item(self):
        # pystray kör menyns standardval vid vänsterklick på ikonen
        for visible in (True, False):
            items = make_app(visible=visible)._build_menu().items
            self.assertEqual([item.text for item in items if item.default], ["Visa widget"])

    def test_bold_default_only_when_widget_is_hidden(self):
        # Fetstilen visas bara på synliga rader. "Dölj widget" ska aldrig vara fetstilad.
        self.assertEqual([i.text for i in make_app(visible=True)._build_menu() if i.default], [])
        self.assertEqual([i.text for i in make_app(visible=False)._build_menu() if i.default], ["Visa widget"])

    def test_left_click_shows_a_hidden_widget(self):
        widget = self.left_click(make_app(visible=False))
        widget.set_visible.assert_called_once_with(True, mock.ANY)

    def test_left_click_never_hides_a_visible_widget(self):
        widget = self.left_click(make_app(visible=True))
        widget.set_visible.assert_called_once_with(True, mock.ANY)

    def test_hide_widget_in_menu_hides_it(self):
        a = make_app()
        a.widget = mock.Mock()
        hide = next(item for item in a._build_menu() if item.text == "Dölj widget")
        hide(mock.Mock())
        a.ui_queue.put.call_args.args[0]()
        a.widget.set_visible.assert_called_once_with(False, "Dölj widget i menyn")


class VisibilityLogTests(unittest.TestCase):
    def make_widget(self, visible=True):
        w = app.Widget.__new__(app.Widget)
        w.settings = {"visible": visible}
        w.win = mock.Mock()
        w.show = mock.Mock()
        w._tip_cancel = mock.Mock()
        w._sync_close_button = mock.Mock()
        self.addCleanup(mock.patch.stopall)
        self.log = mock.patch.object(app, "log").start()
        self.save = mock.patch.object(app, "save_settings").start()
        return w

    def test_hiding_logs_the_reason_and_saves(self):
        w = self.make_widget()
        w.set_visible(False, "✕ på widgeten")
        w.win.withdraw.assert_called_once()
        self.log.assert_called_once_with("Widgeten döljs (✕ på widgeten)")
        self.assertFalse(w.settings["visible"])
        self.save.assert_called_once()

    def test_showing_logs_the_reason_and_raises(self):
        w = self.make_widget(visible=False)
        w.set_visible(True, "en ny kopia startades")
        w.show.assert_called_once()         # show lägger widgeten överst
        self.log.assert_called_once_with("Widgeten visas (en ny kopia startades)")
        self.assertTrue(w.settings["visible"])

    def test_close_button_hides_with_reason(self):
        w = self.make_widget()
        w.close_btn = mock.Mock()
        w.close_btn.winfo_width.return_value = 10
        w.close_btn.winfo_height.return_value = 10
        w.set_visible = mock.Mock()
        self.assertEqual(w._close_clicked(mock.Mock(x=5, y=5)), "break")
        w.set_visible.assert_called_once_with(False, "✕ på widgeten")


class ShowMenuTests(unittest.TestCase):
    def test_posts_right_click_to_session_icon(self):
        a = make_app()
        a.icons = {"session": mock.Mock(_hwnd=1234), "week": mock.Mock(_hwnd=5678)}
        with mock.patch.object(app.pystray_win32, "PostMessage") as post:
            a.show_menu()
        post.assert_called_once_with(1234, app.pystray_win32.WM_NOTIFY, 0, app.pystray_win32.WM_RBUTTONUP)

    def test_early_click_waits_for_the_icon(self):
        a = make_app()
        a.ready["session"].clear()
        posted = threading.Event()
        with mock.patch.object(app.pystray_win32, "PostMessage", side_effect=lambda *_: posted.set()) as post, \
                mock.patch.object(app, "log") as log:
            a.show_menu()
            a.show_menu()       # ett andra tidigt klick köas inte en gång till
            self.assertFalse(posted.wait(0.2))
            a.icons = {"session": mock.Mock(_hwnd=1234)}
            a.ready["session"].set()
            self.assertTrue(posted.wait(2))
        post.assert_called_once()
        self.assertIn("innan menyn var klar", log.call_args_list[0].args[0])

    def test_icon_without_window_is_logged(self):
        a = make_app()
        a.icons = {"session": mock.Mock(_hwnd=None)}
        with mock.patch.object(app.pystray_win32, "PostMessage") as post, mock.patch.object(app, "log") as log:
            a.show_menu()
        post.assert_not_called()
        log.assert_called_once()


class OpenMenuGuardTests(unittest.TestCase):
    RBUTTONUP = app.pystray_win32.WM_RBUTTONUP

    def make_icon(self):
        icon = app.TrayIcon.__new__(app.TrayIcon)     # utan fönster och ikon i Windows
        icon._running = False       # för pystrays __del__
        icon._icon_handle = None
        icon._menu_open = False
        icon._on_menu_closed = mock.Mock()
        icon.update_menu = mock.Mock()
        return icon

    def test_right_click_while_menu_is_open_is_ignored(self):
        icon = self.make_icon()
        shown = []

        def show_menu(_self, wparam, lparam):
            shown.append(lparam)
            icon._on_notify(0, self.RBUTTONUP)      # högerklick på widgeten medan menyn visas
        with mock.patch.object(app.pystray.Icon, "_on_notify", show_menu):
            icon._on_notify(0, self.RBUTTONUP)
        self.assertEqual(shown, [self.RBUTTONUP])           # menyn visades en gång
        icon.update_menu.assert_called_once()               # och byggdes inte om medan den visades
        icon._on_menu_closed.assert_called_once()
        self.assertFalse(icon._menu_open)

    def test_menu_can_open_again_after_an_error(self):
        icon = self.make_icon()
        with mock.patch.object(app.pystray.Icon, "_on_notify", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                icon._on_notify(0, self.RBUTTONUP)
        self.assertFalse(icon._menu_open)

    def test_other_messages_pass_through(self):
        icon = self.make_icon()
        with mock.patch.object(app.pystray.Icon, "_on_notify") as base:
            icon._on_notify(0, app.pystray_win32.WM_LBUTTONUP)
        base.assert_called_once()
        icon.update_menu.assert_not_called()
        icon._on_menu_closed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
