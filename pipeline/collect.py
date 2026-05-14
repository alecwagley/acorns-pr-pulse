#!/usr/bin/env python3
"""
Collect PR / news items for all brands. Writes data/current.json.

Run:
    python3 pipeline/collect.py

Sources:
    - Google News RSS per brand (wide net)
    - SEC EDGAR JSON for the 4 public brands (8-K + earnings releases)

Output schema (list of items, sorted by date desc):
    {
      "brand":   "Chime",
      "slug":    "chime",
      "title":   "Chime Closes $200M Series F at $25B Valuation",
      "summary": "First paragraph or RSS description, trimmed to ~280 chars.",
      "url":     "https://...",  # canonical article URL (Google News redirects unfollowed; the news.google.com URL is what we keep so the dashboard always reaches the article via Google's resolver)
      "source":  "techcrunch.com",  # publisher domain
      "date":    "2026-05-10T14:23:00+00:00",  # ISO 8601, UTC
      "type":    "news",  # "news" (Google News) or "filing" (SEC EDGAR)
    }
"""
from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx
from dateutil import parser as dateparser
from googlenewsdecoder import gnewsdecoder

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "current.json"

sys.path.insert(0, str(ROOT))
from pipeline.sources import (
    ALL_TRACKED, SEC_CIKS, SEC_FORMS, SEC_8K_ITEMS,
    BLOCKED_PUBLISHERS, BLOCKED_TITLE_PATTERNS,
    OFFICIAL_PR_PUBLISHERS,
    SENTIMENT_POSITIVE, SENTIMENT_NEGATIVE,
    google_news_rss_url, sec_filings_url,
)

# Cache of fetched 8-K TLDRs, keyed by filing URL. Persisted to disk so weekly
# refreshes don't re-fetch the same filings. Filings are immutable once filed.
SEC_TLDR_CACHE_PATH = ROOT / "data" / "sec_tldr_cache.json"

# Cache of fetched OG meta descriptions, keyed by article URL. Articles don't
# change after publication, so we cache them indefinitely. Pruned by URL-not-in-
# current-data check at the end of each run to keep the file bounded.
OG_CACHE_PATH = ROOT / "data" / "og_cache.json"

# Cache of decoded Google News redirect URLs → real publisher URLs. Google News
# RSS gives us base64-encoded redirect URLs; the decoder hits a Google endpoint
# to resolve to the publisher's article URL. We cache per-redirect-URL since the
# mapping is stable. This makes subsequent runs fast (no decoding round-trips).
URL_DECODE_CACHE_PATH = ROOT / "data" / "url_decode_cache.json"


# Brand-name patterns used for relevance checking. The brand name (or one of these
# aliases) must appear in the article TITLE for the item to be considered relevant.
# This catches Google News false-matches (e.g. a "Portland man sentenced" article
# tagged to "Cash App" because the article mentions "cash app" in the body).
BRAND_TITLE_TERMS = {
    "Acorns":      ["acorns"],
    "Chime":       ["chime"],
    "Robinhood":   ["robinhood", "hood"],   # "HOOD" = ticker
    "Alinea":      ["alinea"],
    "Greenlight":  ["greenlight"],
    "Cash App":    ["cash app", "cashapp"],
    "Betterment":  ["betterment"],
    "Wealthfront": ["wealthfront"],
    "SoFi":        ["sofi"],
    "Kalshi":      ["kalshi"],
    "Polymarket":  ["polymarket"],
    "Chase":       ["chase", "jpmorgan", "jpm"],
}


def classify_official(item: dict) -> bool:
    """True if the item is from an official PR source (SEC filing or PR wire)."""
    if item.get("type") == "filing":
        return True
    publisher = (item.get("source") or "").lower()
    return any(p in publisher for p in OFFICIAL_PR_PUBLISHERS)

# SEC EDGAR requires a User-Agent identifying the requester per their fair-access policy.
SEC_UA = "VSCRL acorns-pr-pulse (alec@vscrl.co)"

# Window: only keep items posted in the last N days. PR team scans weekly;
# 14 days gives a buffer so items from late last week don't drop on Monday refresh.
WINDOW_DAYS = 14


