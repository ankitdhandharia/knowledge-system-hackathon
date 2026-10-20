# NOTES — how the system works

**Submission folder:** `submission/` (`solution.py` → `build()`; logic in `submission/cuknow/`).
**API keys:** none. **Paid APIs:** none. **Packages:** Python standard library only (3.9+).
Answers are deterministic and take ~0.01–0.15 s each; ingesting all five snapshots takes ~15 s.

```
python harness/run_eval.py --submission submission --eval eval/public_eval.jsonl
```
(Put the kit's `snapshots_zip/` folder at the repo root first — the snapshots are not committed.)

## 1. Keeping history, not just the latest copy

`ingest()` stores every *version* of every page. A page whose markdown is unchanged keeps its old
version id, so five snapshots of ~700 pages collapse to ~2,800 distinct versions. For each snapshot
we keep a *view* (`url → version`) that also carries forward pages missing from a later crawl
(the snapshot is "every page we had, in its newest version as of that day").

Each version is cleaned (images, data URIs, link targets, chart dumps, cookie / country-picker
boilerplate, the "Handpicked for You" related-articles rail are removed; links are kept separately)
and turned into:

* **passage units** — paragraph-sized blocks with their heading, indexed in one incremental BM25 index;
* **rate tables** — every markdown table (and `Label: 3.50%` line) with its caption ("Special
  Certificates" under "Certificates"), headers, rows, and the **"Rates are effective as of …" date**
  found under it; promos whose notes say they ended are flagged expired;
* **page metadata** — page kind / authority (see below) and the publication date of news & blog posts.

`answer(q, as_of)` picks the snapshot view for `as_of` (latest snapshot on or before that date; an
"as of May 2026" phrase inside the question works too) and only looks at versions live in that view.

## 2. Deciding what is "current"

1. **Authority of the page.** `/rates` and product pages (`/personal/…`, `/business/…`) beat landing
   pages, PDFs, newsroom posts and blogs; calculators and tag/author listings rank lowest; the
   `offer-expired` page and modal duplicates are ignored.
2. **Newest effective date wins inside a product family.** Tables are grouped into *families*
   (same product tier/term labels, or same caption). Within a family the table with the newest
   effective date — then the most authoritative page — is the answer. This is how the stale
   calculator / "switch-and-earn" auto table (5.34%, effective 2026-01-01) loses to the auto-loan
   rate table (5.39%, effective 2026-08-31), and the April mortgage table on the home-buying guide
   loses to the September rate sheet.
3. **Newest publication wins for recurring statements.** Press releases repeat boilerplate
   ("…22 branch office locations", "the fifth time Forbes recognized Dupaco"); counts and ordinals
   come from the most recently published page.
4. **`as_of` means the website as it stood then** — older snapshot views give the older values
   (the 10-month jumbo special is 3.90% APY in May, 4.00% in September).

## 3. Respecting qualifiers (tiers are not conflicts)

The question is parsed for **term** ("60-month", "30-year", "six month"), **model year**, **dollar
amount** (matched against balance / loan-amount tiers, tightest tier wins), **superlatives**
(highest / lowest), and discriminating words that appear in some rows but not others (jumbo, Roth,
purchase, refinance, intro…). Concepts the question does not mention (business, IRA, HSA, refinance,
home equity…) are down-weighted, so "Share Savings $10,000–$24,999" gives 0.15%, not the HSA or
money-market tier. Answers name the tier and the effective date and cite the page(s) showing them.

## 4. Routing and honest "I don't know"

* links / URLs → the page most linked with matching anchor text (weighted by the target's authority;
  "online" prefers the application sites such as join.dupaco.com);
* rates → the table engine above; hours → distinct lobby / drive-up schedules (or one branch's);
  addresses, phone, routing number → passages that contain that kind of value;
* everything else → BM25 passages, then the best sentences from the chosen page.

The system declines ("isn't listed… please contact us", `confident=False`, no numbers) when a rate
question names a product no table covers (7/1 ARM, construction loan, a term that does not exist),
when a named branch has no hours / address on the site (e.g. Mullan Avenue), or when no passage
covers the question well enough.

## 5. `conflicts()`

* **Rate tables** — for each product family, rows of the same tier/term are compared (a) across pages
  in the latest snapshot (stale copies) and (b) across snapshots on the authoritative page (the rate
  moved). One conflict per product + term, listing current and competing values; resolution = the
  authoritative newest-effective value, reason says which page is stale or that the rate changed.
* **Recurring statements** — the same phrase with different numbers / ordinals on different pages
  (branch-office count 19/20/22/23/24, "fourth/fifth time Forbes recognized"); resolution = the most
  recently published value.

## 6. Layout

| File | Role |
|---|---|
| `submission/solution.py` | contract adapter, answer cache (consistency), `build()` |
| `submission/cuknow/store.py` | page versions, snapshot views, units, tables, families, BM25 |
| `submission/cuknow/answer.py` | question parsing, routing, table / passage / hours / link answers |
| `submission/cuknow/conflicts.py` | contradiction detection and resolution |
| `submission/cuknow/textutil.py` | markdown cleanup, stemming, dates, terms, tiers |
| `tools/` | dev helpers (trace a page across snapshots, inspect misses, ask questions) |

Results on the kit: public eval **0.91** (accuracy pillar; consistency 1.0, speed 1.0 — p95 ≈ 0.1 s),
harness self-test **1.00** including conflicts F1 1.0.
