#!/bin/bash
# Daily review of the Plex bridge: deterministic digest -> (only if something is wrong) headless Claude review
# that may fix and commit -> dated report in reports/ -> macOS notification (+ optional mail).
#
# Env: FORCE_REVIEW=1  run the Claude step even if the digest is clean
#      DRY_RUN=1       print the claude command instead of running it
#      PLEX_REVIEW_MODEL=sonnet   model for the Claude step (default sonnet: lighter on subscription limits)
# Email: ops/mail.conf (git-ignored) -> sent through Mail.app by ops/send-report.sh.
# Claude step: runs on the subscription login only (token in ~/.claude/unattended-oauth-token); API-key / cloud-provider
# variables are unset, and there is no dollar budget flag because nothing here is billed per token.
set -uo pipefail
cd "$(dirname "$0")/.."
P="$(pwd)"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.orbstack/bin"
# Unattended auth: create a long-lived token once with `claude setup-token` and save it (chmod 600) in this file.
TOKEN_FILE="$HOME/.claude/unattended-oauth-token"
[ -r "$TOKEN_FILE" ] && export CLAUDE_CODE_OAUTH_TOKEN="$(tr -d '\n' < "$TOKEN_FILE")"
D="$(date +%F)"; R="$P/reports"; mkdir -p "$R"
DIG_JSON="$R/$D.digest.json"; DIG_MD="$R/$D.digest.md"; REPORT="$R/$D.md"; COUT="$R/$D.claude.txt"; CERR="$R/$D.claude.err"
LOCK="$R/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then echo "another review is running"; exit 0; fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

notify() {  # $1 = message
  osascript -e "display notification \"${1//\"/\\\"}\" with title \"Plex bridge daily review\"" >/dev/null 2>&1 || true
  local st; st="$(grep -m1 -oE 'STATUS: [a-z ]+' "$REPORT" 2>/dev/null || echo 'STATUS: needs attention')"
  ( [ -r "$P/ops/mail.conf" ] && . "$P/ops/mail.conf"
    if [ -n "${MAIL_TO:-}" ] && { [ "${MAIL_WHEN:-always}" = always ] || [ "$st" != "STATUS: all clear" ]; }; then
      "$P/ops/send-report.sh" "Plex bridge daily review $D: ${st#STATUS: }" "$REPORT" >> "$R/mail.log" 2>&1 || echo "$(date +%FT%T) email failed" >> "$R/mail.log"
    fi )
}
advance_cursor() { python3 -c "import json;print(json.load(open('$DIG_JSON'))['window']['until'])" > "$R/.cursor" 2>/dev/null || true; }
finish() {  # $1 = first line for the notification
  ln -sf "$REPORT" "$R/latest.md"
  find "$P/transcode" -maxdepth 1 -type d -name 'review-*' -mtime +7 -exec rm -rf {} + 2>/dev/null || true
  find "$R" -maxdepth 1 -type f -name '20*' -mtime +60 -delete 2>/dev/null || true
  notify "$1"
}

python3 ops/daily-digest.py --json "$DIG_JSON" --markdown "$DIG_MD"; rc=$?

if [ $rc -eq 2 ]; then
  { echo "# Plex bridge daily review $D"; echo; echo "**STATUS: needs attention**"; echo; echo "The digest script itself failed; see launchd.log."; } > "$REPORT"
  finish "needs attention: digest script failed"; exit 1
fi

if [ $rc -eq 0 ] && [ -z "${FORCE_REVIEW:-}" ]; then
  { echo "# Plex bridge daily review $D"; echo; echo "**STATUS: all clear**"; echo; echo "No Claude review needed."; echo; cat "$DIG_MD"; } > "$REPORT"
  advance_cursor; finish "all clear ($(grep -m1 '^Jobs:' "$DIG_MD" | cut -c1-90))"; exit 0
fi

# Subscription-only: never route to an API key or a cloud provider.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY
BEFORE="$(git rev-parse HEAD)"
PROMPT="$(sed "s#@PROJECT@#$P#g" ops/daily-review-prompt.md)

Digest file (JSON): $DIG_JSON
Digest summary:
$(cat "$DIG_MD")"

