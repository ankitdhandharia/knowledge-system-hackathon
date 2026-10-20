# Knowledge system that knows what's true now

A snapshot-aware question-answering system over five crawls of a credit union's website. It answers
with the **current** value (or the value **as of** a date), the right **tier**, a **source**, flags
**contradictions**, and says **"I don't know"** when the site doesn't say.

- Submission: [`submission/`](submission/) — `solution.py` exposes `build()` (ingest / answer / conflicts)
- Approach and rules for "what is current": [`NOTES.md`](NOTES.md)
- No API keys, no third-party packages (Python 3.9+ standard library)

## Run

The website snapshots are **not** in this repository. Copy the kit's `snapshots_zip/` folder (five zips)
to the repo root, then:

```bash
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl
```

The scorer unpacks the zips into `snapshots/` on first run.

| | Score |
|---|---|
| Public eval (40 questions) | 0.91 final — consistency 1.0, speed 1.0 (p95 ≈ 0.1 s) |
| Harness self-test | 1.00, conflicts F1 1.0 |

Original kit instructions: [`docs/KIT_README.md`](docs/KIT_README.md).
