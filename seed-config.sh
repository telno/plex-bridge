#!/bin/bash
# ONE-TIME copy of the native macOS Plex config into the plex_config Docker volume.
# Refuses to run if native Plex is running or the volume already has data (use --force to overwrite).
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "Missing .env: run ./setup-host.sh first."; exit 1; }
set -a; . ./.env; set +a
SRC="$HOME/Library/Application Support/Plex Media Server"
PLIST="$HOME/Library/Preferences/com.plexapp.plexmediaserver.plist"
IMAGE="plex-bridged:${PLEX_VERSION:?PLEX_VERSION missing in .env}"
DEST="/config/Library/Application Support/Plex Media Server"
TRANSCODE_DIR=/transcode
PUID="${PLEX_UID:-501}"; PGID="${PLEX_GID:-20}"

pgrep -f "Plex Media Server.app/Contents/MacOS/Plex Media Server" >/dev/null && { echo "Native Plex is running; quit it first."; exit 1; }
docker volume create plex_config >/dev/null
if [ "${1:-}" != "--force" ] && docker run --rm -v plex_config:/config --entrypoint sh "$IMAGE" -c '[ -e "'"$DEST"'/Plug-in Support" ]'; then
  echo "plex_config already seeded. Use --force to overwrite."; exit 1; fi

echo "Copying config (excluding platform-specific/transient dirs)..."
COPYFILE_DISABLE=1 tar -C "$SRC" --no-xattrs \
  --exclude='./Codecs' --exclude='./Updates' --exclude='./Crash Reports' --exclude='./Diagnostics' \
  --exclude='./Drivers' --exclude='./Logs' --exclude='./Cache' --exclude='./Plug-ins' \
  --exclude='*.db-20*' --exclude='.DS_Store' --exclude='.LocalAdminToken' -cf - . \
| docker run --rm -i -v plex_config:/config --entrypoint sh "$IMAGE" -c \
  "mkdir -p '$DEST' && tar -C '$DEST' -xf - && chown -R $PUID:$PGID /config"

echo "Generating Preferences.xml from the macOS plist..."
python3 - "$PLIST" "$TRANSCODE_DIR" "${PLEX_EXTRA_LAN_NETWORKS:-}" <<'PY' | docker run --rm -i -v plex_config:/config --entrypoint sh "$IMAGE" -c \
  "cat > '$DEST/Preferences.xml' && chown $PUID:$PGID '$DEST/Preferences.xml' && chmod 600 '$DEST/Preferences.xml'"
import json, subprocess, sys
from xml.sax.saxutils import quoteattr
d = json.loads(subprocess.check_output(["plutil", "-convert", "json", "-o", "-", sys.argv[1]]))
d.pop("LastAutomaticMappedPort", None)
d["TranscoderTempDirectory"] = sys.argv[2]
# optional extra "LAN networks" from .env (PLEX_EXTRA_LAN_NETWORKS); the "allowed without auth" list is left exactly as it was
nets = [n for n in d.get("LanNetworksBandwidth", "").split(",") if n]
nets += [n.strip() for n in sys.argv[3].split(",") if n.strip() and n.strip() not in nets]
if nets: d["LanNetworksBandwidth"] = ",".join(nets)
d["FSEventLibraryUpdatesEnabled"] = False; d["FSEventLibraryPartialScanEnabled"] = False  # no inotify over bind mounts
out = ['<?xml version="1.0" encoding="utf-8"?>', "<Preferences"]
for k in sorted(d):
    v = d[k]; v = ("1" if v else "0") if isinstance(v, bool) else str(v)
    out.append("  %s=%s" % (k, quoteattr(v)))
out.append("/>")
print("\n".join(out))
PY
echo "Done. Native config was only read, never modified."