CMD=(claude -p "$PROMPT" --permission-mode acceptEdits --no-session-persistence --output-format text
     --model "${PLEX_REVIEW_MODEL:-sonnet}"
     --allowedTools "Read" "Glob" "Grep" "Edit" "Write"
       "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git show:*)" "Bash(git add:*)" "Bash(git commit:*)" "Bash(git revert:*)"
       "Bash(python3:*)" "Bash(curl:*)" "Bash(docker logs:*)" "Bash(docker exec plex:*)" "Bash(docker compose:*)" "Bash(docker ps:*)" "Bash(docker inspect:*)"
       "Bash(launchctl kickstart:*)" "Bash(ps:*)" "Bash(ls:*)" "Bash(tail:*)" "Bash(head:*)" "Bash(grep:*)" "Bash(cat:*)" "Bash(mkdir:*)" "Bash(sleep:*)" "Bash(bash -n:*)"
       "Bash(/Applications/Plex Media Server.app/Contents/MacOS/Plex Transcoder:*)"
     --disallowedTools "Bash(sudo:*)" "Bash(rm:*)" "Bash(git push:*)" "Bash(git branch:*)" "Bash(git checkout:*)" "Bash(git switch:*)"
       "Bash(git worktree:*)" "Bash(git reset:*)" "Bash(git clean:*)" "Bash(docker rm:*)" "Bash(docker volume:*)" "Bash(docker system:*)" "Bash(docker compose down:*)")

if [ -n "${DRY_RUN:-}" ]; then echo "would run: claude -p <prompt, $(printf %s "$PROMPT" | wc -c | tr -d ' ') bytes> ${CMD[*]:3}" | cut -c1-900; exit 0; fi

# 45 minute cap (macOS has no timeout(1))
AUTHFAIL=""; LIMITED=""
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  crc=99; AUTHFAIL=1; : > "$COUT"; echo "no unattended token at $TOKEN_FILE; Claude step skipped" > "$CERR"
else
  perl -e 'alarm 2700; exec @ARGV' -- "${CMD[@]}" > "$COUT" 2> "$CERR" < /dev/null; crc=$?
  grep -qi "failed to authenticate\|setup-token\|not logged in" "$COUT" "$CERR" 2>/dev/null && AUTHFAIL=1
  grep -qiE "usage limit|rate limit|limit reached|too many requests|overloaded|quota" "$COUT" "$CERR" 2>/dev/null && LIMITED=1
fi
AFTER="$(git rev-parse HEAD)"
COMMITS="$(git log --oneline "$BEFORE..$AFTER" 2>/dev/null)"

{
  echo "# Plex bridge daily review $D"; echo
  if [ $crc -ne 0 ] || [ ! -s "$COUT" ]; then
    echo "**STATUS: needs attention**"; echo
    if [ -n "$LIMITED" ]; then
      echo "The digest found problems, but the Claude review hit your plan's usage limit. It was skipped and will be retried on the next run (the review window was not advanced). Nothing was billed."
    elif [ -n "$AUTHFAIL" ]; then
      echo "The digest found problems, but the automated Claude review could not authenticate (the stored login cannot renew unattended)."
      echo "**One-time fix:** run \`claude setup-token\` in a terminal, then save the token it prints to \`~/.claude/unattended-oauth-token\` (chmod 600). Until then this job only reports."
    else
      echo "The Claude review did not complete (exit $crc). See \`reports/$D.claude.err\`."
    fi
    echo "The digest below still applies."
  else
    cat "$COUT"
  fi
  echo; echo "## Commits made by this review"; echo; if [ -n "$COMMITS" ]; then echo '```'; echo "$COMMITS"; echo '```'; else echo "none"; fi
  echo; echo "## Digest"; echo; cat "$DIG_MD"
} > "$REPORT"

[ $crc -eq 0 ] && [ -s "$COUT" ] && advance_cursor
S="$(grep -m1 -oE 'STATUS: [a-z ]+' "$REPORT" || echo 'STATUS: needs attention')"
finish "$S${COMMITS:+ ($(echo "$COMMITS" | wc -l | tr -d ' ') commit(s))}"
