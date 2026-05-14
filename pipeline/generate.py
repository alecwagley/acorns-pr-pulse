#!/usr/bin/env python3
"""
Generate dashboard HTML from data/current.json + data/reporters.json.

Run:
    python3 pipeline/generate.py

Homepage sections (in order):
    1. Acorns in the News — items where brand == "Acorns" (the subject)
    2. Official PR        — competitor items where is_official == True
                            (PR wires + SEC filings)
    3. The Buzz           — competitor items where is_official == False
                            (tech press, analyst coverage, regulatory news, etc.)
    4. By Brand           — 11 competitor tiles + Acorns tile
    5. Top Reporters      — running aggregate from data/reporters.json
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "current.json"
REPORTERS_PATH = ROOT / "data" / "reporters.json"
DASHBOARD = ROOT / "dashboard"

sys.path.insert(0, str(ROOT))
from pipeline.sources import SUBJECT, BRANDS, ALL_TRACKED

ACCENT_HEX = "#74C947"

# How many items to show in each homepage section.
ACORNS_LIMIT = 20
OFFICIAL_LIMIT = 25
BUZZ_LIMIT = 40
TOP_REPORTERS_LIMIT = 25


# ============================================================================
# PAGE CHROME
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
  /* Why-pill attention animation */
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
      <p class="mb-3">Yo PR team! This dashboard pulls the latest press coverage on Acorns and 11 fintech competitors. Refreshes every Monday at 7am ET. Nothing for you to maintain.</p>
      <p class="mb-3"><span class="brand-accent font-medium">Acorns in the News</span> is at the top — your own coverage first. Below that, <span class="brand-accent font-medium">Official PR</span> shows competitor press releases + SEC filings (the high-signal stuff). <span class="brand-accent font-medium">The Buzz</span> is everything else — tech press, regulatory news, analyst commentary. Per-brand tiles below; running reporter list at the bottom.</p>
      <p>Every headline links straight to the source. When the same story is covered by multiple outlets, you'll see them stacked on one card.</p>
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
    Built and maintained weekly by <span class="brand-accent">VSCRL</span> · PR Pulse · Tracks Acorns + $brand_count fintech competitors
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
    """'2 days ago' style for recent dates; fall back to YYYY-MM-DD."""
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
    """Render an item card. Title is the lead; sources stack below the summary
    (one row per source publisher when group_size > 1)."""
    brand_chip = ""
    if show_brand:
        brand_chip = (
            f'<a href="{escape(item["slug"])}.html" '
            f'class="text-[10px] uppercase tracking-widest brand-accent font-semibold '
            f'hover:underline whitespace-nowrap">{escape(item["brand"])}</a>'
            f'<span class="text-white/20">·</span>'
        )
    filing_chip = ""
    if item.get("type") == "filing":
        filing_chip = (
            '<span class="text-[10px] uppercase tracking-widest text-amber-400/80 '
            'bg-amber-400/10 px-2 py-0.5 rounded ml-2 align-middle">SEC Filing</span>'
        )
    official_chip = ""
    if item.get("is_official") and item.get("type") != "filing":
        official_chip = (
            '<span class="text-[10px] uppercase tracking-widest text-[#74C947]/90 '
            'bg-[#74C947]/10 px-2 py-0.5 rounded ml-2 align-middle">Official</span>'
        )
    summary_html = ""
    if item.get("summary"):
        summary_html = (
            f'<p class="text-sm text-white/60 leading-relaxed mt-2">'
            f'{escape(item["summary"])}</p>'
        )

    # Sources block — one row per (publisher, url) tuple in the group.
    sources = item.get("sources") or [{"publisher": item.get("source", ""), "url": item["url"], "author": item.get("author", "")}]
    if len(sources) == 1:
        s = sources[0]
        author_str = f' · {escape(s["author"])}' if s.get("author") else ""
        sources_html = f"""
<div class="flex items-center gap-2 text-[11px] text-white/45 mt-3">
  {brand_chip}
  <span>{_relative_date(item['date'])}</span>
  <span class="text-white/20">·</span>
  <a href="{escape(s['url'])}" target="_blank" rel="noopener" class="hover:text-white/80 underline-offset-2 hover:underline">
    {escape(s.get('publisher', '') or 'Source')}
  </a>
  <span>{author_str}</span>
</div>
"""
    else:
        # Multi-source: stack as a list of clickable publisher links.
        rows = []
        for s in sources:
            author_str = f' <span class="text-white/30">· {escape(s["author"])}</span>' if s.get("author") else ""
            rows.append(
                f'<li><a href="{escape(s["url"])}" target="_blank" rel="noopener" '
                f'class="text-white/65 hover:text-white underline-offset-2 hover:underline">'
                f'{escape(s.get("publisher", "") or "Source")}</a>{author_str}</li>'
            )
        sources_html = f"""
