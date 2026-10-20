"""Dev helper: ingest once (cached pickle), then answer questions from argv / stdin with debug info.
usage: ask.py [--rebuild] [--as-of DATE] "question" ..."""
import pickle, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness")); sys.path.insert(0, str(ROOT / "submission"))
CACHE = ROOT / "results" / "ks.pkl"   # results/ is git-ignored
CACHE.parent.mkdir(exist_ok=True)

args = sys.argv[1:]
as_of = None
if "--as-of" in args:
    i = args.index("--as-of"); as_of = args[i + 1]; del args[i:i + 2]
if "--rebuild" in args or not CACHE.exists() or CACHE.stat().st_mtime < max(p.stat().st_mtime for p in (ROOT / "submission").rglob("*.py")):
    args = [a for a in args if a != "--rebuild"]
    from run_eval import load_snapshots
    import solution
    ks = solution.build()
    for s in load_snapshots(ROOT / "snapshots"):
        ks.ingest(s)
    CACHE.write_bytes(pickle.dumps(ks))
else:
    import solution  # noqa
    ks = pickle.loads(CACHE.read_bytes())
for q in args or [l.strip() for l in sys.stdin if l.strip()]:
    t = time.time()
    a = ks.answer(q, as_of)
    print(f"Q: {q}\nA: {a.text}\n   conf={a.confident} t={time.time()-t:.2f}s src={[s.url for s in a.sources][:4]}\n   note={a.conflict_note}\n")
