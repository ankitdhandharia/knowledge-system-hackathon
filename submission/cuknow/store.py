"""Snapshot store: keeps every version of every page, which version was live in which snapshot,
passage units for retrieval, parsed rate tables, and an incremental BM25 index."""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from urllib.parse import urlparse

from .textutil import (clean_cell, clean_markdown, norm_space, parse_date, split_sentences, to_date,
                       tokens, _DATE_LONG)

BOILERPLATE_MIN_PAGES = 25
GENERIC_CAPTIONS = {"rates", "loan rates", "home loans", "deposit rates", "savings accounts", "checking accounts",
                    "certificates", "credit cards", "item", "value"}

LANDING = {"switch-and-earn", "earn-more", "money-makeover", "mortgage-refi", "sprint-to-savings-challenge",
           "21-day-sprint-to-savings-challenge", "brighttrack", "checking", "loans", "personal-loans",
           "kccc", "not-for-sale", "thankuse"}


def classify(url: str) -> tuple[str, float]:
    """Page kind and how much we trust it as a statement of *current* fact."""
    p = urlparse(url)
    path, query = p.path.lower().rstrip("/"), p.query.lower()
    last = path.rsplit("/", 1)[-1]
    if "offer-expired" in path:
        return "expired", 0.05
    if "privacy-policy" in path and query:
        return "modal", 0.05
    if "/tag/" in path or "/author/" in path or "category=" in query:
        return "listing", 0.25
    if last in ("rates", "business-rates"):
        return "rates", 1.0
    if "calculator" in path:
        return "calculator", 0.35
    if path.startswith("/newsroom/"):
        return "news", 0.6
    if path in ("/newsroom", "/resources/dupaco-news", "/learn/blog", "/resources"):
        return "listing", 0.3
    if path.startswith(("/learn", "/dupacodaily", "/business/resources", "/resources")) or re.match(r"^/20\d\d/\d\d/", path):
        return "blog", 0.5
    if path.startswith("/hubfs/"):
        return "file", 0.55
    if last.endswith("-lp") or last in LANDING or "offer" in last or "covid" in last:
        return "landing", 0.7
    if path.startswith(("/personal", "/business", "/savings-account", "/online-and-mobile-banking", "/connect",
                        "/protection", "/shine", "/mobile")):
        return "product", 0.95
    if path in ("", "/"):
        return "home", 0.9
    if path.startswith(("/contact", "/about", "/membership", "/disclosures", "/financial-services", "/careers")):
        return "core", 0.9
    return "page", 0.75


@dataclass
class PageVersion:
    pid: int
    url: str
    title: str
    kind: str
    authority: float
    snap_idx: int            # snapshot where this version first appeared
    fetched: Optional[date]
    pub: Optional[date]      # publication date for news / blog posts
    text: str
    links: list
    expired: bool = False


@dataclass
class Unit:
    uid: int
    url: str
    heading: str
    text: str
    kind: str                # 'para' | 'row'
    pos: int = 0             # order of first appearance within its page
    pids: set = field(default_factory=set)


@dataclass
class Table:
    tid: int
    url: str
    group: str               # e.g. "Certificates"
    caption: str             # e.g. "Special Certificates"
    heading: str
    headers: list
    rows: list               # list[list[str]] (cleaned cells)
    effective: Optional[date]
    notes: str
    expired: bool = False
    pids: set = field(default_factory=set)
    family: int = -1

    @property
    def is_rate(self) -> bool:
        return any("%" in c for r in self.rows for c in r)

    def context(self, page_title: str = "") -> str:
        return " ".join([self.group, self.caption, self.heading, page_title, " ".join(self.headers)])


def _url_words(url: str) -> str:
    p = urlparse(url)
    return re.sub(r"[-_/.?=&%]+", " ", p.path + " " + p.query)