<div class="mt-3">
  <div class="flex items-center gap-2 text-[11px] text-white/45 mb-2">
    {brand_chip}
    <span>{_relative_date(item['date'])}</span>
    <span class="text-white/20">·</span>
    <span class="brand-accent font-semibold">{len(sources)} sources</span>
  </div>
  <ul class="text-[12px] space-y-1 ml-1 list-disc list-inside marker:text-white/30">
    {''.join(rows)}
  </ul>
</div>
"""

    # Primary URL: when multi-source, link the title to the first source so a click
    # on the title still opens an article. Otherwise the whole card is the link.
    primary_url = sources[0]["url"]
    return f"""
<div class="item-card border border-white/10 rounded-xl px-5 py-4">
  <h3 class="text-base font-medium text-white leading-snug">
    <a href="{escape(primary_url)}" target="_blank" rel="noopener" class="hover:underline">{escape(item['title'])}</a>{filing_chip}{official_chip}
  </h3>
  {summary_html}
  {sources_html}
</div>
"""


def render_brand_tile(brand: str, slug: str, items: list[dict]) -> str:
    """Render the homepage tile for a brand (or for Acorns)."""
    count = len(items)
    label = _relative_date(items[0]["date"]) if items else "no items"
    badge_color = "brand-accent" if count > 0 else "text-white/40"
    return f"""
<a href="{escape(slug)}.html"
   class="brand-tile block border border-white/10 rounded-xl px-5 py-5 bg-white/[0.02]">
  <div class="flex items-start justify-between mb-3">
    <div class="text-base font-semibold text-white">{escape(brand)}</div>
    <div class="text-xs {badge_color}">{count}</div>
  </div>
  <div class="text-[11px] text-white/50">
    Last: <span class="text-white/80">{label}</span>
  </div>
</a>
"""


def render_reporter_row(row: dict) -> str:
    brands_str = " · ".join(escape(b) for b in row.get("brands", []))
    return f"""
<tr class="border-b border-white/5">
  <td class="py-2 pr-4 text-white">{escape(row.get('author', ''))}</td>
  <td class="py-2 pr-4 text-white/70">{escape(row.get('publisher', ''))}</td>
  <td class="py-2 pr-4 text-white/60 text-xs">{brands_str}</td>
  <td class="py-2 text-right text-white/50 text-xs tabular-nums">{row.get('count', 0)}</td>
</tr>
"""


def render_index(
    *,
    acorns_items: list[dict],
    official_items: list[dict],
    buzz_items: list[dict],
    by_brand: dict,
    reporters_top: list[dict],
    refresh_date: str,
) -> str:
    head = Template(PAGE_HEAD).substitute(title="Acorns PR Pulse · VSCRL", accent=ACCENT_HEX)
    header = Template(HEADER_HOMEPAGE).substitute(refresh_date=refresh_date)
    footer = Template(FOOTER).substitute(brand_count=len(BRANDS))

    acorns_html = "\n".join(render_item(it, show_brand=False) for it in acorns_items[:ACORNS_LIMIT])
    if not acorns_items:
        acorns_html = '<p class="text-white/50 text-sm">No Acorns mentions in the last 14 days.</p>'

    official_html = "\n".join(render_item(it, show_brand=True) for it in official_items[:OFFICIAL_LIMIT])
    if not official_items:
        official_html = '<p class="text-white/50 text-sm">No official press releases or SEC filings from competitors in the last 14 days.</p>'

    buzz_html = "\n".join(render_item(it, show_brand=True) for it in buzz_items[:BUZZ_LIMIT])
    if not buzz_items:
        buzz_html = '<p class="text-white/50 text-sm">No buzz items in the last 14 days.</p>'

    # Tiles: Acorns first, then 11 competitors.
    tiles_html = render_brand_tile(SUBJECT[0], SUBJECT[1], by_brand.get(SUBJECT[0], []))
    for brand, slug in BRANDS:
        tiles_html += render_brand_tile(brand, slug, by_brand.get(brand, []))

    reporters_html = ""
    if reporters_top:
        rows = "\n".join(render_reporter_row(r) for r in reporters_top)
        reporters_html = f"""
<table class="w-full text-sm">
  <thead>
    <tr class="text-[10px] uppercase tracking-widest text-white/40 border-b border-white/10">
      <th class="text-left py-2 pr-4">Reporter</th>
      <th class="text-left py-2 pr-4">Outlet</th>
      <th class="text-left py-2 pr-4">Brands covered</th>
      <th class="text-right py-2">Stories</th>
    </tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>
"""
    else:
        reporters_html = '<p class="text-white/50 text-sm">No bylined coverage in the feed yet. This list fills in over time as feeds with author data come in.</p>'

    body = f"""
