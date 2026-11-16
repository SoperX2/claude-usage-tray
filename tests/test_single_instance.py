"""Tester för att bara en kopia av appen kör åt gången. Kör med: python -m unittest discover tests"""

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

from PIL import Image

os.environ["CLAUDE_USAGE_LANG"] = "sv"
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import claude_usage_tray as app  # noqa: E402

# Kör i en egen process: tar mutexen, säger till och väntar tills stdin stängs
HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
import claude_usage_tray as app
handle = app.claim_single_instance(sys.argv[2])
print("held" if handle else "busy", flush=True)
sys.stdin.read()
"""


def unique_mutex_name() -> str:
    return f"Local\\ClaudeUsageTrayTest-{uuid.uuid4()}"     # krockar aldrig med en riktig kopia


class MutexTests(unittest.TestCase):
    def test_second_claim_in_same_session_is_refused(self):
        name = unique_mutex_name()
        first = app.claim_single_instance(name)
        self.addCleanup(app.release_single_instance, first)
        self.assertTrue(first)
        self.assertIsNone(app.claim_single_instance(name))

    def test_released_mutex_can_be_claimed_again(self):
        name = unique_mutex_name()
        app.release_single_instance(app.claim_single_instance(name))
        again = app.claim_single_instance(name)
        self.addCleanup(app.release_single_instance, again)
        self.assertTrue(again)

    def test_mutex_is_released_when_the_process_exits(self):
        name = unique_mutex_name()
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(REPO), name],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertIsNone(app.claim_single_instance(name))     # den andra processen kör
        finally:
            holder.stdin.close()        # processen avslutas utan att släppa mutexen själv
            holder.wait(10)
            holder.stdout.close()
        after = app.claim_single_instance(name)
        self.addCleanup(app.release_single_instance, after)
        self.assertTrue(after)

    def test_failed_mutex_still_starts_the_app(self):
        with mock.patch.object(app._kernel32_err, "CreateMutexW", return_value=None), \
                mock.patch.object(app, "log") as log:
            self.assertEqual(app.claim_single_instance(unique_mutex_name()), 0)
        log.assert_called_once()


class MainTests(unittest.TestCase):
    def run_main(self, claimed, argv=("claude_usage_tray.py",)):
        with mock.patch.object(app, "claim_single_instance", return_value=claimed) as claim, \
                mock.patch.object(app, "ask_running_copy_to_show") as ask, \
                mock.patch.object(app, "release_single_instance") as release, \
                mock.patch.object(app, "App") as app_cls, \
                mock.patch.object(app, "set_dpi_awareness"), mock.patch.object(app, "enable_dark_menus"), \
                mock.patch.object(app.sys, "argv", list(argv)), \
                mock.patch.object(app, "log") as log:
            app.main()
        return claim, ask, release, app_cls, log

    def test_second_copy_asks_the_first_and_exits(self):
        _claim, ask, _release, app_cls, log = self.run_main(claimed=None)
        ask.assert_called_once()
        app_cls.assert_not_called()         # inga ikoner eller fönster
        self.assertIn("körs redan", log.call_args.args[0])

    def test_first_copy_runs_and_releases_the_mutex(self):
        _claim, ask, release, app_cls, _log = self.run_main(claimed=1234)
        ask.assert_not_called()
        app_cls.return_value.run.assert_called_once()
        release.assert_called_once_with(1234)

    def test_login_process_is_not_limited(self):
        claim, ask, _release, app_cls, _log = self.run_main(claimed=None, argv=("x", "--login"))
        claim.assert_not_called()
        ask.assert_not_called()
        app_cls.return_value.run.assert_called_once()


class ShowRequestTests(unittest.TestCase):
    def test_message_is_the_same_in_every_process(self):
        here = app.show_widget_message()
        other = subprocess.run(
            [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(REPO)!r}); "
                                   "import claude_usage_tray as a; print(a.show_widget_message())"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        self.assertTrue(here)
        self.assertEqual(str(here), other)

    def test_session_icon_shows_the_widget(self):
        a = app.App.__new__(app.App)
        a.ui_queue = mock.Mock()
        a.widget = mock.Mock()
        icon = app.TrayIcon("test", Image.new("RGBA", (16, 16)), tray_uid=1,
                            tray_guid=str(uuid.uuid4()), on_show_request=a._show_requested)
        with mock.patch.object(app, "log") as log:
            icon._message_handlers[app.show_widget_message()](0, 0)
        log.assert_called_once()
        task = a.ui_queue.put.call_args.args[0]
        task()                              # körs i Tk-tråden i appen
        a.widget.set_visible.assert_called_once_with(True, "en ny kopia startades")

    def test_week_icon_does_not_listen(self):
        icon = app.TrayIcon("test", Image.new("RGBA", (16, 16)), tray_uid=2, tray_guid=str(uuid.uuid4()))
        self.assertNotIn(app.show_widget_message(), icon._message_handlers)


if __name__ == "__main__":
    unittest.main()
