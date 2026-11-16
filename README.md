# plex-bridge

Run **Plex Media Server in a Docker container on a Mac** without giving up the two things a container normally costs you:

* **GPU transcoding.** A container on macOS cannot use the Mac's GPU. Here every transcode is handed to the macOS
  `Plex Transcoder` on the host, so video is decoded and encoded by VideoToolbox (Quick Sync on Intel) instead of the CPU.
* **Real client addresses.** Docker on macOS forwards published ports through a proxy, so Plex sees one gateway address
  for every client and cannot tell devices apart, or LAN from remote. Here Plex sees each client's actual IP.

Why bother with a container at all: Plex's network-facing code runs isolated (unprivileged user, read-only media, no
access to your home directory) instead of as you on the Mac. The host only runs a small, sandboxed transcoder.

```
 clients ──► Mac :32400   plex-ipbridge (host half): sees the real client address
                 └─► 127.0.0.1:32390 ─► plex-ipbridge (container half) ─► PMS, *from that client address*

                        ┌──────────────────────────── container "plex" ─────────────────────────────────┐
                        │  PMS (Linux, pinned image)         media: read-only   config: docker volume    │
                        │    └─ "Plex Transcoder"  = transcoder-stub.sh                                   │
                        └──────────────┬──────────────────────────────────────────────────────▲──────────┘
                                       │ POST /run  {argv, env, cwd}  (host.docker.internal)     │ shared dir
                                       ▼ bearer token, 127.0.0.1 only                            │ ./transcode
   macOS host                 runner/plex-runner.py (LaunchAgent)  ── validates, rewrites, logs ─┘ (same path
                                       │ sandbox-exec (seatbelt: loopback net only, writes ./transcode only)  both sides)
                                       ▼
                          /Applications/Plex Media Server.app/.../Plex Transcoder  ──► VideoToolbox GPU
```

**macOS only, on purpose.** Both problems exist only because Docker on a Mac runs containers inside a Linux VM. On a
Linux host you need none of this: pass the GPU straight into the standard Plex image (`/dev/dri` for Quick Sync/VAAPI,
or the NVIDIA runtime) and Docker's bridge networking already shows Plex the real client addresses.

