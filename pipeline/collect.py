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

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "current.json"

sys.path.insert(0, str(ROOT))
from pipeline.sources import (
    ALL_TRACKED, SEC_CIKS, SEC_FORMS, SEC_8K_ITEMS,
    BLOCKED_PUBLISHERS, BLOCKED_TITLE_PATTERNS,
    OFFICIAL_PR_PUBLISHERS,
    google_news_rss_url, sec_filings_url,
)

# Cache of fetched 8-K TLDRs, keyed by filing URL. Persisted to disk so weekly
# refreshes don't re-fetch the same filings. Filings are immutable once filed.
SEC_TLDR_CACHE_PATH = ROOT / "data" / "sec_tldr_cache.json"


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


def fetch_8k_tldr(filing_url: str, cache: dict) -> str:
    """Fetch an 8-K's cover page and extract a plain-English TLDR from its Item codes.

    Returns a short comma-joined description like "Earnings release / financial results,
    Other material event" — or empty string if extraction fails. Cached by URL since
    SEC filings are immutable.
    """
    if filing_url in cache:
        return cache[filing_url]

    try:
        resp = httpx.get(filing_url, headers={"User-Agent": SEC_UA}, timeout=20.0, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text
    except Exception as e:
        print(f"  ! TLDR fetch failed for {filing_url}: {e}", file=sys.stderr)
        cache[filing_url] = ""
        return ""

    # 8-K cover pages contain headings like "Item 5.02 Departure of Directors..."
    # We extract every "Item N.NN" code that appears and map to plain English.
    # Most 8-Ks list 2-4 items; some only have one.
    item_codes = re.findall(r"Item\s+(\d+\.\d+)", text)
    # Dedupe preserving order
    seen = set()
    unique_codes = []
    for code in item_codes:
        if code not in seen:
            seen.add(code)
            unique_codes.append(code)

    descriptions = [SEC_8K_ITEMS[code] for code in unique_codes if code in SEC_8K_ITEMS]
    tldr = ", ".join(descriptions[:3])  # cap at 3 items so the TLDR stays scannable
    if len(descriptions) > 3:
        tldr += f" (+ {len(descriptions) - 3} more)"

    cache[filing_url] = tldr
    return tldr


def fetch_sec_filings(brand: str, slug: str, tldr_cache: dict, window_cutoff: datetime) -> list[dict]:
    """Fetch recent SEC filings for a public brand. Returns 8-K / 10-Q / 10-K items.

    For 8-Ks (the high-signal material-events filings), fetches the cover page and
    extracts a plain-English TLDR from the Item codes. Cached per-URL since filings
    are immutable.
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

        # Build the TLDR. For 8-Ks, parse item codes. For other forms, use the
        # static label. Fall back to the primaryDocDescription field if available.
        tldr = ""
        if form in ("8-K", "8-K/A"):
            tldr = fetch_8k_tldr(filing_url, tldr_cache)
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
    """Tokenize a title for similarity comparison. Lowercase, drop stopwords, drop
    brand-name tokens (so 'Kalshi sues NM tribes' vs 'New Mexico tribes sue Kalshi'
    don't both reduce to 'kalshi + tribes')."""
    text = title.lower()
    for alias in brand_aliases:
        text = text.replace(alias, " ")
    tokens = re.findall(r"[a-z0-9]+", text)
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 2}


def group_similar(
    items: list[dict],
    title_threshold: float = 0.45,
    combined_threshold: float = 0.35,
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
            if title_jac >= title_threshold or full_jac >= combined_threshold:
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
            key = f"{author} :: {publisher}"
            row = existing.get(key, {
                "author": author,
                "publisher": publisher,
                "first_seen": today_iso,
                "brands": [],
                "count": 0,
            })
            row["last_seen"] = today_iso
            row["count"] = row.get("count", 0) + 1
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