def _clean_text(text: str) -> str:
    """Decode HTML entities, strip tags, collapse whitespace. Used for titles AND summaries.

    Handles common contamination in RSS / Google News output:
      - HTML entities: &amp; &nbsp; &#39; &quot; &mdash; etc.
      - Embedded tags: <a>, <em>, <br>
      - Doubled-up whitespace from concatenated fragments
      - Control characters (rare but seen in algorithmic-source headlines)
      - Leading/trailing punctuation crud
    """
    if not text:
        return ""
    # 1. Decode HTML entities (twice — some feeds double-encode, e.g. &amp;amp;)
    text = html.unescape(html.unescape(text))
    # 2. Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # 3. Drop control characters (preserve newlines/tabs as spaces)
    text = re.sub(r"[\x00-\x08\x0B-\x1F\x7F]", " ", text)
    # 4. Collapse whitespace (including non-breaking-space U+00A0)
    text = re.sub(r"[\s ]+", " ", text).strip()
    # 5. Trim stray leading/trailing punctuation
    text = text.strip(" \t​·-—|·,")
    return text


def _summary(text: str, limit: int = 280) -> str:
    """Clean + trim to ~limit chars at a word boundary."""
    text = _clean_text(text)
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def _suspicious_chars(text: str) -> str:
    """Return any 'suspicious' chars still in a title after cleaning. Used for audit logging."""
    # Suspicious: HTML entity remnants, raw HTML brackets, control chars
    matches = []
    if re.search(r"&[a-z#0-9]+;", text):
        matches.append("html-entity")
    if "<" in text or ">" in text:
        matches.append("angle-bracket")
    if re.search(r"[\x00-\x1F\x7F]", text):
        matches.append("control-char")
    if " " in text:
        matches.append("nbsp")
    return ",".join(matches)


def _domain(url: str) -> str:
    """Extract the publisher domain from a URL. Strip www. and any port."""
    try:
        host = urlparse(url).hostname or ""
        return host.removeprefix("www.")
    except Exception:
        return ""


