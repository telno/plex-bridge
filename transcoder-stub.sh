#!/bin/bash
# Stub installed in place of "Plex Transcoder" inside the container.
# Forwards the invocation (args + X_PLEX_*/PLEX_* env) to the macOS runner and relays output/exit code.
# Falls back to the real (software) Linux transcoder if the runner is unreachable.
REAL="/usr/lib/plexmediaserver/Plex Transcoder.real"
URL="${PLEX_RUNNER_URL:-http://host.docker.internal:32499}"
LOGF="/transcode/stub-calls.log"

esc() { local s=$1
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}; s=${s//$'\t'/\\t}
  s=${s//[$'\x01'-$'\x08'$'\x0b'$'\x0c'$'\x0e'-$'\x1f']/}
  printf '%s' "$s"; }

# Minimal audit trail of what Plex asked for, before translation (argv + env KEY NAMES only; tokens redacted).
# The runner's bridge.jsonl is the authoritative log; this one also shows calls that never reach the runner.
[ -f "$LOGF" ] && [ "$(stat -c %s "$LOGF" 2>/dev/null || echo 0)" -gt 5000000 ] && mv -f "$LOGF" "$LOGF.1" 2>/dev/null
{ printf '%s pid=%s cwd=%s argv:' "$(date +%FT%T)" "$$" "$PWD"; printf ' [%s]' "$@" | sed -E 's/(X-Plex-Token=|X_PLEX_TOKEN=)[^&" ]*/\1***/g'; echo
  printf '   envkeys: '; env | cut -d= -f1 | grep -E '^(X_PLEX|PLEX|FFMPEG|LD_|EAE|TRANSCODER)' | tr '\n' ' '; echo; } >> "$LOGF" 2>/dev/null

if ! curl -sf -m 2 "$URL/health" >/dev/null 2>&1; then
  echo "$(date +%FT%T) pid=$$ FALLBACK: runner unreachable, using local software transcoder" >> "$LOGF" 2>/dev/null
  echo "plex-stub: runner unreachable, falling back to local transcoder" >&2
  exec "$REAL" "$@"
fi

body="{\"cwd\":\"$(esc "$PWD")\",\"argv\":["
first=1
for a in "$@"; do [ $first = 1 ] || body+=","; first=0; body+="\"$(esc "$a")\""; done
body+="],\"env\":{"
first=1
while IFS='=' read -r -d '' k v; do
  case "$k" in X_PLEX_*|PLEX_*) [ $first = 1 ] || body+=","; first=0; body+="\"$(esc "$k")\":\"$(esc "$v")\"";; esac
done < <(env -0)
body+="}}"

fifo=$(mktemp -u /tmp/plexstub.XXXXXX); mkfifo "$fifo"
# Plex stops a job (seek, stop, quality change) with SIGKILL, which the trap below cannot catch. Without --pdeathsig the
# orphaned curl would keep the connection open, the runner would never see a disconnect, and the host transcoder would
# run on until its own callbacks to Plex fail. With it, the kernel ends curl with this script and the runner kills the job.
PDEATH=(); command -v setpriv >/dev/null 2>&1 && PDEATH=(setpriv --pdeathsig TERM)
"${PDEATH[@]}" curl -sSN -X POST -H "Authorization: Bearer ${BRIDGE_TOKEN}" --data-binary @- "$URL/run" <<<"$body" > "$fifo" &
CURL=$!
trap 'kill $CURL 2>/dev/null; rm -f "$fifo"; exit 143' TERM INT HUP
code=""; first=""; n=0
while IFS= read -r line || [ -n "$line" ]; do
  n=$((n+1)); [ $n = 1 ] && first=$line
  case "$line" in ::EXIT::*) code=${line#::EXIT::};; *) printf '%s\n' "$line" >&2;; esac
done < "$fifo"
wait $CURL 2>/dev/null
rm -f "$fifo"
[ -n "$code" ] && exit "$code"
# The runner refused the job before starting it (busy / rejected / unauthorized): run locally instead of failing playback.
case "$first" in busy|rejected:*|unauthorized|"relay error")
  echo "$(date +%FT%T) pid=$$ FALLBACK: runner said '$first', using local software transcoder" >> "$LOGF" 2>/dev/null
  exec "$REAL" "$@";;
esac
echo "plex-stub: no exit status from runner" >&2
exit 1
