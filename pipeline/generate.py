#!/usr/bin/env python3
"""
Generate dashboard HTML from data/current.json.

Run:
    python3 pipeline/generate.py

Produces:
    dashboard/index.html         — homepage: This Week feed + 11 brand tiles
    dashboard/<slug>.html        — one page per brand with that brand's items
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "current.json"
DASHBOARD = ROOT / "dashboard"

sys.path.insert(0, str(ROOT))
from pipeline.sources import BRANDS

# Number of items shown in the homepage "This Week" feed.
TOP_FEED_LIMIT = 30

# Acorns brand color (extracted from the official logo SVG — matches the
# existing acorns.vscrl.co social tracker).
ACCENT_HEX = "#74C947"


# ============================================================================
# HTML TEMPLATES
# ============================================================================

PAGE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  body { background: #0F0F11; color: #F5F5F4; font-family: 'Inter', sans-serif; }
  .brand-accent { color: $accent; }
  .bg-brand-accent { background-color: $accent; }
  .border-brand-accent { border-color: $accent; }
  .item-card { transition: background-color 0.15s ease, border-color 0.15s ease; }
  .item-card:hover { background-color: rgba(255,255,255,0.03); border-color: rgba(116, 201, 71, 0.3); }
  .brand-tile { transition: transform 0.15s ease, border-color 0.15s ease; }
  .brand-tile:hover { transform: translateY(-2px); border-color: $accent; }
  /* Why-pill attention animation (first-visit wiggle) */
  @keyframes why-wiggle {
    0%, 100% { transform: translateX(0) scale(1); }
    10% { transform: translateX(-3px) scale(1.06); }
    20% { transform: translateX(3px) scale(1.06); }
    30% { transform: translateX(-3px) scale(1.06); }
    40% { transform: translateX(3px) scale(1.06); }
    50% { transform: translateX(-2px) scale(1.04); }
    60% { transform: translateX(2px) scale(1.04); }
    70%, 90% { transform: translateX(0) scale(1.02); }
  }
  @keyframes why-pulse-ring {
    0% { box-shadow: 0 0 0 0 rgba(116, 201, 71, 0.5); }
    70% { box-shadow: 0 0 0 12px rgba(116, 201, 71, 0); }
    100% { box-shadow: 0 0 0 0 rgba(116, 201, 71, 0); }
  }
  .why-attention {
    animation: why-wiggle 1.1s ease-in-out 0.6s 3, why-pulse-ring 1.6s ease-out 0.6s 4;
  }
</style>
</head>
<body class="min-h-screen">
"""

HEADER_HOMEPAGE = """
<header class="border-b border-white/10">
  <div class="max-w-7xl mx-auto px-6 py-6 flex items-center justify-between">
    <div class="flex items-center gap-5">
      <img src="assets/vscrl-wordmark.png" alt="VSCRL" class="h-14 w-auto" />
      <div class="text-white/40 font-light text-2xl">×</div>
      <div class="flex flex-col gap-1">
        <img src="assets/acorns-logo.svg" alt="Acorns" class="h-10 w-auto" />
        <div class="text-xs text-white/50">PR Pulse · Weekly</div>
      </div>
      <button id="why-pill" onclick="toggleWhy()" class="ml-2 text-xs text-[#74C947]/80 hover:text-[#74C947] border border-[#74C947]/30 hover:border-[#74C947]/60 px-3 py-1.5 rounded-full transition whitespace-nowrap">
        Start here →
      </button>
    </div>
    <div class="text-right text-xs text-white/40">
      <div>Refreshed weekly · Mon 7am ET</div>
      <div class="text-white/60 mt-1">Last refresh: $refresh_date</div>
    </div>
  </div>
</header>

<div id="why-banner" class="hidden border-b border-[#74C947]/20 bg-[#74C947]/[0.05]">
  <div class="max-w-7xl mx-auto px-6 py-5 flex items-start gap-5">
    <div class="flex-1 text-sm text-white/80 leading-relaxed max-w-3xl">
      <div class="text-[10px] uppercase tracking-widest brand-accent font-bold mb-2">Start here</div>
      <p class="mb-3">Yo PR team! This dashboard pulls the latest press releases, news coverage, and SEC filings for your 11 fintech competitors. Refreshes every Monday at 7am ET. Nothing for you to maintain.</p>
      <p class="mb-3">Top section is <span class="brand-accent font-medium">This Week</span> — chronological across all brands. Scroll down for per-brand tiles to drill into one competitor at a time. Every headline links straight to the source.</p>
      <p>Don't miss anything. Stay sharp.</p>
    </div>
    <button onclick="dismissWhy()" class="text-white/40 hover:text-white/80 text-2xl leading-none -mt-1" aria-label="Dismiss">×</button>
  </div>
</div>
"""

