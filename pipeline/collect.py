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
    BRANDS, SEC_CIKS, SEC_FORMS,
    BLOCKED_PUBLISHERS, BLOCKED_TITLE_PATTERNS,
    google_news_rss_url, sec_filings_url,
)


# Brand-name patterns used for relevance checking. The brand name (or one of these
# aliases) must appear in the article TITLE for the item to be considered relevant.
# This catches Google News false-matches (e.g. a "Portland man sentenced" article
# tagged to "Cash App" because the article mentions "cash app" in the body).
BRAND_TITLE_TERMS = {
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

        items.append({
            "brand":   brand,
            "slug":    slug,
            "title":   title,
            "summary": _summary(entry.get("summary", "") or entry.get("description", "")),
            "url":     link,
            "source":  source_label,
            "date":    dt.astimezone(timezone.utc).isoformat(),
            "type":    "news",
        })
    return items


def fetch_sec_filings(brand: str, slug: str) -> list[dict]:
    """Fetch recent SEC filings for a public brand. Returns 8-K / 10-Q / 10-K items."""
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

    for i, form in enumerate(forms):
        if form not in SEC_FORMS:
            continue
        try:
            dt = datetime.fromisoformat(dates[i]).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        acc = accession_numbers[i].replace("-", "")
        doc = primary_docs[i]
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
        desc = descriptions[i] if i < len(descriptions) else ""

        items.append({
            "brand":   brand,
            "slug":    slug,
            "title":   _clean_text(f"{form} Filing" + (f" · {desc}" if desc else "")),
            "summary": f"SEC {form} filing by {brand} on {dates[i]}.",
            "url":     filing_url,
            "source":  "SEC EDGAR",
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


def filter_window(items: list[dict], days: int) -> list[dict]:
    """Drop items older than `days` from now."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [i for i in items if datetime.fromisoformat(i["date"]) >= cutoff]


def main() -> None:
    all_items: list[dict] = []
    print(f"Collecting PR items (last {WINDOW_DAYS} days) across {len(BRANDS)} brands...")
    for brand, slug in BRANDS:
        print(f"  · {brand}", end="", flush=True)
        news = fetch_google_news(brand, slug)
        sec = fetch_sec_filings(brand, slug)
        n_news, n_sec = len(news), len(sec)
        all_items.extend(news)
        all_items.extend(sec)
        print(f"  → news={n_news}, sec={n_sec}")

    print(f"\nRaw total: {len(all_items)}")
    all_items = filter_noise(all_items)
    print(f"After noise filter: {len(all_items)}")
    all_items = dedupe(all_items)
    print(f"After dedupe: {len(all_items)}")
    all_items = filter_window(all_items, WINDOW_DAYS)
    print(f"After {WINDOW_DAYS}-day window: {len(all_items)}")

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


if __name__ == "__main__":
    main()
