"""Dev helper: show how one URL's content changes across snapshots (unified diffs)."""
import difflib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "snapshots"


def load(url_sub):
    out = []
    for d in sorted(p for p in ROOT.iterdir() if (p / "manifest.json").exists()):
        m = json.loads((d / "manifest.json").read_text())
        for p in m["pages"]:
            if p["url"].rstrip("/").endswith(url_sub.rstrip("/")):
                out.append((d.name, p["url"], p.get("fetched_at"), (d / p["file"]).read_text()))
    return out


if __name__ == "__main__":
    hist = load(sys.argv[1])
    prev = None
    for sid, url, fa, md in hist:
        print(f"=== {sid} {url} fetched {fa} ({len(md)} chars)")
        if prev is not None:
            a=[l for l in prev.splitlines() if l.strip()]; b=[l for l in md.splitlines() if l.strip()]
            for line in difflib.unified_diff(a, b, lineterm="", n=0):
                if not line.startswith(("---", "+++", "@@")):
                    print(line[:300])
        prev = md
