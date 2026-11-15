#!/usr/bin/env bash
# Laddar upp den publicerade ClaudeUsage.exe för en release till VirusTotal och skriver
# detektionssiffran i release-anteckningarna. Körs av .github/workflows/virustotal.yml.
#
# Skanningen är bara information. Skriptet avslutar alltid med 0, och alla fel blir varningar.
#
# Miljövariabler: TAG, VT_API_KEY, GH_TOKEN, GH_REPO (och RUNNER_TEMP i GitHub Actions).
#
# Nyckeln:
# - skrivs bara till en temporär fil (med printf, som är inbyggt i bash och inte startar någon
#   process), och curl läser den med -H @fil. Den syns alltså aldrig på en kommandorad
# - den temporära mappen ligger i RUNNER_TEMP, utanför arbetskatalogen, och tas alltid bort (trap)
# - curl körs utan -v, och alla varningar maskeras och görs till en rad innan de skrivs ut
#
# Tidsgräns: hela skriptet håller sig inom BUDGET sekunder, även om VirusTotal eller GitHub inte
# svarar. Varje nätverksanrop har en egen gräns, och väntan på analysen slutar i tid för att
# anteckningarna alltid ska hinna skrivas, med reservraden om analysen inte blev klar.
set -uo pipefail
export PYTHONIOENCODING=utf-8   # å, ä, ö och emojier i anteckningarna, oavsett maskinens inställning

API="https://www.virustotal.com/api/v3"
BUDGET="${VT_BUDGET_SECONDS:-900}"          # hela skriptet, 15 min. Jobbets gräns är 20 min.
POLL_SECONDS="${VT_POLL_SECONDS:-30}"       # gratis-API:t tillåter 4 anrop per minut
GET_TIMEOUT="${VT_GET_TIMEOUT:-30}"         # per GET mot VirusTotal
UPLOAD_TIMEOUT="${VT_UPLOAD_TIMEOUT:-300}"  # uppladdningen av exe-filen
GH_TIMEOUT="${VT_GH_TIMEOUT:-30}"           # per gh-anrop för anteckningarna
DOWNLOAD_TIMEOUT="${VT_DOWNLOAD_TIMEOUT:-120}"
NOTES_RESERVE=$(( 2 * GH_TIMEOUT + 10 ))    # sparas till slutet: läsa och skriva anteckningarna
deadline=$(( SECONDS + BUDGET ))
left() { echo $(( deadline - SECONDS )); }

# Varningar maskeras och blir en enda rad. Annars visas bara första raden som varning, och en rad
# från ett svar som börjar med :: kunde tolkas som ett kommando till GitHub Actions.
warn() {
  local msg="$*"
  if [[ -n "${VT_API_KEY:-}" ]]; then msg="${msg//"$VT_API_KEY"/***}"; fi
  msg="${msg//$'\r'/ }"
  msg="${msg//$'\n'/ }"
  echo "::warning::$msg"
}

if [[ -z "${VT_API_KEY:-}" ]]; then
  warn "VT_API_KEY is not set, skipping VirusTotal"
  exit 0
fi
if [[ ! "${TAG:-}" =~ ^v[0-9]+(\.[0-9]+){0,3}$ ]]; then
  warn "Not a version tag: '${TAG:-}'. Expected something like v1.0.2"
  exit 0
fi

# Utan temporär mapp fortsätter vi inte, annars kunde nyckelfilen hamna någon annanstans
if ! work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/vt.XXXXXX")" || [[ ! -d "$work" ]]; then
  warn "Could not create a temporary directory, skipping VirusTotal"
  exit 0
fi
trap 'rm -rf "$work"' EXIT
hdr="$work/header"
if ! (umask 077; printf 'x-apikey: %s\n' "$VT_API_KEY" > "$hdr"); then
  warn "Could not write the temporary header file, skipping VirusTotal"
  exit 0
fi

# Läser ett fält ur senaste svaret, t.ex. json data.attributes.status. Tom text om det saknas.
json() {
  python3 - "$work/resp.json" "$1" 2> /dev/null <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for key in sys.argv[2].split("."):
    value = value[key]
print(json.dumps(value) if isinstance(value, (dict, list)) else value)
PY
}

# VirusTotals egen feltext ur senaste svaret, eller början av svaret om det inte är JSON
vt_error_text() {
  python3 - "$work/resp.json" <<'PY'
import json, sys
raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
try:
    err = json.loads(raw)["error"]
    print(f"{err.get('code')}: {err.get('message')}")
except Exception:
    print(raw[:500].strip() or "(empty response)")
PY
}

# Gör ett anrop mot VirusTotal: vt <beskrivning> <max sekunder> <curl-argument…>.
# Sätter STATUS (HTTP-kod) och vid fel ERR. Svaret hamnar i $work/resp.json.
vt() {
  local what="$1" max_time="$2"; shift 2
  echo "VirusTotal: $what"
  : > "$work/resp.json"
  STATUS="$(curl -sS --connect-timeout 10 --max-time "$max_time" -o "$work/resp.json" -w '%{http_code}' \
    -H @"$hdr" "$@" 2> "$work/curl.err")"
  local rc=$?
  if (( rc != 0 )); then
    STATUS="none (curl exit $rc)"
    ERR="$(head -c 500 "$work/curl.err")"
    return 1
  fi
  if [[ "$STATUS" != 2* ]]; then
    ERR="$(vt_error_text)"
    return 1
  fi
}

