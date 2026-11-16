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
  per GUID. Ändra inte GUID:erna. pystray är låst till 0.19.x i requirements.txt: kontrollera `_message`,
  `_on_notify` och `_message_handlers` (temabyte via WM_SETTINGCHANGE) innan du höjer
- Windows knyter en GUID till utgivaren för signerade program (python.exe/pythonw.exe), annars till
  sökvägen. Den osignerade exe-filen får därför GUID:er härledda från sin sökväg (`tray_guid`). Med
  `TRAY_IDS` direkt nekas exe-filen ikonerna
- Bara en kopia kör: `main` tar mutexen `Local\ClaudeUsageTray` (samma för exe och källkod). Finns den
  redan rundsänder den nya kopian `ClaudeUsageTray.ShowWidget` (RegisterWindowMessageW), som
  sessionsikonens dolda fönster i den körande kopian tar emot, och avslutar sig innan något fönster skapas.
  `--login` undantas. Ändra inte namnen: äldre och nyare versioner måste känna igen varandra
- En GUID kan bara ägas av en process: en andra instans (t.ex. en äldre version utan mutex) nekas ikonerna
  (`_tray_added` hindrar den från att ändra eller ta bort den första instansens)
- `TrayIcon._on_notify` bygger om menyn vid högerklick, eftersom pystray bygger den i förväg. Bygg aldrig
  om tray-menyn från en annan tråd (`update_menu`): det kan förstöra en meny som visas. Bocken för
  "Starta med Windows" läses alltid från registret (`autostart_enabled`), aldrig från settings.json
- Bara en dialog åt gången (`AppDialog.raise_open`), eftersom de är modala och tar grab
- Nya dialoger ärver `AppDialog` och lägger knapparna i `add_buttons`, inte `buttonbox`: då får de
  Windows apptema (`apply_window_theme`) innan de visas. Andra fönster anropar `apply_window_theme` själva.
  `_darken` färgar bara Frame, Label, Entry och Button: lägg till fler typer (Checkbutton, Radiobutton,
  Text, Scrollbar …) i den när en dialog börjar använda dem, annars blir de ljusa i mörkt läge
- Menyerna blir mörka via uxtheme (`enable_dark_menus`). Widgeten har ingen egen meny: högerklick postar
  ikonens WM_RBUTTONUP till sessionsikonens dolda fönster (`App.show_menu`), så pystray visar samma
  inbyggda meny i sin tråd. Använd inte `tk.Menu`: Tk ritar menyraderna själv och blir aldrig mörk.
  Ett högerklick innan sessionsikonen är klar köas (`menu_waiting`) och visas när `ready` sätts
- Vänsterklick på en tray-ikon kör menyns standardval (`default=True`), som är "Visa widget": det visar
  widgeten och lägger den överst, men döljer den aldrig. Döljning sker bara med ✕ eller "Dölj widget"
  i menyn. pystray kör standardvalet även när raden är dold (`visible=False`), så "Visa widget" syns
  (i fetstil) bara när widgeten är dold, och "Dölj widget" bara när den syns
- `Widget.set_visible(visible, reason)` loggar alltid orsaken ("Widgeten döljs (✕ på widgeten)"), så att
  en försvunnen widget kan spåras i loggen. Skicka alltid med en begriplig orsak
- Widgeten håller sig överst med `SetWindowPos(HWND_TOPMOST)` när den visas, vid temabyte och skärmändring,
  och med en timer (`_topmost_tick`) som lyfter den om ett vanligt fönster hamnat ovanför eller flaggan
  tappats. Andra "alltid överst"-fönster och spel i exklusiv helskärm lämnas i fred. Använd alltid
  SWP_NOACTIVATE: widgeten får aldrig ta fokus
- Widgetens ✕ visas vid hovring, även när positionen är låst
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
- `settings.json` i `%APPDATA%\ClaudeUsageTray`. Position sparas bara när
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
- Kommentarer och appens egna loggtexter på svenska. En loggrad får innehålla UI-texter på appens språk
  (t.ex. felmeddelandet som också visas i menyn). Meddelanden i GitHub-workflows skrivs på engelska