HEADER_BRAND = """
<header class="border-b border-white/10">
  <div class="max-w-7xl mx-auto px-6 py-5 flex items-center justify-between">
    <div class="flex items-center gap-4">
      <a href="index.html" class="flex items-center gap-4">
        <img src="assets/vscrl-wordmark.png" alt="VSCRL" class="h-10 w-auto" />
        <div class="text-white/40 font-light text-xl">×</div>
        <img src="assets/acorns-logo.svg" alt="Acorns" class="h-7 w-auto" />
      </a>
      <span class="text-white/30 mx-2">/</span>
      <span class="text-sm uppercase tracking-widest text-white/70 font-semibold">$brand</span>
    </div>
    <a href="index.html" class="text-xs text-white/50 hover:text-white/80 transition">← Back to overview</a>
  </div>
</header>
"""

FOOTER = """
<footer class="border-t border-white/10 mt-16">
  <div class="max-w-7xl mx-auto px-6 py-8 text-center text-xs text-white/40">
    Built and maintained weekly by <span class="brand-accent">VSCRL</span> · PR Pulse · Tracks $brand_count fintech competitors
  </div>
</footer>

<script>
  function toggleWhy() {
    var b = document.getElementById('why-banner');
    var p = document.getElementById('why-pill');
    if (!b || !p) return;
    b.classList.toggle('hidden');
    p.classList.remove('why-attention');
  }
  function dismissWhy() {
    var b = document.getElementById('why-banner');
    var p = document.getElementById('why-pill');
    if (b) b.classList.add('hidden');
    if (p) p.classList.remove('why-attention');
    try { localStorage.setItem('vscrl-pr-pulse-why-seen', '1'); } catch(e) {}
  }
  try {
    if (!localStorage.getItem('vscrl-pr-pulse-why-seen')) {
      var b = document.getElementById('why-banner');
      var p = document.getElementById('why-pill');
      if (b) b.classList.remove('hidden');
      if (p) p.classList.add('why-attention');
      localStorage.setItem('vscrl-pr-pulse-why-seen', '1');
    }
  } catch(e) {}
</script>
</body>
</html>
"""


# ============================================================================
# RENDERERS
# ============================================================================

def _relative_date(iso: str) -> str:
    """Render '2 days ago' style for a recent date; fall back to YYYY-MM-DD."""
    try:
        dt = datetime.fromisoformat(iso)
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.days == 0:
            hours = int(delta.total_seconds() / 3600)
            if hours <= 1:
                return "just now"
            return f"{hours}h ago"
        if delta.days == 1:
            return "yesterday"
        if delta.days < 7:
            return f"{delta.days}d ago"
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso[:10]


def render_item(item: dict, *, show_brand: bool = False) -> str:
    """Render a single news/filing item as a card."""
    brand_chip = ""
    if show_brand:
        brand_chip = (
            f'<a href="{escape(item["slug"])}.html" '
            f'class="text-[10px] uppercase tracking-widest brand-accent font-semibold '
            f'whitespace-nowrap hover:underline">{escape(item["brand"])}</a>'
            f'<span class="text-white/20">·</span>'
        )
    filing_chip = ""
    if item.get("type") == "filing":
        filing_chip = (
            '<span class="text-[10px] uppercase tracking-widest text-amber-400/80 '
            'bg-amber-400/10 px-2 py-0.5 rounded">SEC Filing</span>'
        )
    summary_html = ""
    if item.get("summary"):
        summary_html = (
            f'<p class="text-sm text-white/60 leading-relaxed mt-1">'
            f'{escape(item["summary"])}</p>'
        )
    return f"""
<a href="{escape(item['url'])}" target="_blank" rel="noopener"
   class="item-card block border border-white/10 rounded-xl px-5 py-4">
  <div class="flex items-center gap-3 text-[11px] text-white/50 mb-2">
    {brand_chip}
    <span>{_relative_date(item['date'])}</span>
    <span class="text-white/20">·</span>
    <span>{escape(item.get('source', '') or 'Source')}</span>
    {filing_chip}
  </div>
  <h3 class="text-base font-medium text-white leading-snug">{escape(item['title'])}</h3>
  {summary_html}
</a>
"""


