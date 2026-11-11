# Claude Usage Tray

## Datakälla (inofficiellt, odokumenterat API)
- Svaret innehåller även (oanvänt hittills): `limits[]` (kind/group/percent/severity/resets_at/is_active),
  `extra_usage`, `spend`, `seven_day_breakdown.rows[]` (andel per Claude Code / Chats / Cowork / Other),
  plus många modellspecifika fält som oftast är `null`
- `curl_cffi` med `impersonate="chrome"` krävs, annars blockerar Cloudflare

## Viktiga detaljer
- pystray på Windows: varje `Icon` måste **skapas och köras i samma tråd** (fönstret skapas i konstruktorn,
  meddelandeloopen läser bara den trådens fönster). Därför en tråd per ikon i `App._icon_thread`.
- `TrayIcon` ersätter pystrays privata `_message` för att sätta fast `uID` och GUID (`TRAY_IDS`), annars
  blir `uID` alltid 0 (pystray skickar felstavat `hID`). Windows sparar valet i "Andra ikoner i systemfältet"
  per GUID. Ändra inte GUID:erna, och kontrollera `_message` vid uppgradering av pystray
- En GUID kan bara ägas av en process: en andra instans nekas ikonerna (`_tray_added` hindrar den från att
  ändra eller ta bort den första instansens), men visar ändå en egen widget
- Widgetens ✕ visas vid hovring, även när positionen är låst. Trayns "Visa/Dölj widget" kräver
  `_update_tray_menus`, eftersom pystray bara bygger om menyn när `update_menu` anropas
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
- Ikoner ritas i 4x upplösning och skalas ner (PIL-bågar har ingen antialiasing)

## Språk
UI-texter och kommentarer på svenska.
