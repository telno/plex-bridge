#!/usr/bin/env python3
"""Host-side runner: executes the macOS Plex Transcoder on behalf of the PMS container.

Listens on 127.0.0.1 only (containers reach it via host.docker.internal). Requires a bearer
token. Validates arguments, translates them for the macOS transcoder, scrubs the environment,
runs the transcoder under a seatbelt profile (loopback network only, writes only to the shared
transcode dir) and streams output back.

Response body: transcoder output (stdout+stderr merged), then a final line  ::EXIT::<code>

Logging (runner/logs/):
  bridge.jsonl   one JSON record per event; the per-job "job" record carries the original and
                 rewritten argv (tokens redacted), an argv diff, the decoded/encoded stream summary,
                 resource usage, GPU samples, relay stats and the transcoder output tail.
  runner.log     the same events as short human-readable lines.
  jobs/<id>.out  full transcoder output for jobs that printed anything (pruned to the newest 200).
Both logs rotate. `runner/summarize-log.py` prints a table from bridge.jsonl.
"""
import difflib, glob, hmac, json, logging, logging.handlers, os, re, select, signal, socket
import subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import envconf

# Configuration comes from the git-ignored .env in the project directory (see .env.example); real environment
# variables override it. The project directory is derived from where this file lives.
PROJECT   = os.environ.get("PLEX_PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
envconf.load(PROJECT)
MEDIA     = os.environ.get("PLEX_MEDIA_DIR", "")
TRANSCODE = os.path.join(PROJECT, "transcode")
SUPPORT   = os.path.join(PROJECT, "host-support")
CODECS    = os.path.join(SUPPORT, "Codecs")
TRANSCODER = "/Applications/Plex Media Server.app/Contents/MacOS/Plex Transcoder"
PROFILE_TEMPLATE = os.path.join(PROJECT, "runner", "transcoder.sb.in")
PROFILE   = os.path.join(SUPPORT, "transcoder.sb")       # rendered from the template at startup (render_profile)
PORT      = int(os.environ.get("PLEX_RUNNER_PORT", "32499"))
MAX_JOBS  = int(os.environ.get("PLEX_RUNNER_MAX_JOBS", "8"))
LOGDIR    = os.environ.get("PLEX_RUNNER_LOGDIR") or os.path.join(PROJECT, "runner", "logs")
JOBDIR    = os.path.join(LOGDIR, "jobs")
TOKEN     = os.environ.get("BRIDGE_TOKEN", "")
TRACE     = os.environ.get("PLEX_RUNNER_TRACE") == "1"     # raise ffmpeg/Plex log levels for every job

READ_ROOTS = [os.path.realpath(p) for p in (MEDIA or "/nonexistent-media-dir", TRANSCODE, CODECS)]
URL_OK   = re.compile(r"^https?://(127\.0\.0\.1|localhost):32400(/|$)")
PROTO    = re.compile(r"^(file|concat|pipe|subfile|tcp|udp|rtp|rtsp|ftp|smb|data|unix|crypto|cache|async|hls|http|https|tls|sftp|ssh|gopher):", re.I)
ENV_PASS = re.compile(r"^(X_PLEX_[A-Z0-9_]+|PLEX_[A-Z0-9_]+|TZ|LANG|LC_[A-Z]+)$")
ENV_DROP = re.compile(r"^(PLEX_MEDIA_SERVER_.*|PLEX_ARCH|PLEX_UID|PLEX_GID)$")
MANIFEST_URL = re.compile(r"^http://(127\.0\.0\.1|localhost):32400/(video/:/transcode/session/[^ ]*/manifest[^ ]*)$")
SESSION_RE = re.compile(r"/session/([a-z0-9]{24})/")
TOKEN_RE = re.compile(r"((?:X-Plex-Token|X_PLEX_TOKEN|token)=)[^&\s\"']+", re.I)
PMS_HOST, PMS_PORT = "127.0.0.1", 32400
slots = threading.BoundedSemaphore(MAX_JOBS)
RELAYS = {}   # job id -> {"token":..., "posts":n, "errors":n, "status":{code:n}}

# ---------------------------------------------------------------- logging
os.makedirs(JOBDIR, exist_ok=True)
_human = logging.getLogger("runner.human"); _json = logging.getLogger("runner.json")
for lg, fn in ((_human, "runner.log"), (_json, "bridge.jsonl")):
    lg.setLevel(logging.INFO); lg.propagate = False
    h = logging.handlers.RotatingFileHandler(os.path.join(LOGDIR, fn), maxBytes=10_000_000, backupCount=10)
    h.setFormatter(logging.Formatter("%(message)s")); lg.addHandler(h)

def redact(s):
    return TOKEN_RE.sub(r"\1***", s) if isinstance(s, str) else s

def log(msg):
    _human.info(time.strftime("%Y-%m-%d %H:%M:%S ") + redact(msg))

def event(kind, **kw):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": kind}
    rec.update(kw)
    _json.info(json.dumps(rec, default=str))

def prune_jobs(keep=200):
    files = sorted(glob.glob(os.path.join(JOBDIR, "*.out")), key=os.path.getmtime)
    for f in files[:-keep]:
        try: os.unlink(f)
        except OSError: pass

# ---------------------------------------------------------------- argument handling
def under(path, roots):
    rp = os.path.realpath(path)
    return any(rp == r or rp.startswith(r + os.sep) for r in roots)

def check_args(argv):
    if not isinstance(argv, list) or len(argv) > 600: return "bad argv"
    for a in argv:
        if not isinstance(a, str) or len(a) > 16384 or "\0" in a: return "bad argument"
        if URL_OK.match(a): continue
        if PROTO.match(a): return "protocol not allowed: " + redact(a)[:60]
        # options like  key=/path  inside filter strings are covered by the seatbelt profile
        if a.startswith("/") and not under(a, READ_ROOTS): return "path not allowed: " + a[:120]
    return None

DECODER_MAP = {"aac_lc": "aac", "aac_main": "aac", "aac_ltp": "aac", "aac_he": "aac", "aac_he_v2": "aac"}

def map_decoders(argv):
    """Plex names decoders for the Linux transcoder it thinks it is talking to; use the macOS names.
    Only rewrites the value of -codec:N / -c:N options that appear BEFORE the first -i (input decoders)."""
    try: first_in = argv.index("-i")
    except ValueError: return argv, []
    out, changed = list(argv), []
    for i in range(first_in - 1):
        if re.fullmatch(r"-(?:codec|c):\d+", out[i]) and out[i + 1] in DECODER_MAP:
            changed.append("%s->%s" % (out[i + 1], DECODER_MAP[out[i + 1]])); out[i + 1] = DECODER_MAP[out[i + 1]]
    return out, changed

def use_hw_encoder(argv):
    """Plex (inside the container) always asks for software x264. Swap in the Mac's VideoToolbox
    encoder: replace the codec, drop x264-only options, and derive a target bitrate from -maxrate."""
    if os.environ.get("PLEX_RUNNER_HWENC", "1") != "1": return argv, []
    streams = [re.fullmatch(r"-(?:codec|c):(\d+)", a).group(1) for i, a in enumerate(argv[:-1])
               if re.fullmatch(r"-(?:codec|c):\d+", a) and argv[i + 1] == "libx264"]
    if not streams: return argv, []
    x264_only = re.compile(r"-(crf|preset|x264opts|x264-params|tune):(\d+)")
    maxrate = {}
    for i, a in enumerate(argv[:-1]):
        m = re.fullmatch(r"-maxrate:(\d+)", a); v = re.fullmatch(r"(\d+)([kKmM]?)", argv[i + 1])
        if m and v: maxrate[m.group(1)] = int(v.group(1)) * {"": 1, "k": 1000, "m": 1000000}[v.group(2).lower()]
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        m = x264_only.fullmatch(a)
        if m and m.group(2) in streams:
            i += 2; continue                              # drop option and its value
        if a == "libx264" and i > 0 and re.fullmatch(r"-(?:codec|c):(\d+)", argv[i - 1]):
            s = re.fullmatch(r"-(?:codec|c):(\d+)", argv[i - 1]).group(1)
            out += ["h264_videotoolbox", "-allow_sw", "0"]   # hardware only; fail rather than fall back to CPU
            if s in maxrate: out += ["-b:" + s, str(int(maxrate[s] * 0.85))]
            i += 1; continue
        out.append(a); i += 1
    return out, streams

def use_hw_decoder(argv):
    """Decode h264/hevc on the GPU (VideoToolbox) instead of the CPU. Input option, so it goes before -i."""
    if os.environ.get("PLEX_RUNNER_HWDEC", "1") != "1": return argv, False
    try: first_in = argv.index("-i")
    except ValueError: return argv, False
    decs = {argv[j + 1] for j in range(first_in - 1) if argv[j] == "-codec:0"}
    if not decs & {"h264", "hevc"}: return argv, False
    return argv[:first_in] + ["-hwaccel", "videotoolbox"] + argv[first_in:], True

HW_MIN_W, HW_MIN_H = 640, 360   # the Intel hardware H.264 encoder (VideoToolbox) refuses smaller outputs (error -12903)

def fit_hw_min_size(argv, hw_streams):
    """Upscale a hardware-encoded output that is smaller than the encoder's minimum (keeps the aspect ratio,
    dimensions rounded up to a multiple of 4). The player adapts to the stream's real size via the manifest."""
    if "0" not in hw_streams: return argv, None
    out, note = list(argv), None
    for i, a in enumerate(out):
        # the video chain is the filter_complex that scales (audio chains use aresample); its input label varies
        # with the container: [0:0], [0:1] (audio first), or a PID like [0:#0x1e0] for MPEG program streams
        if a.startswith("[") and "scale=w=" in a and "aresample" not in a:
            m = re.search(r"scale=w=(\d+):h=(\d+)", a)
            if not m: break
            w, h = int(m.group(1)), int(m.group(2))
            if w < HW_MIN_W or h < HW_MIN_H:
                f = max(HW_MIN_W / w, HW_MIN_H / h)
                w2, h2 = -(-int(round(w * f)) // 4) * 4, -(-int(round(h * f)) // 4) * 4
                new = a.replace("scale=w=%d:h=%d" % (w, h), "scale=w=%d:h=%d" % (w2, h2), 1)
                # burn-in subtitles: Plex scales the subtitle canvas to the same size (scale=W:H) before overlaying
                new = re.sub(r"scale=%d:%d(?=[\[;,:])" % (w, h), "scale=%d:%d" % (w2, h2), new)
                out[i] = new
                note = "%dx%d->%dx%d" % (w, h, w2, h2)
            break
    return out, note

def software_fallback(argv):
    """Second attempt after a fast failure of a hardware-accelerated run: no hwaccel decode, allow software encode."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] == "-hwaccel" and i + 1 < len(argv) and argv[i + 1] == "videotoolbox": i += 2; continue
        if argv[i] == "-allow_sw" and i + 1 < len(argv): out += ["-allow_sw", "1"]; i += 2; continue
        out.append(argv[i]); i += 1
    return out

def summarize(argv):
    """Compact description of what a command line does: input decoders and output encoders per stream."""
    try: first_in = argv.index("-i")
    except ValueError: first_in = len(argv)
    dec, enc = {}, {}
    for i, a in enumerate(argv[:-1]):
        m = re.fullmatch(r"-(?:codec|c):(\d+)", a)
        if m: (dec if i < first_in else enc)[m.group(1)] = argv[i + 1]
    fmt = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-f"), None)
    return {"input": argv[first_in + 1] if first_in + 1 < len(argv) else None, "decoders": dec,
            "encoders": enc, "format": fmt, "hwaccel": next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-hwaccel"), None),
            "video": "copy" if enc.get("0") == "copy" else enc.get("0"),
            "filters": [a for a in argv if a.startswith("[") and "]" in a][:4]}

def argv_diff(a, b):
    ops = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal": ops.append({"op": tag, "from": [redact(x) for x in a[i1:i2]], "to": [redact(x) for x in b[j1:j2]]})
    return ops

def codec_dir():
    c = [d for d in sorted(glob.glob(os.path.join(CODECS, "*-darwin-*"))) if not os.path.basename(d).startswith("EasyAudioEncoder")]
    return (c[-1] if c else CODECS) + "/"

def map_cwd(cwd):
    """Container /transcode/... -> shared host transcode dir. Must stay inside it."""
    if not isinstance(cwd, str) or not (cwd == "/transcode" or cwd.startswith("/transcode/")):
        return TRANSCODE
    host = os.path.realpath(os.path.join(TRANSCODE, cwd[len("/transcode/"):] if cwd != "/transcode" else ""))
    return host if under(host, [os.path.realpath(TRANSCODE)]) and os.path.isdir(host) else TRANSCODE

# ---- EAE (Plex's EasyAudioEncoder helper). E-AC-3 / TrueHD / MLP decoding is done by a helper service that
# watches folders under EAE_ROOT. Plex runs a Linux copy inside the container; the macOS transcoder needs the
# macOS copy on the host, with a host-side EAE_ROOT. One shared instance, started on demand.
EAE_ROOT = os.path.join(SUPPORT, "home", "eae")
_eae_lock = threading.Lock()
_eae = {"proc": None}

def eae_binary():
    c = sorted(glob.glob(os.path.join(CODECS, "EasyAudioEncoder-*-darwin-*", "EasyAudioEncoder", "EasyAudioEncoder")))
    return c[-1] if c else None

def ensure_eae():
    """Make sure the host EAE service is running; returns its EAE_ROOT, or None if unavailable."""
    with _eae_lock:
        b = eae_binary()
        if not b: return None
        p = _eae["proc"]
        running = (p is not None and p.poll() is None) or subprocess.run(["pgrep", "-f", b], capture_output=True).returncode == 0
        if not running:
            os.makedirs(EAE_ROOT, exist_ok=True)
            logf = open(os.path.join(LOGDIR, "eae.log"), "ab")
            cmd = ["/usr/bin/sandbox-exec", "-D", "PROJECT=" + PROJECT, "-D", "MEDIA=" + MEDIA, "-D", "TRANSCODE=" + TRANSCODE,
                   "-f", PROFILE, b]
            _eae["proc"] = subprocess.Popen(cmd, cwd=EAE_ROOT, env={"PATH": "/usr/bin:/bin", "HOME": os.path.join(SUPPORT, "home")},
                                            stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
            event("eae_started", pid=_eae["proc"].pid, root=EAE_ROOT, binary=b); log("EAE service started pid=%d" % _eae["proc"].pid)
        for _ in range(50):   # the service creates its watch folders ("Convert to ...") on startup
            if glob.glob(os.path.join(EAE_ROOT, "Convert to*")): break
            time.sleep(0.1)
        return EAE_ROOT

def build_env(env_in):
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.join(SUPPORT, "home"),
           "FFMPEG_EXTERNAL_LIBS": codec_dir(), "TMPDIR": os.path.join(SUPPORT, "home")}
    for k, v in (env_in or {}).items():
        if isinstance(k, str) and isinstance(v, str) and ENV_PASS.match(k) and not ENV_DROP.match(k):
            env[k] = v
    return env

def gpu_util():
    try:
        out = subprocess.run(["ioreg", "-r", "-d", "1", "-w0", "-c", "IOAccelerator"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r'"Device Utilization %"=(\d+)', out)
        return int(m.group(1)) if m else None
    except Exception: return None

# ---------------------------------------------------------------- HTTP server
class H(BaseHTTPRequestHandler):
    server_version = "plex-runner"
    def log_message(self, *a): pass
    def _deny(self, code, msg):
        self.send_response(code); self.end_headers(); self.wfile.write((msg + "\n").encode())
    def do_GET(self):
        self._deny(200, "ok") if self.path == "/health" else self._deny(404, "no")
    def _read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            body = b""
            while True:
                size = int(self.rfile.readline().strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline(); break
                body += self.rfile.read(size); self.rfile.readline()
                if len(body) > 4_000_000: raise ValueError("too large")
            return body
        n = int(self.headers.get("Content-Length", "0"))
        if n > 4_000_000: raise ValueError("too large")
        return self.rfile.read(n)

    def _relay(self):
        # /relay/<jobid>/<video/:/transcode/session/...>  ->  PMS, with the session token added.
        # (PMS only trusts token-less callbacks from loopback; OrbStack's port forwarder makes them look remote.)
        parts = self.path.split("/", 3)
        job = RELAYS.get(parts[2]) if len(parts) == 4 else None
        if not job or not parts[3].startswith("video/:/transcode/session/"): return self._deny(404, "no")
        import http.client
        try:
            body = self._read_body()
            c = http.client.HTTPConnection(PMS_HOST, PMS_PORT, timeout=30)
            c.request(self.command, "/" + parts[3], body=body,
                      headers={"X-Plex-Token": job["token"], "Content-Type": self.headers.get("Content-Type", "application/octet-stream")})
            resp = c.getresponse(); data = resp.read(); c.close()
            job["posts"] += 1; job["status"][str(resp.status)] = job["status"].get(str(resp.status), 0) + 1
        except Exception as e:
            job["errors"] += 1; job["last_error"] = str(e)
            log("RELAY ERROR (upstream) %s" % e); return self._deny(502, "relay error")
        try:
            self.send_response(resp.status); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        except OSError:
            pass   # transcoder closed the connection without reading the reply; expected

    def do_PUT(self):
        if self.path.startswith("/relay/"): return self._relay()
        self._deny(404, "no")

    def do_POST(self):
        if self.path.startswith("/relay/"): return self._relay()
        if self.path != "/run": return self._deny(404, "no")
        if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + TOKEN):
            log("AUTH FAIL from %s" % self.client_address[0]); event("auth_fail", client=self.client_address[0])
            return self._deny(401, "unauthorized")
        try:
            n = int(self.headers.get("Content-Length", "0")); body = json.loads(self.rfile.read(n) if 0 < n < 4_000_000 else b"")
        except Exception: return self._deny(400, "bad request")
        argv0, env_in = body.get("argv"), body.get("env") or {}
        cwd_container = body.get("cwd"); cwd = map_cwd(cwd_container)
        err = check_args(argv0)
        if err:
            log("REJECT: " + err)
            event("reject", reason=err, argv=[redact(a) for a in (argv0 if isinstance(argv0, list) else [])][:80])
            return self._deny(403, "rejected: " + err)
        if not slots.acquire(blocking=False):
            event("busy", max_jobs=MAX_JOBS); return self._deny(429, "busy")

        proc, jobid, t0 = None, os.urandom(6).hex(), time.time()
        rec = {"job_id": jobid, "cwd_container": cwd_container, "cwd_host": cwd, "pid": None}
        try:
            env = build_env(env_in)
            tok = env_in.get("X_PLEX_TOKEN", "")
            rec["session"] = next((m.group(1) for a in argv0 for m in [SESSION_RE.search(a)] if m), None)
            rec["env_keys_in"] = sorted(env_in.keys()); rec["env_keys_passed"] = sorted(env.keys())

            argv, transforms = list(argv0), []
            def relay(a):
                m = MANIFEST_URL.match(a)
                return "http://127.0.0.1:%d/relay/%s/%s" % (PORT, jobid, m.group(2)) if (m and tok) else a
            argv = [relay(a) for a in argv]
            if argv != list(argv0): transforms.append("manifest->relay")
            argv, dec = map_decoders(argv)
            if dec: transforms.append("decoder-map:" + ",".join(dec))
            argv, hw = use_hw_encoder(argv)
            if hw: transforms.append("hw-encode:libx264->h264_videotoolbox(stream %s)" % ",".join(hw))
            if hw:
                argv, hd = use_hw_decoder(argv)
                if hd: transforms.append("hw-decode:videotoolbox")
                argv, mn = fit_hw_min_size(argv, hw)
                if mn: transforms.append("hw-min-size:" + mn)
            if TRACE:
                for i, a in enumerate(argv[:-1]):
                    if a == "-loglevel": argv[i + 1] = "verbose"
                    if a == "-loglevel_plex": argv[i + 1] = "debug"
                transforms.append("trace-loglevel")
            if any(isinstance(a, str) and a.endswith("_eae") for a in argv):
                root = ensure_eae()
                if root:
                    env["EAE_ROOT"] = root; transforms.append("eae-service:host"); rec["eae_root"] = root
                else:
                    transforms.append("eae-service:unavailable")
            RELAYS[jobid] = {"token": tok, "posts": 0, "errors": 0, "status": {}}

            rec.update({"plex_summary": summarize(argv0), "run_summary": summarize(argv), "transforms": transforms,
                        "argv_original": [redact(a) for a in argv0], "argv_final": [redact(a) for a in argv],
                        "argv_diff": argv_diff(list(argv0), argv)})
            log("RUN  job=%s session=%s transforms=%s video=%s" % (jobid, rec["session"], transforms or "none", rec["run_summary"]["video"]))

            def build_cmd(av):
                return ["/usr/bin/sandbox-exec", "-D", "PROJECT=" + PROJECT, "-D", "MEDIA=" + MEDIA,
                        "-D", "TRANSCODE=" + TRANSCODE, "-f", PROFILE, TRANSCODER] + av
            holder, attempts = {"proc": None}, []
            self.send_response(200); self.send_header("Content-Type", "application/octet-stream"); self.end_headers()

            done, killed_by = threading.Event(), []
            gpu = []
            def watch():  # kill the job if the client (container stub) goes away
                while not done.is_set():
                    try: r, _, _ = select.select([self.connection], [], [], 1.0)
                    except (OSError, ValueError): return
                    if r:
                        try: gone = self.connection.recv(1, socket.MSG_PEEK) == b""
                        except OSError: gone = True
                        if gone:
                            killed_by.append("client-disconnect")
                            p = holder["proc"]
                            if p:
                                try: os.killpg(p.pid, signal.SIGTERM)
                                except OSError: pass
                            return
            def sample_gpu():
                while not done.wait(5.0):
                    g = gpu_util()
                    if g is not None and len(gpu) < 120: gpu.append(g)
            threading.Thread(target=watch, daemon=True).start()
            threading.Thread(target=sample_gpu, daemon=True).start()

            out_bytes, tail, outfile, ru, code = 0, b"", None, None, None
            cur = argv
            try:
                for attempt in (1, 2):
                    t_a = time.time()
                    proc = subprocess.Popen(build_cmd(cur), env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
                    holder["proc"] = proc; rec["pid"] = proc.pid
                    for chunk in iter(lambda: proc.stdout.read1(65536), b""):
                        out_bytes += len(chunk); tail = (tail + chunk)[-4096:]
                        if outfile is None: outfile = open(os.path.join(JOBDIR, jobid + ".out"), "wb")
                        outfile.write(chunk)
                        self.wfile.write(chunk); self.wfile.flush()
                    try: _, status, ru = os.wait4(proc.pid, 0)
                    except ChildProcessError: status, ru = 0, None
                    code = os.waitstatus_to_exitcode(status); proc.returncode = code
                    attempts.append({"attempt": attempt, "exit": code, "wall_s": round(time.time() - t_a, 2)})
                    accel = ("-hwaccel" in cur) or ("-allow_sw" in cur)
                    if attempt == 1 and code != 0 and accel and not killed_by and (time.time() - t_a) < 15:
                        cur = software_fallback(cur)
                        transforms.append("fallback:software(attempt1 exit=%s)" % code)
                        log("FALLBACK job=%s attempt1 exit=%s after %.1fs; retrying with software" % (jobid, code, time.time() - t_a))
                        rec["argv_fallback"] = [redact(a) for a in cur]
                        continue
                    break
                try: self.wfile.write(b"\n::EXIT::%d\n" % code); self.wfile.flush()
                except OSError: pass
            except (BrokenPipeError, ConnectionResetError):
                killed_by.append("client-disconnect")
                p = holder["proc"]
                if p:
                    try: os.killpg(p.pid, signal.SIGTERM)
                    except OSError: pass
                    try: _, status, ru = os.wait4(p.pid, 0)
                    except ChildProcessError: status, ru = 0, None
                    code = os.waitstatus_to_exitcode(status); p.returncode = code
            finally:
                done.set()
                proc = holder["proc"]
                if outfile: outfile.close()

            wall = time.time() - t0
            cpu = (ru.ru_utime + ru.ru_stime) if ru else None
            rec.update({"exit": code, "attempts": attempts, "wall_s": round(wall, 2), "killed_by": killed_by or None,
                        "cpu_user_s": round(ru.ru_utime, 2) if ru else None, "cpu_sys_s": round(ru.ru_stime, 2) if ru else None,
                        "cpu_avg_cores": round(cpu / wall, 2) if (cpu is not None and wall > 0) else None,
                        "max_rss_mb": round(ru.ru_maxrss / 1048576, 1) if ru else None,
                        "output_bytes": out_bytes, "output_tail": redact(tail.decode("utf8", "replace")[-2000:]),
                        "gpu_util_samples": gpu, "gpu_util_max": max(gpu) if gpu else None,
                        "relay": {k: v for k, v in RELAYS.get(jobid, {}).items() if k != "token"}})
            event("job", **rec)
            log("DONE job=%s exit=%s wall=%ss cpu_avg=%s cores gpu_max=%s relay=%s" % (
                jobid, code, rec["wall_s"], rec["cpu_avg_cores"], rec["gpu_util_max"], rec["relay"].get("posts")))
            prune_jobs()
        except Exception as e:
            event("error", job_id=jobid, error=repr(e)); log("ERROR job=%s %r" % (jobid, e)); raise
        finally:
            RELAYS.pop(jobid, None)
            if proc and proc.poll() is None:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except OSError: pass
            slots.release()

def render_profile():
    """Fill the seatbelt template: allow metadata (lstat) on the project/media dirs and all their parents."""
    anc = set()
    for p in (PROJECT, MEDIA):
        for q in {p, os.path.realpath(p)}:
            anc.add(q)
            d = os.path.dirname(q)
            while d and d != "/": anc.add(d); d = os.path.dirname(d)
    esc = lambda x: x.replace("\\", "\\\\").replace('"', '\\"')
    rule = " ".join('(literal "%s")' % esc(a) for a in sorted(anc))
    os.makedirs(SUPPORT, exist_ok=True)
    with open(PROFILE, "w") as f: f.write(open(PROFILE_TEMPLATE).read().replace(";;ANCESTORS;;", rule))

def validate_config():
    problems = []
    if not MEDIA or not os.path.isdir(MEDIA): problems.append("PLEX_MEDIA_DIR is not set to an existing directory (see .env.example)")
    if not TOKEN: problems.append("BRIDGE_TOKEN is empty (run ./setup-host.sh)")
    if not os.path.exists(TRANSCODER): problems.append("Plex Media Server.app not found in /Applications")
    return problems

def reap_orphans():
    """A runner restart leaves the previous run's transcoders alive (each job runs in its own session).
    Kill transcoder processes whose parent is launchd (pid 1); the shared EAE service is left alone."""
    killed = []
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[1] == "1" and parts[2].startswith(TRANSCODER + " "):
                try: os.killpg(int(parts[0]), signal.SIGTERM)
                except OSError:
                    try: os.kill(int(parts[0]), signal.SIGTERM)
                    except OSError: continue
                killed.append(int(parts[0]))
    except Exception as e:
        log("reap_orphans failed: %r" % e)
    return killed

if __name__ == "__main__":
    probs = validate_config()
    if probs:
        for pr in probs: print("config error: " + pr, file=sys.stderr); log("config error: " + pr)
        sys.exit(2)
    render_profile()
    stale = reap_orphans()
    if stale: log("reaped %d orphaned transcoder(s) from a previous run: %s" % (len(stale), stale)); event("reaped", pids=stale)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H); srv.daemon_threads = True
    log("runner listening on 127.0.0.1:%d (trace=%s)" % (PORT, TRACE)); event("start", port=PORT, trace=TRACE, max_jobs=MAX_JOBS, pid=os.getpid())
    srv.serve_forever()
