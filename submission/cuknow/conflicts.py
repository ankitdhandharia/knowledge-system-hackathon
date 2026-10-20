"""Find places where the website contradicts itself, pick the value we believe, and say why.

Two detectors:
  1. Rate tables. Rows of the same product tier/term are compared (a) across pages in the latest
     snapshot (stale copies on calculators, landing pages, old guides) and (b) across snapshots on
     the authoritative page (the rate moved). Winner: the authoritative table with the newest
     effective date in the latest snapshot.
  2. Recurring statements. Boilerplate facts repeated across press releases ("It has over 600
     employees and 22 branch office locations", "the fifth time Forbes recognized Dupaco") whose
     number differs between pages. Winner: the most recently published page.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from .store import Store, Table
from .textutil import STOP, terms_in, tokens, year_range

ORDINALS = {w: i + 1 for i, w in enumerate(
    "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth".split())}
_PCT_CELL = re.compile(r"\d+(?:\.\d+)?%")
_OLD = date(1900, 1, 1)


def _row_keys(t: Table) -> list[str]:
    """Stable key per row: product/tier label with model-year ranges replaced by their rank
    (so '2024-27' on the current table lines up with '2023-26' on a stale copy)."""
    ranges: list = []
    keys = []
    for r in t.rows:
        lab = Store._row_label(t, r)
        cells = " ".join(c for c in r if "%" not in c)
        yr = year_range(cells)
        if yr:
            if yr not in ranges:
                ranges.append(yr)
            tag = f"yr{ranges.index(yr) + 1}"
            lab = re.sub(r"\byr\b|\b(19|20)\d\d\b", tag, lab)
        keys.append(re.sub(r"\s+", " ", lab).strip())
    return keys


def _sig(r: list, headers: list = ()) -> tuple:
    """The rate values of a row (points / payment columns excluded)."""
    hs = list(headers) + [""] * (len(r) - len(headers))
    return tuple(v for h, c in zip(hs, r) if "%" in c and not re.search(r"points|payment", h, re.I)
                 for v in _PCT_CELL.findall(c))


def _term_phrase(label: str) -> str:
    ts = terms_in(label)
    if not ts:
        return ""
    n = ts[0]
    return f"{n // 12}-year" if n % 12 == 0 and n >= 120 else f"{n}-month"


def _rate_word(t: Table) -> str:
    h = " ".join(t.headers).lower()
    if "apy" in h or "yield" in h or "dividend" in h:
        return "APY"
    if "apr" in h or "annual percentage rate" in h:
        return "APR"
    return "rate"


def table_conflicts(store: Store) -> list[dict]:
    if not store.snapshots:
        return []
    last = len(store.snapshots) - 1
    sid = {i: s[0] for i, s in enumerate(store.snapshots)}
    fams: dict = defaultdict(list)
    for t in store.tables:
        if t.is_rate and not t.expired:
            fams[t.family].append(t)
    out = []
    for fam, tables in fams.items():
        live = []
        for t in tables:
            pv = store.live_page(t, last)
            if pv is not None and pv.kind not in ("expired", "modal", "listing"):
                live.append((t, pv))
        if not live:
            continue
        rep_t, rep_pv = max(live, key=lambda x: (x[0].effective or _OLD, x[1].authority, -x[0].tid))
        cur = dict(zip(_row_keys(rep_t), rep_t.rows))
        # observations: key -> sig -> [(url, snapshot, effective, quote)]
        obs: dict = defaultdict(lambda: defaultdict(list))
        for t, pv in live:                                   # (a) other pages, latest snapshot
            for k, r in zip(_row_keys(t), t.rows):
                if _sig(r, t.headers):
                    obs[k][_sig(r, t.headers)].append((pv.url, sid[last], t.effective, " | ".join(r), "page"))
        for s_idx in range(last):                            # (b) same page, earlier snapshots
            for t in tables:
                pv = store.live_page(t, s_idx)
                if pv is None or t.url != rep_t.url:
                    continue
                for k, r in zip(_row_keys(t), t.rows):
                    if _sig(r, t.headers) and k in cur:
                        obs[k][_sig(r, t.headers)].append((pv.url, sid[s_idx], t.effective, " | ".join(r), "time"))
        cur_keys = list(cur)
        groups: dict = defaultdict(list)
        for k in obs:
            if k not in cur:
                continue
            want = len(_sig(cur[k], rep_t.headers))
            obs[k] = {sg: seen for sg, seen in obs[k].items() if len(sg) == want}   # same-shape rows only
            if len(obs[k]) > 1:
                groups[_term_phrase(k) or k].append(k)
        for gkey, keys in groups.items():
            keys.sort(key=cur_keys.index)
            head = keys[0]
            cur_sig = _sig(cur[head], rep_t.headers)
            values, sources, why = list(cur_sig), [], set()
            for k in keys:
                csig = _sig(cur[k], rep_t.headers)
                others = sorted((x for x in obs[k].items() if x[0] != csig),
                                key=lambda x: (min(o[4] for o in x[1]), -max((o[2] or _OLD).toordinal() for o in x[1])))
                for sg, seen in [(csig, obs[k].get(csig, []))] + others:
                    for v in sg:
                        if v not in values:
                            values.append(v)
                    if seen:
                        o = seen[0]
                        sources.append((o[0], o[1], o[3][:200]))
                    if sg != csig:
                        why |= {o[4] for o in seen}
            if not why:
                continue
            label = " ".join(c for c in cur[head] if "%" not in c)
            topic = f"{gkey + ' ' if gkey != head else ''}{rep_t.caption} {_rate_word(rep_t)} ({label})".strip()
            if rep_t.group and rep_t.group.lower() not in topic.lower():
                topic = f"{rep_t.group}: {topic}"
            eff = rep_t.effective.isoformat() if rep_t.effective else "undated"
            parts = []
            if "page" in why:
                stale = sorted({o[0] for k in keys for sg, seen in obs[k].items() if sg != _sig(cur[k])
                                for o in seen if o[4] == "page"})
                parts.append(f"{rep_pv.url} carries the authoritative table (effective {eff}); "
                             f"{', '.join(stale[:3])} still show older figures")
            if "time" in why:
                parts.append("the rate changed between snapshots; the latest snapshot is current "
                             "(earlier values only apply to 'as of' questions)")
            out.append({
                "topic": topic,
                "values": values[:12],
                "sources": sources[:8],
                "resolution": " / ".join(cur_sig),
                "reason": "; ".join(parts) + ".",
                "_weight": len(why) + ("page" in why),
            })
    return out


def statement_conflicts(store: Store) -> list[dict]:
    """Numbers (or ordinals) in recurring statements that disagree between pages."""
    if not store.snapshots:
        return []
    last = len(store.snapshots) - 1
    slots: dict = defaultdict(lambda: defaultdict(list))
    for url, pid in store.views[last].items():
        pv = store.pages[pid]
        if pv.kind in ("expired", "modal", "listing", "calculator"):
            continue
        when = pv.pub or (store.snapshots[last][1] if pv.kind not in ("news", "blog", "file") else _OLD)
        seen = set()
        for uid in store.page_units.get(pid, []):
            u = store.units[uid]
            if u.kind != "para":
                continue
            for m in re.finditer(r"\b(\d{1,3}(?:,\d{3})*|\d+|" + "|".join(ORDINALS) + r")\+?\s+((?:[A-Za-z’'-]+\s+){2}[A-Za-z’'-]+)",
                                 u.text):
                val = m.group(1).lower()
                nxt = [w for w in tokens(m.group(2), keep_stop=True)]
                before = u.text[max(0, m.start() - 2):m.start()]
                if len(nxt) < 3 or nxt[0] in STOP or re.fullmatch(r"(19|20)\d\d|\d{5}", val) or "$" in before \
                        or "\n" in m.group(2) or nxt[0] in ("million", "billion", "percent", "year", "month", "day"):
                    continue
                key = " ".join(nxt[:3])
                if (key, val) in seen:
                    continue
                seen.add((key, val))
                start = u.text.rfind(".", 0, m.start()) + 1
                end = u.text.find(".", m.end())
                quote = u.text[start:end + 1 if end > 0 else None].strip()[:200]
                slots[key][val].append((when, url, quote, m.group(0)))
    out = []
    for key, vals in slots.items():
        n_pages = sum(len(v) for v in vals.values())
        ordinal = all(v in ORDINALS for v in vals)
        multi = sum(1 for v in vals.values() if len(v) >= 2)
        if len(vals) < 2 or len(vals) > 6 or (not ordinal and (n_pages < 6 or multi < 2)):
            continue
        if not any(w for w in key.split() if w not in STOP and len(w) > 3):
            continue
        if not ordinal and not all(re.fullmatch(r"[\d,]+", v) for v in vals):
            continue
        newest = max(vals.items(), key=lambda kv: max(o[0] for o in kv[1]))
        phrase = newest[1][0][3]
        words = re.sub(r"^\S+\s+", "", phrase)
        values = sorted(vals, key=lambda v: max(o[0] for o in vals[v]))
        out.append({
            "topic": f"{'how many times: ' if ordinal else 'number of '}{words}",
            "values": [f"{v} {words}" for v in values],
            "sources": [(vals[v][0][1], store.snapshots[last][0], vals[v][0][2]) for v in values][:8],
            "resolution": f"{newest[0]} {words}",
            "reason": f"Repeated statement disagrees across {n_pages} pages; the most recently published one "
                      f"({max(o[0] for o in newest[1]).isoformat()}, {newest[1][0][1]}) is taken as current.",
            "_weight": 1,
        })
    return out


def find_conflicts(store: Store) -> list[dict]:
    items = table_conflicts(store) + statement_conflicts(store)
    items.sort(key=lambda c: (-c["_weight"], c["topic"]))
    for c in items:
        c.pop("_weight", None)
    return items
