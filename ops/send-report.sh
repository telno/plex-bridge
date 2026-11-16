#!/bin/bash
# usage: ops/send-report.sh <subject> <body-file>
# Reads ops/mail.conf (git-ignored): MAIL_TO, MAIL_FROM (an account configured in Mail.app), MAIL_WHEN=always|attention,
# MAIL_FORMAT=rich|plain (default rich: the markdown report is laid out and styled by ops/render-report.py;
# plain sends the markdown as is).
set -u
cd "$(dirname "$0")/.."
[ -r ops/mail.conf ] && . ops/mail.conf
[ -n "${MAIL_TO:-}" ] && [ -n "${MAIL_FROM:-}" ] || { echo "ops/mail.conf missing MAIL_TO/MAIL_FROM"; exit 3; }
body="$2"; tmp=""
if [ "${MAIL_FORMAT:-rich}" != plain ]; then
  tmp="$(mktemp -d -t plex-report)"
  if python3 ops/render-report.py "$2" "$tmp/body.txt" "$tmp/styles" 2>/dev/null; then body="$tmp/body.txt"
  else rm -rf "$tmp"; tmp=""; fi   # rendering problem: send the markdown as plain text instead
fi
out="$(osascript ops/send-mail.applescript "$1" "$body" "$MAIL_TO" "$MAIL_FROM" ${tmp:+"$tmp/styles"} 2>&1)"; rc=$?
[ -n "$tmp" ] && rm -rf "$tmp"
[ $rc -eq 0 ] && echo "mail: $out to ${MAIL_TO%%@*}@…" || { echo "mail FAILED (rc=$rc): $out"; exit $rc; }
