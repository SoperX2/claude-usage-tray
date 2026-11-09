# Claude Usage Tray

Windows tray-app i Python som visar Claude-användning som två mätar-ikoner (gauge, ingen text):
vänster = session (5 h), höger = vecka. Tooltip visar % och nollställningstid, högerklick ger meny.
Samma mätare visas också i en flytande widget (klassen `Widget`) som alltid ligger överst.

## Köra
- `pip install -r requirements.txt`
- `python claude_usage_tray.py` (loggar till konsolen), `pythonw` för att köra utan konsol
- Exe: `python -m PyInstaller --onefile --noconsole --name ClaudeUsage claude_usage_tray.py`

## Datakälla (inofficiellt, odokumenterat API)
- Auth: cookien `sessionKey` från claude.ai, sparad i Windows Credential Manager via `keyring`
  (service `ClaudeUsageTray`, user `sessionKey`)
- `GET https://claude.ai/api/organizations` → välj org med `"chat"` i `capabilities` → `uuid`
- `GET https://claude.ai/api/organizations/{uuid}/usage` → används just nu:
  - `five_hour.utilization` (%), `five_hour.resets_at` (ISO, UTC)
  - `seven_day.utilization`, `seven_day.resets_at`
- Svaret innehåller även (oanvänt hittills): `limits[]` (kind/group/percent/severity/resets_at/is_active),
  `extra_usage`, `spend`, `seven_day_breakdown.rows[]` (andel per Claude Code / Chats / Cowork / Other),
  plus många modellspecifika fält som oftast är `null`
- `curl_cffi` med `impersonate="chrome"` krävs, annars blockerar Cloudflare

## Viktiga detaljer
- pystray på Windows: varje `Icon` måste **skapas och köras i samma tråd** (fönstret skapas i konstruktorn,
  meddelandeloopen läser bara den trådens fönster). Därför en tråd per ikon i `App._icon_thread`.
- Huvudtråden kör Tk:s `mainloop`. All Tk-kod måste köras där: andra trådar lägger en funktion i
  `App.ui_queue`, och `App._pump` läser kön var 100:e ms med `root.after`
- `_pump` bokar nästa körning före uppgifterna, eftersom sessionKey-dialogen kör en egen händelseloop
  inuti sin uppgift och kön måste läsas även då
- `App._ask_session_key` väntar på svar och får inte anropas från Tk-tråden (den låser sig då).
  `_change_key` startar därför alltid en egen tråd
- `_quit` sätter bara `stop_evt` och får anropas från alla trådar. `_pump` stoppar ikonerna och stänger Tk.
  Ctrl+C går via en egen SIGINT-hanterare, för Tk sväljer annars `KeyboardInterrupt` i sina callbacks
- Widgeten: mörk, ogenomskinlig platta i stället för `-transparentcolor`, som ger färgade kanter på
  mätarnas mjuka kanter. Rundade hörn via DWM (bara Windows 11)
- Processen är DPI-medveten per skärm (`SetProcessDpiAwareness(2)`), men Tk skalar inte om widgeten.
  `Widget` gör det själv, med fysiska pixlar från Win32. Pekaren läses med `GetCursorPos`
- `settings.json` i `%APPDATA%\ClaudeUsageTray` (x, y, visible, locked). Position sparas bara när
  widgeten dras, så reservplaceringen vid bortkopplad skärm skriver inte över den
- Windows tooltip max 127 tecken (gäller tray-ikonerna, inte widgetens egen tooltip)
- Ikoner ritas i 4x upplösning och skalas ner (PIL-bågar har ingen antialiasing)
- Ikonfärg följer Windows-temat (`SystemUsesLightTheme` i registret); orange ≥ 80 %, röd ≥ 95 %, nedtonad vid fel
- Notis via `icon.notify` när sessionen passerar 80/95 %, en gång per nollställningsperiod
- `log()` skriver till konsolen; är en no-op under `pythonw` (sys.stdout är None)

## Språk
UI-texter och kommentarer på svenska.
