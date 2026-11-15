# Plex in OrbStack, with transcoding bridged to the Mac's GPU

Plex Media Server (PMS) runs in an OrbStack container for isolation. Every transcode is handed to
the **macOS `Plex Transcoder`** on the host, so video is decoded and encoded on the Intel GPU
(VideoToolbox / Quick Sync) instead of the CPU. A container cannot use the Mac's GPU, so this
project is the bridge that makes the two work together.

Developed and tested on an Intel Mac mini (UHD 630 graphics) running macOS 15 with OrbStack. Apple Silicon should also work (VideoToolbox is
the same API) but is untested. The Plex version is pinned (`PLEX_VERSION` in `.env`) and must match the installed macOS Plex app.

```
 clients ──► Mac :32400  plex-ipbridge (LaunchAgent): sees the real client address
                 └─► 127.0.0.1:32390 ──► ipbridge service in the container ──► PMS, *from that client address*
                        ┌────────────────────────── OrbStack container "plex" ───────────────────────────┐
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
| `ipbridge/` | `plex-ipbridge`, one small Go program in two halves, so that Plex sees real client addresses instead of OrbStack's gateway (see [Client IPs and OrbStack](#client-ips-and-orbstack)). The host half is built into `host-support/` by `setup-host.sh`; the container half is built by the Dockerfile and runs as an s6 service (`ipbridge/s6-run`). |
| `seed-config.sh` | **One-time** copy of the native macOS Plex config into the `plex_config` volume (generates `Preferences.xml` from the macOS plist). Read-only with respect to the native install. |
| `setup-host.sh` | Creates/fills `.env`, host scratch dirs, copies the macOS codec bundles, generates the shared secret, renders and installs the LaunchAgents (`--daily-review` adds the nightly review). |
| `launchd/*.plist.in` | LaunchAgent templates (`@PROJECT@` is filled in by `setup-host.sh`). |
| `ops/` | The nightly review: digest, wrapper, prompt, Mail.app sender (see [Daily review](#daily-review-automated)). |
| `.env`, `docker-compose.media.yml`, `ops/mail.conf`, `host-support/`, `transcode/`, `runner/logs/`, `reports/` | Your configuration, generated files, secrets and runtime state; git-ignored. |

## Setup

1. Install OrbStack and **Plex Media Server.app** (only its `Plex Transcoder` binary is used; if it has never run, launch it once so it downloads its codec bundles, then quit it and remove it from Login Items).
2. `./setup-host.sh` — creates `.env` from `.env.example` and fills in what it can detect (project path, LAN IP, time zone, uid/gid, a random `BRIDGE_TOKEN`). **Edit `.env` and set `PLEX_MEDIA_DIRS`** (one or more media roots), then re-run it: it checks the Plex version, copies the macOS codec bundles, and installs and starts the runner and `plex-ipbridge` LaunchAgents. (`--daily-review` also installs the nightly review.)
3. `./seed-config.sh` — one-time copy of an existing native Plex config into the `plex_config` volume (refuses if native Plex is running or the volume is already seeded; `--force` overwrites). Skip it for a fresh server; you will claim it in the web UI.
4. `docker compose up -d --build`.

## Configuration

Everything site-specific lives in one git-ignored `.env` (template: `.env.example`), read by docker compose, the shell scripts, the runner and the ops scripts. Real environment variables override it.

| Variable | Meaning |
|---|---|
| `PLEX_MEDIA_DIRS` | One or more media roots, `:`-separated like `PATH` (e.g. `/Volumes/Movies:/Volumes/TV:/Users/me/Videos`; paths containing `:` are not supported). Each is mounted **read-only** at the same absolute path in the container, readable by the runner and the sandbox, and nothing else is. `setup-host.sh` generates `docker-compose.media.yml` from it, so **re-run `./setup-host.sh` and `docker compose up -d` after changing the list**. The old single `PLEX_MEDIA_DIR` is migrated automatically. |
| `COMPOSE_FILE` | Makes every `docker compose` command load the generated media mounts (`docker-compose.yml:docker-compose.media.yml`). Append your own compose files here for other customisations. |
| `PLEX_LAN_IP` | This Mac's LAN IP, advertised to Plex clients. |
| `TZ`, `PLEX_UID`, `PLEX_GID` | Container time zone; uid/gid of the macOS user so the shared transcode directory is writable by both sides. |
| `PLEX_VERSION` | Plex version/tag (`<version>-<build>`); must equal the installed macOS app's version. |
| `PLEX_PROJECT_DIR` | Absolute path of this checkout (set by `setup-host.sh`). |
| `PLEX_RUNNER_PORT` | Runner port (127.0.0.1 only). |
| `PLEX_IPBRIDGE_PORT` | Loopback-only host port published to the container half of `plex-ipbridge` (default 32390). The host half always listens on 32400. |
| `IPBRIDGE_TOKEN` | **Secret** shared by the two halves of `plex-ipbridge`. Generated by `setup-host.sh`. |
| `PLEX_EXTRA_LAN_NETWORKS` | Optional extra "LAN networks" for `seed-config.sh`. |
| `BRIDGE_TOKEN` | **Secret** shared by the container and the runner. Generated by `setup-host.sh`; never commit `.env`. |

The nightly review's recipient and sender are in `ops/mail.conf` (template: `ops/mail.conf.example`). No path, address or credential is hardcoded in tracked files; the project directory is derived from where the scripts live, the LaunchAgents and the seatbelt profile are rendered from templates, and the container name (`plex`) and volume name (`plex_config`) are fixed.

## Security model

* **Container:** non-default user (`PLEX_UID`/`PLEX_GID`, matching the Mac user so the shared transcode dir is writable by both), `cap_drop: ALL` plus six capabilities, `no-new-privileges`. Every media root is mounted **read-only** at the same absolute path as on the host. Config lives in a Docker volume, not on the Mac filesystem.
* **Client-address bridge (`plex-ipbridge`):** the container publishes no port to the LAN. The host half listens on `:32400` and forwards to a loopback-only port; the container half accepts a connection only with `IPBRIDGE_TOKEN`, so another local process (including the sandboxed transcoder, which may use loopback) cannot claim to be an arbitrary client. It never connects to Plex from a loopback address (Plex trusts loopback without a token): connections from the Mac itself, and any address it cannot impersonate, arrive from the container's own address and must authenticate. The container gains `NET_ADMIN`, usable only by the root-owned bridge service; Plex runs as an unprivileged user with no capabilities.
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
* **Client addresses depend on `plex-ipbridge`.** OrbStack's own port forwarding hides every client behind its gateway address, so port 32400 is served by the `local.plex-ipbridge` LaunchAgent instead. If Plex is unreachable while the container is healthy, check `runner/ipbridge.out` and `docker logs plex | grep back:`. Clients on the Mac itself show as the container's own address (e.g. `192.168.148.2`), on purpose. See [Client IPs and OrbStack](#client-ips-and-orbstack).
* **Plex's "allowed without auth" list now means what it says.** With real addresses, any LAN device inside `allowedNetworks` (Settings → Network → "List of IP addresses and networks that are allowed without auth") reaches Plex without a token, as on a native install. Before the bridge that list never matched, because every client was the gateway. Empty it if you do not want that.
* **No automatic library scans from file events** over the bind mount; the 15-minute scheduled scan is on.
* Job limit is 8 (Plex's own limit is 4); idle throttled transcoders keep their slot until Plex ends the session.
* The Plex Web player can get stuck after a failed start (blank player, repeated header requests). Reload the page; if a "Resume Playback" dialog appears, choose an option. Not a server fault.
* The transcode dir lives in this project (`transcode/`); Plex cleans finished sessions. A RAM disk would be quicker but was not needed.

## Client IPs and OrbStack

OrbStack's port forwarder rewrites the source of every published-port connection to its gateway address (for example `192.168.148.1` on a
compose network), so with a plain `ports: "32400:32400"` Plex sees one address for every client and cannot tell LAN from WAN.

`plex-ipbridge` (`ipbridge/`) works around it, at the TCP level, so it covers plain HTTP and Plex's own `plex.direct` TLS alike:

1. **Host half** (`local.plex-ipbridge` LaunchAgent) listens on the Mac's `:32400`, where the real peer address is still visible, and forwards
   each connection to `127.0.0.1:$PLEX_IPBRIDGE_PORT` with a one-line preface: `PLEXIP1 <IPBRIDGE_TOKEN> <client address>`.
2. **Container half** (s6 service, root, `NET_ADMIN`) checks the token and opens the connection to Plex on `127.0.0.1:32400` with the client's
   address as its *source* (`IP_TRANSPARENT`). A policy route (`ip rule from 127.0.0.0/8 iif lo lookup 123`, `local 0.0.0.0/0 dev lo` in table 123)
   brings Plex's replies back to that socket. Plex's `getpeername()` is the real client, so its log, dashboard, LAN/WAN classification, bandwidth
   limits and "allowed without auth" list all work as on a native install.

Connections from the Mac itself (loopback or its own LAN address: the runner's relay, the nightly digest, a browser on the Mac) are deliberately
*not* passed through; they arrive from the container's own address (classified "Subnet" like the gateway address before) and must authenticate.
IPv6 clients are passed through when the container has IPv6 loopback routing, otherwise they appear the same way.

Check it: `docker exec plex grep "Request: \[" "/config/Library/Application Support/Plex Media Server/Logs/Plex Media Server.log" | tail` shows
`[<client address>:<port> (Subnet|WAN)]` per request. `cd ipbridge && go test ./...` covers the address-selection rules.

Alternatives that were checked on this machine:

| Approach | Result |
|---|---|
| Published port (`-p 32400:32400`) | Every client is the OrbStack gateway. No OrbStack setting changes this. |
| `network_mode: host` | That is the OrbStack Linux VM's network, not the Mac's; every connection arrives as `127.0.0.1`. **Never use it for Plex:** loopback is trusted without a token. |
| Caddy / nginx as an HTTP reverse proxy with `X-Forwarded-For` | Plex does not adopt the forwarded address: the log still shows the proxy's address, and a request carrying the header is merely classified WAN (even with a LAN address in it). A layer-7 proxy also cannot terminate the `plex.direct` TLS that clients use on 32400. Caddy could replace only the host half here (PROXY protocol via a layer-4 plugin build), losing the token and the "Mac stays authenticated" rule. |
| `pf` `rdr` on the Mac to the container address | Packets can be redirected in with the source intact, but the container's replies leave through the OrbStack VM's default route (its NAT interface), not back through the Mac-facing bridge, so the handshake never completes. Making it work needs root `pf` rules plus policy routing inside OrbStack's VM, which OrbStack does not persist. Reasoned from the VM's routing table; not tested. |
| macvlan | Not available for the macOS/OrbStack VM (untested). |

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
./setup-host.sh                     # rebuild + restart the host half of plex-ipbridge after changing ipbridge/ (also restarts the runner)
tail runner/ipbridge.out            # host half of plex-ipbridge
docker logs plex                    # container log
tail -f runner/logs/runner.log      # bridge activity
```
