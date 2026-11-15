#!/bin/bash
# One-time (re-runnable) setup of the macOS host side of the transcode bridge.
#
#   ./setup-host.sh                  create .env if missing and fill in detectable values, check prerequisites,
#                                    copy the macOS codec bundles, generate the shared secret, install + start the runner
#   ./setup-host.sh --daily-review   additionally install the nightly review LaunchAgent (see README)
#
# Prerequisites: "Plex Media Server.app" in /Applications (installed, never launched long-term: only its Plex Transcoder
# is used) and Docker/OrbStack. Edit .env (at least PLEX_MEDIA_DIR) before the first full run.
set -euo pipefail
cd "$(dirname "$0")"
PROJECT="$(pwd)"
APP="/Applications/Plex Media Server.app"
NATIVE_CODECS="$HOME/Library/Application Support/Plex Media Server/Codecs"
AGENTS="$HOME/Library/LaunchAgents"
EXAMPLE_MEDIA="/path/to/your/media"

getv() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' || true; }
setv() {
  python3 - "$1" "$2" <<'PY'
import re, sys
k, v = sys.argv[1], sys.argv[2]; s = open('.env').read()
if re.search(r'^%s=' % re.escape(k), s, re.M): s = re.sub(r'^%s=.*$' % re.escape(k), lambda m: '%s=%s' % (k, v), s, flags=re.M)
else: s += '\n%s=%s\n' % (k, v)
open('.env', 'w').write(s)
PY
}

[ -d "$APP" ] || { echo "Install Plex Media Server.app in /Applications first (do not keep it running)."; exit 1; }

# 1. .env: create from the example, then fill in what can be detected (only values still at their example placeholder)
if [ ! -f .env ]; then
  cp .env.example .env; chmod 600 .env
  setv PLEX_UID "$(id -u)"; setv PLEX_GID "$(id -g)"
  tz="$(readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##')"; [ -n "$tz" ] && setv TZ "$tz"
  echo "created .env from .env.example"
fi
[ "$(getv PLEX_PROJECT_DIR)" = "/path/to/this/project" ] && setv PLEX_PROJECT_DIR "$PROJECT"
if [ "$(getv PLEX_LAN_IP)" = "192.168.1.10" ]; then ip="$(ipconfig getifaddr en0 2>/dev/null || true)"; [ -n "$ip" ] && setv PLEX_LAN_IP "$ip"; fi
if [ -z "$(getv BRIDGE_TOKEN)" ]; then setv BRIDGE_TOKEN "$(head -c 32 /dev/urandom | base64 | tr -d '\n=/+')"; echo "generated BRIDGE_TOKEN"; fi
chmod 600 .env

MEDIA="$(getv PLEX_MEDIA_DIR)"
if [ -z "$MEDIA" ] || [ "$MEDIA" = "$EXAMPLE_MEDIA" ] || [ ! -d "$MEDIA" ]; then
  echo "Edit .env and set PLEX_MEDIA_DIR to an existing media directory, then re-run ./setup-host.sh"; exit 1
fi
appver="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$APP/Contents/Info.plist" 2>/dev/null || true)"
case "$(getv PLEX_VERSION)" in "$appver"*) ;; *) echo "WARNING: PLEX_VERSION ($(getv PLEX_VERSION)) does not match the installed app ($appver). They must be the same version." ;; esac

# 2. scratch dirs and the macOS codec bundles the Plex Transcoder needs
mkdir -p host-support/home host-support/Codecs runner/logs transcode reports
if ! ls host-support/Codecs/*darwin* >/dev/null 2>&1; then
  if ls "$NATIVE_CODECS"/*darwin* >/dev/null 2>&1; then
    cp -R "$NATIVE_CODECS"/*darwin* host-support/Codecs/; echo "copied macOS codec bundles from the native Plex install"
  else
    echo "No macOS codec bundles found. Launch Plex Media Server.app once so it downloads them, quit it, then re-run."; exit 1
  fi
fi

# 3. LaunchAgents rendered from templates
mkdir -p "$AGENTS"
render() { sed "s#@PROJECT@#$PROJECT#g" "launchd/$1.plist.in" > "$AGENTS/$1.plist"; launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true; launchctl bootstrap "gui/$(id -u)" "$AGENTS/$1.plist"; }
render local.plex-runner
sleep 2
PORT="$(getv PLEX_RUNNER_PORT)"; PORT="${PORT:-32499}"
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && echo "runner is up on 127.0.0.1:$PORT" || { echo "runner did not start; see runner/runner.out and runner/logs/runner.log"; exit 1; }

if [ "${1:-}" = "--daily-review" ]; then
  [ -f ops/mail.conf ] || { cp ops/mail.conf.example ops/mail.conf; chmod 600 ops/mail.conf; echo "created ops/mail.conf: edit MAIL_TO / MAIL_FROM to enable email reports"; }
  render local.plex-daily-review; echo "installed the nightly review (04:17). See README: it needs 'claude setup-token' for the automated fix step."
fi
echo "Next: ./seed-config.sh (one-time config copy), then: docker compose up -d --build"
