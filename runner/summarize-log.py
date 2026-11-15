#!/usr/bin/env python3
"""Summarize runner/logs/bridge.jsonl (and rotated copies).

  summarize-log.py              table of recent jobs
  summarize-log.py --transforms how often each argument rewrite fired
  summarize-log.py --job ID     full record for one job
  summarize-log.py --rejects    rejected/failed requests
"""
import glob, json, os, sys
from collections import Counter

LOGDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

def records():
    files = sorted(glob.glob(os.path.join(LOGDIR, "bridge.jsonl.*")), reverse=True) + [os.path.join(LOGDIR, "bridge.jsonl")]
    for f in files:
        if not os.path.exists(f): continue
        for line in open(f, errors="replace"):
            try: yield json.loads(line)
            except ValueError: pass

def main():
    a = sys.argv[1:]
    recs = list(records())
    jobs = [r for r in recs if r.get("event") == "job"]
    if "--job" in a:
        jid = a[a.index("--job") + 1]
        for r in jobs:
            if r["job_id"] == jid: print(json.dumps(r, indent=2)); return
        print("no such job"); return
    if "--last" in a:
        n = int(a[a.index("--last") + 1]) if a.index("--last") + 1 < len(a) else 1
        for r in jobs[-n:]:
            rs, ps = r.get("run_summary", {}), r.get("plex_summary", {})
            print("job %s exit=%s wall=%ss cpu=%s cores gpu_max=%s relay=%s" % (r["job_id"], r.get("exit"), r.get("wall_s"), r.get("cpu_avg_cores"), r.get("gpu_util_max"), r.get("relay")))
            print("  input      :", os.path.basename(str(rs.get("input"))))
            print("  plex asked : dec=%s enc=%s fmt=%s" % (ps.get("decoders"), ps.get("encoders"), ps.get("format")))
            print("  ran as     : dec=%s enc=%s hwaccel=%s" % (rs.get("decoders"), rs.get("encoders"), rs.get("hwaccel")))
            print("  filters    :", [f[:110] for f in rs.get("filters", [])])
            print("  transforms :", r.get("transforms"))
            if r.get("killed_by"): print("  killed_by  :", r["killed_by"])
            if r.get("output_tail", "").strip(): print("  output     :", r["output_tail"].strip().replace("\n", " | ")[:400])
        return
    if "--rejects" in a:
        for r in recs:
            if r.get("event") in ("reject", "auth_fail", "busy", "error"): print(json.dumps(r)[:400])
        return
    if "--transforms" in a:
        c = Counter(t.split(":")[0] for r in jobs for t in r.get("transforms", []))
        for k, v in c.most_common(): print("%5d  %s" % (v, k))
        print("%5d  jobs total, %d with no rewrite" % (len(jobs), sum(1 for r in jobs if not r.get("transforms"))))
        return
    print("%-19s %-8s %-4s %-7s %-9s %-24s %-5s %-5s %-4s %s" % ("time", "job", "exit", "wall_s", "cpu_cores", "video", "gpu%", "hwdec", "post", "input"))
    for r in jobs[-40:]:
        rs = r.get("run_summary", {})
        print("%-19s %-8s %-4s %-7s %-9s %-24s %-5s %-5s %-4s %s" % (
            r["ts"][:19].replace("T", " "), r["job_id"], r.get("exit"), r.get("wall_s"), r.get("cpu_avg_cores"),
            str(rs.get("video")), r.get("gpu_util_max"), "yes" if rs.get("hwaccel") else "-",
            r.get("relay", {}).get("posts"), os.path.basename(str(rs.get("input")))[:50]))

if __name__ == "__main__":
    main()
