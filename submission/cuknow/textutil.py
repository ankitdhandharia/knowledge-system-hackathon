"""Text helpers: markdown cleanup, tokenising, dates, and money / term / tier parsing."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9

NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "twenty-four": 24, "twenty four": 24,
    "thirty": 30, "thirty-six": 36, "thirty six": 36, "forty": 40, "forty-eight": 48, "forty eight": 48,
    "sixty": 60, "seventy-two": 72, "eighty-four": 84,
}

STOP = set("""
a an the and or of to in on for with by at from as is are was were be been being it its this that these those
i me my we our us you your yours he she they them their what which who whom whose when where why how do does
did can could would should will shall may might must have has had not no yes if then than so such any some all
about into over under up down out off again further there here just also very too more most own same other
please tell know like want get give need im i'm whats what's dupaco dupacos dupaco's s t
""".split())

# Words that say "this is about a rate" (not product words).
RATE_WORDS = {"rate", "rates", "apy", "apr", "dividend", "dividends", "yield", "interest", "earn", "earning",
              "percentage", "percent", "pay", "pays", "paying", "charge", "charges"}

_TOKEN = re.compile(r"[a-z0-9]+(?:[.'/][a-z0-9]+)*")


def stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def words(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower().replace("’", "'"))


def tokens(text: str, keep_stop: bool = False) -> list[str]:
    out = []
    for w in words(text):
        w = w.strip("'.")
        if w.endswith("'s"):
            w = w[:-2]
        if not w or (not keep_stop and w in STOP):
            continue
        out.append(stem(w))
    return out


# --------------------------------------------------------------------------- markdown cleanup
_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\[\]]*)\]\(\s*([^)\s]*)[^)]*\)")
_ESC = re.compile(r"\\([*_#$&\[\]().!+\-|>~`])")
_HEADING = re.compile(r"^(#{1,6})\s*(.*?)\s*#*\s*$")
_JUNK_LINE = re.compile(
    r"^(✕|\\+|\*\s*\*\s*\*|-{3,}|\|[\s|]*\||sort by:.*|load more|#+\s*filter|previous|next|cancel|"
    r"\d+ min read|loading locations\.\.\.|go|\[continue\].*|continue cancel)$", re.I)


def clean_markdown(md: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (clean text, links). Strips images, data URIs, link targets, chart dumps, the
    'Handpicked for You' related-article rail and modal boilerplate. Keeps tables and headings."""
    md = (md or "").replace("\\r\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    md = re.sub(r"\(data:[^)]*\)", "()", md)
    md = _IMG.sub("", md)
    links: list[tuple[str, str]] = []
    # call-to-action rows made only of short links ("Open my account  See details") are noise
    md = re.sub(r"(?m)^[ \t]*(?:\[[^\[\]\n]{1,60}\]\([^)\s]*[^)]*\)[ \t]*){1,4}$",
                lambda m: m.group(0) if len(re.findall(r"\w+", re.sub(r"\]\([^)]*\)", "]", m.group(0)))) > 10
                else "\x00" + m.group(0), md)

    def _link(m):
        text, url = m.group(1), m.group(2)
        text_clean = re.sub(r"[*\\\n]+", " ", text).strip()
        if url and not url.startswith("#"):
            links.append((text_clean, url))
        return text
    md = _LINK.sub(_link, md)
    md = _LINK.sub(_link, md)   # nested [[..](..)](..) leftovers
    md = re.sub(r"\\\\\s*\n", "\n", md)
    md = _ESC.sub(r"\1", md)
    md = md.replace("**", "").replace("__", "")

    out, skip_level = [], 0
    for raw in md.split("\n"):
        line = raw.strip()
        h = _HEADING.match(line)
        if h:
            level = len(h.group(1))
            if skip_level and level <= min(skip_level, 2):
                skip_level = 0
            if re.match(r"handpicked for you|related (articles|posts)", h.group(2), re.I):
                skip_level = level
                continue
        if skip_level:
            continue
        if not line:
            out.append("")
            continue
        if line.startswith("\x00"):
            continue
        if "data points" in line or line.startswith("Bar chart showing") or "chimney" in line.lower():
            continue
        if _JUNK_LINE.match(line) or line.startswith("- 🇦") or re.match(r"^- \S+ .*\+\d+$", line):
            continue
        if "this link leads to a different website" in line.lower() or line.startswith("We only do this when it"):
            continue
        out.append(line.rstrip("\\").rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return text, links


# --------------------------------------------------------------------------- dates
_DATE_LONG = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I)
_DATE_NUM = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_DATE_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")


def parse_date(text: str) -> Optional[date]:
    """First date in the text (Month D, YYYY / MM/DD/YY(YY) / YYYY-MM-DD)."""
    best = None
    for rx, kind in ((_DATE_LONG, "long"), (_DATE_NUM, "num"), (_DATE_ISO, "iso")):
        m = rx.search(text or "")
        if not m:
            continue
        try:
            if kind == "long":
                d = date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
            elif kind == "num":
                y = int(m.group(3))
                y = y + 2000 if y < 100 else y
                d = date(y, int(m.group(1)), int(m.group(2)))
            else:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if best is None or m.start() < best[0]:
            best = (m.start(), d)
    return best[1] if best else None