**Status.** Developed and tested on an Intel Mac mini (UHD 630), macOS 15, with [OrbStack](https://orbstack.dev) as the
container engine. Nothing in it is OrbStack-specific: it uses only standard Docker features (Compose, bind mounts,
`host.docker.internal`, a loopback-published port, the `NET_ADMIN` capability), so Docker Desktop should work, but that
is **untested**. Apple Silicon should also work (VideoToolbox is the same API) and is also untested. This is a personal
project, not affiliated with Plex.

## Quick setup

You need macOS, a Docker engine with Compose (OrbStack or Docker Desktop), Python 3 (`/usr/bin/python3`), and either Go
or nothing (without Go the host helper is built in a container).

1. Install **Plex Media Server.app** in `/Applications`. Only its `Plex Transcoder` binary is used. If it has never
   run, launch it once so it downloads its codec bundles, then quit it and remove it from Login Items.
2. Create the configuration:
   ```sh
   ./setup-host.sh          # creates .env and fills in what it can detect, then stops
   $EDITOR .env             # set PLEX_MEDIA_DIRS (your media folders) and check PLEX_VERSION / PLEX_LAN_IP
   ./setup-host.sh          # copies codec bundles, builds the host helper, installs and starts the LaunchAgents
   ```
3. Optional, to keep an existing native server (libraries, watch state, identity):
   ```sh
   ./seed-config.sh         # one-time copy of the native Plex config into the container's volume
   ```
   Skip it for a fresh server and claim it in the web UI instead.
4. Start Plex:
   ```sh
   docker compose up -d --build
   ```
5. Open `http://<this Mac>:32400/web`. Play something that needs a transcode and run `python3 runner/summarize-log.py`
   to see it go through the GPU.

Two rules to remember: **`PLEX_VERSION` must equal the installed macOS app's version**, and **native Plex must never run
alongside the container** (they share one server identity).

## Configuration

Everything site-specific lives in one git-ignored `.env` (template: `.env.example`), read by Docker Compose, the shell
scripts, the runner, `plex-ipbridge` and the ops scripts. Real environment variables override it.

| Variable | Meaning |
|---|---|
| `PLEX_MEDIA_DIRS` | One or more media roots, `:`-separated like `PATH` (e.g. `/Volumes/Movies:/Volumes/TV:/Users/me/Videos`; paths containing `:` are not supported). Each is mounted **read-only** at the same absolute path in the container, readable by the runner and the sandbox, and nothing else is. `setup-host.sh` generates `docker-compose.media.yml` from it, so **re-run `./setup-host.sh` and `docker compose up -d` after changing the list**. |
| `COMPOSE_FILE` | Makes every `docker compose` command load the generated media mounts (`docker-compose.yml:docker-compose.media.yml`). Append your own compose files here for other customisations. |
| `PLEX_LAN_IP` | This Mac's LAN IP, advertised to Plex clients. Give it a DHCP reservation. |
| `TZ`, `PLEX_UID`, `PLEX_GID` | Container time zone; uid/gid of the macOS user so the shared transcode directory is writable by both sides. |
| `PLEX_VERSION` | Plex version/tag (`<version>-<build>`); must equal the installed macOS app's version. |
| `PLEX_PROJECT_DIR` | Absolute path of this checkout (set by `setup-host.sh`). |
| `PLEX_RUNNER_PORT` | Port of the host transcode runner (127.0.0.1 only; default 32499). |
| `PLEX_IPBRIDGE_PORT` | Loopback-only host port published to the container half of `plex-ipbridge` (default 32390). The host half always listens on 32400. |
| `PLEX_EXTRA_LAN_NETWORKS` | Optional extra "LAN networks" for `seed-config.sh`. |
| `BRIDGE_TOKEN` | **Secret** shared by the container and the runner. Generated by `setup-host.sh`. |
| `IPBRIDGE_TOKEN` | **Secret** shared by the two halves of `plex-ipbridge`. Generated by `setup-host.sh`. |

Never commit `.env`. No path, address or credential is hardcoded in tracked files: the project directory is derived from
where the scripts live, and the LaunchAgents and the seatbelt profile are rendered from templates. The container name
(`plex`) and volume name (`plex_config`) are fixed. With Docker Desktop, the media roots and this checkout must be inside
its shared folders (the defaults cover `/Users` and `/Volumes`).

The optional nightly review has its own settings in `ops/mail.conf` (template: `ops/mail.conf.example`).

---

The rest of this document is reference detail.

## What is in this folder

| Path | Purpose |
|---|---|
| `.env.example` | Documented template for all site-specific settings. |
| `docker-compose.yml`, `Dockerfile` | The PMS container. The Dockerfile swaps the Linux transcoder for the stub (the real one is kept as `Plex Transcoder.real`, used as a fallback) and adds the container half of `plex-ipbridge`. |
| `setup-host.sh` | Creates/fills `.env`, host scratch dirs, copies the macOS codec bundles, generates the shared secrets, builds the host half of `plex-ipbridge`, renders and installs the LaunchAgents (`--daily-review` adds the nightly review). Re-runnable. |
| `seed-config.sh` | **One-time** copy of the native macOS Plex config into the `plex_config` volume (generates `Preferences.xml` from the macOS plist). Read-only with respect to the native install; refuses if native Plex is running or the volume is already seeded (`--force` overwrites). |
| `transcoder-stub.sh` | Runs inside the container *as* `Plex Transcoder`. Sends argv/env/cwd to the runner, relays output and exit code, falls back to the local software transcoder if the runner refuses or is down. |
| `runner/plex-runner.py` | Host service. Validates, **translates** the arguments for the macOS transcoder, runs it sandboxed, streams output back, logs everything. |
| `runner/transcoder.sb.in` | Seatbelt profile *template* for the transcoder (and the EAE audio helper); rendered into `host-support/transcoder.sb` at startup with your paths. |
| `runner/summarize-log.py`, `runner/test_translations.py` | Log reader (see [Logging](#logging)); regression tests for the translation rules. |
| `ipbridge/` | `plex-ipbridge`, one small Go program in two halves, so that Plex sees real client addresses (see [Real client addresses](#real-client-addresses)). The host half is built into `host-support/` by `setup-host.sh`; the container half is built by the Dockerfile and runs as an s6 service (`ipbridge/s6-run`). |
| `launchd/*.plist.in` | LaunchAgent templates (`@PROJECT@` is filled in by `setup-host.sh`): the runner, `plex-ipbridge`, and the optional nightly review. |
| `ops/` | The optional nightly review: digest, wrapper, prompt, Mail.app sender (see [Daily review](#daily-review-optional)). |
| `.env`, `docker-compose.media.yml`, `ops/mail.conf`, `host-support/`, `transcode/`, `runner/logs/`, `reports/` | Your configuration, generated files, secrets and runtime state; git-ignored. |

## Security model

* **Container:** Plex runs as an unprivileged user (`PLEX_UID`/`PLEX_GID`, matching the Mac user so the shared transcode dir is writable by both) with `cap_drop: ALL` plus a short list, and `no-new-privileges`. Every media root is mounted **read-only** at the same absolute path as on the host. Config lives in a Docker volume, not on the Mac filesystem.
* **Client-address bridge (`plex-ipbridge`):** the container publishes no port to the LAN. The host half listens on `:32400` and forwards to a loopback-only port; the container half accepts a connection only with `IPBRIDGE_TOKEN`, so another local process (including the sandboxed transcoder, which may use loopback) cannot claim to be an arbitrary client. It never connects to Plex from a loopback address (Plex trusts loopback without a token): connections from the Mac itself, and any address it cannot impersonate, arrive from the container's own address and must authenticate. The container gains `NET_ADMIN`, usable only by the root-owned bridge service; Plex itself has no capabilities.
* **Runner:** listens on `127.0.0.1` only (containers reach it via `host.docker.internal`), requires a bearer token (`BRIDGE_TOKEN`), allows only the Plex Transcoder binary, rejects any path outside media/transcode/codecs and any URL other than `http://127.0.0.1:32400`, blocks dangerous ffmpeg protocols, scrubs the environment to an allow-list, limits concurrent jobs (8), kills jobs if the container disconnects, and kills orphaned transcoders when it restarts.
* **Transcoder sandbox (`sandbox-exec`):** loopback network only, cannot read `/Users` except the project's support files, cannot read other volumes, writes only to `transcode/` and its scratch home. A compromised container that abuses the runner therefore cannot read your home directory or reach the internet through it.
* **Residual risk:** the container can still make the host run the transcoder on files under the media directories, and the runner and the bridge are new host-side surfaces. The attack surface this removes is Plex's network-facing code (it runs in the container, not as you on the host).

## Real client addresses

Docker engines on macOS (OrbStack and Docker Desktop alike) run containers in a Linux VM and forward published ports
through a proxy on the Mac. That proxy rewrites the source of every connection to a gateway address, so with a plain
`ports: "32400:32400"` Plex sees one address for every client and cannot tell LAN from WAN.

`plex-ipbridge` (`ipbridge/`) works around it at the TCP level, so it covers plain HTTP and Plex's own `plex.direct` TLS alike:

1. **Host half** (`local.plex-ipbridge` LaunchAgent) listens on the Mac's `:32400`, where the real peer address is still
   visible, and forwards each connection to `127.0.0.1:$PLEX_IPBRIDGE_PORT` with a one-line preface:
   `PLEXIP1 <IPBRIDGE_TOKEN> <client address>`.
2. **Container half** (s6 service, root, `NET_ADMIN`) checks the token and opens the connection to Plex on
   `127.0.0.1:32400` with the client's address as its *source* (`IP_TRANSPARENT`). A policy route
   (`ip rule from 127.0.0.0/8 iif lo lookup 123`, `local 0.0.0.0/0 dev lo` in table 123) brings Plex's replies back to
   that socket. Plex's `getpeername()` is the real client, so its log, dashboard, bandwidth limits and "allowed without
   auth" list all work as on a native install.

Things to know:

* **Connections from the Mac itself are deliberately not passed through** (loopback or the Mac's own LAN address: the
  runner's relay, the nightly digest, a browser on the Mac). They arrive from the container's own address, which Plex
  classifies as "Subnet", and must authenticate.
* **Plex's "allowed without auth" list means what it says.** Any LAN device inside `allowedNetworks` (Settings → Network →
  "List of IP addresses and networks that are allowed without auth") reaches Plex without a token, as on a native
  install. `seed-config.sh` copies that list unchanged; empty it if you do not want that.
* **LAN devices are tagged `(WAN)` in Plex's request log** because your LAN is not the container's own subnet. For
  bandwidth limits Plex uses Settings → Network → "LAN Networks", so list your LAN there (`seed-config.sh` carries it over).
* **IPv6 clients** are passed through when the container has IPv6 loopback routing; otherwise they arrive like Mac-local connections.
* **Failure handling:** both halves are supervised (launchd `KeepAlive`, s6) and stateless. They retry instead of
  exiting, contain a panic to the one connection it happened in, and a restart of either half, of Plex, of the container
  or of the engine only drops the connections open at that moment. The container's healthcheck (`docker ps`) probes
  the whole path: container half → policy route → Plex.

Check it:

```sh
docker exec plex grep "Request: \[" "/config/Library/Application Support/Plex Media Server/Logs/Plex Media Server.log" | tail
# [<client address>:<port> (...)] per request
cd ipbridge && go test ./...      # address-selection rules
```

Alternatives that were checked (on OrbStack):

| Approach | Result |
|---|---|
| Published port (`-p 32400:32400`) | Every client is the engine's gateway address. No engine setting changes this. |
| `network_mode: host` | That is the Linux VM's network, not the Mac's; every connection arrives as `127.0.0.1`. **Never use it for Plex:** loopback is trusted without a token. |
| Caddy / nginx as an HTTP reverse proxy with `X-Forwarded-For` | Plex does not adopt the forwarded address: the log still shows the proxy's address, and a request carrying the header is merely classified WAN (even with a LAN address in it). A layer-7 proxy also cannot terminate the `plex.direct` TLS that clients use on 32400. |
| `pf` `rdr` on the Mac to the container address | Packets can be redirected in with the source intact, but the container's replies leave through the VM's default route (its NAT interface), not back the way they came, so the handshake never completes. Reasoned from the VM's routing table; not tested. |
| A Linux Docker host, or native Plex | Real client addresses without any of this (bridge networking on Linux keeps them). |

## The argument translation layer

Plex builds its transcoder command line from what it detects on the machine it runs on. Inside the
container that is "Linux, an Intel CPU, no GPU", so Plex asks for software x264, Linux decoder names,
and so on. The runner rewrites each command for the macOS transcoder. Every rewrite is recorded in
the job's `transforms` list.

| Transform (`transforms` value) | What Plex asks for | What runs on the Mac | Why |
|---|---|---|---|
| `manifest->relay` | `-manifest_name http://127.0.0.1:32400/.../manifest` | `http://127.0.0.1:32499/relay/<job>/...` | PMS only trusts token-less callbacks from loopback, and the Mac's callbacks do not reach PMS as loopback (see above), so PMS would silently drop the manifest. The runner relays it with the session token. **Never add the container's network to Plex's "allowed without auth" list to work around this.** |
| `decoder-map:aac_lc->aac` | `-codec:N aac_lc` (input) | `-codec:N aac` | Linux decoder names the macOS build does not have. |
| `hw-encode:libx264->h264_videotoolbox` | `libx264 -crf -preset -x264opts …` | `h264_videotoolbox -allow_sw 0 -b:0 <0.85×maxrate>` | GPU encode. x264-only options dropped. `-allow_sw 0`: the encoder fails rather than silently using the CPU. |
| `hw-decode:videotoolbox` | (software decode) | `-hwaccel videotoolbox` before `-i` | GPU decode for H.264/HEVC input when re-encoding. Cuts CPU about 3× (1080p) to 5× (4K HEVC). |
| `hw-min-size:WxH->W2xH2` | any output size | output upscaled to ≥ 640×360 (multiple of 4, same aspect); burn-in subtitle canvas scaled with it | The Intel hardware H.264 encoder refuses outputs below 640 px wide (`cannot create compression session: -12903`). Affects small/old sources (FLV, MPEG-1, 320×240 AVI…). |
| `eae-service:host` | `eac3_eae`/`truehd_eae` + `EAE_ROOT=<container path>` | host `EasyAudioEncoder` service, host `EAE_ROOT` | E-AC3/TrueHD decode goes through Plex's EAE helper service and a watch folder. Plex runs a Linux copy in the container; the Mac transcoder needs the Mac copy with a host-side folder. Started on demand, shared, logs to `runner/logs/eae.log`. |
| `fallback:software(attemptN exit=E)` | — | same command without `-hwaccel`, `-allow_sw 1` | If a hardware attempt fails within 15 s the runner retries once in software instead of failing playback. Should be rare; watch for it in the log. |

Other behaviour:

* `cwd` is mapped from `/transcode/…` (container) to the same folder on the host.
* Video that Plex *copies* (direct stream) is passed through unmodified.
* The stub falls back to the in-container software transcoder if the runner answers `busy`, `rejected`, `unauthorized` or is unreachable.
* When Plex kills a job (it uses SIGKILL on every seek/stop) the stub's connection to the runner dies with it and the
  runner kills the host transcoder (`killed_by: client-disconnect` in the log). An `exit 254` with `killed_by: null`
  means the transcoder ended itself after Plex answered its callbacks with 404.
* ASS/SSA burn-in needs no translation: the Mac build renders with CoreText system fonts and ignores the container's `font_path`.

## Logging

All transcode-bridge activity is logged in `runner/logs/` (rotating, 10 MB × 10):

* `bridge.jsonl` — one JSON record per event. The `job` record has: `job_id`, `session`, container/host `cwd`, `argv_original` and `argv_final` (tokens redacted), an `argv_diff` (replace/delete/insert ops), `plex_summary` vs `run_summary` (decoders, encoders, container, hwaccel, filters), `transforms`, `attempts`, `exit`, `wall_s`, `cpu_user_s`/`cpu_sys_s`/`cpu_avg_cores`, `max_rss_mb`, `gpu_util_samples` (every 5 s; `gpu_util_max`), `relay` stats, `killed_by`, `output_tail`, env key *names* (never values). Other events: `start`, `reject`, `auth_fail`, `busy`, `error`, `reaped`, `eae_started`.
* `runner.log` — the same events as one-line text.
* `jobs/<id>.out` — full transcoder output for jobs that printed anything (newest 200 kept).
* `eae.log` — the EAE helper.
* `transcode/stub-calls.log` (container side) — what Plex asked for *before* translation, plus `FALLBACK` lines.

`plex-ipbridge` logs only start-up and failures: the host half to `runner/ipbridge.out`, the container half to `docker logs plex` (lines starting `back:`).

```sh
python3 runner/summarize-log.py              # table of recent jobs
python3 runner/summarize-log.py --last 3     # what Plex asked for vs what ran, per job
python3 runner/summarize-log.py --transforms # how often each rewrite fired
python3 runner/summarize-log.py --rejects    # rejects / auth failures / busy / errors
python3 runner/summarize-log.py --job <id>   # full record
```

Plex runs the transcoder at `-loglevel quiet`, so a failing job often prints nothing. To see ffmpeg's
own messages, set `PLEX_RUNNER_TRACE=1` in the runner LaunchAgent's `EnvironmentVariables` (raises the
level for every job and keeps the output in `jobs/`), or replay a job from its `argv_final`.

## Tested scenarios

Driven through Plex Web in Chrome on the development machine (Intel UHD 630). "Played" means the browser's video clock advanced in real time.

| Scenario | Result | Notes |
|---|---|---|
| Direct play (H.264/AAC MP4) | Played | Served straight from Plex; the transcode bridge is not involved. |
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
| Playback from another LAN device | Played | Plex logged the device's own address; local bandwidth accounting applied. |
| Forced kill of either `plex-ipbridge` half, or of the Plex process | Recovered | Serving again within seconds, without intervention. |

Not tested: Docker Desktop, Apple Silicon, TrueHD (shares the EAE path with E-AC3), Optimized Versions / downloads, music and photo transcodes, Live TV/DVR, several simultaneous streams, remote (WAN) streams, visual correctness of deinterlacing/subtitles.

## Known limits and gotchas

* **Plex and the macOS app must be the same version.** `PLEX_VERSION` in `.env` and `/Applications/Plex Media Server.app` must match. Never launch the app and keep auto-update away from it; to update, bump the tag, update the app, rebuild (`docker compose up -d --build`), run a few playbacks.
* **Native Plex must stay off.** It shares the server identity with the container. Remove it from Login Items; do not run both.
* **Port 32400 is served by a LaunchAgent, not by Docker.** If Plex is unreachable while the container is healthy, check `runner/ipbridge.out` and `docker logs plex | grep back:`. LaunchAgents run only while you are logged in, so the Mac needs automatic login (as the container engine does).
* **CPU-only work remains** for: HDR tone-mapping, scaling, audio transcodes, AV1 and VP9/MPEG-4 decode. The encode is hardware-only by design (`-allow_sw 0`).
* **No automatic library scans from file events** over the bind mount; rely on Plex's scheduled scan.
* Job limit is 8 (Plex's own limit is 4); idle throttled transcoders keep their slot until Plex ends the session.
* The Plex Web player can get stuck after a failed start (blank player, repeated header requests). Reload the page; if a "Resume Playback" dialog appears, choose an option. Not a server fault.
* The transcode dir lives in this project (`transcode/`); Plex cleans finished sessions.
* Addresses such as `192.168.148.2` in logs and examples are the container network's and differ per engine and install.

## Daily review (optional)

`./setup-host.sh --daily-review` installs a launchd job (`local.plex-daily-review`, every day at 04:17) that runs `ops/daily-review.sh`:

1. `ops/daily-digest.py` digests `runner/logs/bridge.jsonl`, the stub log, Plex's transcoder errors and health since the last completed review (at most 72 h back). It is deterministic and free.
2. **Clean** → writes an "all clear" report and a macOS notification. Nothing else runs.
3. **Anomalies** (failed jobs, fallbacks, rejects, `busy`, relay errors, unexpected Plex errors, container/runner down, transcoders stuck over an hour) → runs headless [Claude Code](https://claude.com/claude-code) with `ops/daily-review-prompt.md`: investigate, classify, fix small verifiable problems (with a test in `runner/test_translations.py`), commit on `main`, restart the runner only when no transcoder is running, revert on failure, and report. Limits: max 3 fixes, 45-minute cap, no `sudo`/`rm`/`git push`/branches, no edits outside the repo, security controls must not be weakened. **Subscription only:** it authenticates with the long-lived subscription token below, unsets `ANTHROPIC_API_KEY` and cloud-provider variables, uses a lighter model (`PLEX_REVIEW_MODEL`, default `sonnet`) and only runs on days the digest finds a problem. If your plan's usage limit is hit, the review is skipped, the window is not advanced, and it retries the next day.
4. Reports go to `reports/YYYY-MM-DD.md` (`reports/latest.md`), a macOS notification, and **email** through Mail.app (`ops/send-report.sh`, using an account configured in Mail.app, so no mail password lives in this project). Recipient and policy are in `ops/mail.conf` (git-ignored): `MAIL_TO`, `MAIL_FROM` (an account name in Mail.app) and `MAIL_WHEN=always|attention`. Mail results are logged in `reports/mail.log`.

**One-time setup for step 3:** an unattended job cannot use the interactive login. Run `claude setup-token` in a terminal and save the printed token to `~/.claude/unattended-oauth-token` (`chmod 600`). Without it the job still produces the digest, the report and the email, and says so.

```sh
FORCE_REVIEW=1 ./ops/daily-review.sh            # run the full review now
DRY_RUN=1 FORCE_REVIEW=1 ./ops/daily-review.sh  # show what would run
python3 ops/daily-digest.py --hours 24          # digest only (exit 10 = anomalies)
launchctl bootout gui/$(id -u)/local.plex-daily-review   # disable
```

## Maintenance

```sh
docker compose up -d --build        # apply Dockerfile/stub/compose/ipbridge changes (restarts Plex)
launchctl kickstart -k gui/$(id -u)/local.plex-runner   # apply runner changes (kills running jobs; orphans are reaped)
./setup-host.sh                     # rebuild + restart the host half of plex-ipbridge after changing ipbridge/ (also restarts the runner)
docker logs plex                    # container log
tail -f runner/logs/runner.log      # transcode bridge activity
tail runner/ipbridge.out            # host half of plex-ipbridge
python3 runner/test_translations.py # regression tests for the translation rules
```

## License

[MIT](LICENSE). Use it for anything; no warranty.