def render_brand_tile(brand: str, slug: str, items: list[dict]) -> str:
    """Render the homepage tile for a brand."""
    count = len(items)
    most_recent = items[0]["date"][:10] if items else "—"
    most_recent_label = _relative_date(items[0]["date"]) if items else "no items"
    badge_color = "brand-accent" if count > 0 else "text-white/40"
    return f"""
<a href="{escape(slug)}.html"
   class="brand-tile block border border-white/10 rounded-xl px-5 py-5 bg-white/[0.02]">
  <div class="flex items-start justify-between mb-3">
    <div class="text-base font-semibold text-white">{escape(brand)}</div>
    <div class="text-xs {badge_color}">{count}</div>
  </div>
  <div class="text-[11px] text-white/50">
    Last: <span class="text-white/80">{most_recent_label}</span>
  </div>
</a>
"""


def render_index(items: list[dict], by_brand: dict, refresh_date: str) -> str:
    """Render dashboard/index.html"""
    top_items_html = "\n".join(
        render_item(it, show_brand=True) for it in items[:TOP_FEED_LIMIT]
    )
    tiles_html = "\n".join(
        render_brand_tile(brand, slug, by_brand.get(brand, []))
        for brand, slug in BRANDS
    )
    head = Template(PAGE_HEAD).substitute(
        title="Acorns PR Pulse · VSCRL",
        accent=ACCENT_HEX,
    )
    header = Template(HEADER_HOMEPAGE).substitute(refresh_date=refresh_date)
    footer = Template(FOOTER).substitute(brand_count=len(BRANDS))
    body = f"""
<main class="max-w-7xl mx-auto px-6 py-10">

  <section class="mb-12">
    <div class="flex items-center gap-3 mb-5">
      <h2 class="text-2xl font-semibold text-white">This Week</h2>
      <span class="text-xs text-white/40">
        Top {TOP_FEED_LIMIT} across all {len(BRANDS)} competitors · most recent first
      </span>
    </div>
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-3">
      {top_items_html}
    </div>
  </section>

  <section>
    <div class="flex items-center gap-3 mb-5">
      <h2 class="text-2xl font-semibold text-white">By Brand</h2>
      <span class="text-xs text-white/40">
        Click a brand to see all 14-day items
      </span>
    </div>
    <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4">
      {tiles_html}
    </div>
  </section>

</main>
"""
    return head + header + body + footer


def render_brand_page(brand: str, slug: str, items: list[dict]) -> str:
    """Render dashboard/<slug>.html"""
    head = Template(PAGE_HEAD).substitute(
        title=f"{brand} · Acorns PR Pulse",
        accent=ACCENT_HEX,
    )
    header = Template(HEADER_BRAND).substitute(brand=escape(brand))
    footer = Template(FOOTER).substitute(brand_count=len(BRANDS))

    if not items:
        body = f"""
<main class="max-w-4xl mx-auto px-6 py-12">
  <h1 class="text-3xl font-semibold mb-3">{escape(brand)}</h1>
  <p class="text-white/60">No items in the last 14 days. Check back after the next refresh.</p>
</main>
"""
    else:
        items_html = "\n".join(render_item(it, show_brand=False) for it in items)
        body = f"""
<main class="max-w-4xl mx-auto px-6 py-10">
  <div class="mb-8">
    <h1 class="text-3xl font-semibold text-white mb-2">{escape(brand)}</h1>
    <p class="text-sm text-white/50">
      {len(items)} {"item" if len(items) == 1 else "items"} in the last 14 days · newest first
    </p>
  </div>
  <div class="grid grid-cols-1 gap-3">
    {items_html}
  </div>
</main>
"""
    return head + header + body + footer


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    data = json.loads(DATA_PATH.read_text())
    print(f"Loaded {len(data)} items from {DATA_PATH}")

    by_brand: dict[str, list[dict]] = defaultdict(list)
    for item in data:
        by_brand[item["brand"]].append(item)
    for brand in by_brand:
        by_brand[brand].sort(key=lambda x: x["date"], reverse=True)

    refresh_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    DASHBOARD.mkdir(parents=True, exist_ok=True)
    (DASHBOARD / "index.html").write_text(render_index(data, by_brand, refresh_date))
    print(f"  → dashboard/index.html ({len(data[:TOP_FEED_LIMIT])} feed items)")

    for brand, slug in BRANDS:
        items = by_brand.get(brand, [])
        (DASHBOARD / f"{slug}.html").write_text(render_brand_page(brand, slug, items))
        print(f"  → dashboard/{slug}.html ({len(items)} items)")

    print("\nGenerated.")


if __name__ == "__main__":
    main()
