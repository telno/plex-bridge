#!/bin/bash
# usage: ops/send-report.sh <subject> <body-file>
# Reads ops/mail.conf (git-ignored): MAIL_TO, MAIL_FROM (an account configured in Mail.app), MAIL_WHEN=always|attention
set -u
cd "$(dirname "$0")/.."
[ -r ops/mail.conf ] && . ops/mail.conf
[ -n "${MAIL_TO:-}" ] && [ -n "${MAIL_FROM:-}" ] || { echo "ops/mail.conf missing MAIL_TO/MAIL_FROM"; exit 3; }
out="$(osascript ops/send-mail.applescript "$1" "$2" "$MAIL_TO" "$MAIL_FROM" 2>&1)"; rc=$?
[ $rc -eq 0 ] && echo "mail: $out to ${MAIL_TO%%@*}@…" || { echo "mail FAILED (rc=$rc): $out"; exit $rc; }
