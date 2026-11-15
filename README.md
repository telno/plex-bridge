# Plex in OrbStack, with transcoding bridged to the Mac's GPU

Plex Media Server (PMS) runs in an OrbStack container for isolation. Every transcode is handed to
the **macOS `Plex Transcoder`** on the host, so video is decoded and encoded on the Intel GPU
(VideoToolbox / Quick Sync) instead of the CPU. A container cannot use the Mac's GPU, so this
project is the bridge that makes the two work together.

Developed and tested on an Intel Mac mini (UHD 630 graphics) running macOS 15 with OrbStack. Apple Silicon should also work (VideoToolbox is
the same API) but is untested. The Plex version is pinned (`PLEX_VERSION` in `.env`) and must match the installed macOS Plex app.

```
 LAN clients ──► :32400 ┌────────────────────────── OrbStack container "plex" ───────────────────────────┐
                        │  PMS (Linux, pinned image)         media: read-only   config: docker volume    │
                        │    └─ "Plex Transcoder"  = transcoder-stub.sh                                   │
                        └──────────────┬──────────────────────────────────────────────────────▲──────────┘
                                       │ POST /run  {argv, env, cwd}  (host.docker.internal)     │ shared dir
                                       ▼ bearer token, 127.0.0.1 only                            │ ./transcode
   macOS host                 runner/plex-runner.py (LaunchAgent)  ── validates, rewrites, logs ─┘ (same path
                                       │ sandbox-exec (seatbelt: loopback net only, writes ./transcode only)  both sides)
                                       ▼
                          /Applications/Plex Media Server.app/.../Plex Transcoder  ──► VideoToolbox GPU
                          (+ macOS EasyAudioEncoder service on demand, for E-AC3/TrueHD)
```

## What is in this folder