class Store:
    def __init__(self):
        self.snapshots: list[tuple[str, date]] = []
        self.pages: list[PageVersion] = []
        self.views: list[dict] = []          # per snapshot: url -> pid (carry-forward of missing pages)
        self.live: list[set] = []            # per snapshot: set of pids
        self.units: list[Unit] = []
        self.tables: list[Table] = []
        self._unit_key: dict = {}
        self._table_key: dict = {}
        self._latest_md: dict = {}           # url -> (md5, pid)
        self.page_units: dict = defaultdict(list)   # pid -> [uid, ...] in page order
        self.boiler: set = set()
        # BM25
        self.postings: dict = defaultdict(list)
        self.doclen: list[int] = []
        self.unit_tokens: list[Counter] = []

    # ------------------------------------------------------------------ ingest
    def ingest(self, snapshot_id: str, taken_on: str, pages) -> None:
        sidx = len(self.snapshots)
        taken = to_date(taken_on) or date.today()
        self.snapshots.append((snapshot_id, taken))
        view = dict(self.views[-1]) if self.views else {}
        cleaned = []
        line_pages: Counter = Counter()
        for pg in pages:
            h = hashlib.md5((pg.markdown or "").encode("utf-8", "ignore")).hexdigest()
            prev = self._latest_md.get(pg.url)
            if prev and prev[0] == h:
                view[pg.url] = prev[1]
                continue
            text, links = clean_markdown(pg.markdown)
            cleaned.append((pg, h, text, links))
            for ln in set(text.split("\n")):
                if ln and not ln.startswith("|"):
                    line_pages[ln] += 1
        # lines repeated on many pages (nav, footers, country pickers) are boilerplate
        self.boiler |= {ln for ln, c in line_pages.items() if c >= BOILERPLATE_MIN_PAGES}
        for pg, h, text, links in cleaned:
            kind, auth = classify(pg.url)
            pid = len(self.pages)
            pv = PageVersion(pid, pg.url, pg.title or "", kind, auth, sidx, to_date(pg.fetched_at) or taken,
                             self._pub_date(pg.url, text), text, links)
            self.pages.append(pv)
            self._latest_md[pg.url] = (h, pid)
            view[pg.url] = pid
            self._index_page(pv, taken)
        self.views.append(view)
        self.live.append(set(view.values()))
        self._assign_families()

    @staticmethod
    def _pub_date(url: str, text: str) -> Optional[date]:
        m = re.search(r"/(20\d\d|19\d\d)/(\d\d)/(\d\d)/", url)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        for ln in text.split("\n")[:15]:
            if len(ln) < 40 and _DATE_LONG.search(ln):
                return parse_date(ln)
        return None

    # ------------------------------------------------------------------ units + tables
    def _index_page(self, pv: PageVersion, taken: date) -> None:
        lines = pv.text.split("\n")
        self._unit_pos = 0
        heading = pv.title
        self._group = ""
        i, n = 0, len(lines)
        blocks: list[tuple[str, str]] = []     # (heading, block text)
        cur: list[str] = []

        def flush():
            if cur:
                blocks.append((heading, "\n".join(cur)))
                cur.clear()

        while i < n:
            ln = lines[i]
            hm = re.match(r"^(#{1,6})\s*(.*)$", ln)
            if hm:
                flush()
                heading = norm_space(hm.group(2)) or heading
                i += 1
                continue
            if ln.startswith("|") and i + 1 < n and re.match(r"^\|\s*:?-{3,}", lines[i + 1]):
                flush()
                j = i + 2
                rows = []
                while j < n and lines[j].startswith("|"):
                    rows.append([clean_cell(c) for c in lines[j].strip().strip("|").split("|")])
                    j += 1
                headers = [clean_cell(c) for c in ln.strip().strip("|").split("|")]
                self._add_table(pv, taken, lines, i, j, heading, headers, rows)
                i = j
                continue
            if not ln.strip():
                flush()
            elif ln not in self.boiler:
                cur.append(ln)
                kv = re.match(r"^([A-Za-z][^:|]{2,80}):\s+(.*\d.*)$", ln)
                if kv and pv.kind not in ("blog", "news", "calculator", "listing") and re.search(r"\d%|\$\d", kv.group(2)) and len(kv.group(2)) < 90:
                    self._add_table(pv, taken, lines, i, i, heading, ["Item", "Value"],
                                    [[clean_cell(kv.group(1)), clean_cell(kv.group(2))]], kv_line=True)
            i += 1
        flush()

        # merge short consecutive blocks under the same heading into ~passage-sized units
        merged: list[tuple[str, str]] = []
        for hd, txt in blocks:
            if merged and merged[-1][0] == hd and (len(merged[-1][1]) < 260 or merged[-1][1].endswith(":")) \
                    and len(merged[-1][1]) + len(txt) < 900:
                merged[-1] = (hd, merged[-1][1] + "\n" + txt)
            else:
                merged.append((hd, txt))
        for hd, txt in merged:
            if len(txt) > 1400:
                buf = ""
                for s in split_sentences(txt.replace("\n", " ")):
                    if len(buf) + len(s) > 700 and buf:
                        self._add_unit(pv, hd, buf.strip(), "para")
                        buf = ""
                    buf += " " + s
                if buf.strip():
                    self._add_unit(pv, hd, buf.strip(), "para")
            elif len(txt) >= 3:
                self._add_unit(pv, hd, txt, "para")

    def _add_table(self, pv, taken, lines, start, end, heading, headers, rows, kv_line=False):
        # caption = short label lines right above the table (e.g. "Certificates" / "Special Certificates")
        caps, k = [], start - 1
        while k >= 0 and len(caps) < 2 and not kv_line:
            ln = lines[k].strip()
            if not ln:
                k -= 1
                continue
            if ln.startswith(("|", "#")) or len(ln) > 70 or ln.endswith((".", ":")) or "effective" in ln.lower() \
                    or ln.startswith("[") or "](" in ln:
                break
            caps.insert(0, ln)
            k -= 1
        if not caps and not kv_line:
            caps = [heading]
        if len(caps) == 2:
            self._group = caps[0]
        group, caption = (caps[0], caps[-1]) if len(caps) == 2 else (self._group, caps[0] if caps else heading)
        eff, notes = None, []
        for ln in lines[end:end + 8] if not kv_line else [lines[start]]:
            if ln.startswith("|") or ln.startswith("#"):
                break
            if ln.strip():
                notes.append(ln.strip())
                if eff is None and re.search(r"effective|as of", ln, re.I):
                    eff = parse_date(ln)
        note_txt = " ".join(notes)[:600]
        expired = bool(re.search(r"\b(offer|promotion|special|promo)\b[^.]{0,40}\b(ended|expired)\b", note_txt + " "
                                 + " ".join(" ".join(r) for r in rows), re.I))
        if not expired:
            m = re.search(r"\b(?:ends?|ended|expires?|expired|through|until|valid through)\s+([A-Za-z]+\.? \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{2,4}|20\d\d-\d\d-\d\d)",
                          note_txt + " " + " ".join(" ".join(r) for r in rows), re.I)
            if m:
                d = parse_date(m.group(1))
                expired = bool(d and d < taken)
        rows = [r for r in rows if any(c for c in r)]
        if not rows:
            return
        key = (pv.url, caption, tuple(headers), tuple(tuple(r) for r in rows), eff)
        tid = self._table_key.get(key)
        if tid is None:
            tid = len(self.tables)
            self._table_key[key] = tid
            self.tables.append(Table(tid, pv.url, group, caption, heading, headers, rows, eff, note_txt, expired))
        self.tables[tid].pids.add(pv.pid)
        if not kv_line:
            for r in rows:
                txt = f"{caption}: " + "; ".join(f"{h}: {c}" if h else c for h, c in zip(headers, r) if c)
                self._add_unit(pv, heading, txt, "row")

    def _add_unit(self, pv: PageVersion, heading: str, text: str, kind: str):
        key = (pv.url, heading, text)
        uid = self._unit_key.get(key)
        if uid is None:
            uid = len(self.units)
            self._unit_key[key] = uid
            u = Unit(uid, pv.url, heading, text, kind, self._unit_pos)
            self.units.append(u)
            toks = Counter(tokens(text))
            for t in tokens(heading):
                toks[t] += 1
            for t in set(tokens(pv.title)) | set(tokens(_url_words(pv.url))):
                toks[t] += 0.5
            self.unit_tokens.append(toks)
            self.doclen.append(int(sum(toks.values())))
            for t, tf in toks.items():
                self.postings[t].append((uid, tf))
        self.units[uid].pids.add(pv.pid)
        self.page_units[pv.pid].append(uid)
        self._unit_pos += 1

    # ------------------------------------------------------------------ table families
    @staticmethod
    def _row_label(t: Table, r: list) -> str:
        """Product + tier/term part of a row (no rates, no min-balance / payment columns)."""
        cells = []
        for h, c in zip(t.headers, r):
            if not c or "%" in c or re.search(r"points|payment|min\.? to|minimum", h, re.I):
                continue
            cells.append(c)
        lab = " ".join(cells).lower().replace("–", "-").replace("—", "-")
        lab = re.sub(r"\b(19|20)\d\d\s*-\s*\d\d(\d\d)?\b", " yr ", lab)
        lab = re.sub(r"(\d),(\d)", r"\1\2", lab)
        lab = re.sub(r"\.00\b", "", lab)
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$+ ]+", " ", lab)).strip()

    def _assign_families(self) -> None:
        """Group tables that describe the same product (same row labels) so stale copies on
        other pages are compared with — and superseded by — the authoritative one."""
        sigs = []
        for t in self.tables:
            labels = {self._row_label(t, r) for r in t.rows} - {""}
            sigs.append((labels, re.sub(r"[^a-z ]", "", t.caption.lower()).strip()))
        parent = list(range(len(self.tables)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        by_label = defaultdict(list)
        for i, (labels, _) in enumerate(sigs):
            for lab in labels:
                by_label[lab].append(i)
        by_caption = defaultdict(list)
        for i, (_, cap) in enumerate(sigs):
            if self.tables[i].is_rate and len(cap) >= 8 and cap not in GENERIC_CAPTIONS:
                by_caption[cap].append(i)
        for members in by_caption.values():
            for j in members[1:]:
                parent[find(j)] = find(members[0])
        for i, (li, ci) in enumerate(sigs):
            if not self.tables[i].is_rate:
                continue
            cand = {j for lab in li for j in by_label[lab] if j > i}
            for j in cand:
                lj, cj = sigs[j]
                if not self.tables[j].is_rate:
                    continue
                jac = len(li & lj) / max(1, len(li | lj))
                if jac >= 0.5 and (len(li & lj) >= 2 or len(li | lj) == 1):
                    parent[find(i)] = find(j)
        for i, t in enumerate(self.tables):
            t.family = find(i)

    # ------------------------------------------------------------------ queries
    def snapshot_for(self, as_of: Optional[date]) -> int:
        if not self.snapshots:
            return -1
        if as_of is None:
            return len(self.snapshots) - 1
        idx = 0
        for i, (_, d) in enumerate(self.snapshots):
            if d <= as_of:
                idx = i
        return idx

    def page_for(self, url: str, sidx: int) -> Optional[PageVersion]:
        pid = self.views[sidx].get(url)
        return self.pages[pid] if pid is not None else None

    def live_page(self, obj, sidx: int) -> Optional[PageVersion]:
        """The live page version (in snapshot sidx) that contains this unit/table, if any."""
        live = self.live[sidx]
        for pid in obj.pids:
            if pid in live:
                return self.pages[pid]
        return None

    def bm25(self, qweights: dict, sidx: int, kinds=("para", "row"), k1: float = 1.2, b: float = 0.75,
             limit: int = 60) -> list[tuple[float, Unit]]:
        N = max(1, len(self.units))
        avgdl = (sum(self.doclen) / N) if self.doclen else 1.0
        scores: dict = defaultdict(float)
        for t, w in qweights.items():
            pl = self.postings.get(t)
            if not pl:
                continue
            idf = math.log(1 + (N - len(pl) + 0.5) / (len(pl) + 0.5))
            for uid, tf in pl:
                dl = self.doclen[uid]
                scores[uid] += w * idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / avgdl))
        live = self.live[sidx]
        out = []
        for uid, s in sorted(scores.items(), key=lambda x: (-x[1], x[0])):
            u = self.units[uid]
            if u.kind not in kinds or not (u.pids & live):
                continue
            out.append((s, u))
            if len(out) >= limit * 4:
                break
        return out

    def idf(self, t: str) -> float:
        N = max(1, len(self.units))
        df = len(self.postings.get(t, ()))
        return math.log(1 + (N - df + 0.5) / (df + 0.5))
