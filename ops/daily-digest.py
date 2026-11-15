#!/usr/bin/env python3
"""Digest the bridge's logs for a time window and decide whether anything needs attention.

  daily-digest.py [--hours N | --since ISO] [--json PATH] [--markdown PATH]

Window default: since reports/.cursor (the end of the previous review), at most 72 h back; otherwise 24 h.
Exit status: 0 = clean, 10 = anomalies found (see "reasons"), 2 = the digest itself failed.
Deterministic and cheap: used as the gate so the Claude review only runs when there is something to look at.
"""
import argparse, glob, json, os, re, subprocess, sys
from collections import Counter
from datetime import datetime, timedelta

P = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(P, "runner"))
import envconf
envconf.load(P)
RUNNER_PORT = os.environ.get("PLEX_RUNNER_PORT", "32499")
LOGDIR = os.path.join(P, "runner", "logs")
CURSOR = os.path.join(P, "reports", ".cursor")
NORMAL_EXITS = {0, 254, 255, 143, -15}          # 254/-15/143: Plex or the client ended the job; not failures
BENIGN_PMS = re.compile(r"Error muxing a packet|failed to rename file|Error submitting a packet to the muxer|"
                        r"Invalid library metadata ID|UltraBlurProcessor|Could not find job|Nothing was written into output|"
                        r"NAT: PMP|Promise destroyed without handling|Error while filtering: Generic error|"
                        r"Error muxing|No such file or directory|non-existing PPS|decode_slice_header error|no frame!|SPS unavailable|"
                        r"error while decoding MB|concealing \d+ DC", re.I)   # ffmpeg decode noise from damaged / mid-stream TS captures


def run(cmd, timeout=30):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception: return ""


def window(args):
    now = datetime.now().astimezone()
    if args.since: return datetime.fromisoformat(args.since).astimezone(), now
    if args.hours: return now - timedelta(hours=args.hours), now
    try:
        c = datetime.fromisoformat(open(CURSOR).read().strip()).astimezone()
        if now - timedelta(hours=72) <= c <= now: return c, now
    except Exception: pass
    return now - timedelta(hours=24), now


def events(since):
    files = sorted(glob.glob(os.path.join(LOGDIR, "bridge.jsonl.*")), reverse=True) + [os.path.join(LOGDIR, "bridge.jsonl")]
    for f in files:
        if not os.path.exists(f): continue
        for line in open(f, errors="replace"):
            try: r = json.loads(line)
            except ValueError: continue
            try: ts = datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%S%z")
            except Exception: continue
            if ts >= since: r["_ts"] = ts; yield r


def normalize(msg):
    msg = re.sub(r"\[Req#[0-9a-f]+[^\]]*\]", "", msg)
    msg = re.sub(r"0x[0-9a-f]+", "0x…", msg)
    msg = re.sub(r"[a-z0-9]{24}", "<sid>", msg)
    msg = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "<uuid>", msg)
    msg = re.sub(r"\d+", "N", msg)
    return msg.strip()[:200]


def pms_errors(since):
    out = run(["docker", "exec", "plex", "sh", "-c",
               'cd "/config/Library/Application Support/Plex Media Server/Logs" && cat "Plex Media Server.1.log" "Plex Media Server.log" 2>/dev/null | grep -E " (ERROR|WARN) - " | tail -4000'], 60)
    c, benign = Counter(), 0
    for line in out.splitlines():
        m = re.match(r"(\w{3} \d+, \d{4} \d+:\d+:\d+)\.\d+ \[\d+\] (ERROR|WARN) - (.*)", line)
        if not m or " ERROR " not in (" " + m.group(2) + " "): continue
        try: ts = datetime.strptime(m.group(1), "%b %d, %Y %H:%M:%S").astimezone()
        except ValueError: continue
        if ts < since: continue
        # only transcoding-related errors are in scope (library/metadata-agent errors are unrelated to this bridge)
        if not re.search(r"Transcode|Transcoder|EAE|JobRunner|EasyAudio", line): continue
        if BENIGN_PMS.search(m.group(3)): benign += 1; continue
        c[normalize(m.group(3))] += 1
    return c, benign


def stub_fallbacks(since):
    f = os.path.join(P, "transcode", "stub-calls.log"); out = []
    for path in (f + ".1", f):
        if not os.path.exists(path): continue
        for line in open(path, errors="replace"):
            if "FALLBACK" not in line: continue
            try: ts = datetime.strptime(line[:19], "%Y-%m-%dT%H:%M:%S").astimezone()
            except ValueError: continue
            if ts >= since: out.append(line.strip()[:240])
    return out


