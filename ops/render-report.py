#!/usr/bin/env python3
"""Turn a daily-review report (markdown) into styled text for Mail.app.

usage: ops/render-report.py <report.md> <out.txt> <out.styles>

Mail.app ignores scripted HTML bodies on current macOS (the message goes out empty), but it does send rich text, so
the report is rendered as clean text plus a list of style runs that ops/send-mail.applescript applies:

  out.txt      the message text, markdown syntax removed (headings, bullets, tables laid out in a fixed-width font)
  out.styles   one run per line, tab separated:  paragraph  from  to  font  size  colour
               paragraph is 1-based; from/to are 1-based characters within it (0 0 = the whole paragraph);
               colour is RRGGBB or "-" for the reader's default text colour (so dark mode keeps working).

No dependencies; only the markdown subset the digest and the review write is handled.
"""
import re, sys

SANS, BOLD, ITALIC, MONO, MONOBOLD = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Menlo-Regular", "Menlo-Bold"
GREY = "6B7280"
STATUS = {"all clear": "1E8E3E", "clean": "1E8E3E", "fixed": "2563C9", "needs attention": "D93025"}
STATUS_RE = re.compile(r"^\**\s*STATUS:\s*([a-z ]+?)\s*\**$", re.I)
INLINE_RE = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*|\[([^\]]+)\]\((https?://[^)\s]+)\)|(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])")
LIST_RE = re.compile(r"^\s*([-*]|\d+[.)])\s+(.*)$")


def inline(text, offset=0):
    """Strip inline markdown. Returns (plain text, [(from, to, font)]) with 1-based inclusive positions."""
    out, spans, pos = "", [], 0
    for m in INLINE_RE.finditer(text):
        out += text[pos:m.start()]; pos = m.end()
        code, bold, label, url, ital = m.groups()
        if label is not None:
            out += "%s (%s)" % (label, url); continue
        piece, font = (code, MONO) if code is not None else (bold, BOLD) if bold is not None else (ital, ITALIC)
        spans.append((offset + len(out) + 1, offset + len(out) + len(piece), font)); out += piece
    return out + text[pos:], spans


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render(md):
    """Returns (text, runs); runs are (paragraph, from, to, font, size, colour)."""
    paras, runs = [], []

    def add(text, font=SANS, size=14, colour="-", spans=()):
        paras.append(text)
        if text.strip():
            n = len(paras); runs.append((n, 0, 0, font, size, colour))
            runs.extend((n, a, b, f, size if f != MONO else size - 1, colour) for a, b, f in spans)

    def gap():
        if paras and paras[-1] != "": paras.append("")

    lines, i, title, status = md.splitlines(), 0, None, None
    # header: first "# title" and first STATUS line, wherever they are
    for k, line in enumerate(lines):
        m = STATUS_RE.match(line.strip())
        if status is None and m and m.group(1).lower() in STATUS: status = m.group(1).lower(); lines[k] = ""
        elif title is None and line.startswith("# "): title = line[2:].strip(); lines[k] = ""
        if title and status: break
    add(title or "Plex bridge daily review", BOLD, 20)
    add((status or "no status").upper(), BOLD, 14, STATUS.get(status, GREY))
    gap()
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            gap(); i += 1; continue
        if line.lstrip().startswith("```"):
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                add("    " + lines[i], MONO, 12); i += 1
            i += 1; gap(); continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            gap(); text, _ = inline(m.group(2))
            if len(m.group(1)) == 2: add(text.upper(), BOLD, 12, GREY)
            else: add(text, BOLD, 16 if len(m.group(1)) == 1 else 14)
            gap(); i += 1; continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]):
            rows = [[inline(c)[0] for c in _cells(line)]]; i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([inline(c)[0] for c in _cells(lines[i])]); i += 1
            ncol = max(len(r) for r in rows)
            rows = [r + [""] * (ncol - len(r)) for r in rows]
            width = [max(len(r[c]) for r in rows) for c in range(ncol)]
            fmt = lambda r: "    " + "   ".join(r[c].ljust(width[c]) for c in range(ncol)).rstrip()
            add(fmt(rows[0]), MONOBOLD, 12)
            add("    " + "   ".join("─" * w for w in width), MONO, 12, GREY)
            for r in rows[1:]: add(fmt(r), MONO, 12)
            gap(); continue
        m = LIST_RE.match(line)
        if m:
            while i < len(lines):
                m = LIST_RE.match(lines[i])
                if not m: break
                item = m.group(2); i += 1
                while i < len(lines) and lines[i].strip() and lines[i].startswith((" ", "\t")) and not LIST_RE.match(lines[i]):
                    item += " " + lines[i].strip(); i += 1
                lead = "  %s  " % ("•" if m.group(1) in "-*" else m.group(1))
                text, spans = inline(item, len(lead)); add(lead + text, spans=spans)
            gap(); continue
        text, spans = inline(line.strip()); add(text, spans=spans); i += 1
    while paras and paras[-1] == "": paras.pop()
    return "\n".join(paras) + "\n", runs


if __name__ == "__main__":
    if len(sys.argv) != 4: sys.exit(__doc__)
    text, runs = render(open(sys.argv[1], encoding="utf-8").read())
    open(sys.argv[2], "w", encoding="utf-8").write(text)
    open(sys.argv[3], "w", encoding="utf-8").write("".join("\t".join(str(x) for x in r) + "\n" for r in runs))