def fetch_google_news(brand: str, slug: str) -> list[dict]:
    """Fetch the brand's Google News RSS feed and normalize to our schema."""
    url = google_news_rss_url(brand)
    items: list[dict] = []
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"  ! {brand} Google News fetch failed: {e}", file=sys.stderr)
        return items

    for entry in feed.entries:
        # Google News wraps publisher in the title: "Headline - Publisher Name".
        # We split that off and use it as the source label when present.
        # IMPORTANT: clean BEFORE splitting so HTML entities don't break the rpartition.
        title = _clean_text(entry.get("title", ""))
        publisher_from_title = None
        if " - " in title:
            head_part, _, tail = title.rpartition(" - ")
            if head_part and tail and len(tail) < 60:  # heuristic: publisher names are short
                title = head_part
                publisher_from_title = tail

        link = entry.get("link", "")
        # Try the feedparser "source" element first (Google News sometimes provides it),
        # else fall back to the title-derived publisher, else fall back to the domain.
        source_obj = entry.get("source", {})
        source_label = (
            (source_obj.get("title") if isinstance(source_obj, dict) else None)
            or publisher_from_title
            or _domain(link)
        )

        date_str = entry.get("published") or entry.get("updated") or ""
        try:
            dt = dateparser.parse(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue  # skip items without parseable dates

        # Author extraction — feedparser surfaces author fields inconsistently across
        # Google News output. Try a few possible keys.
        author = entry.get("author") or ""
        if not author:
            authors_list = entry.get("authors") or []
            if authors_list and isinstance(authors_list, list):
                author = authors_list[0].get("name") if isinstance(authors_list[0], dict) else str(authors_list[0])
        author = _clean_text(author) if author else ""

        items.append({
            "brand":   brand,
            "slug":    slug,
            "title":   title,
            "summary": _summary(entry.get("summary", "") or entry.get("description", "")),
            "url":     link,
            "source":  source_label,
            "author":  author,
            "date":    dt.astimezone(timezone.utc).isoformat(),
            "type":    "news",
        })
    return items


def _load_url_decode_cache() -> dict:
    if URL_DECODE_CACHE_PATH.exists():
        try:
            return json.loads(URL_DECODE_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_url_decode_cache(cache: dict) -> None:
    URL_DECODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    URL_DECODE_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def decode_gnews_url(gnews_url: str, cache: dict) -> str:
    """Resolve a Google News redirect URL to the publisher's actual article URL.
    Cached forever (Google News URLs map 1:1 to article URLs and never change).

    Returns the decoded URL on success; the original URL on failure (so the link
    still works via the Google News resolver in the user's browser).
    """
    if gnews_url in cache:
        return cache[gnews_url]
    if "news.google.com/rss/articles" not in gnews_url:
        # Not a Google News URL; return as-is.
        cache[gnews_url] = gnews_url
        return gnews_url
    try:
        result = gnewsdecoder(gnews_url, interval=0)
        if isinstance(result, dict) and result.get("status") and result.get("decoded_url"):
            cache[gnews_url] = result["decoded_url"]
            return result["decoded_url"]
    except Exception:
        pass
    # Decoder failed — fall back to the original URL (still clickable via Google News).
    cache[gnews_url] = gnews_url
    return gnews_url


def _load_og_cache() -> dict:
    if OG_CACHE_PATH.exists():
        try:
            return json.loads(OG_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_og_cache(cache: dict) -> None:
    OG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OG_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _extract_ld_author(node) -> str:
    """Pull a plausible author name out of a parsed JSON-LD node.
    JSON-LD shapes are inconsistent: author can be a string, a dict, or a list
    of either. Returns the first string we can recover, or empty."""
    if not node:
        return ""
    if isinstance(node, list):
        for n in node:
            v = _extract_ld_author(n)
            if v:
                return v
        return ""
    if isinstance(node, dict):
        author = node.get("author")
        if isinstance(author, str):
            return author
        if isinstance(author, dict):
            return author.get("name") or ""
        if isinstance(author, list):
            for a in author:
                if isinstance(a, str) and a:
                    return a
                if isinstance(a, dict) and a.get("name"):
                    return a["name"]
    return ""


_GENERIC_BYLINE_TOKENS = {
    "staff", "newsroom", "editorial", "editor", "admin", "contributor",
    "reuters", "associated press", "ap", "bloomberg news",
    "business wire", "pr newswire", "globe newswire", "accesswire",
    "marketwatch", "investor's business daily", "ibd", "pymnts",
    "yahoo finance", "yahoo", "the motley fool", "motley fool",
}

# Junk characters that suggest the byline came from an unrendered template,
# JSON blob, or HTML fragment rather than a real person's name.
_JUNK_BYLINE_CHARS = set("${}[]<>|\\")

def _publisher_aliases(publisher: str) -> set:
    """Return lowercase variants of a publisher name to compare against."""
    if not publisher:
        return set()
    p = publisher.strip().lower()
    out = {p}
    for suffix in (".com", ".net", ".org", ".co", ".io"):
        if p.endswith(suffix):
            out.add(p[:-len(suffix)])
    if p.startswith("the "):
        out.add(p[4:])
    return out

def _is_real_byline(author: str, publisher: str = "") -> bool:
    """Filter junk bylines. Returns False for empty, generic staff labels,
    URLs, template fragments, publisher echoes, or all-caps section names."""
    if not author:
        return False
    a = author.strip()
    if a.startswith(("http://", "https://", "/")):
        return False
    if len(a) < 3 or len(a) > 80:
        return False
    if any(c in _JUNK_BYLINE_CHARS for c in a):
        return False
    low = a.lower()
    if any(tok == low or low.startswith(tok + " ") or low.endswith(" " + tok) for tok in _GENERIC_BYLINE_TOKENS):
        return False
    pub_aliases = _publisher_aliases(publisher)
    if low in pub_aliases:
        return False
    # Mostly-uppercase strings (>=70% capitals among letter chars) are almost
    # always section headers or publication names (e.g. "COMPLETE iGAMING"),
    # not a person's name.
    letters_only = re.sub(r"[^A-Za-z]", "", a)
    if len(letters_only) > 4:
        upper_ratio = sum(1 for c in letters_only if c.isupper()) / len(letters_only)
        if upper_ratio >= 0.7:
            return False
    return True


def fetch_og_meta(url: str, cache: dict, publisher: str = "") -> dict:
    """Fetch an article and extract both an OG description and a byline.

    Cache shape: {url: {"summary": str, "author": str, "_v": 2}}
    Legacy entries (plain strings from older runs) are migrated to the dict
    shape with author="" so the next collect run refetches them once to
    backfill byline data, then sets _v=2 to settle them.

    Returns the cached dict on hit, or fresh metadata on miss / migration.
    Caller reads ["summary"] and ["author"] off the result.
    """
    entry = cache.get(url)
    # Legacy string entries — re-fetch this run to extract byline, keep
    # existing summary if the new fetch doesn't return one.
    if isinstance(entry, str):
        legacy_summary = entry
        entry = {"summary": legacy_summary, "author": "", "_v": 1}
    needs_fetch = (entry is None) or (entry.get("_v", 0) < 2)
    if not needs_fetch:
        return entry

    legacy_summary = (entry or {}).get("summary", "")

    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; VSCRL-PR-Pulse/1.0)"},
            timeout=10.0,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            cache[url] = {"summary": legacy_summary, "author": "", "_v": 2}
            return cache[url]
        html_text = resp.text
    except Exception:
        cache[url] = {"summary": legacy_summary, "author": "", "_v": 2}
        return cache[url]

    # --- Summary extraction (unchanged logic) ---
    desc = ""
    m = re.search(
        r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
        html_text, re.IGNORECASE,
    )
    if m:
        desc = m.group(1)
    if not desc:
        m = re.search(
            r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']',
            html_text, re.IGNORECASE,
        )
        if m:
            desc = m.group(1)
    if not desc:
        m = re.search(r"<p[^>]*>([^<]{40,})</p>", html_text)
        if m:
            desc = m.group(1)
    desc = _clean_text(desc)
    if len(desc) > 320:
        desc = desc[:317].rsplit(" ", 1)[0] + "…"
    # Don't lose a previously-cached summary if this fetch returned nothing.
    if not desc and legacy_summary:
        desc = legacy_summary

    # --- Byline extraction (new) ---
    author = ""
    # 1. <meta name="author">
    m = re.search(
        r'<meta\s+name=["\']author["\']\s+content=["\']([^"\']+)["\']',
        html_text, re.IGNORECASE,
    )
    if m:
        author = m.group(1)
    # 2. <meta property="article:author">
    if not author:
        m = re.search(
            r'<meta\s+property=["\']article:author["\']\s+content=["\']([^"\']+)["\']',
            html_text, re.IGNORECASE,
        )
        if m:
            author = m.group(1)
    # 3. JSON-LD <script type="application/ld+json">
    if not author:
        for block in re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
            html_text, re.IGNORECASE,
        ):
            try:
                data = json.loads(block.strip())
                cand = _extract_ld_author(data)
                if cand:
                    author = cand
                    break
            except Exception:
                continue
    # JSON-LD can return non-string author values; coerce defensively.
    if not isinstance(author, str):
        author = ""
    author = _clean_text(author)
    if not _is_real_byline(author, publisher):
        author = ""

    cache[url] = {"summary": desc, "author": author, "_v": 2}
    return cache[url]


def fetch_og_summary(url: str, cache: dict) -> str:
    """Back-compat wrapper. Prefer fetch_og_meta for new callers."""
    return fetch_og_meta(url, cache).get("summary", "")


def classify_sentiment(text: str) -> str:
    """Heuristic sentiment classifier using positive/negative keyword lexicons.
    Returns 'positive', 'negative', or 'neutral'. Title-only signals are
    weighted more heavily than summary text.

    Not bulletproof — keyword lists never are for news. Catches the obvious
    funding/launch (positive) vs lawsuit/probe (negative) cases; ambiguous
    falls to neutral. Renders as a colored badge in the Acorns section.
    """
    lower = text.lower()
    pos = sum(1 for w in SENTIMENT_POSITIVE if w in lower)
    neg = sum(1 for w in SENTIMENT_NEGATIVE if w in lower)
    if pos == neg:
        return "neutral"
    return "positive" if pos > neg else "negative"


def _load_tldr_cache() -> dict:
    if SEC_TLDR_CACHE_PATH.exists():
        try:
            return json.loads(SEC_TLDR_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_tldr_cache(cache: dict) -> None:
    SEC_TLDR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEC_TLDR_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def items_string_to_tldr(items_str: str) -> str:
    """Convert a comma-separated SEC items string ('3.03,5.03,8.01') to a
    plain-English TLDR using the SEC_8K_ITEMS mapping.

    The SEC submissions JSON exposes the items field directly per filing, so we
    don't need to fetch + parse the filing HTML. Earlier versions of this code
    did HTML scraping; that's gone now.
    """
    if not items_str:
        return ""
    codes = [c.strip() for c in items_str.split(",") if c.strip()]
    descriptions = [SEC_8K_ITEMS[c] for c in codes if c in SEC_8K_ITEMS]
    if not descriptions:
        return ""
    tldr = ", ".join(descriptions[:3])
    if len(descriptions) > 3:
        tldr += f" (+ {len(descriptions) - 3} more)"
    return tldr


def fetch_sec_filings(brand: str, slug: str, tldr_cache: dict, window_cutoff: datetime) -> list[dict]:
    """Fetch recent SEC filings for a public brand. Returns 8-K / 10-Q / 10-K items.

    For 8-Ks, uses the per-filing `items` field from the submissions JSON to
    build a plain-English TLDR (no HTML scrape needed).
    """
    url = sec_filings_url(brand)
    if not url:
        return []
    items: list[dict] = []
    try:
        resp = httpx.get(url, headers={"User-Agent": SEC_UA}, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  ! {brand} SEC fetch failed: {e}", file=sys.stderr)
        return items

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accession_numbers = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])
    item_codes_list = recent.get("items", [""] * len(forms))
    cik = SEC_CIKS[brand].lstrip("0")

    # Plain-English labels for the major form types (used as a fallback TLDR
    # when the form doesn't lend itself to item-code extraction).
    FORM_LABELS = {
        "8-K":    "Material event",
        "8-K/A":  "Material event (amended)",
        "10-Q":   "Quarterly report",
        "10-K":   "Annual report",
        "S-1":    "IPO registration",
        "S-1/A":  "IPO registration (amended)",
    }

    for i, form in enumerate(forms):
        if form not in SEC_FORMS:
            continue
        try:
            dt = datetime.fromisoformat(dates[i]).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        # Skip filings older than our window so we don't fetch TLDRs for items
        # that wouldn't make it into the dashboard anyway.
        if dt < window_cutoff:
            continue

        acc = accession_numbers[i].replace("-", "")
        doc = primary_docs[i]
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
        primary_desc = descriptions[i] if i < len(descriptions) else ""

        # Build the TLDR. For 8-Ks, use the items field from the submissions
        # JSON (comma-separated item codes like "3.03,5.03,8.01"). For other
        # forms, use the static form label.
        tldr = ""
        if form in ("8-K", "8-K/A"):
            items_str = item_codes_list[i] if i < len(item_codes_list) else ""
            tldr = items_string_to_tldr(items_str)
        if not tldr:
            tldr = FORM_LABELS.get(form, "")
        if not tldr and primary_desc and primary_desc.upper() != f"FORM {form}":
            tldr = primary_desc

        # Render the title as a plain-English TLDR followed by the form-code tag.
        # E.g. "Earnings release / financial results · 8-K Filing" instead of
        # the old "8-K Filing · FORM 8-K".
        if tldr:
            title = f"{tldr} · {form} Filing"
        else:
            title = f"{form} Filing"
        # Cap title length to avoid breaking the card layout.
        if len(title) > 140:
            title = title[:137] + "…"

        items.append({
            "brand":   brand,
            "slug":    slug,
            "title":   _clean_text(title),
            "summary": f"SEC {form} filing by {brand} on {dates[i]}. {tldr}" if tldr else f"SEC {form} filing by {brand} on {dates[i]}.",
            "url":     filing_url,
            "source":  "SEC EDGAR",
            "author":  "",
            "date":    dt.isoformat(),
            "type":    "filing",
        })
    return items


def filter_noise(items: list[dict]) -> list[dict]:
    """Drop items from blocked publishers, matching promo/algorithmic title patterns,
    or where the brand name doesn't appear in the title (Google News false-match)."""
    out: list[dict] = []
    dropped_pub = 0
    dropped_title = 0
    dropped_relevance = 0
    for item in items:
        if item["type"] == "filing":
            out.append(item)  # SEC filings bypass all noise filters
            continue
        publisher_lower = (item.get("source") or "").lower()
        if any(blocked in publisher_lower for blocked in BLOCKED_PUBLISHERS):
            dropped_pub += 1
            continue
        title_lower = item.get("title", "").lower()
        if any(pat in title_lower for pat in BLOCKED_TITLE_PATTERNS):
            dropped_title += 1
            continue
        # Relevance check: brand name (or an alias) must appear in the title.
        # EXCEPTION: skip this check entirely for Acorns (the subject) — Google News
        # already disambiguates with the query's "(fintech OR investing OR app)" filter,
        # and the PR team wants every Acorns mention surfaced, including roundup articles
        # ("Best robo-advisors of 2026") whose titles don't name the brand.
        if item["brand"] != "Acorns":
            terms = BRAND_TITLE_TERMS.get(item["brand"], [item["brand"].lower()])
            if not any(term in title_lower for term in terms):
                dropped_relevance += 1
                continue
        out.append(item)
    print(f"  noise filter: dropped {dropped_pub} publisher, {dropped_title} title pattern, {dropped_relevance} off-topic")
    return out


def dedupe(items: list[dict]) -> list[dict]:
    """Dedupe by URL. If two items share a URL, keep the first (preserves source order)."""
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        key = item["url"]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# Stopwords stripped before computing title similarity. Includes common English
# function words + filler words common in headlines.
_STOPWORDS = {
    "the", "a", "an", "of", "in", "for", "on", "with", "to", "and", "or", "is",
    "by", "at", "as", "be", "are", "was", "were", "this", "that", "from", "but",
    "if", "not", "it", "its", "they", "their", "have", "has", "had", "will",
    "new", "now", "today", "year", "after", "before", "over", "under", "into",
}


def _title_tokens(title: str, brand_aliases: list[str]) -> set[str]:
    """Tokenize a title for similarity comparison. Returns unigrams + adjacent
    bigrams (a-b pairs).

    Bigrams are critical for catching syndicated stories: 'second venture fund'
    is a strong topical signal that 'second' or 'venture' or 'fund' alone aren't.
    A 5-way syndicated story about Robinhood's RVII fund all share the bigrams
    {second-venture, venture-fund} even when their full token sets diverge.
    """
    text = title.lower()
    for alias in brand_aliases:
        text = text.replace(alias, " ")
    raw = re.findall(r"[a-z0-9]+", text)
    significant = [t for t in raw if t not in _STOPWORDS and len(t) > 2]
    tokens: set[str] = set(significant)
    # Add adjacent-pair bigrams (joined with '-' to namespace them away from unigrams).
    for i in range(len(significant) - 1):
        tokens.add(f"{significant[i]}-{significant[i+1]}")
    return tokens


def group_similar(
    items: list[dict],
    title_threshold: float = 0.30,
    combined_threshold: float = 0.25,
    bigram_threshold: int = 2,
    hours: int = 72,
) -> list[dict]:
    """Group items reporting the same story across multiple publishers.

    Two items belong in the same group when:
      - same brand
      - filed within `hours` of each other
      - EITHER title Jaccard >= title_threshold,
        OR title+summary combined Jaccard >= combined_threshold (catches cases
        where two headlines about the same event word things differently but the
        summary lede mentions overlapping facts)

    Each group becomes ONE consolidated item carrying a `sources` list of all the
    original (publisher, url, author) tuples. Title becomes the longest title in
    the group (most informative).
    """
    if not items:
        return []
    # Sort by date desc so the "primary" (first in group) is the most recent.
    items = sorted(items, key=lambda x: x["date"], reverse=True)
    used = [False] * len(items)
    out: list[dict] = []

    for i, item in enumerate(items):
        if used[i]:
            continue
        used[i] = True
        # Filing-type items are unique by URL already; don't group them.
        if item.get("type") == "filing":
            item["sources"] = [{"publisher": item.get("source", ""), "url": item["url"], "author": item.get("author", "")}]
            out.append(item)
            continue

        aliases = BRAND_TITLE_TERMS.get(item["brand"], [item["brand"].lower()])
        anchor_title_tokens = _title_tokens(item["title"], aliases)
        anchor_full_tokens = _title_tokens(
            item["title"] + " " + item.get("summary", ""), aliases
        )
        anchor_date = datetime.fromisoformat(item["date"])
        group = [item]

        for j in range(i + 1, len(items)):
            if used[j]:
                continue
            other = items[j]
            if other["brand"] != item["brand"]:
                continue
            if other.get("type") == "filing":
                continue
            other_date = datetime.fromisoformat(other["date"])
            if abs((anchor_date - other_date).total_seconds()) > hours * 3600:
                continue
            other_title_tokens = _title_tokens(other["title"], aliases)
            other_full_tokens = _title_tokens(
                other["title"] + " " + other.get("summary", ""), aliases
            )
            title_jac = (
                len(anchor_title_tokens & other_title_tokens)
                / max(len(anchor_title_tokens | other_title_tokens), 1)
            )
            full_jac = (
                len(anchor_full_tokens & other_full_tokens)
                / max(len(anchor_full_tokens | other_full_tokens), 1)
            )
            # Count shared bigrams (tokens with a '-'). 2+ shared bigrams is a strong
            # topical signal even when overall Jaccard is low — catches the case where
            # writers vary word choice but agree on the core noun phrase ('second venture fund',
            # 'interactive brokers', 'new mexico tribes').
            shared_bigrams = sum(
                1 for t in (anchor_title_tokens & other_title_tokens) if "-" in t
            )
            shared_bigrams_full = sum(
                1 for t in (anchor_full_tokens & other_full_tokens) if "-" in t
            )
            if (
                title_jac >= title_threshold
                or full_jac >= combined_threshold
                or shared_bigrams >= bigram_threshold
                or shared_bigrams_full >= bigram_threshold + 1
            ):
                used[j] = True
                group.append(other)

        # Build the consolidated item. Use the longest title (most informative).
        primary = max(group, key=lambda x: len(x["title"]))
        sources = [
            {"publisher": g.get("source", ""), "url": g["url"], "author": g.get("author", "")}
            for g in group
        ]
        consolidated = {
            **primary,
            "sources": sources,
            "group_size": len(group),
        }
        out.append(consolidated)

    return out


def filter_window(items: list[dict], days: int) -> list[dict]:
    """Drop items older than `days` from now."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [i for i in items if datetime.fromisoformat(i["date"]) >= cutoff]


def update_reporters_log(items: list[dict]) -> None:
    """Maintain a running log of reporter bylines at data/reporters.json.
    Each unique (author, publisher) pair tracks: first_seen, last_seen, brands
    covered, total_count. Built up over time across weekly refreshes."""
    log_path = ROOT / "data" / "reporters.json"
    existing: dict = {}
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text())
        except Exception:
            existing = {}

    # Drop any pre-existing log rows that no longer pass the current filter
    # (filter rules tighten over time; stale junk shouldn't be grandfathered).
    existing = {
        k: v for k, v in existing.items()
        if _is_real_byline(v.get("author", ""), v.get("publisher", ""))
    }

    today_iso = datetime.now(timezone.utc).date().isoformat()
    for item in items:
        # SEC filings have no byline; skip.
        if item.get("type") == "filing":
            continue
        # Each consolidated item may carry multiple sources after group_similar.
        sources = item.get("sources") or [{"publisher": item.get("source", ""), "author": item.get("author", "")}]
        for s in sources:
            author = (s.get("author") or "").strip()
            publisher = (s.get("publisher") or "").strip()
            if not author or not publisher:
                continue
            # Final guard: ditch junk bylines, publisher echoes, wire services,
            # template fragments, etc. that may have slipped through cached
            # entries written by earlier filter versions.
            if not _is_real_byline(author, publisher):
                continue
            key = f"{author} :: {publisher}"
            row = existing.get(key, {
                "author": author,
                "publisher": publisher,
                "first_seen": today_iso,
                "brands": [],
                "urls": [],
                "count": 0,
            })
            row["last_seen"] = today_iso
            # Dedupe by URL so daily refreshes don't double-count the same
            # article when it stays inside the 14-day window across multiple
            # runs. urls is the authoritative count; the legacy `count` key
            # is kept in sync for any older consumers.
            src_url = (s.get("url") or "").strip()
            urls = set(row.get("urls", []))
            if src_url and src_url not in urls:
                urls.add(src_url)
            row["urls"] = sorted(urls)
            row["count"] = len(row["urls"])
            if item["brand"] not in row["brands"]:
                row["brands"] = sorted(set(row["brands"] + [item["brand"]]))
            existing[key] = row

    log_path.write_text(json.dumps(existing, indent=2, sort_keys=True))
    print(f"\nReporter log: {len(existing)} unique (author, publisher) pairs at {log_path}")


def main() -> None:
    all_items: list[dict] = []
    window_cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    tldr_cache = _load_tldr_cache()
    print(f"Collecting PR items (last {WINDOW_DAYS} days) across {len(ALL_TRACKED)} entities (1 subject + {len(ALL_TRACKED)-1} competitors)...")
    print(f"  SEC TLDR cache: {len(tldr_cache)} entries")
    for brand, slug in ALL_TRACKED:
        print(f"  · {brand}", end="", flush=True)
        news = fetch_google_news(brand, slug)
        sec = fetch_sec_filings(brand, slug, tldr_cache, window_cutoff)
        n_news, n_sec = len(news), len(sec)
        all_items.extend(news)
        all_items.extend(sec)
        print(f"  → news={n_news}, sec={n_sec}")
    _save_tldr_cache(tldr_cache)
    print(f"  SEC TLDR cache: {len(tldr_cache)} entries (saved)")

    print(f"\nRaw total: {len(all_items)}")
    all_items = filter_noise(all_items)
    print(f"After noise filter: {len(all_items)}")
    all_items = dedupe(all_items)
    print(f"After URL dedupe: {len(all_items)}")
    all_items = filter_window(all_items, WINDOW_DAYS)
    print(f"After {WINDOW_DAYS}-day window: {len(all_items)}")

    # Group near-duplicate stories across publishers. Each consolidated item
    # carries a `sources` list with all the (publisher, url, author) tuples.
    before_grouping = len(all_items)
    all_items = group_similar(all_items)
    print(f"After similar-story grouping: {len(all_items)} (collapsed {before_grouping - len(all_items)} duplicates)")

    # Mark each item as Official PR or Buzz (skip for filings — they're always Official).
    for item in all_items:
        item["is_official"] = classify_official(item)

    # Decode Google News redirect URLs to actual publisher article URLs. This
    # also fixes the dashboard's source links to go straight to the publisher
    # instead of via the Google News resolver (one fewer click for the PR team).
    url_decode_cache = _load_url_decode_cache()
    print(f"\nDecoding Google News URLs → publisher URLs...")
    print(f"  URL decode cache: {len(url_decode_cache)} entries")
    decoded_count = 0
    for item in all_items:
        if item.get("type") == "filing":
            continue
        orig_url = item.get("url", "")
        if not orig_url:
            continue
        real_url = decode_gnews_url(orig_url, url_decode_cache)
        if real_url != orig_url:
            item["url"] = real_url
            decoded_count += 1
        # Also decode URLs inside grouped sources.
        for s in item.get("sources", []):
            s_url = s.get("url", "")
            if s_url:
                s["url"] = decode_gnews_url(s_url, url_decode_cache)
    _save_url_decode_cache(url_decode_cache)
    print(f"  URL decode cache: {len(url_decode_cache)} entries (saved, {decoded_count} resolved this run)")

    # Enrich summaries from the article's Open Graph / meta description tag.
    # Now that URLs point at publisher pages (not the Google News resolver),
    # OG meta tags actually surface real summaries. Cached per URL; subsequent
    # runs are nearly free.
    og_cache = _load_og_cache()
    print(f"\nFetching article summaries (OG meta descriptions)...")
    print(f"  OG cache: {len(og_cache)} entries")
    fetched_count = 0
    for item in all_items:
        if item.get("type") == "filing":
            continue
        url = item.get("url", "")
        if not url:
            continue
        cached_entry = og_cache.get(url)
        # "Was this entry already a v2 dict before this call?" — if so we won't
        # be hitting the network. Anything else (missing or legacy string)
        # means we'll fetch.
        already_settled = isinstance(cached_entry, dict) and cached_entry.get("_v", 0) >= 2
        og_meta = fetch_og_meta(url, og_cache, publisher=item.get("source", ""))
        og_desc = og_meta.get("summary", "")
        og_author = og_meta.get("author", "")
        # Re-validate against the current filter — cache entries written by
        # older runs may have stored values that the latest filter rejects.
        if og_author and not _is_real_byline(og_author, item.get("source", "")):
            og_author = ""
        if og_desc and og_desc.lower() != item.get("title", "").lower():
            # Only replace summary if the OG description differs from the title
            # (some sites set OG description to the title verbatim — useless).
            item["summary"] = og_desc
        if og_author and not item.get("author"):
            item["author"] = og_author
            # Mirror onto the matching source entry so the reporter log picks it up.
            for src in item.get("sources", []):
                if src.get("url") == url and not src.get("author"):
                    src["author"] = og_author
        if not already_settled:
            fetched_count += 1
    _save_og_cache(og_cache)
    print(f"  OG cache: {len(og_cache)} entries (saved, {fetched_count} new fetches this run)")

    # Sentiment classification (every item, every brand — not just Acorns).
    # Uses Claude Haiku 4.5 when ANTHROPIC_API_KEY is set; falls back to a
    # keyword heuristic otherwise. Cached per URL so daily refreshes only
    # classify new articles. Module: pipeline/llm_sentiment.py.
    from pipeline.llm_sentiment import classify_all as _classify_all
    print(f"\nClassifying sentiment...")
    stats = _classify_all(all_items)
    print(f"  cached: {stats['cached']}, LLM-new: {stats['llm_new']}, heuristic-fallback: {stats['heuristic']}")

    all_items.sort(key=lambda x: x["date"], reverse=True)

    # Title audit: scan for any titles that still carry suspicious characters
    # AFTER cleaning. If anything shows up, it means _clean_text needs more cases;
    # surface so we can patch instead of shipping garbled headlines to the PR team.
    audit_failures = []
    for item in all_items:
        flags = _suspicious_chars(item["title"])
        if flags:
            audit_failures.append((flags, item["brand"], item["title"]))
    print(f"\nTitle audit: {len(all_items) - len(audit_failures)}/{len(all_items)} clean")
    if audit_failures:
        print(f"  ! {len(audit_failures)} titles failed audit:")
        for flags, brand, title in audit_failures[:10]:
            print(f"    [{flags}] [{brand}] {title[:90]}")
        if len(audit_failures) > 10:
            print(f"    ... and {len(audit_failures) - 10} more")

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(all_items, indent=2))
    print(f"\nWrote {DATA_PATH}")

    # Persist the running reporter log so the dashboard can show top bylines over time.
    update_reporters_log(all_items)


if __name__ == "__main__":
    main()
