"""Tester för användningsnotiserna. Kör med: python -m unittest discover tests"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ["CLAUDE_USAGE_LANG"] = "sv"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_usage_tray as app  # noqa: E402

NOW = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)


def window(pct: float, resets_at: str) -> dict:
    return {"utilization": pct, "resets_at": resets_at}


def parse(session: dict | None, week: dict | None = None) -> dict:
    return {"session": app.parse_window(session), "week": app.parse_window(week)}


class NoticeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(app, "SETTINGS_PATH", Path(tmp.name) / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, data: dict, now: datetime = NOW) -> list[str]:
        """Som en körning av appen: läser settings.json, kontrollerar och sparar."""
        settings = app.load_settings()
        messages, settings["notified"] = app.check_notices(data, settings["notified"], now)
        app.save_settings(settings)
        return messages

    def test_microseconds_do_not_repeat(self):
        # Riktiga svar: mikrosekunderna ändras vid varje anrop, ibland över minutgränsen
        first = self.check(parse(window(96, "2026-11-13T01:49:59.963334+00:00")))
        again = self.check(parse(window(96, "2026-11-13T01:50:00.182726+00:00")))
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [])

    def test_not_repeated_after_restart(self):
        data = parse(window(97, "2026-11-13T01:50:00.1+00:00"))
        self.assertEqual(len(self.check(data)), 1)
        # Ny "process": allt läses från settings.json igen
        self.assertEqual(self.check(data, NOW + timedelta(minutes=2)), [])

    def test_once_per_threshold(self):
        self.assertEqual(self.check(parse(window(94.9, "2026-11-13T01:50:00Z"))), [])
        self.assertEqual(len(self.check(parse(window(95, "2026-11-13T01:50:00Z")))), 1)
        self.assertEqual(self.check(parse(window(98, "2026-11-13T01:50:00Z"))), [])
        self.assertEqual(len(self.check(parse(window(99, "2026-11-13T01:50:00Z")))), 1)
        self.assertEqual(self.check(parse(window(100, "2026-11-13T01:50:00Z"))), [])

    def test_jump_past_both_thresholds_gives_one_notice(self):
        self.assertEqual(len(self.check(parse(window(99.5, "2026-11-13T01:50:00Z")))), 1)
        self.assertEqual(self.check(parse(window(99.5, "2026-11-13T01:50:00Z"))), [])

    def test_repeats_after_reset(self):
        self.assertEqual(len(self.check(parse(window(96, "2026-11-13T01:50:00Z")))), 1)
        after_reset = NOW.replace(day=13, hour=2)
        msgs = self.check(parse(window(96, "2026-11-13T06:50:00Z")), after_reset)
        self.assertEqual(len(msgs), 1)
        # Den gamla periodens poster har rensats
        self.assertEqual(app.load_settings()["notified"], ["session|95|2026-11-13T06:50Z"])

    def test_session_and_week_are_separate(self):
        msgs = self.check(parse(window(96, "2026-11-13T01:50:00Z"), window(99, "2026-11-19T09:00:00Z")))
        self.assertEqual(len(msgs), 2)
        self.assertTrue(msgs[0].startswith("Session: 96 % använt, nollställs "))
        self.assertTrue(msgs[1].startswith("Vecka: 99 % använt, nollställs "))

    def test_week_text_includes_weekday(self):
        local = datetime(2026, 11, 19, 10, 0).astimezone()   # torsdag 10:00 lokal tid
        with mock.patch.object(app, "datetime", wraps=datetime) as dt:
            dt.now.return_value = datetime(2026, 11, 12, 12, 0).astimezone()
            msg = app.t("notify_week", pct=99, when=app.fmt_reset(local))
        self.assertEqual(msg, "Vecka: 99 % använt, nollställs tors 10:00")

    def test_broken_entries_are_dropped(self):
        settings = app.load_settings()
        settings["notified"] = ["skräp", 42, "session|95|2020-01-01T00:00Z"]
        app.save_settings(settings)
        self.check(parse(None))
        self.assertEqual(app.load_settings()["notified"], [])


class AuthNoticeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (
            mock.patch.object(app, "SETTINGS_PATH", Path(tmp.name) / "settings.json"),
            mock.patch.object(app, "get_session_key", return_value="test"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = app.App()
        self.app.settings["notifications"] = False     # felnotisen ska visas ändå
        self.app._notify = mock.Mock()
        self.client = mock.Mock()
        self.client.usage.side_effect = app.AuthError("HTTP 401")

    def refresh(self) -> None:
        # _refresh släpper klienten vid AuthError, så samma låtsasklient sätts tillbaka varje gång
        self.app.client, self.app.client_key = self.client, "test"
        self.app._refresh()

    def test_once_per_expiry_and_again_after_success(self):
        self.refresh()
        self.refresh()
        self.refresh()
        self.app._notify.assert_called_once_with(
            "Claude Usage: inloggningen har gått ut. Högerklicka och välj Byt sessionKey.")

        self.client.usage.side_effect = None
        self.client.usage.return_value = {"five_hour": None, "seven_day": None}
        self.refresh()
        self.assertEqual(self.app._notify.call_count, 1)

        self.client.usage.side_effect = app.AuthError("HTTP 403")
        self.refresh()
        self.refresh()
        self.assertEqual(self.app._notify.call_count, 2)

    def test_network_error_does_not_reset(self):
        self.refresh()
        self.client.usage.side_effect = ConnectionError("nätverket nere")
        self.refresh()
        self.client.usage.side_effect = app.AuthError("HTTP 401")
        self.refresh()
        self.app._notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
