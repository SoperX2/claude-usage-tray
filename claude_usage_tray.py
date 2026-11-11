"""
Claude Usage Tray
Visar din Claude-användning (session + vecka) som en ikon i Windows systemfält.

Två mätar-ikoner: vänster = session (5 h), höger = vecka.
Hovra för detaljer, vänsterklicka för att uppdatera, högerklicka för meny.

Samma två mätare visas även i en flytande widget som alltid ligger överst.
Dra för att flytta, dubbelklicka för att uppdatera, högerklicka för meny.

OBS: Använder claude.ai:s interna (odokumenterade) API – samma som
inställningssidan själv anropar. Det kan ändras utan förvarning.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import signal
import sys
import threading
import tkinter as tk
import uuid
import webbrowser
import winreg
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import simpledialog

import keyring
import pystray
from curl_cffi import requests
from PIL import Image, ImageDraw, ImageFont, ImageTk
from pystray._util import win32 as pystray_win32

__version__ = "1.0.0"

APP_NAME = "ClaudeUsageTray"
REPO_URL = "https://github.com/SoperX2/claude-usage-tray"
BASE_URL = "https://claude.ai/api"
USAGE_PAGE = "https://claude.ai/settings/usage"
POLL_SECONDS = 120          # hur ofta vi hämtar ny data
NOTIFY_THRESHOLDS = (80, 95)  # notis när sessionen passerar dessa %
PUMP_MS = 100               # hur ofta Tk läser kön från de andra trådarna
SETTINGS_PATH = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME / "settings.json"

# Fasta id:n som Windows kommer ihåg ikonerna med. Ändras de nollställs användarens val
# i "Andra ikoner i systemfältet".
TRAY_IDS = {
    "session": (1, "85133889-541a-4664-953f-9c5a3655af65"),
    "week": (2, "223945e3-9509-47be-9fa2-ca07a8d589c5"),
}


def executable_path() -> str:
    """Exe-filens fullständiga sökväg. realpath ger samma sökväg oavsett om filen nåtts via
    kort 8.3-namn, junction eller symbolisk länk."""
    return os.path.realpath(sys.executable)


def tray_guid(key: str) -> str:
    """GUID för ikonen. Windows knyter en GUID till utgivaren om programmet är signerat
    (python.exe och pythonw.exe), annars till sökvägen. En osignerad exe som flyttats skulle
    då nekas ikonerna, så exe-filen får en egen GUID per sökväg."""
    base = TRAY_IDS[key][1]
    if getattr(sys, "frozen", False):
        return str(uuid.uuid5(uuid.UUID(base), os.path.normcase(executable_path())))
    return base


def log(msg: str) -> None:
    """Skriver till konsolen när appen körs med python (gör inget med pythonw)."""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
#  sessionKey – sparas i Windows Credential Manager, inte i en textfil
# --------------------------------------------------------------------------- #
def get_session_key() -> str | None:
    return keyring.get_password(APP_NAME, "sessionKey")


def set_session_key(key: str) -> None:
    keyring.set_password(APP_NAME, "sessionKey", key)


class AppDialog(simpledialog.Dialog):
    """Bas för appens dialoger, som bara får visas en åt gången. De är modala och tar grab, och en
    andra dialog som öppnas i den förstas händelseloop skulle ta grab från den."""

    current: "AppDialog | None" = None

    def __init__(self, parent, title: str):
        # Visar dialogen och väntar tills den stängs. Anropas bara från Tk-tråden.
        AppDialog.current = self
        try:
            super().__init__(parent, title=title)
        finally:
            AppDialog.current = None

    @staticmethod
    def raise_open() -> bool:
        """Lägger en redan öppen dialog överst. Sant om det fanns en, och då ska ingen ny visas."""
        if AppDialog.current is None:
            return False
        try:
            AppDialog.current.lift()
            AppDialog.current.focus_force()
        except tk.TclError:         # håller på att stängas
            pass
        return True


class SessionKeyDialog(AppDialog):
    """Frågar efter sessionKey. Skapas alltid i Tk-tråden, se App._ask_session_key."""

    def body(self, master):
        self.attributes("-topmost", True)
        tk.Label(master, text=t("key_prompt"), justify="left").pack(anchor="w")
        self.entry = tk.Entry(master, show="*", width=50)
        self.entry.pack(fill="x", pady=(6, 0))
        self.after(50, self.entry.focus_force)
        return self.entry

    def buttonbox(self):
        box = tk.Frame(self)
        tk.Button(box, text=t("ok"), width=10, command=self.ok, default="active").pack(side="left", padx=5, pady=5)
        tk.Button(box, text=t("cancel"), width=10, command=self.cancel).pack(side="left", padx=5, pady=5)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)
        box.pack()

    def apply(self):
        self.result = self.entry.get().strip() or None


class AboutDialog(AppDialog):
    """Version och länk till GitHub. Skapas alltid i Tk-tråden, se App._show_about."""

    def body(self, master):
        self.attributes("-topmost", True)
        self.resizable(False, False)
        tk.Label(master, text="Claude Usage", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(master, text=t("about_version", version=__version__)).pack(anchor="w")
        tk.Label(master, text=t("about_disclaimer"), justify="left").pack(anchor="w", pady=(8, 0))
        return None

    def buttonbox(self):
        box = tk.Frame(self)
        tk.Button(box, text=t("about_github"), command=lambda: webbrowser.open(REPO_URL)).pack(
            side="left", padx=5, pady=5)
        tk.Button(box, text=t("close"), width=10, command=self.cancel, default="active").pack(
            side="left", padx=5, pady=5)
        self.bind("<Return>", self.cancel)
        self.bind("<Escape>", self.cancel)
        box.pack()


# --------------------------------------------------------------------------- #
#  Inställningar – position, synlighet och lås för widgeten
# --------------------------------------------------------------------------- #
def load_settings() -> dict:
    settings = {"x": None, "y": None, "visible": True, "locked": False}
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            settings.update(saved)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log(f"Kunde inte läsa inställningarna, använder standardvärden: {e!r}")
    return settings


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_PATH)   # så att en krasch inte lämnar en halv fil
    except OSError as e:
        log(f"Kunde inte spara inställningarna: {e!r}")


# --------------------------------------------------------------------------- #
#  Starta med Windows – ett värde under HKCU\...\Run. Registret är enda källan,
#  så bocken i menyn stämmer även om användaren ändrat i Aktivitetshanteraren.
# --------------------------------------------------------------------------- #
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Aktivitetshanteraren sparar "Inaktiverad" här, som binärdata med udda första byte
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
AUTOSTART_VALUE = APP_NAME


def autostart_command() -> str:
    """Kommandot som startar just den här exe-filen eller det här skriptet."""
    if getattr(sys, "frozen", False):   # PyInstaller-exe
        return f'"{executable_path()}"'
    exe = Path(executable_path())
    pythonw = exe.with_name("pythonw.exe")
    if exe.name.lower() == "python.exe" and pythonw.exists():
        exe = pythonw                   # inget konsolfönster vid inloggning
    return f'"{exe}" "{Path(__file__).resolve()}"'


def _read_value(key_path: str, name: str):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _delete_value(key_path: str, name: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def autostart_enabled() -> bool:
    """Bara sant om värdet startar den här kopian och inte är inaktiverat i Aktivitetshanteraren."""
    command = _read_value(RUN_KEY, AUTOSTART_VALUE)
    if not isinstance(command, str):
        return False
    if os.path.normcase(command.strip()) != os.path.normcase(autostart_command()):
        return False            # pekar på en flyttad eller annan kopia
    approved = _read_value(STARTUP_APPROVED_KEY, AUTOSTART_VALUE)
    return not (isinstance(approved, bytes) and approved[:1] and approved[0] & 1)


def set_autostart(enabled: bool) -> None:
    if enabled:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, AUTOSTART_VALUE, 0, winreg.REG_SZ, autostart_command())
    else:
        _delete_value(RUN_KEY, AUTOSTART_VALUE)
    # Tar bort en gammal "Inaktiverad" från Aktivitetshanteraren. Utan värde räknas posten som aktiverad.
    _delete_value(STARTUP_APPROVED_KEY, AUTOSTART_VALUE)


# --------------------------------------------------------------------------- #
#  Win32 – skärmar, DPI och fönsterstilar
# --------------------------------------------------------------------------- #
# Egna WinDLL-instanser så att argtypes inte krockar med pystrays ctypes.windll
user32 = ctypes.WinDLL("user32")
shcore = ctypes.WinDLL("shcore")
dwmapi = ctypes.WinDLL("dwmapi")

MONITOR_DEFAULTTONULL = 0
MONITOR_DEFAULTTOPRIMARY = 1
MONITOR_DEFAULTTONEAREST = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.MonitorFromPoint.argtypes = (wintypes.POINT, wintypes.DWORD)
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = (wintypes.HMONITOR, ctypes.POINTER(MONITORINFO))
user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
shcore.SetProcessDpiAwareness.argtypes = (ctypes.c_int,)
shcore.SetProcessDpiAwareness.restype = ctypes.c_long
shcore.GetDpiForMonitor.argtypes = (
    wintypes.HMONITOR, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
)
shcore.GetDpiForMonitor.restype = ctypes.c_long
dwmapi.DwmSetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long


def set_dpi_awareness() -> None:
    hr = shcore.SetProcessDpiAwareness(2)   # per skärm
    if hr != 0:
        log(f"SetProcessDpiAwareness misslyckades: {hr & 0xFFFFFFFF:#x}")


def is_on_a_monitor(x: int, y: int) -> bool:
    return bool(user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONULL))


def monitor_scale(x: int, y: int) -> float:
    """Skalfaktorn (1.0 = 100 %) för skärmen närmast punkten."""
    hmon = user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONEAREST)
    dpi_x, dpi_y = wintypes.UINT(96), wintypes.UINT(96)
    shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
    return dpi_x.value / 96


def work_area(x: int, y: int, flags: int = MONITOR_DEFAULTTONEAREST) -> wintypes.RECT:
    """Skärmens yta utan aktivitetsfältet."""
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromPoint(wintypes.POINT(x, y), flags), ctypes.byref(info))
    return info.rcWork


def cursor_pos() -> tuple[int, int]:
    """Muspekaren i fysiska pixlar, även negativa på skärmar till vänster om huvudskärmen."""
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def style_as_tool_window(win: tk.Misc) -> None:
    """Håller fönstret borta från aktivitetsfältet och Alt+Tab."""
    hwnd = int(win.wm_frame(), 16)
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)


def round_window_corners(win: tk.Misc) -> None:
    """Mjukt rundade hörn via Windows 11. Äldre Windows ignorerar det och ger raka hörn."""
    pref = ctypes.c_int(DWMWCP_ROUND)
    dwmapi.DwmSetWindowAttribute(
        int(win.wm_frame(), 16), DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), ctypes.sizeof(pref),
    )


# --------------------------------------------------------------------------- #
#  Språk – engelska, eller svenska om Windows visas på svenska
# --------------------------------------------------------------------------- #
LANG_ENV = "CLAUDE_USAGE_LANG"      # för test: tvingar fram "en" eller "sv"
LANG_SWEDISH = 0x1D                 # primärt språk-id för svenska (alla regioner)

TEXTS = {
    "en": {
        "session": "Session",
        "week": "Week",
        "weekdays": "Mon Tue Wed Thu Fri Sat Sun",
        "line_reset": "(resets {when})",
        "tip_title": "Claude – {label}",
        "tip_used": "{pct}% used",
        "tip_reset": " · resets {when}",
        "loading": "Loading…",
        "updated": "Updated {time}",
        "menu_refresh": "Refresh now",
        "menu_open_usage": "Open usage in browser",
        "menu_change_key": "Change sessionKey…",
        "menu_close_widget": "Close widget",
        "menu_show_widget": "Show widget",
        "menu_hide_widget": "Hide widget",
        "menu_lock": "Lock position",
        "menu_autostart": "Start with Windows",
        "menu_about": "About Claude Usage",
        "menu_quit": "Quit Claude Usage",
        "about_version": "Version {version}",
        "about_disclaimer": "Unofficial hobby project, not affiliated with Anthropic.",
        "about_github": "Open on GitHub",
        "close": "Close",
        "key_prompt": "Paste the value of the 'sessionKey' cookie from claude.ai\n"
                      "(F12 → Application → Cookies → https://claude.ai):",
        "ok": "OK",
        "cancel": "Cancel",
        "err_no_key": "No sessionKey – right-click → Change sessionKey",
        "err_auth": "sessionKey invalid or expired ({detail})",
        "err_fetch": "Could not fetch: {detail}",
        "notify": "You have used {pct}% of the session. Resets {when}.",
        "err_autostart": "Could not change Start with Windows: {detail}",
    },
    "sv": {
        "session": "Session",
        "week": "Vecka",
        "weekdays": "mån tis ons tors fre lör sön",
        "line_reset": "(nollst. {when})",
        "tip_title": "Claude – {label}",
        "tip_used": "{pct}% använt",
        "tip_reset": " · nollst. {when}",
        "loading": "Hämtar…",
        "updated": "Uppdaterad {time}",
        "menu_refresh": "Uppdatera nu",
        "menu_open_usage": "Öppna användning i webbläsaren",
        "menu_change_key": "Byt sessionKey…",
        "menu_close_widget": "Stäng widget",
        "menu_show_widget": "Visa widget",
        "menu_hide_widget": "Dölj widget",
        "menu_lock": "Lås position",
        "menu_autostart": "Starta med Windows",
        "menu_about": "Om Claude Usage",
        "menu_quit": "Avsluta Claude Usage",
        "about_version": "Version {version}",
        "about_disclaimer": "Inofficiellt hobbyprojekt, inte kopplat till Anthropic.",
        "about_github": "Öppna på GitHub",
        "close": "Stäng",
        "key_prompt": "Klistra in värdet på cookien 'sessionKey' från claude.ai\n"
                      "(F12 → Application → Cookies → https://claude.ai):",
        "ok": "OK",
        "cancel": "Avbryt",
        "err_no_key": "Ingen sessionKey – högerklicka → Byt sessionKey",
        "err_auth": "sessionKey ogiltig/utgången ({detail})",
        "err_fetch": "Kunde inte hämta: {detail}",
        "notify": "Du har använt {pct}% av sessionen. Nollställs {when}.",
        "err_autostart": "Kunde inte ändra Starta med Windows: {detail}",
    },
}

kernel32 = ctypes.WinDLL("kernel32")
kernel32.GetUserDefaultUILanguage.restype = wintypes.WORD


def detect_language() -> str:
    """Svenska om Windows visningsspråk är svenska, annars engelska."""
    forced = os.environ.get(LANG_ENV, "").strip().lower()
    if forced in TEXTS:
        return forced
    return "sv" if kernel32.GetUserDefaultUILanguage() & 0x3FF == LANG_SWEDISH else "en"


LANG = detect_language()


def t(key: str, **values) -> str:
    """Hämtar en UI-text på appens språk. Saknas den används engelska."""
    text = TEXTS[LANG].get(key) or TEXTS["en"][key]
    return text.format(**values) if values else text


# --------------------------------------------------------------------------- #
#  API-klient
# --------------------------------------------------------------------------- #
class AuthError(Exception):
    pass


class ClaudeClient:
    def __init__(self, session_key: str):
        # impersonate="chrome" gör att Cloudflare inte blockar anropen
        self.http = requests.Session(impersonate="chrome")
        self.http.cookies.set("sessionKey", session_key, domain=".claude.ai")
        self.org_id: str | None = None

    def _get(self, path: str):
        r = self.http.get(BASE_URL + path, timeout=20)
        if r.status_code in (401, 403):
            raise AuthError(f"HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()

    def usage(self) -> dict:
        if self.org_id is None:
            orgs = self._get("/organizations")
            if not orgs:
                raise RuntimeError("Hittade ingen organisation på kontot")
            # Välj den org som har chatt (det personliga kontot)
            pick = next(
                (o for o in orgs if "chat" in (o.get("capabilities") or [])),
                orgs[0],
            )
            self.org_id = pick["uuid"]
        return self._get(f"/organizations/{self.org_id}/usage")


# --------------------------------------------------------------------------- #
#  Tolkning & formatering
# --------------------------------------------------------------------------- #
def parse_window(block: dict | None) -> tuple[float, datetime | None] | None:
    """{'utilization': 35.0, 'resets_at': '...'} -> (35.0, lokal datetime)"""
    if not block or block.get("utilization") is None:
        return None
    reset = block.get("resets_at")
    dt = None
    if reset:
        dt = datetime.fromisoformat(reset.replace("Z", "+00:00")).astimezone()
    return float(block["utilization"]), dt


def fmt_reset(dt: datetime | None) -> str:
    if dt is None:
        return ""
    now = datetime.now().astimezone()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return f"{t('weekdays').split()[dt.weekday()]} {dt.strftime('%H:%M')}"


def fmt_line(label: str, window) -> str:
    if window is None:
        return f"{label}: –"
    pct, reset = window
    s = f"{label}: {round(pct)}%"
    if reset:
        s += " " + t("line_reset", when=fmt_reset(reset))
    return s


# --------------------------------------------------------------------------- #
#  Ikon – en enkel mätare (gauge), ingen text
# --------------------------------------------------------------------------- #
def taskbar_is_light() -> bool:
    """Läser Windows-temat så att ikonen syns på både mörkt och ljust aktivitetsfält."""
    return _read_value(r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "SystemUsesLightTheme") == 1


def make_gauge(pct: float | None, stale: bool = False, light: bool = False, size: int = 64) -> Image.Image:
    SS = 4                      # rita i 4x och skala ner = mjuka kanter
    S = 64 * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    base = (30, 30, 30) if light else (240, 240, 240)
    track = base + (70,)
    if pct is None or stale:
        fill = base + (120,)
    elif pct >= 95:
        fill = (232, 84, 72, 255)    # röd
    elif pct >= 80:
        fill = (240, 165, 40, 255)   # orange
    else:
        fill = base + (255,)

    w = 7 * SS                  # linjetjocklek
    m = 5 * SS                  # marginal
    c = S / 2
    r_mid = c - m - w / 2       # radie till mitten av bågen

    def point(angle_deg: float, r: float):
        a = math.radians(angle_deg)
        return c + r * math.cos(a), c + r * math.sin(a)

    def cap(angle_deg: float, color):
        x, y = point(angle_deg, r_mid)
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color)

    START, SWEEP = 135, 270     # öppen båge med glipa nedtill
    box = (m, m, S - m, S - m)

    # Bakgrundsbåge
    d.arc(box, START, START + SWEEP, fill=track, width=w)
    cap(START, track)
    cap(START + SWEEP, track)

    if pct is not None:
        p = max(0.0, min(pct, 100.0)) / 100
        end = START + SWEEP * p
        if p > 0:
            d.arc(box, START, end, fill=fill, width=w)
        cap(START, fill)
        cap(end, fill)

        # Visare
        nx, ny = point(end, r_mid * 0.62)
        d.line((c, c, nx, ny), fill=fill, width=5 * SS)
        d.ellipse((nx - 2.5 * SS, ny - 2.5 * SS, nx + 2.5 * SS, ny + 2.5 * SS), fill=fill)

    # Navet i mitten
    hub = 6 * SS
    d.ellipse((c - hub, c - hub, c + hub, c + hub), fill=fill)

    return img.resize((size, size), Image.LANCZOS)


def make_app_icon(size: int, pct: float = 65) -> Image.Image:
    """Appens ikon (exe-filen och dialogerna): en vit mätare på widgetens mörka, rundade platta,
    så att den syns på både ljusa och mörka bakgrunder. Se scripts/make_icon.py."""
    SS = 4
    plate = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle(
        (0, 0, size * SS - 1, size * SS - 1), radius=round(size * SS * 0.22), fill=Widget.BG + (255,),
    )
    img = plate.resize((size, size), Image.LANCZOS)
    img.alpha_composite(make_gauge(pct, size=size))   # ritas i exakt storlek så att den blir skarp
    return img


# --------------------------------------------------------------------------- #
#  Flytande widget – samma mätare, alltid överst. Rörs bara från Tk-tråden.
# --------------------------------------------------------------------------- #
class Widget:
    GAUGE_PX = 38               # alla mått i logiska pixlar, skalas med skärmens DPI
    PAD_PX = 5
    GAP_PX = 2
    MARGIN_PX = 12              # avstånd till skärmkanten i standardpositionen
    TIP_GAP_PX = 6
    TIP_FONT_PX = 12
    TIP_DELAY_MS = 400
    DRAG_THRESHOLD_PX = 3       # så att en darrig klickning inte flyttar widgeten
    CLOSE_PX = 12               # ✕ i hörnet, som bara syns när musen är över widgeten
    CLOSE_FONT_PX = 10
    CLOSE_FG = "#9a9a9a"
    CLOSE_FG_HOVER = "#ffffff"
    # Mörk platta på alla bakgrunder. Mätarnas halvgenomskinliga kanter blandas mot
    # den, medan -transparentcolor skulle ge en färgad kant på ljusa bakgrunder.
    BG = (32, 32, 32)
    BG_HEX = "#202020"

    def __init__(self, app: "App"):
        self.app = app
        self.settings = app.settings
        self.state = app._widget_state()
        self.images: dict[str, ImageTk.PhotoImage] = {}   # Tk kräver att referensen lever kvar
        self.drag: tuple[int, int, int, int] | None = None  # offset + startpunkt för dragningen
        self.dragging = False
        self.tip_key: str | None = None
        self.tip_job: str | None = None

        self.win = tk.Toplevel(app.root, bg=self.BG_HEX)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)

        self.labels = {}
        for key in app.KEYS:
            label = tk.Label(self.win, bg=self.BG_HEX, bd=0, highlightthickness=0)
            label.bind("<Enter>", lambda _e, k=key: self._tip_enter(k))
            label.bind("<Leave>", lambda _e: self._tip_cancel())
            self.labels[key] = label

        self.close_shown = False
        self.close_btn = tk.Label(
            self.win, text="✕", bg=self.BG_HEX, fg=self.CLOSE_FG, bd=0, highlightthickness=0, cursor="hand2",
        )
        self.close_btn.bind("<Enter>", lambda _e: self.close_btn.configure(fg=self.CLOSE_FG_HOVER))
        self.close_btn.bind("<Leave>", lambda _e: self.close_btn.configure(fg=self.CLOSE_FG))
        # "break" stoppar Toplevel-bindningarna nedan, så att ✕ inte startar en dragning
        for sequence in ("<ButtonPress-1>", "<Double-Button-1>", "<B1-Motion>"):
            self.close_btn.bind(sequence, lambda _e: "break")
        self.close_btn.bind("<ButtonRelease-1>", self._close_clicked)

        # Toplevel-bindningar gäller även etiketterna inuti
        self.win.bind("<ButtonPress-1>", self._press)
        self.win.bind("<Double-Button-1>", self._double_click)
        self.win.bind("<B1-Motion>", self._motion)
        self.win.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Button-3>", self._show_menu)
        self.win.bind("<Enter>", lambda _e: self._sync_close_button())
        self.win.bind("<Leave>", lambda _e: self._sync_close_button())

        self.tip = tk.Toplevel(self.win, bg=self.BG_HEX)
        self.tip.withdraw()
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip_label = tk.Label(self.tip, bg=self.BG_HEX, fg="#f0f0f0", justify="left")
        self.tip_label.pack()

        self.menu = tk.Menu(self.win, tearoff=0)
        self.menu_vars: list[tk.BooleanVar] = []

        self.x, self.y = self._initial_position()
        self.scale = self._scale_here()
        self._layout()
        self._draw()
        if self.settings["visible"]:
            self.show()

    # ---- position & storlek ----
    def _size(self, scale: float) -> tuple[int, int]:
        px, pad, gap = (round(v * scale) for v in (self.GAUGE_PX, self.PAD_PX, self.GAP_PX))
        return 2 * px + gap + 2 * pad, px + 2 * pad

    def _initial_position(self) -> tuple[int, int]:
        x, y = self.settings["x"], self.settings["y"]
        if type(x) is int and type(y) is int:
            w, h = self._size(monitor_scale(x, y))
            if is_on_a_monitor(x + w // 2, y + h // 2):
                return x, y
            log(f"Sparad position ({x}, {y}) ligger inte på någon skärm – flyttar till huvudskärmen")
        # Skrivs inte till filen, så widgeten hittar tillbaka om skärmen kopplas in igen
        scale = monitor_scale(0, 0)
        area = work_area(0, 0, MONITOR_DEFAULTTOPRIMARY)
        w, h = self._size(scale)
        margin = round(self.MARGIN_PX * scale)
        return area.right - w - margin, area.bottom - h - margin

    def _scale_here(self) -> float:
        w, h = self._size(1.0)
        return monitor_scale(self.x + w // 2, self.y + h // 2)

    def _layout(self) -> None:
        s = self.scale
        px, pad, gap = (round(v * s) for v in (self.GAUGE_PX, self.PAD_PX, self.GAP_PX))
        for i, key in enumerate(self.app.KEYS):   # session till vänster
            self.labels[key].place(x=pad + i * (px + gap), y=pad, width=px, height=px)
        w, h = self._size(s)
        self.win.geometry(f"{w}x{h}+{self.x}+{self.y}")
        self.close_btn.configure(font=("Segoe UI", -round(self.CLOSE_FONT_PX * s)))   # negativt = pixlar
        if self.close_shown:
            self._place_close_button()

    def _place_close_button(self) -> None:
        s = self.scale
        size, inset = round(self.CLOSE_PX * s), round(s)
        self.close_btn.place(x=self._size(s)[0] - size - inset, y=inset, width=size, height=size)
        self.close_btn.lift()

    def _sync_close_button(self) -> None:
        """Visar ✕ om pekaren är över widgeten. Läser pekaren eftersom ett Leave också
        kommer vid varje övergång mellan etiketterna inuti widgeten."""
        cx, cy = cursor_pos()
        w, h = self._size(self.scale)
        over = (
            self.win.state() == "normal"
            and self.x <= cx < self.x + w
            and self.y <= cy < self.y + h
        )
        if over == self.close_shown:
            return
        self.close_shown = over
        if over:
            self._place_close_button()
        else:
            self.close_btn.place_forget()
            self.close_btn.configure(fg=self.CLOSE_FG)

    def _set_scale(self, scale: float) -> None:
        self.scale = scale
        log(f"Widgeten skalas om till {round(scale * 100)} %")
        self._layout()
        self._draw()

    def _sync_scale(self) -> None:
        scale = self._scale_here()
        if scale != self.scale:
            self._set_scale(scale)

    def _draw(self) -> None:
        px = round(self.GAUGE_PX * self.scale)   # ritas i exakt storlek så att de blir skarpa
        for key, (pct, stale, _text) in self.state.items():
            img = Image.new("RGBA", (px, px), self.BG + (255,))
            img.alpha_composite(make_gauge(pct, stale=stale, size=px))
            photo = ImageTk.PhotoImage(img.convert("RGB"), master=self.win)
            self.labels[key].configure(image=photo)
            self.images[key] = photo

    def refresh(self, state: dict) -> None:
        self.state = state
        self._sync_scale()      # fångar också att skalningen ändrats i Windows
        self._draw()
        if self.tip.state() == "normal":
            self._show_tip()

    # ---- visa/dölj ----
    def show(self) -> None:
        self.win.deiconify()
        self.win.update_idletasks()
        style_as_tool_window(self.win)
        round_window_corners(self.win)

    def set_visible(self, visible: bool) -> None:
        if visible:
            self.show()
        else:
            self._tip_cancel()
            self.win.withdraw()
        self._sync_close_button()
        self.settings["visible"] = visible
        save_settings(self.settings)

    def toggle(self) -> None:
        self.set_visible(not self.settings["visible"])

    def _close_clicked(self, event) -> str:
        # Släpps knappen utanför ✕ ångrar man sig, som på en vanlig knapp
        if 0 <= event.x < self.close_btn.winfo_width() and 0 <= event.y < self.close_btn.winfo_height():
            self.set_visible(False)
        return "break"

    # ---- dra & klicka ----
    def _press(self, _event=None) -> None:
        self._tip_cancel()
        cx, cy = cursor_pos()
        self.drag = (cx - self.x, cy - self.y, cx, cy)
        self.dragging = False

    def _double_click(self, event) -> None:
        self._press(event)
        self.app._refresh_now()

    def _motion(self, _event) -> None:
        if self.drag is None or self.settings["locked"]:
            return
        ox, oy, sx, sy = self.drag
        cx, cy = cursor_pos()
        if not self.dragging:
            if max(abs(cx - sx), abs(cy - sy)) < self.DRAG_THRESHOLD_PX:
                return
            self.dragging = True
        # Annan DPI på skärmen under pekaren? Skala om och behåll greppet på samma ställe.
        scale = monitor_scale(cx, cy)
        if scale != self.scale:
            ratio = scale / self.scale
            ox, oy = round(ox * ratio), round(oy * ratio)
            self.drag = (ox, oy, sx, sy)
            self.x, self.y = cx - ox, cy - oy
            self._set_scale(scale)
            return
        self.x, self.y = cx - ox, cy - oy
        self.win.geometry(f"+{self.x}+{self.y}")

    def _release(self, _event) -> None:
        moved = self.dragging
        self.drag = None
        self.dragging = False
        self._sync_close_button()
        if moved:
            self._sync_scale()
            self.settings["x"], self.settings["y"] = self.x, self.y
            save_settings(self.settings)
            log(f"Widgeten flyttad till ({self.x}, {self.y})")

    # ---- högerklicksmeny ----
    def _show_menu(self, _event) -> None:
        self._tip_cancel()
        self.menu.delete(0, "end")
        self.menu_vars = []
        for entry in self.app._menu_entries(for_widget=True):
            if entry is None:
                self.menu.add_separator()
                continue
            text, action, *checked = entry
            if action is None:
                self.menu.add_command(label=text() if callable(text) else text, state="disabled")
            elif checked:
                var = tk.BooleanVar(self.win, value=checked[0]())
                self.menu_vars.append(var)      # Tk kräver att variabeln lever kvar
                self.menu.add_checkbutton(label=text, variable=var, command=action)
            else:
                self.menu.add_command(label=text, command=action)
        self.win.focus_force()  # annars stängs inte menyn av ett klick utanför
        cx, cy = cursor_pos()
        try:
            self.menu.tk_popup(cx, cy)
        finally:
            self.menu.grab_release()
            self._sync_close_button()   # musen kan ha lämnat widgeten medan menyn var öppen

    def toggle_lock(self) -> None:
        self.settings["locked"] = not self.settings["locked"]
        save_settings(self.settings)

    # ---- tooltip ----
    def _tip_enter(self, key: str) -> None:
        self._tip_cancel()
        if self.drag is None:
            self.tip_key = key
            self.tip_job = self.win.after(self.TIP_DELAY_MS, self._show_tip)

    def _tip_cancel(self) -> None:
        if self.tip_job is not None:
            self.win.after_cancel(self.tip_job)
            self.tip_job = None
        self.tip.withdraw()

    def _show_tip(self) -> None:
        self.tip_job = None
        s = self.scale
        self.tip_label.configure(
            text=self.state[self.tip_key][2],
            font=("Segoe UI", -round(self.TIP_FONT_PX * s)),   # negativt = pixlar
            padx=round(6 * s),
            pady=round(3 * s),
        )
        self.tip.update_idletasks()
        w, h = self.tip_label.winfo_reqwidth(), self.tip_label.winfo_reqheight()
        win_h = self._size(s)[1]
        gap = round(self.TIP_GAP_PX * s)
        label = self.labels[self.tip_key]

        # Ovanför mätaren, eller under om det inte får plats, och alltid på samma skärm
        area = work_area(self.x, self.y)
        x = self.x + label.winfo_x() + label.winfo_width() // 2 - w // 2
        x = max(area.left, min(x, area.right - w))
        y = self.y - h - gap
        if y < area.top:
            y = self.y + win_h + gap
        self.tip.geometry(f"{w}x{h}+{x}+{y}")
        self.tip.deiconify()
        self.tip.update_idletasks()
        style_as_tool_window(self.tip)
        self.tip.lift()


# --------------------------------------------------------------------------- #
#  Tray-ikon med fast identitet i Windows
# --------------------------------------------------------------------------- #
class TrayIcon(pystray.Icon):
    """Ger ikonen ett fast uID och en fast GUID, så att Windows minns om den ska synas.

    pystray 0.19.5 skickar hID=id(self), men fältet heter uID. Det blir alltså alltid 0,
    och båda ikonerna delar samma post i Windows inställningar. Med NIF_GUID får varje
    ikon en egen post, och den gäller oavsett om appen körs med python eller pythonw.
    """

    def __init__(self, *args, tray_uid: int, tray_guid: str, **kwargs):
        guid_type = type(pystray_win32.NOTIFYICONDATAW().guidItem)
        self._tray_uid = tray_uid
        self._tray_guid = guid_type.from_buffer_copy(uuid.UUID(tray_guid).bytes_le)
        self._tray_added = False
        super().__init__(*args, **kwargs)

    def _on_notify(self, wparam, lparam):
        # Ersätter pystrays privata metod med samma namn. pystray bygger menyn i förväg, så den
        # byggs om här precis innan den visas, med aktuella procent, status och bock för
        # "Starta med Windows" (läst från registret). Menyn byggs bara om i ikonens egen tråd,
        # aldrig från andra trådar, som annars kunde förstöra den medan den visas.
        if lparam == pystray_win32.WM_RBUTTONUP:
            self.update_menu()
        super()._on_notify(wparam, lparam)

    def _message(self, code, flags, **kwargs):
        # Ersätter pystrays privata metod med samma namn.
        # En GUID identifierar ikonen oavsett process, så en andra instans som nekats
        # ikonen får inte ändra eller ta bort den som den första instansen äger.
        if code != pystray_win32.NIM_ADD and not self._tray_added:
            return
        ok = pystray_win32.Shell_NotifyIcon(code, pystray_win32.NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(pystray_win32.NOTIFYICONDATAW),
            hWnd=self._hwnd,
            uID=self._tray_uid,
            uFlags=flags | pystray_win32.NIF_GUID,
            guidItem=self._tray_guid,
            **kwargs))
        if code == pystray_win32.NIM_ADD:
            self._tray_added = bool(ok)
            if not ok:
                log(f"FEL: Windows vägrade lägga till ikonen {self.name} (körs appen redan?)")


# --------------------------------------------------------------------------- #
#  Appen – två ikoner (session + vecka) och widgeten
# --------------------------------------------------------------------------- #
class App:
    KEYS = ("session", "week")
    LABELS = {"session": t("session"), "week": t("week")}

    def __init__(self):
        self.settings = load_settings()
        self.root: tk.Tk | None = None
        self.widget: Widget | None = None
        self.ui_queue: queue.Queue = queue.Queue()    # uppgifter som ska köras i Tk-tråden
        self.data = {"session": None, "week": None}   # (pct, reset_dt)
        self.error: str | None = None
        self.updated: datetime | None = None
        self.client: ClaudeClient | None = None
        self.client_key: str | None = None
        self.notified: set[tuple] = set()
        self.icons: dict[str, pystray.Icon] = {}
        self.ready = {k: threading.Event() for k in self.KEYS}
        self.stop_evt = threading.Event()
        self.wake_evt = threading.Event()

    # ---- meny (delas av båda ikonerna och widgeten) ----
    def _menu_entries(self, for_widget: bool = False) -> list:
        """Menyraderna i ordning: (text, åtgärd) eller (text, åtgärd, bockad?), None = avdelare.
        Utan åtgärd = bara text. Text och bock kan vara funktioner, som läses när menyn byggs."""
        widget_entries = [
            (t("menu_close_widget"), self._hide_widget),
            (t("menu_lock"), self._toggle_lock, lambda: bool(self.settings["locked"])),
        ] if for_widget else [
            (self._widget_menu_text, self._toggle_widget),
        ]
        return [
            (lambda: fmt_line(self.LABELS["session"], self.data["session"]), None),
            (lambda: fmt_line(self.LABELS["week"], self.data["week"]), None),
            (self._status_line, None),
            None,
            (t("menu_refresh"), self._refresh_now),
            (t("menu_open_usage"), self._open_usage_page),
            (t("menu_change_key"), self._change_key),
            *widget_entries,
            None,
            (t("menu_autostart"), self._toggle_autostart, autostart_enabled),
            (t("menu_about"), self._show_about),
            None,
            (t("menu_quit"), self._quit),
        ]

    def _widget_menu_text(self) -> str:
        return t("menu_hide_widget") if self.settings["visible"] else t("menu_show_widget")

    def _build_menu(self) -> pystray.Menu:
        items = []
        for entry in self._menu_entries():
            if entry is None:
                items.append(pystray.Menu.SEPARATOR)
                continue
            text, action, *checked = entry
            label = (lambda _, fn=text: fn()) if callable(text) else text
            if action is None:
                items.append(pystray.MenuItem(label, None, enabled=False))
            elif checked:
                items.append(pystray.MenuItem(label, action, checked=lambda _, fn=checked[0]: fn()))
            else:
                items.append(pystray.MenuItem(label, action, default=action == self._refresh_now))
        return pystray.Menu(*items)

    def _status_line(self) -> str:
        if self.error:
            return f"⚠ {self.error}"
        if self.updated:
            return t("updated", time=self.updated.strftime("%H:%M"))
        return t("loading")

    def _refresh_now(self):
        self.wake_evt.set()

    def _open_usage_page(self):
        webbrowser.open(USAGE_PAGE)

    def _toggle_widget(self):
        self.ui_queue.put(self.widget.toggle)

    def _hide_widget(self):
        self.ui_queue.put(lambda: self.widget.set_visible(False))

    def _toggle_lock(self):
        self.ui_queue.put(self.widget.toggle_lock)

    def _toggle_autostart(self):
        # Utgår från registret, inte från bocken, som kan vara inaktuell
        enable = not autostart_enabled()
        try:
            set_autostart(enable)
            log(f"Starta med Windows: {'på' if enable else 'av'}")
        except OSError as e:
            # Loggen syns inte i exe-filen eller med pythonw, så användaren får en notis
            log(f"FEL: kunde inte ändra Starta med Windows: {e!r}")
            self._notify(t("err_autostart", detail=e.strerror or type(e).__name__))

    def _show_about(self):
        self.ui_queue.put(self._about_dialog)

    def _about_dialog(self):
        if not AppDialog.raise_open():
            AboutDialog(self.root, title=t("menu_about"))

    def _change_key(self):
        # Egen tråd eftersom svaret väntas in. Från widgetens meny körs vi i Tk-tråden,
        # och en väntan där skulle låsa dialogen som vi väntar på.
        threading.Thread(target=self._change_key_worker, daemon=True).start()

    def _change_key_worker(self):
        key = self._ask_session_key()
        if key:
            set_session_key(key)
            self.wake_evt.set()

    def _ask_session_key(self) -> str | None:
        """Visar dialogen i Tk-tråden och väntar på svaret. Får inte anropas från Tk-tråden."""
        answer: queue.Queue = queue.Queue(maxsize=1)
        self.ui_queue.put(lambda: answer.put(self._session_key_dialog()))
        while not self.stop_evt.is_set():
            try:
                return answer.get(timeout=0.2)
            except queue.Empty:
                pass
        return None

    def _session_key_dialog(self) -> str | None:
        if AppDialog.raise_open():
            return None
        return SessionKeyDialog(self.root, title="Claude Usage").result

    def _quit(self):
        # Säkert från alla trådar och från Ctrl+C. Själva stängningen sker i _pump.
        self.stop_evt.set()
        self.wake_evt.set()

    # ---- Tk-tråden ----
    def _pump(self):
        if self.stop_evt.is_set():
            self._shutdown_ui()
            return
        # Bokas före uppgifterna: dialogen kör en egen händelseloop inuti sin uppgift,
        # och kön måste läsas även under tiden.
        self.root.after(PUMP_MS, self._pump)
        while not self.stop_evt.is_set():
            try:
                task = self.ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                task()
            except Exception as e:
                log(f"FEL i UI-uppgift: {e!r}")

    def _shutdown_ui(self):
        log("Avslutar…")
        for icon in self.icons.values():
            try:
                icon.stop()
            except Exception as e:
                log(f"FEL när ikonen stoppades: {e!r}")
        self.root.destroy()     # avslutar mainloop, även om dialogen är öppen

    def _report_tk_error(self, _exc, val, _tb):
        log(f"FEL i Tk: {val!r}")

    # ---- ikontrådar ----
    # På Windows måste varje ikon skapas i samma tråd som kör dess meddelandeloop.
    def _icon_thread(self, key: str):
        def setup(icon):
            icon.visible = True
            self.ready[key].set()

        icon = TrayIcon(
            f"{APP_NAME}_{key}",
            make_gauge(None, light=taskbar_is_light()),
            self._tooltip_text(key)[:127],
            menu=self._build_menu(),
            tray_uid=TRAY_IDS[key][0],
            tray_guid=tray_guid(key),
        )
        self.icons[key] = icon
        icon.run(setup=setup)

    # ---- polling ----
    def _poll_loop(self):
        while not self.stop_evt.is_set():
            self._refresh()
            self.wake_evt.wait(POLL_SECONDS)
            self.wake_evt.clear()

    def _refresh(self):
        key = get_session_key()
        if not key:
            self.error = t("err_no_key")
            self._render()
            return

        if self.client is None or self.client_key != key:
            self.client, self.client_key = ClaudeClient(key), key

        try:
            data = self.client.usage()
            self.data["session"] = parse_window(data.get("five_hour"))
            self.data["week"] = parse_window(data.get("seven_day"))
            self.updated = datetime.now()
            self.error = None
            log(f"{fmt_line('Session', self.data['session'])} | {fmt_line('Vecka', self.data['week'])}")
            self._maybe_notify()
        except AuthError as e:
            self.error = t("err_auth", detail=e)
            self.client = None
            log(f"FEL: {self.error}")
        except Exception as e:  # nätverk, Cloudflare, ändrat API …
            self.error = t("err_fetch", detail=type(e).__name__)
            log(f"FEL: {e!r}")
        self._render()

    def _notify(self, message: str) -> None:
        """Windows-notis från sessionsikonen. Säkert från alla trådar."""
        icon = self.icons.get("session")
        if icon is None:
            return
        try:
            icon.notify(message, "Claude Usage")
        except Exception as e:
            log(f"FEL: kunde inte visa notisen: {e!r}")

    def _maybe_notify(self):
        win = self.data["session"]
        if not win:
            return
        pct, reset = win
        for threshold in NOTIFY_THRESHOLDS:
            tag = (threshold, reset)
            if pct >= threshold and tag not in self.notified:
                self.notified.add(tag)
                self._notify(t("notify", pct=round(pct), when=fmt_reset(reset)))

    def _tooltip_text(self, key: str) -> str:
        lines = [t("tip_title", label=self.LABELS[key].lower())]
        win = self.data[key]
        if win:
            pct, reset = win
            line = t("tip_used", pct=round(pct))
            if reset:
                line += t("tip_reset", when=fmt_reset(reset))
            lines.append(line)
        if self.error:
            lines.append(f"⚠ {self.error}")
        elif not self.updated:
            lines.append(t("loading"))
        return "\n".join(lines)

    def _widget_state(self) -> dict:
        stale = self.error is not None
        return {
            key: (self.data[key][0] if self.data[key] else None, stale, self._tooltip_text(key))
            for key in self.KEYS
        }

    def _render(self):
        if self.stop_evt.is_set():   # ikonerna kan redan vara stängda
            return
        light = taskbar_is_light()
        stale = self.error is not None
        for key, icon in self.icons.items():
            win = self.data[key]
            icon.icon = make_gauge(win[0] if win else None, stale=stale, light=light)
            icon.title = self._tooltip_text(key)[:127]   # Windows max-längd på tooltip
        # Menyn byggs inte om här: TrayIcon._on_notify bygger den när den öppnas
        state = self._widget_state()
        self.ui_queue.put(lambda: self.widget.refresh(state))

    def run(self):
        # Ctrl+C sätter bara en flagga. Ett KeyboardInterrupt inne i en Tk-callback
        # skulle annars bli en traceback och sedan sväljas av Tk.
        signal.signal(signal.SIGINT, lambda *_: self._quit())

        self.root = tk.Tk()
        self.root.withdraw()    # osynlig, men äger dialogen och widgeten
        self.root.report_callback_exception = self._report_tk_error
        # Appens ikon i dialogernas titelrad i stället för Tk:s fjäder. Referenserna måste leva kvar.
        self.window_icons = [ImageTk.PhotoImage(make_app_icon(s), master=self.root) for s in (16, 32, 48)]
        self.root.iconphoto(True, *self.window_icons)
        self.widget = Widget(self)

        threads = []
        for key in self.KEYS:   # session först så att den hamnar till vänster
            thread = threading.Thread(target=self._icon_thread, args=(key,), daemon=True)
            thread.start()
            self.ready[key].wait(10)
            threads.append(thread)

        log(f"Ikoner startade: {', '.join(k for k in self.KEYS if self.ready[k].is_set())}")
        threading.Thread(target=self._poll_loop, daemon=True).start()
        if not get_session_key():
            self._change_key()

        self.root.after(PUMP_MS, self._pump)
        self.root.mainloop()
        for thread in threads:  # låt ikonerna hinna ta bort sig från systemfältet
            thread.join(2)
        log("Avslutat")


def main():
    set_dpi_awareness()
    App().run()


if __name__ == "__main__":
    main()
