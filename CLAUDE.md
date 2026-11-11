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
  per GUID. Ändra inte GUID:erna. pystray är låst till 0.19.x i requirements.txt: kontrollera `_message`
  och `_on_notify` innan du höjer
- Windows knyter en GUID till utgivaren för signerade program (python.exe/pythonw.exe), annars till
  sökvägen. Den osignerade exe-filen får därför GUID:er härledda från sin sökväg (`tray_guid`). Med
  `TRAY_IDS` direkt nekas exe-filen ikonerna
- En GUID kan bara ägas av en process: en andra instans nekas ikonerna (`_tray_added` hindrar den från att
  ändra eller ta bort den första instansens), men visar ändå en egen widget
- `TrayIcon._on_notify` bygger om menyn vid högerklick, eftersom pystray bygger den i förväg. Bocken för
  "Starta med Windows" läses alltid från registret (`autostart_enabled`), aldrig från settings.json
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
- Ikoner ritas i 4x upplösning och skalas ner (PIL-bågar har ingen antialiasing). `assets/icon.ico`
  skapas med `scripts/make_icon.py`. Kör det igen om `make_gauge` eller `make_app_icon` ändras

## Release
- Taggen `vX.Y.Z` måste stämma med `__version__`, annars stoppar release.yml. Testa exe-bygget lokalt i
  `.venv` med requirements-build.txt
- curl_cffi är låst till 0.17.x, eftersom en ny version kan imitera Chrome annorlunda mot Cloudflare

## Språk
- UI-texter finns på engelska och svenska i `TEXTS` och hämtas med `t()`. Lägg alltid till båda.
  Språket följer Windows visningsspråk. `CLAUDE_USAGE_LANG=sv` eller `en` tvingar fram ett språk vid test
- Kommentarer och loggmeddelanden på svenska
