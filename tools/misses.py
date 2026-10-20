"""Dev helper: print the questions a run missed, with the answer text and sources."""
import json, sys
from pathlib import Path

res = Path(sys.argv[1] if len(sys.argv) > 1 else "results") / "per_question.jsonl"
items = {json.loads(l)["id"]: json.loads(l) for l in open(Path(__file__).resolve().parents[1] / "eval/public_eval.jsonl")}
for line in res.read_text().splitlines():
    r = json.loads(line)
    if r["score"] >= 0.999 and "-a" not in sys.argv:
        continue
    it = items.get(r["id"], {})
    print(f"## {r['id']} [{r['category']}] score={r['score']} missed={r['missed']} traps={r['trap_hits']} src={r['source_hit']}")
    print("Q:", it.get("question"), "| as_of:", it.get("as_of"))
    print("EXP:", it.get("expected_answer"))
    print("ANS:", r["answer"]["text"][:700], "| conf:", r["answer"]["confident"])
    print("SRC:", [s["url"] for s in r["answer"]["sources"]][:4], "| expected:", it.get("expected_sources"))
    print()