<main class="max-w-7xl mx-auto px-6 py-10 space-y-12">

  <!-- 1. Acorns in the News -->
  <section>
    <div class="flex items-baseline gap-3 mb-5">
      <h2 class="text-2xl font-semibold text-white">Acorns in the News</h2>
      <span class="text-xs text-white/40">{len(acorns_items)} items · last 14 days</span>
    </div>
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-3">
      {acorns_html}
    </div>
  </section>

  <!-- 2. Official PR -->
  <section>
    <div class="flex items-baseline gap-3 mb-5">
      <h2 class="text-2xl font-semibold text-white">Official PR</h2>
      <span class="text-xs text-white/40">{len(official_items)} items · PR wires + SEC filings · competitors only</span>
    </div>
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-3">
      {official_html}
    </div>
  </section>

  <!-- 3. The Buzz -->
  <section>
    <div class="flex items-baseline gap-3 mb-5">
      <h2 class="text-2xl font-semibold text-white">The Buzz</h2>
      <span class="text-xs text-white/40">{len(buzz_items)} items · press coverage, analyst commentary, regulatory news</span>
    </div>
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-3">
      {buzz_html}
    </div>
  </section>

  <!-- 4. By Brand -->
  <section>
    <div class="flex items-baseline gap-3 mb-5">
      <h2 class="text-2xl font-semibold text-white">By Brand</h2>
      <span class="text-xs text-white/40">Click any tile to drill into that brand's items</span>
    </div>
    <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4">
      {tiles_html}
    </div>
  </section>

  <!-- 5. Top Reporters (running) -->
  <section>
    <div class="flex items-baseline gap-3 mb-5">
      <h2 class="text-2xl font-semibold text-white">Top Reporters</h2>
      <span class="text-xs text-white/40">Running list · grows weekly · ranked by story count</span>
    </div>
    {reporters_html}
  </section>

</main>
"""
    return head + header + body + footer


def render_brand_page(brand: str, slug: str, items: list[dict]) -> str:
    head = Template(PAGE_HEAD).substitute(
        title=f"{brand} · Acorns PR Pulse", accent=ACCENT_HEX,
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
        # Split per-brand items into Official + Buzz.
        official = [it for it in items if it.get("is_official")]
        buzz = [it for it in items if not it.get("is_official")]
        official_html = "\n".join(render_item(it, show_brand=False) for it in official) or '<p class="text-white/50 text-sm">No official items in the last 14 days.</p>'
        buzz_html = "\n".join(render_item(it, show_brand=False) for it in buzz) or '<p class="text-white/50 text-sm">No buzz items in the last 14 days.</p>'

        body = f"""
<main class="max-w-4xl mx-auto px-6 py-10 space-y-10">
  <div>
    <h1 class="text-3xl font-semibold text-white mb-2">{escape(brand)}</h1>
    <p class="text-sm text-white/50">
      {len(items)} {"item" if len(items) == 1 else "items"} in the last 14 days · newest first
    </p>
  </div>

  <section>
    <div class="flex items-baseline gap-3 mb-4">
      <h2 class="text-xl font-semibold text-white">Official PR</h2>
      <span class="text-xs text-white/40">{len(official)} items</span>
    </div>
    <div class="grid grid-cols-1 gap-3">
      {official_html}
    </div>
  </section>

  <section>
    <div class="flex items-baseline gap-3 mb-4">
      <h2 class="text-xl font-semibold text-white">The Buzz</h2>
      <span class="text-xs text-white/40">{len(buzz)} items</span>
    </div>
    <div class="grid grid-cols-1 gap-3">
      {buzz_html}
    </div>
  </section>
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

    # Acorns items (subject) — separate top-of-page section.
    acorns_items = by_brand.get(SUBJECT[0], [])

    # Competitor items split into Official PR / Buzz.
    competitor_items = [it for it in data if it["brand"] != SUBJECT[0]]
    official_items = [it for it in competitor_items if it.get("is_official")]
    buzz_items = [it for it in competitor_items if not it.get("is_official")]

    # Top reporters from running log.
    reporters_top: list[dict] = []
    if REPORTERS_PATH.exists():
        try:
            reporters_log = json.loads(REPORTERS_PATH.read_text())
            reporters_top = sorted(
                reporters_log.values(),
                key=lambda r: r.get("count", 0),
                reverse=True,
            )[:TOP_REPORTERS_LIMIT]
        except Exception as e:
            print(f"  ! reporters log unreadable: {e}")

    refresh_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    DASHBOARD.mkdir(parents=True, exist_ok=True)
    (DASHBOARD / "index.html").write_text(render_index(
        acorns_items=acorns_items,
        official_items=official_items,
        buzz_items=buzz_items,
        by_brand=by_brand,
        reporters_top=reporters_top,
        refresh_date=refresh_date,
    ))
    print(f"  → dashboard/index.html · Acorns:{len(acorns_items)} Official:{len(official_items)} Buzz:{len(buzz_items)} Reporters:{len(reporters_top)}")

    for brand, slug in ALL_TRACKED:
        items = by_brand.get(brand, [])
        (DASHBOARD / f"{slug}.html").write_text(render_brand_page(brand, slug, items))
        print(f"  → dashboard/{slug}.html ({len(items)} items)")

    print("\nGenerated.")


if __name__ == "__main__":
    main()