def health():
    h = {}
    h["container"] = run(["docker", "inspect", "-f", "{{.State.Status}}/{{.State.Health.Status}}", "plex"]).strip() or "missing"
    h["runner"] = "ok" if run(["curl", "-s", "-m", "3", "http://127.0.0.1:%s/health" % RUNNER_PORT]).strip() == "ok" else "DOWN"
    h["pms_identity"] = "ok" if "machineIdentifier" in run(["curl", "-s", "-m", "3", "http://127.0.0.1:32400/identity"]) else "DOWN"
    stuck = []
    for line in run(["ps", "-axo", "pid=,etime=,command="]).splitlines():
        if "Plex Media Server.app/Contents/MacOS/Plex Transcoder -" in line:
            et = line.split(None, 2)[1]
            if et.count(":") == 2 or "-" in et:   # H:MM:SS or D-HH:MM:SS => running over an hour
                stuck.append(line.strip()[:120])
    h["transcoders_over_1h"] = stuck
    h["unpushed_dirty_files"] = len([l for l in run(["git", "-C", P, "status", "--short"]).splitlines() if l.strip()])
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float); ap.add_argument("--since")
    ap.add_argument("--json"); ap.add_argument("--markdown")
    args = ap.parse_args()
    since, until = window(args)
    ev = list(events(since))
    jobs = [e for e in ev if e["event"] == "job"]
    failed = []
    for j in jobs:
        bad_exit = j.get("exit") not in NORMAL_EXITS
        fell_back = any(t.startswith("fallback") for t in j.get("transforms", [])) or len(j.get("attempts") or []) > 1
        if bad_exit or fell_back:
            rs, ps = j.get("run_summary", {}), j.get("plex_summary", {})
            failed.append({"job_id": j["job_id"], "ts": j["ts"], "exit": j.get("exit"), "wall_s": j.get("wall_s"),
                           "input": os.path.basename(str(rs.get("input"))), "plex_decoders": ps.get("decoders"), "plex_encoders": ps.get("encoders"),
                           "ran_decoders": rs.get("decoders"), "ran_encoders": rs.get("encoders"), "transforms": j.get("transforms"),
                           "attempts": j.get("attempts"), "argv_diff": j.get("argv_diff"), "output_tail": (j.get("output_tail") or "")[-600:],
                           "filters": rs.get("filters")})
    others = [{k: v for k, v in e.items() if k != "_ts"} for e in ev if e["event"] in ("reject", "auth_fail", "busy", "error")]
    relay_bad = [{"job_id": j["job_id"], "relay": j.get("relay")} for j in jobs
                 if j.get("relay", {}).get("errors") or any(s != "200" for s in j.get("relay", {}).get("status", {}))]
    unavailable = [j["job_id"] for j in jobs if "eae-service:unavailable" in j.get("transforms", [])]
    tcount = Counter(t.split(":")[0] for j in jobs for t in j.get("transforms", []))
    long_jobs = [j for j in jobs if (j.get("wall_s") or 0) > 20 and j.get("cpu_avg_cores") is not None]
    perf = {"jobs": len(jobs), "transforms": dict(tcount),
            "avg_cpu_cores": round(sum(j["cpu_avg_cores"] for j in long_jobs) / len(long_jobs), 2) if long_jobs else None,
            "max_gpu_util": max([j.get("gpu_util_max") or 0 for j in jobs] or [0]),
            "video_modes": dict(Counter(str(j.get("run_summary", {}).get("video")) for j in jobs))}
    errs, benign = pms_errors(since)
    stubfb = stub_fallbacks(since)
    h = health()

    reasons = []
    if failed: reasons.append("%d job(s) failed or needed a fallback" % len(failed))
    if others: reasons.append("%d reject/auth/busy/error event(s)" % len(others))
    if relay_bad: reasons.append("%d job(s) with relay errors or non-200 relay statuses" % len(relay_bad))
    if unavailable: reasons.append("EAE service unavailable for %d job(s)" % len(unavailable))
    if stubfb: reasons.append("%d stub fallback(s) to the local software transcoder" % len(stubfb))
    if errs: reasons.append("%d distinct unexpected Plex ERROR message(s)" % len(errs))
    if h["container"] != "running/healthy": reasons.append("container state: " + h["container"])
    if h["runner"] != "ok": reasons.append("runner is DOWN")
    if h["pms_identity"] != "ok": reasons.append("Plex not answering /identity")
    if h["transcoders_over_1h"]: reasons.append("%d transcoder(s) running over an hour" % len(h["transcoders_over_1h"]))

    d = {"window": {"since": since.isoformat(), "until": until.isoformat()}, "perf": perf, "failed_jobs": failed, "other_events": others,
         "relay_problems": relay_bad, "stub_fallbacks": stubfb, "pms_errors": errs.most_common(12), "pms_benign_errors_ignored": benign,
         "health": h, "reasons": reasons, "clean": not reasons}
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True); open(args.json, "w").write(json.dumps(d, indent=2, default=str))
    md = ["# Plex bridge digest", "", "Window: %s → %s" % (since.strftime("%Y-%m-%d %H:%M"), until.strftime("%Y-%m-%d %H:%M")), "",
          "**Status: %s**" % ("clean" if not reasons else "needs attention"), ""]
    md += ["- " + r for r in reasons] or ["- no failed jobs, fallbacks, rejects, relay problems or unexpected Plex errors"]
    md += ["", "Jobs: %d; video modes: %s; avg CPU (jobs >20 s): %s cores; max GPU util: %s%%" % (
        perf["jobs"], perf["video_modes"], perf["avg_cpu_cores"], perf["max_gpu_util"]),
        "Rewrites: %s" % (perf["transforms"] or "none"), "Health: %s" % h["container"] + ", runner " + h["runner"] + ", Plex " + h["pms_identity"]]
    text = "\n".join(md) + "\n"
    if args.markdown: open(args.markdown, "w").write(text)
    else: print(text)
    sys.exit(0 if not reasons else 10)


if __name__ == "__main__":
    try: main()
    except SystemExit: raise
    except Exception as e:
        print("digest failed: %r" % e, file=sys.stderr); sys.exit(2)