| Path | Purpose |
|---|---|
| `.env.example` | Documented template for all site-specific settings. Copy to `.env` (git-ignored) or let `setup-host.sh` create it. |
| `docker-compose.yml`, `Dockerfile` | The PMS container (version from `.env`). The Dockerfile swaps the Linux transcoder for the stub (the real one is kept as `Plex Transcoder.real`, used as a fallback). |
| `transcoder-stub.sh` | Runs inside the container *as* `Plex Transcoder`. Sends argv/env/cwd to the runner, relays output and exit code, falls back to the local software transcoder if the runner refuses or is down. |
| `runner/plex-runner.py` | Host service. Validates, **translates** the arguments for the macOS transcoder, runs it sandboxed, streams output back, logs everything. |
| `runner/transcoder.sb.in` | Seatbelt profile *template* for the transcoder (and the EAE helper); the runner renders it into `host-support/transcoder.sb` at startup with your paths. Policy: no internet/LAN, reads only media + support files, writes only `transcode/` and its scratch home. |
| `runner/summarize-log.py` | Reads the structured log (see [Logging](#logging)). |
| `seed-config.sh` | **One-time** copy of the native macOS Plex config into the `plex_config` volume (generates `Preferences.xml` from the macOS plist). Read-only with respect to the native install. |
| `setup-host.sh` | Creates/fills `.env`, host scratch dirs, copies the macOS codec bundles, generates the shared secret, renders and installs the LaunchAgents (`--daily-review` adds the nightly review). |
| `launchd/*.plist.in` | LaunchAgent templates (`@PROJECT@` is filled in by `setup-host.sh`). |
| `ops/` | The nightly review: digest, wrapper, prompt, Mail.app sender (see [Daily review](#daily-review-automated)). |
| `.env`, `ops/mail.conf`, `host-support/`, `transcode/`, `runner/logs/`, `reports/` | Your configuration, secrets and runtime state; git-ignored. |

## Setup

1. Install OrbStack and **Plex Media Server.app** (only its `Plex Transcoder` binary is used; if it has never run, launch it once so it downloads its codec bundles, then quit it and remove it from Login Items).
2. `./setup-host.sh` — creates `.env` from `.env.example` and fills in what it can detect (project path, LAN IP, time zone, uid/gid, a random `BRIDGE_TOKEN`). **Edit `.env` and set `PLEX_MEDIA_DIR`**, then re-run it: it checks the Plex version, copies the macOS codec bundles, and installs and starts the runner LaunchAgent. (`--daily-review` also installs the nightly review.)
3. `./seed-config.sh` — one-time copy of an existing native Plex config into the `plex_config` volume (refuses if native Plex is running or the volume is already seeded; `--force` overwrites). Skip it for a fresh server; you will claim it in the web UI.
4. `docker compose up -d --build`.

## Configuration

Everything site-specific lives in one git-ignored `.env` (template: `.env.example`), read by docker compose, the shell scripts, the runner and the ops scripts. Real environment variables override it.

| Variable | Meaning |
|---|---|
| `PLEX_MEDIA_DIR` | Media root, mounted **read-only** at the same absolute path in the container. More roots: a git-ignored `docker-compose.override.yml`. |
| `PLEX_LAN_IP` | This Mac's LAN IP, advertised to Plex clients. |
| `TZ`, `PLEX_UID`, `PLEX_GID` | Container time zone; uid/gid of the macOS user so the shared transcode directory is writable by both sides. |
| `PLEX_VERSION` | Plex version/tag (`<version>-<build>`); must equal the installed macOS app's version. |
| `PLEX_PROJECT_DIR` | Absolute path of this checkout (set by `setup-host.sh`). |
| `PLEX_RUNNER_PORT` | Runner port (127.0.0.1 only). |
| `PLEX_EXTRA_LAN_NETWORKS` | Optional extra "LAN networks" for `seed-config.sh`. |
| `BRIDGE_TOKEN` | **Secret** shared by the container and the runner. Generated by `setup-host.sh`; never commit `.env`. |

The nightly review's recipient and sender are in `ops/mail.conf` (template: `ops/mail.conf.example`). No path, address or credential is hardcoded in tracked files; the project directory is derived from where the scripts live, the LaunchAgents and the seatbelt profile are rendered from templates, and the container name (`plex`) and volume name (`plex_config`) are fixed.

## Security model

* **Container:** non-default user (`PLEX_UID`/`PLEX_GID`, matching the Mac user so the shared transcode dir is writable by both), `cap_drop: ALL` plus six capabilities, `no-new-privileges`. Media is mounted **read-only** at the same absolute path as on the host. Config lives in a Docker volume, not on the Mac filesystem.
* **Runner:** listens on `127.0.0.1` only (containers reach it via `host.docker.internal`), requires a bearer token (`BRIDGE_TOKEN` from `.env`, passed to the container as an environment variable), allows only the Plex Transcoder binary, rejects any path outside media/transcode/codecs and any URL other than `http://127.0.0.1:32400`, blocks dangerous ffmpeg protocols, scrubs the environment to an allow-list, limits concurrent jobs (8), kills jobs if the container disconnects, and kills orphaned transcoders when it restarts.
* **Transcoder sandbox (`sandbox-exec`):** loopback network only, cannot read `/Users` except the project's support files, cannot read other volumes, writes only to `transcode/` and its scratch home. A compromised container that abuses the runner therefore cannot read your home directory or reach the internet through it.
* Residual risk: the container can still make the host run the transcoder on files under the media directory, and the runner is a new host-side surface. The attack surface this removes is Plex's network-facing code (it runs in the container, not as you on the host).

## The argument translation layer

Plex builds its transcoder command line from what it detects on the machine it runs on. Inside the
container that is "Linux, an Intel CPU, no GPU", so Plex asks for software x264, Linux decoder names,
and so on. The runner rewrites each command for the macOS transcoder. Every rewrite is recorded in
the job's `transforms` list.

| Transform (`transforms` value) | What Plex asks for | What runs on the Mac | Why |
|---|---|---|---|
| `manifest->relay` | `-manifest_name http://127.0.0.1:32400/.../manifest` | `http://127.0.0.1:32499/relay/<job>/...` | PMS only trusts token-less callbacks from loopback. OrbStack's port forwarder makes the Mac's callbacks look remote (`192.168.148.1`), so PMS silently drops the manifest. The runner relays it with the session token. **Never add the OrbStack gateway to Plex's "no auth" networks: every LAN client appears from that address.** |
| `decoder-map:aac_lc->aac` | `-codec:N aac_lc` (input) | `-codec:N aac` | Linux decoder names the macOS build does not have. |
| `hw-encode:libx264->h264_videotoolbox` | `libx264 -crf -preset -x264opts …` | `h264_videotoolbox -allow_sw 0 -b:0 <0.85×maxrate>` | GPU encode. x264-only options dropped. `-allow_sw 0`: the encoder fails rather than silently using the CPU. |
| `hw-decode:videotoolbox` | (software decode) | `-hwaccel videotoolbox` before `-i` | GPU decode for H.264/HEVC input when re-encoding. Cuts CPU about 3× (1080p) to 5× (4K HEVC). |
| `hw-min-size:WxH->W2xH2` | any output size | output upscaled to ≥ 640×360 (multiple of 4, same aspect); burn-in subtitle canvas scaled with it | The Intel hardware H.264 encoder refuses outputs below 640 px wide (`cannot create compression session: -12903`). Affects small/old sources (FLV, MPEG-1, 320×240 AVI…). |
| `eae-service:host` | `eac3_eae`/`truehd_eae` + `EAE_ROOT=<container path>` | host `EasyAudioEncoder` service, host `EAE_ROOT` | E-AC3/TrueHD decode goes through Plex's EAE helper service and a watch folder. Plex runs a Linux copy in the container; the Mac transcoder needs the Mac copy with a host-side folder. Started on demand, shared, logs to `runner/logs/eae.log`. |
| `fallback:software(attemptN exit=E)` | — | same command without `-hwaccel`, `-allow_sw 1` | If a hardware attempt fails within 15 s the runner retries once in software instead of failing playback. Should be rare; watch for it in the log. |

Other behaviour: `cwd` is mapped from `/transcode/…` (container) to the same folder on the host; the
stub falls back to the in-container software transcoder if the runner answers `busy`, `rejected`,
`unauthorized` or is unreachable; video that Plex *copies* (direct stream) is passed through
unmodified.

ASS/SSA burn-in needs no translation: the Mac build renders with CoreText system fonts and ignores
the container's `font_path`.

## Logging

All bridge activity is logged for later analysis in `runner/logs/` (rotating, 10 MB × 10):

* `bridge.jsonl` — one JSON record per event. The `job` record has: `job_id`, `session`, container/host `cwd`, `argv_original` and `argv_final` (tokens redacted), an `argv_diff` (replace/delete/insert ops), `plex_summary` vs `run_summary` (decoders, encoders, container, hwaccel, filters), `transforms`, `attempts`, `exit`, `wall_s`, `cpu_user_s`/`cpu_sys_s`/`cpu_avg_cores`, `max_rss_mb`, `gpu_util_samples` (every 5 s; `gpu_util_max`), `relay` stats, `killed_by`, `output_tail`, env key *names* (never values). Other events: `start`, `reject`, `auth_fail`, `busy`, `error`, `reaped`, `eae_started`.
* `runner.log` — the same events as one-line text.
* `jobs/<id>.out` — full transcoder output for jobs that printed anything (newest 200 kept).
* `eae.log` — the EAE helper.
* `transcode/stub-calls.log` (container side) — what Plex asked for *before* translation, plus `FALLBACK` lines.

```sh
python3 runner/summarize-log.py              # table of recent jobs
python3 runner/summarize-log.py --last 3     # what Plex asked for vs what ran, per job
python3 runner/summarize-log.py --transforms # how often each rewrite fired
python3 runner/summarize-log.py --rejects    # rejects / auth failures / busy / errors
python3 runner/summarize-log.py --job <id>   # full record
```

Plex runs the transcoder at `-loglevel quiet`, so a failing job often prints nothing. To see ffmpeg's
own messages, set `PLEX_RUNNER_TRACE=1` in the LaunchAgent's `EnvironmentVariables` (raises the
level for every job and keeps the output in `jobs/`), or replay a job from its `argv_final`.

## Tested scenarios

Driven through Plex Web in Chrome (as a managed user; the account's quality settings were restored afterwards). "Played" means the browser's video clock advanced in real time.

| Scenario | Result | Notes |
|---|---|---|
| Direct play (H.264/AAC MP4) | Played | Served straight from Plex; the bridge is not involved. |
| Direct stream (H.264 MKV; 4K HEVC video copy) | Played | Video copied, audio → AAC; `video=copy`, near-zero load. |
| 1080p H.264 → 720p, 2 Mbps | Played | GPU decode + encode; ~1–2% CPU between segment bursts. |
| 1080p HEVC → 720p | Played | GPU decode + encode. |
| 4K HEVC HDR → 720p and → 1080p 8 Mbps | Played in real time | GPU decode/encode; HDR tone-map is CPU (~2.4 cores at 1080p). |
| VP9, MPEG-4 (AVI), MS-MPEG4v3 (AVI) | Played | VP9/MPEG-4 decode in software (hardware decode is H.264/HEVC only). |
| MPEG-2 (interlaced), MPEG-1, FLV1, VP6F, tiny AVIs | Played | Small outputs upscaled by `hw-min-size`. |
| AV1 + Opus | Played | **CPU-bound** (~4 cores): the UHD 630 has no AV1 hardware decoder. |
| H.264 in MPEG-TS + AC3; PCM / ADPCM audio | Played | |
| E-AC3 audio | Played | Needs the host EAE service. |
| PGS burn-in; ASS burn-in | Played | Overlay chain lines up after the min-size fix; ASS via CoreText. Subtitle *appearance* was not inspected visually. |
| Seek far ahead; audio-track switch | Played | Seek restarts the job for the same session; the old job ends. |
| Runner busy / hardware refusal | Recovered | Stub falls back to local software; runner retries in software. |

Not tested: TrueHD (shares the EAE path with E-AC3), Optimized Versions / downloads, music and photo transcodes, Live TV/DVR, several simultaneous streams, remote (WAN) streams, visual correctness of deinterlacing/subtitles.

## Known limits and gotchas

* **Plex and the macOS app must be the same version.** `PLEX_VERSION` in `.env` and `/Applications/Plex Media Server.app` must match. Never launch the app and keep auto-update away from it; to update, bump the tag, update the app, rebuild (`docker compose up -d --build`), run a few playbacks.
* **Native Plex must stay off.** It shares the server identity with the container. The login item has been removed; do not run both.
* **CPU-only work remains** for: HDR tone-mapping, scaling, audio transcodes, AV1 and VP9/MPEG-4 decode. The encode is hardware-only by design (`-allow_sw 0`).
* **Client addresses are masked.** OrbStack presents every client to Plex as its own gateway address (e.g. `192.168.148.1`), so Plex treats all clients (even remote ones through a port forward) as local: remote-bandwidth rules and per-client IPs are not meaningful. See [Client IPs and OrbStack](#client-ips-and-orbstack) for what was tested and the options.
* **No automatic library scans from file events** over the bind mount; the 15-minute scheduled scan is on.
* Job limit is 8 (Plex's own limit is 4); idle throttled transcoders keep their slot until Plex ends the session.
* The Plex Web player can get stuck after a failed start (blank player, repeated header requests). Reload the page; if a "Resume Playback" dialog appears, choose an option. Not a server fault.
* The transcode dir lives in this project (`transcode/`); Plex cleans finished sessions. A RAM disk would be quicker but was not needed.

## Client IPs and OrbStack

Plex sees every client as OrbStack's gateway address (for example `192.168.148.1` on a compose network or `192.168.215.1` on the default bridge),
including real clients on other LAN machines. Checked on this machine (Plex's own request log; a socat listener that reports the peer address):

| Approach | What the container sees | Verdict |
|---|---|---|
| Published ports (`-p`), current setup | the OrbStack gateway, for LAN clients and for Mac-local clients alike | Source IP is rewritten by the Mac-side forwarder; no OrbStack setting changes this (the docs only offer "Expose ports to LAN" and binding to one interface). |
| `network_mode: host` | OrbStack's **Linux VM** interfaces (not the Mac's `en0`); every connection arrives as `127.0.0.1` | **Do not use for Plex**: Plex trusts loopback requests without a token, so this would remove authentication. |
| Caddy / nginx reverse proxy | the proxy's own (private) address | Does not help: the NAT happens before the proxy, and Plex deliberately ignores `X-Forwarded-For` values from RFC1918 addresses (a header-forgery defence). |
| macvlan | — | Not available for the macOS/OrbStack VM (untested). |
| Linux Docker host (bridge mode keeps client IPs) or native Plex | real client IPs | The only true fixes: i.e. the earlier "N100 / Linux on the mini" option, or abandoning the container. |

Impact: authentication is **still enforced** (unauthenticated requests get HTTP 401 through the forwarded path; Plex's "allowed without auth" networks are your real LAN, not the gateway). What is lost is classification: Plex treats everything as local, so there are no remote-bitrate limits and the dashboard cannot tell
remote from LAN. Practical mitigation: do not port-forward 32400 from the router to this Mac for untrusted remote users, or accept unmetered remote streams.

## Daily review (automated)

A launchd job (`local.plex-daily-review`, every day at 04:17, installed from `ops/local.plex-daily-review.plist`) runs `ops/daily-review.sh`:

1. `ops/daily-digest.py` digests `runner/logs/bridge.jsonl`, the stub log, Plex's transcoder errors and health since the last completed review (at most 72 h back). It is deterministic and free.
2. **Clean** → writes an "all clear" report and a macOS notification. No Claude call.
3. **Anomalies** (failed jobs, fallbacks, rejects, `busy`, relay errors, unexpected Plex errors, container/runner down, transcoders stuck over an hour) → runs headless Claude with `ops/daily-review-prompt.md`: investigate, classify, fix small verifiable problems (with a test in `runner/test_translations.py`), commit on `main`, restart the runner only when no transcoder is running, revert on failure, and report. Limits: max 3 fixes, 45-minute cap, no `sudo`/`rm`/`git push`/branches, no edits outside the repo, security controls must not be weakened. **Subscription only:** it authenticates with the long-lived subscription token below, unsets `ANTHROPIC_API_KEY` and cloud-provider variables, and has no dollar budget because nothing is billed per token; it uses a lighter model (`PLEX_REVIEW_MODEL`, default `sonnet`) and only runs on days the digest finds a problem. If your plan's usage limit is hit, the review is skipped, the window is not advanced, and it retries the next day. (Make sure "extra usage" is off in your Claude settings if you want a hard guarantee of no overage.)
4. Reports go to `reports/YYYY-MM-DD.md` (`reports/latest.md`), a macOS notification, and **email** through Mail.app (`ops/send-report.sh`, using the account configured in Mail.app, so no mail password lives in this project). Recipient and policy are in `ops/mail.conf` (git-ignored): `MAIL_TO`, `MAIL_FROM` (an account name in Mail.app) and `MAIL_WHEN=always|attention`. Mail results are logged in `reports/mail.log`.

**One-time setup needed for step 3:** an unattended job cannot use the interactive login. Run `claude setup-token` in a terminal and save the printed token to `~/.claude/unattended-oauth-token` (`chmod 600`). Until then the job still produces the digest, the gate decision, the report and the email, and says so.

```sh
FORCE_REVIEW=1 ./ops/daily-review.sh            # run the full review now
DRY_RUN=1 FORCE_REVIEW=1 ./ops/daily-review.sh  # show what would run
python3 ops/daily-digest.py --hours 24          # digest only (exit 10 = anomalies)
python3 runner/test_translations.py             # regression tests for the translation rules
launchctl bootout gui/$(id -u)/local.plex-daily-review   # disable
```

## Maintenance

```sh
docker compose up -d --build        # apply Dockerfile/stub/compose changes (restarts Plex)
launchctl kickstart -k gui/$(id -u)/local.plex-runner   # apply runner changes (kills running jobs; orphans are reaped)
docker logs plex                    # container log
tail -f runner/logs/runner.log      # bridge activity
```
