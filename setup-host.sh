#!/bin/bash
# One-time (re-runnable) setup of the macOS host side of the transcode bridge.
#
#   ./setup-host.sh                  create .env if missing and fill in detectable values, check prerequisites,
#                                    copy the macOS codec bundles, generate the shared secrets, install + start the runner
#                                    and plex-ipbridge (the :32400 listener that passes real client addresses to Plex)
#   ./setup-host.sh --daily-review   additionally install the nightly review LaunchAgent (see README)
#
# Prerequisites: "Plex Media Server.app" in /Applications (installed, never launched long-term: only its Plex Transcoder
# is used) and a Docker engine with Compose (OrbStack or Docker Desktop). Edit .env (at least PLEX_MEDIA_DIRS) before the first full run.
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
if [ -z "$(getv IPBRIDGE_TOKEN)" ]; then setv IPBRIDGE_TOKEN "$(head -c 32 /dev/urandom | base64 | tr -d '\n=/+')"; echo "generated IPBRIDGE_TOKEN"; fi
chmod 600 .env

# media roots: PLEX_MEDIA_DIRS is a ":"-separated list (the old single PLEX_MEDIA_DIR is migrated)
if [ -z "$(getv PLEX_MEDIA_DIRS)" ] || [ "$(getv PLEX_MEDIA_DIRS)" = "$EXAMPLE_MEDIA" ]; then
  legacy="$(getv PLEX_MEDIA_DIR)"
  if [ -n "$legacy" ] && [ "$legacy" != "$EXAMPLE_MEDIA" ]; then setv PLEX_MEDIA_DIRS "$legacy"; echo "migrated PLEX_MEDIA_DIR to PLEX_MEDIA_DIRS"; fi
fi
python3 - <<'PY'
import re
s = open('.env').read(); s = re.sub(r'^PLEX_MEDIA_DIR=.*\n', '', s, flags=re.M); open('.env', 'w').write(s)
PY
MEDIA_DIRS="$(getv PLEX_MEDIA_DIRS)"
if [ -z "$MEDIA_DIRS" ] || [ "$MEDIA_DIRS" = "$EXAMPLE_MEDIA" ]; then
  echo "Edit .env and set PLEX_MEDIA_DIRS to your media root(s) (':'-separated), then re-run ./setup-host.sh"; exit 1
fi
IFS=':' read -r -a _media <<< "$MEDIA_DIRS"
for d in "${_media[@]}"; do [ -d "$d" ] || { echo "media directory does not exist: $d (check PLEX_MEDIA_DIRS in .env)"; exit 1; }; done
# one read-only bind mount per root, loaded automatically through COMPOSE_FILE
python3 -c "import sys; sys.path.insert(0, 'runner'); import envconf; open('docker-compose.media.yml', 'w').write(envconf.media_compose_yaml(envconf.parse_media_dirs(sys.argv[1])))" "$MEDIA_DIRS"
case "$(getv COMPOSE_FILE)" in *docker-compose.media.yml*) ;; *) setv COMPOSE_FILE "docker-compose.yml:docker-compose.media.yml" ;; esac
echo "media roots: ${#_media[@]} (mounted read-only; see docker-compose.media.yml)"
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

# 3. host half of plex-ipbridge (real client addresses): built with the local Go toolchain, or in a container without one
case "$(uname -m)" in arm64) goarch=arm64 ;; *) goarch=amd64 ;; esac
if command -v go >/dev/null 2>&1; then
  (cd ipbridge && CGO_ENABLED=0 GOOS=darwin GOARCH=$goarch go build -trimpath -o ../host-support/plex-ipbridge.new .)
else
  docker run --rm -v "$PROJECT/ipbridge:/src:ro" -v "$PROJECT/host-support:/out" -w /src -e CGO_ENABLED=0 -e GOOS=darwin -e GOARCH=$goarch \
    -e GOCACHE=/tmp/gocache golang:1.27-alpine go build -trimpath -o /out/plex-ipbridge.new .
fi
mv -f host-support/plex-ipbridge.new host-support/plex-ipbridge   # atomic: never rewrite the running binary in place
echo "built host-support/plex-ipbridge"

# 4. LaunchAgents rendered from templates
mkdir -p "$AGENTS"
render() { sed "s#@PROJECT@#$PROJECT#g" "launchd/$1.plist.in" > "$AGENTS/$1.plist"; launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true; launchctl bootstrap "gui/$(id -u)" "$AGENTS/$1.plist"; }
render local.plex-runner
sleep 2
PORT="$(getv PLEX_RUNNER_PORT)"; PORT="${PORT:-32499}"
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && echo "runner is up on 127.0.0.1:$PORT" || { echo "runner did not start; see runner/runner.out and runner/logs/runner.log"; exit 1; }
# It owns :32400 on the Mac. If an older container still publishes 32400 it retries every 10 s until `docker compose up -d`.
render local.plex-ipbridge; echo "installed plex-ipbridge (listens on :32400; log: runner/ipbridge.out)"

if [ "${1:-}" = "--daily-review" ]; then
  [ -f ops/mail.conf ] || { cp ops/mail.conf.example ops/mail.conf; chmod 600 ops/mail.conf; echo "created ops/mail.conf: edit MAIL_TO / MAIL_FROM to enable email reports"; }
  render local.plex-daily-review; echo "installed the nightly review (04:17). See README: it needs 'claude setup-token' for the automated fix step."
fi
echo "Next: ./seed-config.sh (one-time config copy), then: docker compose up -d --build"