def iso(d: Optional[date]) -> str:
    return d.isoformat() if d else ""


def to_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    m = _DATE_ISO.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return parse_date(s)


_MONTH_YEAR = re.compile(r"\b(?:as of|in|during|on|back in|for)\s+(?:early |late |mid-?)?(" +
                         "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?,?\s+(20\d\d)\b", re.I)


def question_as_of(q: str) -> Optional[date]:
    """'as of May 2026' / 'in June 2026' inside the question -> end of that month."""
    m = _MONTH_YEAR.search(q)
    if not m:
        return None
    y, mo = int(m.group(2)), MONTHS[m.group(1).lower()]
    nxt = date(y + (mo == 12), mo % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


# --------------------------------------------------------------------------- numbers, terms, tiers
_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(k|m|million|thousand)?\b", re.I)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s?%")


def money_values(text: str) -> list[float]:
    out = []
    for m in _MONEY.finditer(text or ""):
        v = float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))
        suf = (m.group(3) or "").lower()
        v *= 1000 if suf in ("k", "thousand") else 1_000_000 if suf in ("m", "million") else 1
        out.append(v)
    return out


def percents(text: str) -> list[float]:
    return [float(m.group(1)) for m in _PCT.finditer(text or "")]


def replace_number_words(text: str) -> str:
    def rep(m):
        return str(NUM_WORDS[m.group(0).lower()])
    pat = r"\b(" + "|".join(sorted((re.escape(k) for k in NUM_WORDS), key=len, reverse=True)) + r")\b"
    return re.sub(pat, rep, text, flags=re.I)


_TERM = re.compile(r"\b(\d{1,3})[\s-]*(months?|mos?\.?|mo\b|years?|yrs?|yr\b)(?![a-z])", re.I)


def terms_in(text: str) -> list[int]:
    """Loan / certificate terms in months: '60-month' -> 60, '30-year' -> 360, '(60 mo)' -> 60."""
    out = []
    for m in _TERM.finditer(replace_number_words(text or "")):
        n, unit = int(m.group(1)), m.group(2).lower()
        out.append(n * 12 if unit.startswith("y") else n)
    return out


def years_in(text: str) -> list[int]:
    return [int(y) for y in re.findall(r"\b((?:19|20)\d\d)\b", text or "")]


def year_range(label: str) -> Optional[tuple[int, int]]:
    """'2024-27 Model Year' -> (2024, 2027); 'Model Year 2026-23' -> (2023, 2026);
    '2016 Model Year (and older)' -> (0, 2016)."""
    t = label.replace("–", "-").replace("—", "-")
    m = re.search(r"\b((?:19|20)\d\d)\s*-\s*((?:19|20)?\d\d)\b", t)
    if m:
        a = int(m.group(1))
        b = m.group(2)
        b = int(b) if len(b) == 4 else (a // 100) * 100 + int(b)
        return (min(a, b), max(a, b))
    m = re.search(r"\b((?:19|20)\d\d)\b.*\b(older|earlier|prior)\b", t, re.I)
    if m:
        return (0, int(m.group(1)))
    m = re.search(r"\b((?:19|20)\d\d)\b.*\b(newer|later)\b", t, re.I)
    if m:
        return (int(m.group(1)), 9999)
    m = re.search(r"model year\s*((?:19|20)\d\d)\b|\b((?:19|20)\d\d)\s*model year", t, re.I)
    if m:
        y = int(m.group(1) or m.group(2))
        return (y, y)
    return None


def amount_range(label: str) -> Optional[tuple[float, float]]:
    """Balance / loan-amount tier: '$10,000 – $24,999.99', '$25,000+', '$832,750 or less',
    '$10,000-24,999', 'up to $50,000'."""
    t = label.replace("–", "-").replace("—", "-")
    t = re.sub(r"\$(\d{4,})(?!\d)", lambda m: "$" + f"{int(m.group(1)):,}", t)
    vals = money_values(t)
    m2 = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*-\s*\$?\s?([\d,]+(?:\.\d+)?)", t)
    if m2:
        return (float(m2.group(1).replace(",", "")), float(m2.group(2).replace(",", "")))
    if not vals:
        return None
    v = vals[0]
    if re.search(r"\+|or more|and (up|over|above)|minimum|at least", t, re.I):
        return (v, float("inf"))
    if re.search(r"or less|and under|up to|less than|maximum|or below", t, re.I):
        return (0.0, v)
    return None


def clean_cell(c: str) -> str:
    """'4.10%2' -> '4.10%', '5.39% APR1' -> '5.39% APR', '12.50% – 19.50%2' -> '12.50% – 19.50%'."""
    c = c.strip().replace("\\*", "*")
    c = re.sub(r"(\d%)\d\b", r"\1", c)
    c = re.sub(r"\b(APR|APY)\d\b", r"\1", c)
    c = re.sub(r"(%)\s*\*+", r"\1", c)
    c = re.sub(r"\s+", " ", c)
    return c.strip(" *")


def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9“\"(])", text)
    return [p.strip() for p in parts if p.strip()]
