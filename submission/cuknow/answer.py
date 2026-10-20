"""Question answering over the snapshot store.

Routing:
  * link / URL questions        -> most-linked page whose anchor text matches the question
  * rate / APY / APR questions  -> parsed rate tables (authoritative, newest effective date)
  * branch-hours questions      -> hours blocks, grouped into distinct schedules
  * everything else             -> BM25 passage retrieval, trimmed to the best sentences
Each route can decline ("isn't listed") when the site does not actually say.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from urllib.parse import urlparse

from .store import PageVersion, Store, Table, Unit
from .textutil import (RATE_WORDS, amount_range, money_values, norm_space, question_as_of, replace_number_words,
                       split_sentences, stem, terms_in, to_date, tokens, words, year_range, years_in)

_SYN_RAW = {
    "cd": ["certificate"], "cds": ["certificate", "cd"], "certificate": ["cd"], "share": [],
    "mortgage": ["home", "conventional", "purchase"], "house": ["home"], "home": ["mortgage"],
    "car": ["auto", "vehicle"], "vehicle": ["auto"], "auto": ["vehicle"], "truck": ["auto"], "suv": ["auto"],
    "motorcycle": ["auto"], "heloc": ["home", "equity", "line", "credit"],
    "card": ["visa"], "visa": ["card"], "credit": [],
    "ira": ["retirement", "roth", "traditional"], "retirement": ["ira"],
    "boat": ["rv", "recreational"], "rv": ["boat", "recreational"], "camper": ["rv"],
    "atv": ["snowmobile", "jet", "ski"], "snowmobile": ["atv"],
    "hsa": ["health", "saving"], "health": ["hsa"],
    "kid": ["dopack", "youth", "child"], "child": ["kid", "dopack"], "teen": ["youth"],
    "join": ["member", "membership", "become"], "member": ["join", "membership"], "membership": ["join", "member", "eligible"],
    "eligible": ["membership", "join", "eligibility"], "eligibility": ["eligible", "membership"],
    "locator": ["location", "branch", "atm"], "location": ["branch", "locator"],
    "student": ["college", "school"], "college": ["student"], "university": ["student", "college"],
    "school": ["student"], "wallet": ["apple", "google", "samsung", "pay", "mobile"],
    "phone": ["call", "telephone"], "contact": ["call", "phone"], "checking": ["check"],
    "savings": ["saving"], "saving": ["savings"], "address": ["located", "location"],
    "routing": ["aba"], "fee": ["charge", "cost"], "cost": ["fee"], "overdraft": ["nsf"],
    "update": ["change"], "change": ["update"], "info": ["information"], "information": ["info"],
    "volunteer": ["vto"], "vto": ["volunteer"], "forbes": ["best"], "branch": ["location", "office"],
    "lobby": ["branch"], "drive": ["drive-up", "drive-through"], "money": [],
}

SYN = {stem(k): [stem(v) for v in vs] for k, vs in _SYN_RAW.items()}

GENERIC_RAW = {"current", "currently", "today", "right", "now", "much", "offer", "offered", "available", "provide",
           "rate", "rates", "apy", "apr", "dividend", "yield", "interest", "earn", "pay", "percentage", "percent",
           "annual", "number", "kind", "type", "option", "new", "standard", "look", "find", "see", "show",
           "charge", "month", "year", "like", "would", "let", "repeat", "say", "said", "quote", "give", "us",
           "list", "tell", "time", "most", "best", "highest", "lowest", "top", "minimum", "maximum", "cheapest",
               "tier", "tiers", "balance", "many", "dupaco", "aprs", "apys", "use", "should", "go", "link", "url", "website",
               "web", "page", "site", "online", "become", "currently", "info", "information", "details"}
GENERIC = {stem(w) for w in GENERIC_RAW}
RATE_STEMS = {stem(w) for w in RATE_WORDS}

CRITICAL = {"adjustable", "arm", "construction", "land", "lot", "farm", "agricultural", "agriculture",
            "commercial", "variable", "fha", "va", "usda", "balloon", "bridge", "reverse", "payday", "title",
            "interest-only", "7/1", "5/1", "10/1", "3/1", "5/5", "jumbo", "sba", "lease", "leasing", "crypto",
            "bitcoin", "gold", "foreign", "wire"}
CRITICAL = {stem(w) for w in CRITICAL}

# table-level concepts: (trigger words in the question, regex over table caption/group/url)
CONCEPTS = [
    ({"business", "commercial", "company", "llc"}, r"business"),
    ({"ira", "roth", "traditional", "retirement"}, r"\bira|roth|traditional|retirement"),
    ({"refinance", "refinancing", "refi", "refinanced"}, r"refinanc"),
    ({"hsa", "health"}, r"health|hsa"),
    ({"equity", "heloc"}, r"equity|heloc"),
    ({"relief"}, r"relief"),
    ({"student", "college"}, r"student"),
    ({"launchpad"}, r"launchpad"),
    ({"holiday", "club"}, r"holiday"),
]

CHANGE_WORDS = re.compile(r"\b(chang\w*|previous\w*|used to|before|increas\w*|decreas\w*|went up|go up|went down|"
                          r"raised?|lowered|history|differen\w*|compared?|old rate|last time)\b", re.I)
SUPER_HI = re.compile(r"\b(highest|best|top|max(imum)?|most)\b", re.I)
SUPER_LO = re.compile(r"\b(lowest|cheapest|min(imum)?|least|smallest)\b", re.I)
LIST_WORDS = re.compile(r"\b(all|every|list|options|types|kinds|offer|what (cards|loans|accounts|certificates))\b", re.I)

TIME_RX = re.compile(r"\b\d{1,2}(:\d\d)?\s*(a\.?m\.?|p\.?m\.?)|\bnoon\b|\bclosed\b", re.I)
STREET_RX = re.compile(r"\b\d{2,6}\s+(?:[NSEW]\.?\s+)?[A-Z][A-Za-z.]*(?:\s+[A-Z][A-Za-z.]*){0,4}\s+"
                       r"(?:St|Street|Ave|Avenue|Rd|Road|Ln|Lane|Dr|Drive|Blvd|Boulevard|Ct|Court|Pl|Place|Plaza|"
                       r"Hwy|Highway|Way|Pkwy|Parkway|Cir|Circle)\b\.?")
PHONE_RX = re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
ROUTING_RX = re.compile(r"\b\d{9}\b")
NUM_RX = re.compile(r"\b\d[\d,]*\b")

TYPE_WORDS = frozenset(stem(w) for w in ("address", "located", "location", "phone", "telephone", "number", "call",
                                          "hours", "hour", "routing", "open", "close"))

REFUSAL = ("I'm sorry, but that isn't listed on the website, so I can't confirm it. "
           "Please contact us or visit a branch and a Dupaco team member can help.")


@dataclass
class Result:
    text: str
    sources: list = field(default_factory=list)   # (url, snapshot_id, quote)
    confident: bool = True
    note: Optional[str] = None


@dataclass
class Query:
    raw: str
    text: str
    toks: list
    content: list
    weights: dict
    terms: list
    years: list
    amounts: list
    sidx: int
    when: Optional[date]


def _norm_url(u: str) -> str:
    u = re.sub(r"[?&]hsLang=[a-z-]+", "", u.strip())
    u = re.sub(r"\?$", "", u)
    if u.startswith("/"):
        u = "https://www.dupaco.com" + u
    return u


def _short_header(h: str) -> str:
    hl = h.lower()
    if "apy" in hl or "annual percentage yield" in hl:
        return "APY" + (" " + re.sub(r".*apy\)?\*?", "", hl).strip() if "month" in hl or "after" in hl else "")
    if "apr" in hl or "annual percentage rate" in hl:
        return "APR" + (" (as low as)" if "as low" in hl else "")
    if "dividend" in hl:
        return "dividend rate" + (" " + hl.split("rate", 1)[1].strip() if "month" in hl or "after" in hl else "")
    if hl.strip() in ("rate", "interest rate"):
        return "rate"
    return h


class Answerer:
    def __init__(self, store: Store):
        self.s = store
        self._ctx_cache: dict = {}
        self._tidf_cache: dict = {}

    # ================================================================== entry point
    def answer(self, question: str, as_of: Optional[str] = None) -> Result:
        q = self._parse(question, as_of)
        if q.sidx < 0:
            return Result(REFUSAL, confident=False)
        ql = q.text.lower()
        if re.search(r"\b(url|link|web ?page|web ?site address|web address)\b", ql):
            r = self._url_answer(q)
            if r:
                return r
        if re.search(r"\brouting\b", ql):
            r = self._typed_passage(q, ROUTING_RX, "routing")
            if r:
                return r
        rate_q = bool(set(tokens(ql, keep_stop=True)) & RATE_STEMS) and not re.search(r"\bfees?\b", ql)
        if rate_q:
            r = self._rate_answer(q)
            if r:
                return r
        if re.search(r"\bhours?\b|what time", ql):
            return self._hours_answer(q)
        if re.search(r"\baddress\b|\bwhere is\b|\blocated\b", ql):
            r = self._typed_passage(q, STREET_RX, "address")
            if r:
                return r
        if re.search(r"\b(phone|telephone|call)\b", ql) and not re.search(r"\broutine\b", ql):
            r = self._typed_passage(q, PHONE_RX, "phone")
            if r:
                return r
        if rate_q and self._critical_unmatched(q, None):
            return Result(REFUSAL, confident=False)
        r = self._passage_answer(q, require_pct=rate_q)
        return r

    # ================================================================== parsing
    def _parse(self, question: str, as_of: Optional[str]) -> Query:
        text = replace_number_words(question.strip())
        when = to_date(as_of) if as_of else None
        if when is None:
            qd = question_as_of(question)
            if qd and self.s.snapshots and qd < self.s.snapshots[-1][1]:
                when = qd
        sidx = self.s.snapshot_for(when)
        toks = tokens(text)
        content = [t for t in toks if t not in GENERIC and not re.fullmatch(r"[\d.,/]+", t)]
        weights: dict = defaultdict(float)
        for t in toks:
            weights[t] = max(weights[t], 0.35 if t in GENERIC else 0.6 if re.fullmatch(r"[\d.,/]+", t) else 1.0)
            for sy in SYN.get(t, []):
                weights[sy] = max(weights.get(sy, 0.0), 0.5)
        # phrase hints
        ql = text.lower()
        if "credit card" in ql or "credit cards" in ql:
            weights["visa"] = max(weights["visa"], 0.8)
        if "money market" in ql:
            weights["market"] = max(weights["market"], 1.0)
        amounts = money_values(text)
        dates_years = set(int(y) for y in re.findall(r"(?:as of|in|during)\s+\w+\.?\s+(20\d\d)", text, re.I))
        dates_years |= set(int(y) for y in re.findall(r"\(\s*\w*\s*(20\d\d)\s*\)", text))
        years = [y for y in years_in(text) if y not in dates_years]
        return Query(question, text, toks, content, dict(weights), terms_in(text), years, amounts, sidx, when)

    def _sid(self, q: Query) -> str:
        return self.s.snapshots[q.sidx][0]

    # ================================================================== rate tables
    def _table_ctx(self, t: Table, pv: PageVersion) -> dict:
        key = (t.tid, pv.pid)
        c = self._ctx_cache.get(key)
        if c is None:
            labels = set()
            for r in t.rows:
                for cell in r:
                    if "%" not in cell:
                        labels |= set(tokens(cell))
            c = {
                "cap": set(tokens(t.caption + " " + t.group)),
                "lab": labels,
                "page": set(tokens(pv.title + " " + t.heading + " " + urlparse(t.url).path.replace("-", " "))),
                "hdr": set(tokens(" ".join(t.headers))),
                "concept": (t.caption + " " + t.group + " " + urlparse(t.url).path + " " +
                            " ".join(r[0] for r in t.rows if r)).lower(),
            }
            self._ctx_cache[key] = c
        return c

    def _live_rate_tables(self, sidx: int) -> list[tuple[Table, PageVersion]]:
        out = []
        for t in self.s.tables:
            if not t.is_rate:
                continue
            pv = self.s.live_page(t, sidx)
            if pv is not None and pv.kind not in ("expired", "modal", "listing"):
                out.append((t, pv))
        return out

    def _table_idf(self, sidx: int, live) -> dict:
        if sidx not in self._tidf_cache:
            df: dict = defaultdict(int)
            for t, pv in live:
                c = self._table_ctx(t, pv)
                for w in c["cap"] | c["lab"] | c["page"]:
                    df[w] += 1
            n = max(1, len(live))
            self._tidf_cache[sidx] = {w: math.log(1 + n / d) for w, d in df.items()}
        return self._tidf_cache[sidx]

    def _concept_penalty(self, q: Query, ctx: dict, list_q: bool = False) -> float:
        qset = set(q.toks)
        pen = 1.0
        for trig, rx in CONCEPTS:
            if re.search(rx, ctx["concept"]) and not (qset & {stem(w) for w in trig}):
                pen *= 0.9 if (list_q and "business" in trig) else 0.45
        return pen

    def _match_table(self, q: Query, t: Table, pv: PageVersion, idf: dict) -> float:
        c = self._table_ctx(t, pv)
        num = den = 0.0
        for tok in set(q.content):
            w = idf.get(tok, 2.5)
            den += w
            alts = [tok] + SYN.get(tok, [])
            best = 0.0
            for i, a in enumerate(alts):
                f = 1.0 if i == 0 else 0.7
                if a in c["cap"]:
                    best = max(best, 1.0 * f)
                elif a in c["lab"]:
                    best = max(best, 0.85 * f)
                elif a in c["page"]:
                    best = max(best, 0.55 * f)
                elif a in c["hdr"]:
                    best = max(best, 0.4 * f)
            num += w * best
        if den == 0:
            return 0.0
        qall = set(q.toks) | {x for t in q.toks for x in SYN.get(t, [])}
        cap = c["cap"] - GENERIC
        cap_prec = len(cap & qall) / len(cap) if cap else 0.0
        return (num / den) * (0.85 + 0.15 * cap_prec)

    def _row_cells_text(self, r: list) -> str:
        return " ".join(c for c in r if "%" not in c)

    def _filter_rows(self, q: Query, t: Table) -> tuple[list, float]:
        """Rows that satisfy the question's qualifiers, and a fit factor (1 = all qualifiers met)."""
        rows = list(t.rows)
        fit = 1.0
        if q.terms:
            rt = [r for r in rows if set(terms_in(self._row_cells_text(r))) & set(q.terms)]
            if rt:
                rows = rt
            elif any(terms_in(self._row_cells_text(r)) for r in rows):
                fit *= 0.2
        if q.years:
            rngs = [(r, year_range(self._row_cells_text(r))) for r in rows]
            if any(g for _, g in rngs):
                ry = [r for r, g in rngs if g and any(g[0] <= y <= g[1] for y in q.years)]
                if ry:
                    rows = ry
                else:
                    fit *= 0.3
        if q.amounts:
            rng = [(r, amount_range(" ".join(c for c in r if "%" not in c and "$" in c and
                                             not re.fullmatch(r"\$[\d,.]+", c)) or r[0]))
                   for r in rows]
            if any(g for _, g in rng):
                a = q.amounts[0]
                ra = [(r, g) for r, g in rng if g and g[0] <= a <= g[1]]
                if ra:
                    tight = min(g[1] - g[0] for _, g in ra)
                    rows = [r for r, g in ra if g[1] - g[0] == tight]
                else:
                    fit *= 0.4
        # discriminating label words from the question (jumbo, roth, purchase, standard, intro...)
        qset = set(q.toks)
        for tok in sorted(qset):
            if tok in GENERIC and tok != "standard":
                continue
            has = [r for r in rows if tok in set(tokens(self._row_cells_text(r)))]
            if has and len(has) < len(rows):
                rows = has
        labset = [set(tokens(self._row_cells_text(r))) for r in rows]
        for neg in ("jumbo", "introductory", "intro", "relief"):
            if neg not in qset and any(neg in ls for ls in labset) and not all(neg in ls for ls in labset):
                plain = [r for r, ls in zip(rows, labset) if neg not in ls]
                rows = plain + ([r for r, ls in zip(rows, labset) if neg in ls] if len(rows) <= 4 else [])
                labset = [set(tokens(self._row_cells_text(r))) for r in rows]
        if SUPER_HI.search(q.text) or SUPER_LO.search(q.text):
            def key(r):
                vals = [float(m) for c in r for m in re.findall(r"(\d+\.\d+|\d+)\s*%", c)]
                return vals[-1] if vals else None
            scored = [(key(r), r) for r in rows if key(r) is not None]
            if scored:
                tgt = max(v for v, _ in scored) if SUPER_HI.search(q.text) else min(v for v, _ in scored)
                rows = [r for v, r in scored if v == tgt][:1]
        return rows, fit

    def _render_rows(self, t: Table, rows: list) -> list[str]:
        out = []
        for r in rows:
            label, rates = [], []
            for h, cell in zip(t.headers, r):
                if not cell or cell in ("—", "-"):
                    continue
                hl = h.lower()
                if "%" in cell:
                    if re.search(r"\b(APR|APY)\b", cell):
                        rates.append(cell)
                    else:
                        rates.append(f"{cell} {_short_header(h)}".strip())
                elif re.search(r"points|payment|min\.? to|minimum", hl):
                    continue
                else:
                    label.append(cell)
            if not rates:
                continue
            lab = ", ".join(label) if label else t.caption
            out.append(f"{lab}: {', '.join(rates)}")
        return out

    def _family_key(self, t: Table, ctx: dict) -> tuple:
        return (t.family, tuple(bool(re.search(rx, ctx["concept"])) for _, rx in CONCEPTS))

    def _critical_unmatched(self, q: Query, chosen) -> bool:
        crit = [t for t in set(tokens(q.text, keep_stop=True)) | set(re.findall(r"\b\d+/\d+\b", q.text))
                if t in CRITICAL]
        if not crit:
            return False
        if chosen is None:
            return True
        ctxs = chosen
        for tok in crit:
            if not any(tok in (c["cap"] | c["lab"] | c["page"]) or tok in c["concept"] for c in ctxs):
                return True
        return False

    def _rate_answer(self, q: Query) -> Optional[Result]:
        live = self._live_rate_tables(q.sidx)
        if not live:
            return None
        idf = self._table_idf(q.sidx, live)
        list_q = bool(LIST_WORDS.search(q.text)) or bool(re.search(r"\b(cards|loans|certificates|cds|accounts)\b", q.text, re.I))
        fams: dict = defaultdict(list)
        for t, pv in live:
            if t.expired:
                continue
            c = self._table_ctx(t, pv)
            m = self._match_table(q, t, pv, idf)
            if m <= 0.2:
                continue
            rows, fit = self._filter_rows(q, t)
            score = m * fit * self._concept_penalty(q, c, list_q) * (0.3 + 0.7 * pv.authority)
            fams[self._family_key(t, c)].append((score, t, pv, rows, c))
        if not fams:
            return None
        ranked = []
        for key, members in fams.items():
            best_score = max(m[0] for m in members)
            # representative: newest effective date, then most authoritative page, then best match
            rep = max(members, key=lambda m: (m[1].effective or date(1900, 1, 1), m[2].authority, m[0], -m[1].tid))
            ranked.append((best_score, rep, members))
        # ties (e.g. "savings" matches every savings product) go to the table listed first on the page
        ranked.sort(key=lambda x: (-round(x[0], 3), self._table_order(x[1][1]), x[1][1].tid))
        top = ranked[0][0]
        if top < 0.5:
            return None
        if self._critical_unmatched(q, [ranked[0][1][4]]):
            return Result(REFUSAL, confident=False)
        cut = 0.7 if list_q else 0.85
        chosen = [x for x in ranked if x[0] >= cut * top][: (6 if list_q else 3)]
        parts, sources, notes = [], [], []
        seen_caps = set()
        change_q = bool(CHANGE_WORDS.search(q.text))
        for score, (s_, t, pv, rows, c), members in chosen:
            rendered = self._render_rows(t, rows)
            if not rendered:
                continue
            sig = tuple(rendered)
            if sig in seen_caps:
                continue
            seen_caps.add(sig)
            eff = f" (rates effective {t.effective.strftime('%m/%d/%Y')})" if t.effective else ""
            cap = t.caption if t.caption.lower() not in rendered[0].lower() else ""
            if cap and t.group and t.group.lower() not in cap.lower() and len(t.group) < 40 \
                    and not re.search(r"account|rates$", t.group, re.I):
                cap = f"{cap} ({t.group})"
            if change_q:
                hist = self._history(t, rows, q.sidx)
                if hist:
                    rendered = [rendered[0] + f" — {hist}"] + rendered[1:]
            parts.append((f"{cap}: " if cap else "") + "; ".join(rendered) + eff)
            # cite the representative and other live pages that show the same current values
            same = sorted({m[2].url for m in members if self._render_rows(m[1], m[1].rows) == self._render_rows(t, t.rows)}
                          | {pv.url}, key=lambda u: -self.s.page_for(u, q.sidx).authority if self.s.page_for(u, q.sidx) else 0)
            for u in [pv.url] + [u for u in same if u != pv.url]:
                if u not in [x[0] for x in sources]:
                    sources.append((u, self._sid(q), rendered[0][:200]))
            stale = [m for m in members if self._render_rows(m[1], m[3] or m[1].rows) and
                     set(self._render_rows(m[1], m[1].rows)) != set(self._render_rows(t, t.rows))]
            if stale:
                urls = sorted({m[2].url for m in stale})[:2]
                notes.append(f"Some pages ({', '.join(urls)}) show different figures; used the table with the most "
                             f"recent effective date on {pv.url}.")
        if not parts:
            return None
        text = " ".join(p.rstrip(".") + "." for p in parts)
        # "Do any ... pay 0.35%?" style checks: say yes/no without echoing the questioned value
        asked = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s?%", q.text)]
        if asked and re.match(r"\s*(do|does|is|are|can|will)\b", q.text, re.I):
            shown = {float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s?%", text)}
            text = ("Yes. " if all(a in shown for a in asked) else "No, that rate isn't on the current rate sheet. ") + text
        return Result(text, sources[:5], True, " ".join(notes) or None)

    def _table_order(self, t: Table) -> int:
        """Position of the table among the rate tables of its page (0 = first)."""
        same = [x.tid for x in self.s.tables if x.url == t.url and x.is_rate and x.pids & t.pids]
        return same.index(t.tid) if t.tid in same else 99

    def _history(self, t: Table, rows: list, sidx: int) -> str:
        """Earlier values of the same rows in older snapshots (only for 'did it change' questions)."""
        cur = {r[0]: r for r in rows}
        out = []
        for back in range(sidx - 1, -1, -1):
            for t2 in self.s.tables:
                if t2.family != t.family or t2.url != t.url or not self.s.live_page(t2, back):
                    continue
                for r2 in t2.rows:
                    r = cur.get(r2[0])
                    if r and r2 != r:
                        vals = [c for c in r2 if "%" in c]
                        out.append(f"previously {', '.join(vals)} as of {self.s.snapshots[back][1].isoformat()}")
                        cur.pop(r2[0])
                break
        return "; ".join(out[:2])

    # ================================================================== passages
    def _rescore(self, q: Query, hits: list) -> list[tuple[float, Unit, PageVersion]]:
        snap_date = self.s.snapshots[q.sidx][1]
        out = []
        for s, u in hits:
            pv = self.s.live_page(u, q.sidx)
            if pv is None or pv.kind in ("expired", "modal"):
                continue
            f = 0.45 + 0.55 * pv.authority
            if pv.pub:
                age = max(0.0, (snap_date - pv.pub).days / 365.0)
                f *= 1.0 / (1.0 + 0.12 * age)
            out.append((s * f, u, pv))
        out.sort(key=lambda x: (-x[0], x[1].uid))
        return out

    def _coverage(self, q: Query, text: str, skip: frozenset = frozenset()) -> float:
        have = set(tokens(text))
        num = den = 0.0
        for t in set(q.content) - skip:
            w = self.s.idf(t)
            den += w
            if t in have or any(sy in have for sy in SYN.get(t, [])):
                num += w
        return num / den if den else 0.0

    def _best_sentences(self, q: Query, text: str, k: int = 2, must: Optional[re.Pattern] = None) -> str:
        lines = [ln for ln in text.split("\n") if ln.strip()]
        sents = []
        for ln in lines:
            sents.extend(split_sentences(ln) if len(ln) > 220 else [ln])
        if len(" ".join(sents)) <= 320 and not must:
            return norm_space(" ".join(sents))
        scored = []
        for i, s in enumerate(sents):
            sc = self._coverage(q, s)
            if must is not None and must.search(s):
                sc += 1.0
            scored.append((sc, i, s))
        keep = sorted(sorted(scored, key=lambda x: (-x[0], x[1]))[:k], key=lambda x: x[1])
        return norm_space(" ".join(s for _, _, s in keep))

    def _page_sentences(self, q: Query, u: Unit, pv: PageVersion, k: int = 3) -> str:
        """Best sentences from the chosen page: the chosen passage and a news lead are favoured,
        'This/It ...' follow-on sentences are kept with their antecedent, output keeps page order."""
        definitional = bool(re.match(r"\s*(what|who)('s| is| are| was)\b", q.text, re.I))
        cands = []
        for uid in self.s.page_units.get(pv.pid, [u.uid]):
            v = self.s.units[uid]
            if v.kind != "para":
                continue
            lines = [ln for ln in v.text.split("\n") if ln.strip()]
            sents = []
            for ln in lines:
                sents.extend(split_sentences(ln) if len(ln) > 220 else [ln])
            for j, sent in enumerate(sents):
                if len(sent) < 25 and v.uid != u.uid:
                    continue
                sc = self._coverage(q, sent + " " + v.heading) * 0.8 + self._coverage(q, sent) * 0.2 \
                    + (0.35 if v.uid == u.uid else 0.0)
                if pv.kind == "news" and v.pos <= 1:
                    sc += 0.15
                sc -= 0.004 * v.pos
                cands.append([sc, v.pos, j, sent])
        if not cands:
            return self._best_sentences(q, u.text, k=k)
        if definitional:   # the first full mention usually is the definition
            mx = max(c[0] for c in cands)
            first = min((c for c in cands if c[0] >= mx - 0.36), key=lambda c: (c[1], c[2]))
            first[0] += 0.3
        top = sorted(cands, key=lambda x: (-x[0], x[1], x[2]))[:k]
        top = [c for c in top if c[0] >= top[0][0] - 0.6]
        chosen = {(c[1], c[2]) for c in top}
        for c in cands:   # keep anaphoric follow-ons ("This initiative provides ...")
            if (c[1], c[2] - 1) in chosen and re.match(r"(This|It|These|They|The program|The account|The loan)\b", c[3]):
                top.append(c)
        return norm_space(" ".join(c[3] for c in sorted(top, key=lambda x: (x[1], x[2]))))

    def _entities(self, q: Query) -> list[str]:
        """Capitalised names in the question (e.g. 'Galena', 'Hy-Vee', 'Mullan Avenue')."""
        ents = []
        for m in re.finditer(r"(?<![.?!]\s)(?<!^)\b([A-Z][a-zA-Z$'-]+(?:\s+[A-Z][a-zA-Z'-]+)*)", q.raw):
            w = m.group(1)
            if w.lower() in {"dupaco", "dupaco's", "i", "visa", "apy", "apr", "ira", "roth", "cd", "atm", "hsa"}:
                continue
            ws = [x for x in tokens(w) if x not in {"branch", "avenue", "street", "office", "credit", "union"}]
            if ws:
                ents.append(" ".join(ws))
        return ents

    def _typed_passage(self, q: Query, rx: re.Pattern, kind: str) -> Optional[Result]:
        hits = self._rescore(q, self.s.bm25(q.weights, q.sidx, limit=120))
        ents = self._entities(q)
        want_branch = bool(re.search(r"\bbranch", q.text, re.I))
        best = None
        for rank, (s, u, pv) in enumerate(hits[:120]):
            txt = u.text
            if not rx.search(txt):
                continue
            hay = set(tokens(txt + " " + u.heading + " " + pv.title))
            if ents and not all(set(e.split()) <= hay for e in ents):
                continue
            if self._coverage(q, txt + " " + u.heading + " " + pv.title, skip=TYPE_WORDS) < 0.4:
                continue
            sc = s * (0.5 + pv.authority)
            lines = [ln for ln in txt.split("\n") if rx.search(ln)]
            if ents:
                ent_lines = [ln for ln in lines if any(set(e.split()) & set(tokens(ln)) for e in ents)]
                lines = ent_lines or lines
            if want_branch and lines:
                i = pv.text.find(lines[0])
                if i >= 0 and re.search(r"\bbranch", pv.text[max(0, i - 80):i], re.I):
                    sc *= 2.0
            if best is None or sc > best[0]:
                best = (sc, u, pv, lines)
        if best is None:
            if ents and kind in ("address", "phone"):
                return Result(REFUSAL, confident=False)
            return None
        _, u, pv, lines = best
        if kind == "address" and lines:
            ans = lines[0]
            if ents:
                ans = f"The {' '.join(w.capitalize() for w in ents[0].split())} branch is at {ans}."
        elif kind in ("routing", "phone") and lines:
            ans = norm_space(lines[0])
            m = re.search(r"(Routing Number|Phone)[^0-9]{0,20}" + rx.pattern, ans, re.I)
            if m:
                ans = m.group(0)
            if kind == "routing":
                ans = f"Dupaco's routing number is {rx.search(ans).group(0)}."
        else:
            ans = self._best_sentences(q, u.text, k=2, must=rx)
        return Result(ans, [(u.url, self._sid(q), ans[:200])], True)

    def _hours_answer(self, q: Query) -> Result:
        ents = self._entities(q)
        w = dict(q.weights)
        for t in ("lobby", "drive", "hour", "monday", "saturday"):
            w[t] = max(w.get(t, 0), 0.6)
        hits = self._rescore(q, self.s.bm25(w, q.sidx, limit=150))
        drive = bool(re.search(r"drive", q.text, re.I))
        cands = []
        for s, u, pv in hits:
            if len(TIME_RX.findall(u.text)) < 2 or not re.search(r"monday|mon\b|saturday|friday", u.text, re.I):
                continue
            hay = set(tokens(u.text + " " + u.heading + " " + pv.title))
            if ents and not all(set(e.split()) <= hay for e in ents):
                continue
            cands.append((s, u, pv))
        if not cands:
            return Result(REFUSAL, confident=False)
        if ents:
            s, u, pv = cands[0]
            return Result(self._hours_text(u.text, drive), [(u.url, self._sid(q), u.text[:200])], True)
        # general question: summarise distinct schedules (they differ mostly on Fri/Sat)
        schedules, srcs, keys = [], [], []
        for s, u, pv in cands:
            block = self._hours_text(u.text, drive, section_only=True)
            if not block:
                continue
            sat = re.search(r"saturday[^A-Z]*", block, re.I)
            key = re.sub(r"\W+", "", (sat.group(0) if sat else block).lower())
            if key in keys:
                continue
            keys.append(key)
            schedules.append(block)
            srcs.append((u.url, self._sid(q), block[:200]))
            if len(schedules) >= 2:
                break
        if not schedules:
            return Result(REFUSAL, confident=False)
        locator = self._locator_url(q)
        if locator:
            srcs.insert(0, (locator, self._sid(q), None))
        kind = "Drive-up" if drive else "Lobby"
        text = (f"{kind} hours vary by branch. A typical schedule is: {schedules[0]}."
                + (f" Another branch schedule: {schedules[1]}." if len(schedules) > 1 else "")
                + " Which branch would you like hours for?")
        return Result(text, srcs, True)

    def _locator_url(self, q: Query) -> Optional[str]:
        for url in self.s.views[q.sidx]:
            if re.search(r"dupaco\.com/contact/?$", url):
                return url
        return None

    @staticmethod
    def _hours_text(text: str, drive: bool, section_only: bool = False) -> str:
        t = text.replace("\n", " ")
        t = re.sub(r"^.*?(Branch hours are:|hours are:|Hours:)", "", t, flags=re.I)
        lob = re.search(r"Lobby:?\s*(.*?)(?=Drive[- ]?(up|thru|through)|$)", t, re.I)
        drv = re.search(r"Drive[- ]?(?:up|thru|through):?\s*(.*?)(?=Lobby|ABOUT|$)", t, re.I)
        if section_only:
            m = drv if drive else lob
            return norm_space(m.group(1)).rstrip(" ;,.") if m else ""
        return norm_space(t)[:420]

    def _count_hits(self, q: Query, sent: str) -> int:
        toks = tokens(sent, keep_stop=True)
        nouns = set(q.content)
        best = 0
        for i, t in enumerate(toks):
            if re.fullmatch(r"\d[\d,]*", t):
                best = max(best, sum(1 for n in nouns if n in toks[i + 1:i + 4]))
        return best

    def _count_sentence(self, q: Query, text: str) -> Optional[str]:
        nouns = [t for t in q.content if not re.fullmatch(r"[\d.,]+", t)]
        best = None
        for sent in split_sentences(text.replace("\n", " ")):
            if STREET_RX.search(sent) or PHONE_RX.search(sent) or len(sent.split()) < 6:
                continue
            toks = tokens(sent, keep_stop=True)
            for i, t in enumerate(toks):
                if not re.fullmatch(r"\d[\d,]*", t) or t in ("0",) or re.fullmatch(r"(19|20)\d\d", t):
                    continue
                window = toks[i + 1:i + 4]
                hit = sum(1 for n in nouns if n in window)
                if hit and (best is None or hit > best[0]):
                    best = (hit, sent)
        return best[1] if best else None

    def _passage_answer(self, q: Query, require_pct: bool = False) -> Result:
        hits = self._rescore(q, self.s.bm25(q.weights, q.sidx, limit=100))
        if not hits:
            return Result(REFUSAL, confident=False)
        count_q = bool(re.search(r"\bhow many\b", q.text, re.I))
        if count_q:
            snap_date = self.s.snapshots[q.sidx][1]
            best = None
            for s, u, pv in hits[:150]:
                sent = self._count_sentence(q, u.text)
                if not sent:
                    continue
                # counts drift over time: the most recently published statement wins
                when = pv.pub or (date(1900, 1, 1) if pv.kind in ("news", "blog", "file") else snap_date)
                key = (sent_hits := self._count_hits(q, sent), round(self._coverage(q, sent), 1), when, s)
                if best is None or key > best[0]:
                    best = (key, sent, u)
            if best:
                _, sent, u = best
                sent = re.sub(r"^(It|The credit union)\b", "Dupaco", norm_space(sent))
                return Result(sent, [(u.url, self._sid(q), sent[:200])], True)
        best = None
        for s, u, pv in hits[:40]:
            ctx = u.text + " " + u.heading + " " + pv.title
            cov = self._coverage(q, ctx)
            sc = s * (0.5 + cov)
            if pv.kind in ("news", "blog"):
                sc *= 1.0 / (1.0 + 0.03 * u.pos)
            if require_pct and "%" not in u.text:
                sc *= 0.2
            if best is None or sc > best[0]:
                best = (sc, u, pv, cov)
        sc, u, pv, cov = best
        if cov < 0.34 or (require_pct and "%" not in u.text):
            return Result(REFUSAL, confident=False)
        ents = self._entities(q)
        hay = set(tokens(u.text + " " + u.heading + " " + pv.title))
        if ents and not any(set(e.split()) <= hay for e in ents) and cov < 0.6:
            return Result(REFUSAL, confident=False)
        ans = self._page_sentences(q, u, pv, k=3)
        head = re.sub(r"\s*[|–-]\s*Dupaco.*$", "", u.heading or pv.title).strip()
        if head and len(head) < 60 and head.lower() not in ans.lower() and not head.endswith("?"):
            ans = f"{head}: {ans}"
        if re.search(r"\b(online|apply|sign up|enroll|join)\b", q.text, re.I):
            link = self._best_link(q)
            if link and link not in ans:
                ans += f" You can do this online at {link}"
        return Result(ans, [(u.url, self._sid(q), ans[:200])], True)

    # ================================================================== links
    def _link_scores(self, q: Query) -> dict:
        qset = set(q.content) | {s for t in q.content for s in SYN.get(t, [])}
        rate_q = bool(re.search(r"\brates?\b", q.text, re.I))
        if rate_q:
            qset.add("rate")
        if not qset:
            return {}
        app_q = bool(re.search(r"\b(online|apply|application|open|sign up|enroll)\b", q.text, re.I))
        agg: dict = defaultdict(float)
        seen = set()
        view = self.s.views[q.sidx]
        for url, pid in view.items():
            pv = self.s.pages[pid]
            if pv.kind in ("expired", "modal", "listing"):
                continue
            for text, target in pv.links:
                if target.startswith(("tel:", "mailto:", "#")) or not text:
                    continue
                tgt = _norm_url(target)
                lt = set(tokens(text + " " + (tgt.split("#", 1)[1] if "#" in tgt else "")))
                ov = len(qset & lt) / max(1.0, min(len(qset), 3))
                if ov == 0 or (pv.url, tgt) in seen:
                    continue
                seen.add((pv.url, tgt))
                agg[tgt] += ov * (0.5 + pv.authority)
        out = {}
        for tgt, sc in agg.items():
            base = tgt.split("#")[0]
            host = urlparse(tgt).netloc.lower()
            tpv = None
            for cand in (base, base.rstrip("/"), base.rstrip("/") + "/"):
                if cand in view:
                    tpv = self.s.pages[view[cand]]
                    break
            if tpv is not None:
                kind, auth = tpv.kind, tpv.authority
            else:
                from .store import classify
                kind, auth = classify(tgt)
                if host and not host.startswith("www."):
                    auth = 0.8          # application sub-sites (join., shine., homeloans.)
            if kind in ("listing", "expired", "modal"):
                continue
            sc *= auth
            if app_q and host and not host.startswith("www.") and host.endswith("dupaco.com"):
                sc *= 1.6
            elif app_q and re.search(r"\bonline\b", q.text, re.I):
                sc *= 0.5      # "do it online" -> the online application beats an info page
            if rate_q:
                caps = set()
                if tpv is not None:
                    for t in self.s.tables:
                        if t.is_rate and tpv.pid in t.pids:
                            caps |= set(tokens(t.caption + " " + t.group))
                sc *= 1.5 if caps & qset else 1.0 if caps else 0.4
            out[tgt] = sc
        return out

    def _best_link(self, q: Query) -> Optional[str]:
        agg = self._link_scores(q)
        if not agg:
            return None
        return max(agg.items(), key=lambda x: (x[1], -len(x[0]), x[0]))[0]

    def _url_answer(self, q: Query) -> Optional[Result]:
        agg = self._link_scores(q)
        if not agg:
            return None
        best = max(agg.items(), key=lambda x: (x[1], -len(x[0]), x[0]))[0]
        return Result(f"You can use {best}", [(best.split("#")[0], self._sid(q), None)], True)