# Skriver raden i release-anteckningarna. En tidigare VirusTotal-rad ersätts, så att en ny körning
# inte ger två. Anteckningarna skrivs bara över om den nya texten ser rätt ut.
write_notes() {
  local line="$1"
  echo "$line"
  if ! timeout "$GH_TIMEOUT" gh release view "$TAG" --json body --jq .body > "$work/body.md"; then
    warn "Could not read the release notes"
    return
  fi
  if ! python3 - "$work/body.md" "$line" "$work/notes.md" <<'PY'
import sys
out, drop_blank = [], False
for line in open(sys.argv[1], encoding="utf-8").read().splitlines():
    if line.startswith("**VirusTotal:**"):
        # Bara tomraden direkt efter den gamla raden tas bort, resten av texten lämnas orörd
        drop_blank = not out or out[-1] == ""
        continue
    if drop_blank and line == "":
        drop_blank = False
        continue
    drop_blank = False
    out.append(line)
kept = "\n".join(out).rstrip()
open(sys.argv[3], "w", encoding="utf-8", newline="\n").write(f"{kept}\n\n{sys.argv[2]}\n" if kept else f"{sys.argv[2]}\n")
PY
  then
    warn "Could not build the new release notes, leaving them unchanged"
    return
  fi
  if ! grep -qxF -- "$line" "$work/notes.md"; then
    warn "The new release notes look wrong, leaving them unchanged"
    return
  fi
  if ! timeout "$GH_TIMEOUT" gh release edit "$TAG" --notes-file "$work/notes.md" > /dev/null; then
    warn "Could not update the release notes"
    return
  fi
  echo "Release notes for $TAG updated"
}

# 1. Exakt den fil som användarna laddar ner, kontrollerad mot releasens .sha256-fil
dl="$work/download"
if ! timeout "$DOWNLOAD_TIMEOUT" gh release download "$TAG" --dir "$dl" \
    --pattern ClaudeUsage.exe --pattern ClaudeUsage.exe.sha256; then
  warn "Could not download ClaudeUsage.exe from the release $TAG"
  exit 0
fi
if ! (cd "$dl" && sha256sum --check --strict ClaudeUsage.exe.sha256); then
  warn "ClaudeUsage.exe in $TAG does not match its .sha256 file, not scanning it"
  exit 0
fi
exe="$dl/ClaudeUsage.exe"
sha="$(cut -d' ' -f1 "$dl/ClaudeUsage.exe.sha256")"
link="https://www.virustotal.com/gui/file/$sha"
echo "Scanning ClaudeUsage.exe from $TAG, SHA256 $sha"

# 2. Ladda upp. upload_url fungerar för alla storlekar, den vanliga adressen bara under 32 MB.
#    Uppladdningsadressen loggas inte: den är en engångsadress som ger rätt att ladda upp.
if ! vt "getting an upload URL" "$GET_TIMEOUT" "$API/files/upload_url"; then
  warn "VirusTotal: getting an upload URL failed. HTTP status: $STATUS. Error: $ERR"
  exit 0
fi
upload_url="$(json data)"
if [[ "$upload_url" != https://* ]]; then
  warn "VirusTotal: the response had no upload URL. Response: $(head -c 300 "$work/resp.json")"
  exit 0
fi
upload_time=$(( $(left) - NOTES_RESERVE ))
(( upload_time > UPLOAD_TIMEOUT )) && upload_time=$UPLOAD_TIMEOUT
if (( upload_time < 30 )); then
  warn "VirusTotal: no time left to upload the file"
  exit 0
fi
if ! vt "uploading ClaudeUsage.exe ($(stat -c %s "$exe") bytes)" "$upload_time" -F "file=@$exe" "$upload_url"; then
  warn "VirusTotal: upload failed. HTTP status: $STATUS. Error: $ERR"
  exit 0
fi
analysis_id="$(json data.id)"
if [[ -z "$analysis_id" ]]; then
  warn "VirusTotal: the upload response had no analysis id. Response: $(head -c 300 "$work/resp.json")"
  exit 0
fi

# 3. Vänta på analysen så länge tiden räcker. Ett tillfälligt fel (429, 5xx, nätverk) avbryter inte.
result="timeout"
stats=""
while (( $(left) > POLL_SECONDS + GET_TIMEOUT + NOTES_RESERVE )); do
  sleep "$POLL_SECONDS"
  if ! vt "checking the analysis ($(left) s left)" "$GET_TIMEOUT" "$API/analyses/$analysis_id"; then
    warn "VirusTotal: status check failed, trying again. HTTP status: $STATUS. Error: $ERR"
    continue
  fi
  if [[ "$(json data.attributes.status)" == "completed" ]]; then
    stats="$(json data.attributes.stats)"
    result="completed"
    break
  fi
done

# 4. Raden i anteckningarna. Något skrivs alltid när filen väl har laddats upp.
case "$result" in
  completed)
    if line="$(python3 - "$stats" "$link" 2> /dev/null <<'PY'
import json, sys
s = json.loads(sys.argv[1])
total = sum(int(s.get(k, 0)) for k in ("malicious", "suspicious", "undetected", "harmless"))
if total == 0:
    sys.exit(1)
print(f"**VirusTotal:** {int(s.get('malicious', 0))}/{total} engines flag ClaudeUsage.exe as malicious "
      f"({int(s.get('suspicious', 0))} suspicious). [Full report]({sys.argv[2]})")
PY
)"; then
      write_notes "$line"
    else
      warn "VirusTotal: the analysis finished, but its result could not be read: ${stats:-(no stats)}"
      write_notes "**VirusTotal:** the analysis finished, but the result could not be read automatically. [See the report]($link)"
    fi
    ;;
  *)
    warn "VirusTotal: the analysis was not finished within the time limit"
    write_notes "**VirusTotal:** the analysis was not finished in time. [See the report]($link)"
    ;;
esac
exit 0
