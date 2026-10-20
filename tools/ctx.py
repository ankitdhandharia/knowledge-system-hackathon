"""Dev helper: regex search across one snapshot's pages, printing short context windows.
usage: ctx.py PATTERN [snapshot_prefix=05] [width=150] [max=20]"""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "snapshots"
pat = re.compile(sys.argv[1], re.I)
pref = sys.argv[2] if len(sys.argv) > 2 else "05"
w = int(sys.argv[3]) if len(sys.argv) > 3 else 150
mx = int(sys.argv[4]) if len(sys.argv) > 4 else 20
d = next(p for p in sorted(ROOT.iterdir()) if p.name.startswith(pref))
n = 0
for p in json.loads((d / "manifest.json").read_text())["pages"]:
    t = (d / p["file"]).read_text()
    for m in pat.finditer(t):
        s = t[max(0, m.start() - w):m.end() + w].replace("\n", " ⏎ ")
        print(f"[{p['url'][23:90]}] ...{s}...\n")
        n += 1
        if n >= mx:
            sys.exit()
