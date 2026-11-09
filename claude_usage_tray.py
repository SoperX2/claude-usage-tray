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
import threading
import tkinter as tk
import uuid
import webbrowser
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import simpledialog

import keyring
import pystray
from curl_cffi import requests
from PIL import Image, ImageDraw, ImageFont, ImageTk
from pystray._util import win32 as pystray_win32

APP_NAME = "ClaudeUsageTray"
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

WEEKDAYS_SV = ["mån", "tis", "ons", "tors", "fre", "lör", "sön"]


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


class SessionKeyDialog(simpledialog.Dialog):
    """Frågar efter sessionKey. Skapas alltid i Tk-tråden, se App._ask_session_key."""

    def body(self, master):
        self.attributes("-topmost", True)
        tk.Label(
            master,
            text="Klistra in värdet på cookien 'sessionKey' från claude.ai\n"
                 "(F12 → Application → Cookies → https://claude.ai):",
            justify="left",
        ).pack(anchor="w")
        self.entry = tk.Entry(master, show="*", width=50)
        self.entry.pack(fill="x", pady=(6, 0))
        self.after(50, self.entry.focus_force)
        return self.entry

    def buttonbox(self):
        box = tk.Frame(self)
        tk.Button(box, text="OK", width=10, command=self.ok, default="active").pack(side="left", padx=5, pady=5)
        tk.Button(box, text="Avbryt", width=10, command=self.cancel).pack(side="left", padx=5, pady=5)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)
        box.pack()

    def apply(self):
        self.result = self.entry.get().strip() or None


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
    return f"{WEEKDAYS_SV[dt.weekday()]} {dt.strftime('%H:%M')}"


def fmt_line(label: str, window) -> str:
    if window is None:
        return f"{label}: –"
    pct, reset = window
    s = f"{label}: {round(pct)}%"
    if reset:
        s += f" (nollst. {fmt_reset(reset)})"
    return s




