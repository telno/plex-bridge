"""Minimal .env loader shared by the runner and ops scripts: KEY=VALUE lines, # comments, optional quotes.
Real environment variables always win over the file (os.environ.setdefault)."""
import os


def load(project_dir, filename=".env"):
    loaded = {}
    try:
        with open(os.path.join(project_dir, filename)) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1); k, v = k.strip(), v.strip()
                if v[:1] in ("'", '"') and v[-1:] == v[:1] and len(v) >= 2: v = v[1:-1]
                elif " #" in v: v = v.split(" #", 1)[0].strip()
                loaded[k] = v; os.environ.setdefault(k, v)
    except OSError:
        pass
    return loaded