# --------------------------------------------------------------------------- #
#  Ikon – en enkel mätare (gauge), ingen text
# --------------------------------------------------------------------------- #
def taskbar_is_light() -> bool:
    """Läser Windows-temat så att ikonen syns på både mörkt och ljust aktivitetsfält."""
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 1
    except Exception:
        return False


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

        # Toplevel-bindningar gäller även etiketterna inuti
        self.win.bind("<ButtonPress-1>", self._press)
        self.win.bind("<Double-Button-1>", self._double_click)
        self.win.bind("<B1-Motion>", self._motion)
        self.win.bind("<ButtonRelease-1>", self._release)
        self.win.bind("<Button-3>", self._show_menu)

        self.tip = tk.Toplevel(self.win, bg=self.BG_HEX)
        self.tip.withdraw()
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip_label = tk.Label(self.tip, bg=self.BG_HEX, fg="#f0f0f0", justify="left")
        self.tip_label.pack()

        self.locked = tk.BooleanVar(self.win, value=bool(self.settings["locked"]))
        self.menu = tk.Menu(self.win, tearoff=0)

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

    def toggle(self) -> None:
        visible = self.win.state() == "normal"
        if visible:
            self._tip_cancel()
            self.win.withdraw()
        else:
            self.show()
        self.settings["visible"] = not visible
        save_settings(self.settings)

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
        if self.drag is None or self.locked.get():
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
        if moved:
            self._sync_scale()
            self.settings["x"], self.settings["y"] = self.x, self.y
            save_settings(self.settings)
            log(f"Widgeten flyttad till ({self.x}, {self.y})")

    # ---- högerklicksmeny ----
    def _show_menu(self, _event) -> None:
        self._tip_cancel()
        self.menu.delete(0, "end")
        for entry in self.app._menu_entries():
            if entry is None:
                self.menu.add_separator()
                continue
            text, action = entry
            if action is None:
                self.menu.add_command(label=text() if callable(text) else text, state="disabled")
            else:
                self.menu.add_command(label=text, command=action)
        # "Lås position" före sista avdelaren, alltså direkt efter Visa/dölj
        self.menu.insert_checkbutton(
            self.menu.index("end") - 1, label="Lås position", variable=self.locked, command=self._lock_changed,
        )
        self.win.focus_force()  # annars stängs inte menyn av ett klick utanför
        cx, cy = cursor_pos()
        try:
            self.menu.tk_popup(cx, cy)
        finally:
            self.menu.grab_release()

    def _lock_changed(self) -> None:
        self.settings["locked"] = self.locked.get()
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
    LABELS = {"session": "Session", "week": "Vecka"}

    def __init__(self):
        self.settings = load_settings()
        self.root: tk.Tk | None = None
        self.widget: Widget | None = None
        self.ui_queue: queue.Queue = queue.Queue()    # uppgifter som ska köras i Tk-tråden
        self.key_dialog_open = False
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
    def _menu_entries(self) -> list:
        """Menyraderna i ordning: (text, åtgärd), None = avdelare. Utan åtgärd = bara text."""
        return [
            (lambda: fmt_line("Session", self.data["session"]), None),
            (lambda: fmt_line("Vecka", self.data["week"]), None),
            (self._status_line, None),
            None,
            ("Uppdatera nu", self._refresh_now),
            ("Öppna användning i webbläsaren", self._open_usage_page),
            ("Byt sessionKey…", self._change_key),
            ("Visa/dölj widget", self._toggle_widget),
            None,
            ("Avsluta", self._quit),
        ]

    def _build_menu(self) -> pystray.Menu:
        items = []
        for entry in self._menu_entries():
            if entry is None:
                items.append(pystray.Menu.SEPARATOR)
                continue
            text, action = entry
            if action is None:
                items.append(pystray.MenuItem(lambda _, t=text: t(), None, enabled=False))
            else:
                items.append(pystray.MenuItem(text, action, default=action == self._refresh_now))
        return pystray.Menu(*items)

    def _status_line(self) -> str:
        if self.error:
            return f"⚠ {self.error}"
        if self.updated:
            return f"Uppdaterad {self.updated.strftime('%H:%M')}"
        return "Hämtar…"

    def _refresh_now(self):
        self.wake_evt.set()

    def _open_usage_page(self):
        webbrowser.open(USAGE_PAGE)

    def _toggle_widget(self):
        self.ui_queue.put(self.widget.toggle)

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
        if self.key_dialog_open:
            return None
        self.key_dialog_open = True
        try:
            return SessionKeyDialog(self.root, title="Claude Usage").result
        finally:
            self.key_dialog_open = False

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

        tray_uid, tray_guid = TRAY_IDS[key]
        icon = TrayIcon(
            f"{APP_NAME}_{key}",
            make_gauge(None, light=taskbar_is_light()),
            self._tooltip_text(key)[:127],
            menu=self._build_menu(),
            tray_uid=tray_uid,
            tray_guid=tray_guid,
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
            self.error = "Ingen sessionKey – högerklicka → Byt sessionKey"
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
            self.error = f"sessionKey ogiltig/utgången ({e})"
            self.client = None
            log(f"FEL: {self.error}")
        except Exception as e:  # nätverk, Cloudflare, ändrat API …
            self.error = f"Kunde inte hämta: {type(e).__name__}"
            log(f"FEL: {e!r}")
        self._render()

    def _maybe_notify(self):
        win = self.data["session"]
        icon = self.icons.get("session")
        if not win or icon is None or not hasattr(icon, "notify"):
            return
        pct, reset = win
        for t in NOTIFY_THRESHOLDS:
            tag = (t, reset)
            if pct >= t and tag not in self.notified:
                self.notified.add(tag)
                try:
                    icon.notify(
                        f"Du har använt {round(pct)}% av sessionen. "
                        f"Nollställs {fmt_reset(reset)}.",
                        "Claude Usage",
                    )
                except Exception:
                    pass

    def _tooltip_text(self, key: str) -> str:
        lines = [f"Claude – {self.LABELS[key].lower()}"]
        win = self.data[key]
        if win:
            pct, reset = win
            line = f"{round(pct)}% använt"
            if reset:
                line += f" · nollst. {fmt_reset(reset)}"
            lines.append(line)
        if self.error:
            lines.append(f"⚠ {self.error}")
        elif not self.updated:
            lines.append("Hämtar…")
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
            icon.update_menu()
        state = self._widget_state()
        self.ui_queue.put(lambda: self.widget.refresh(state))

    def run(self):
        # Ctrl+C sätter bara en flagga. Ett KeyboardInterrupt inne i en Tk-callback
        # skulle annars bli en traceback och sedan sväljas av Tk.
        signal.signal(signal.SIGINT, lambda *_: self._quit())

        self.root = tk.Tk()
        self.root.withdraw()    # osynlig, men äger dialogen och widgeten
        self.root.report_callback_exception = self._report_tk_error
        self.widget = Widget(self)

        threads = []
        for key in self.KEYS:   # session först så att den hamnar till vänster
            t = threading.Thread(target=self._icon_thread, args=(key,), daemon=True)
            t.start()
            self.ready[key].wait(10)
            threads.append(t)

        log(f"Ikoner startade: {', '.join(k for k in self.KEYS if self.ready[k].is_set())}")
        threading.Thread(target=self._poll_loop, daemon=True).start()
        if not get_session_key():
            self._change_key()

        self.root.after(PUMP_MS, self._pump)
        self.root.mainloop()
        for t in threads:       # låt ikonerna hinna ta bort sig från systemfältet
            t.join(2)
        log("Avslutat")


def main():
    set_dpi_awareness()
    App().run()


if __name__ == "__main__":
    main()
