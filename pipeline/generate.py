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
ACORNS_LIMIT = 30
OFFICIAL_LIMIT = 40
# No cap on Buzz items: the brand filter is the primary navigation, so we want
# every item in the DOM so any per-brand filter shows everything available.
BUZZ_LIMIT = 10_000
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
  <div class="max-w-7xl mx-auto px-4 sm:px-6 py-5 sm:py-6 flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
    <div class="flex flex-wrap items-center gap-3 sm:gap-5">
      <img src="assets/vscrl-wordmark.png" alt="VSCRL" class="h-10 sm:h-14 w-auto" />
      <div class="text-white/40 font-light text-xl sm:text-2xl">×</div>
      <div class="flex flex-col gap-1">
        <img src="assets/acorns-logo.svg" alt="Acorns" class="h-7 sm:h-10 w-auto" />
        <div class="text-[10px] sm:text-xs text-white/50">PR Pulse · Daily</div>
      </div>
      <button id="why-pill" onclick="toggleWhy()" class="text-xs text-[#74C947]/80 hover:text-[#74C947] border border-[#74C947]/30 hover:border-[#74C947]/60 px-3 py-1.5 rounded-full transition whitespace-nowrap">
        Start here →
      </button>
    </div>
    <div class="text-left md:text-right text-xs text-white/40">
      <div>Refreshed daily · 12pm ET</div>
      <div class="text-white/60 mt-1">Last refresh: $refresh_date</div>
    </div>
  </div>
</header>

<div id="why-banner" class="hidden border-b border-[#74C947]/20 bg-[#74C947]/[0.05]">
  <div class="max-w-7xl mx-auto px-6 py-5 flex items-start gap-5">
    <div class="flex-1 text-sm text-white/80 leading-relaxed max-w-3xl">
      <div class="text-[10px] uppercase tracking-widest brand-accent font-bold mb-2">Start here</div>
      <p class="mb-3">Yo PR team! This dashboard pulls the latest press coverage on Acorns and 11 fintech competitors. Refreshes every day at 12pm ET. Nothing for you to maintain.</p>
      <p class="mb-3"><span class="brand-accent font-medium">Mention Volume</span> at the top shows the at-a-glance picture: total stories per brand with sentiment stacked (green positive, gray neutral, red negative). Then <span class="brand-accent font-medium">Acorns in the News</span> for your own coverage, <span class="brand-accent font-medium">Official PR</span> for competitor press releases + SEC filings, and <span class="brand-accent font-medium">The Buzz</span> for everything else (tech press, regulatory, analyst). Running reporter list at the bottom.</p>
      <p>Every headline links straight to the source. When the same story is covered by multiple outlets, you'll see them stacked on one card.</p>
    </div>
    <button onclick="dismissWhy()" class="text-white/40 hover:text-white/80 text-2xl leading-none -mt-1" aria-label="Dismiss">×</button>
  </div>
</div>
"""

HEADER_BRAND = """
<header class="border-b border-white/10">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 py-4 sm:py-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
    <div class="flex flex-wrap items-center gap-3 sm:gap-4">
      <a href="index.html" class="flex items-center gap-3 sm:gap-4">
        <img src="assets/vscrl-wordmark.png" alt="VSCRL" class="h-8 sm:h-10 w-auto" />
        <div class="text-white/40 font-light text-lg sm:text-xl">×</div>
        <img src="assets/acorns-logo.svg" alt="Acorns" class="h-6 sm:h-7 w-auto" />
      </a>
      <span class="text-white/30">/</span>
      <span class="text-xs sm:text-sm uppercase tracking-widest text-white/70 font-semibold">$brand</span>
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

  // Buzz section: filter by brand AND sentiment (AND-combined). Each filter
  // row keeps one active chip; "All" resets that dimension. Visibility = match
  // on BOTH brand and sentiment. Active chip gets brand-accent styling.
  var _buzzFilter = { brand: 'all', sentiment: 'all' };
  function _applyBuzzFilter() {
    var items = document.querySelectorAll('#buzz-grid > .buzz-item');
    var visible = 0;
    items.forEach(function(it) {
      var brandMatch = (_buzzFilter.brand === 'all') || (it.getAttribute('data-brand-slug') === _buzzFilter.brand);
      var sentimentMatch = (_buzzFilter.sentiment === 'all') || (it.getAttribute('data-sentiment') === _buzzFilter.sentiment);
      var match = brandMatch && sentimentMatch;
      it.style.display = match ? '' : 'none';
      if (match) visible++;
    });
    var empty = document.getElementById('buzz-empty');
    if (empty) empty.classList.toggle('hidden', visible > 0);
  }
  function _styleChip(c, active, accent) {
    if (active) {
      c.className = 'px-3 py-1.5 rounded-full text-xs font-semibold border ' + accent.bg + ' ' + accent.text + ' ' + accent.border + ' transition whitespace-nowrap cursor-pointer';
    } else {
      c.className = 'px-3 py-1.5 rounded-full text-xs font-medium border bg-white/[0.03] ' + accent.idleText + ' ' + accent.idleBorder + ' hover:bg-white/[0.06] transition whitespace-nowrap cursor-pointer';
    }
  }
  function setBrandFilter(button, slug) {
    _buzzFilter.brand = slug;
    var chips = document.querySelectorAll('#buzz-brand-filters button[data-brand-filter]');
    chips.forEach(function(c) {
      var active = c.getAttribute('data-brand-filter') === slug;
      _styleChip(c, active, {
        bg: 'bg-brand-accent/15', text: 'text-[#74C947]', border: 'border-brand-accent',
        idleText: 'text-white/70', idleBorder: 'border-white/10 hover:border-white/30 hover:text-white',
      });
    });
    _applyBuzzFilter();
  }
  function setSentimentFilter(button, sentiment) {
    _buzzFilter.sentiment = sentiment;
    var palettes = {
      'all':      { text: 'text-[#74C947]', border: 'border-brand-accent', bg: 'bg-brand-accent/15' },
      'positive': { text: 'text-emerald-400', border: 'border-emerald-400', bg: 'bg-emerald-400/15' },
      'neutral':  { text: 'text-white', border: 'border-white/40', bg: 'bg-white/[0.10]' },
      'negative': { text: 'text-rose-400', border: 'border-rose-400', bg: 'bg-rose-400/15' },
    };
    var idlePalettes = {
      'all':      { idleText: 'text-[#74C947]/80', idleBorder: 'border-brand-accent/30' },
      'positive': { idleText: 'text-emerald-400/90', idleBorder: 'border-emerald-400/30' },
      'neutral':  { idleText: 'text-white/60', idleBorder: 'border-white/15' },
      'negative': { idleText: 'text-rose-400/90', idleBorder: 'border-rose-400/30' },
    };
    var chips = document.querySelectorAll('#buzz-sentiment-filters button[data-sentiment-filter]');
    chips.forEach(function(c) {
      var key = c.getAttribute('data-sentiment-filter');
      var active = (key === sentiment);
      var palette = palettes[key] || palettes['all'];
      var idle = idlePalettes[key] || idlePalettes['all'];
      _styleChip(c, active, Object.assign({}, palette, idle));
    });
    _applyBuzzFilter();
  }
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
    # Each badge gets whitespace-nowrap so its contents stay together when it wraps.
    badges: list[str] = []
    if item.get("type") == "filing":
        badges.append(
            '<span class="inline-block whitespace-nowrap text-[10px] uppercase tracking-widest '
            'text-amber-400/80 bg-amber-400/10 px-2 py-0.5 rounded">SEC Filing</span>'
        )
    if item.get("is_official") and item.get("type") != "filing":
        badges.append(
            '<span class="inline-block whitespace-nowrap text-[10px] uppercase tracking-widest '
            'text-[#74C947]/90 bg-[#74C947]/10 px-2 py-0.5 rounded">Official</span>'
        )
    sentiment = item.get("sentiment")
    if sentiment == "positive":
        badges.append(
            '<span class="inline-block whitespace-nowrap text-[10px] uppercase tracking-widest '
            'text-emerald-400 bg-emerald-400/10 border border-emerald-400/30 px-2 py-0.5 rounded">'
            '▲ Positive</span>'
        )
    elif sentiment == "negative":
        badges.append(
            '<span class="inline-block whitespace-nowrap text-[10px] uppercase tracking-widest '
            'text-rose-400 bg-rose-400/10 border border-rose-400/30 px-2 py-0.5 rounded">'
            '▼ Negative</span>'
        )
    elif sentiment == "neutral":
        badges.append(
            '<span class="inline-block whitespace-nowrap text-[10px] uppercase tracking-widest '
            'text-white/50 bg-white/[0.05] border border-white/15 px-2 py-0.5 rounded">'
            'Neutral</span>'
        )
    badges_html = ""
    if badges:
        badges_html = '<div class="flex flex-wrap gap-2 mt-2">' + "".join(badges) + "</div>"
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
    sentiment_attr = item.get("sentiment") or "neutral"
    return f"""
<div class="item-card buzz-item border border-white/10 rounded-xl px-5 py-4" data-brand-slug="{escape(item.get('slug',''))}" data-sentiment="{escape(sentiment_attr)}">
  <h3 class="text-base font-medium text-white leading-snug">
    <a href="{escape(primary_url)}" target="_blank" rel="noopener" class="hover:underline">{escape(item['title'])}</a>
  </h3>
  {badges_html}
  {summary_html}
  {sources_html}
</div>
"""


def render_mentions_chart(by_brand: dict, refresh_date: str) -> str:
    """Render a horizontal stacked-bar chart: each brand's total mentions split
    into positive / neutral / negative segments. Pure HTML/CSS — no JS."""
    from collections import Counter
    rows = []
    for brand, _slug in ALL_TRACKED:
        items = by_brand.get(brand, [])
        sentiments = Counter(i.get("sentiment", "neutral") for i in items)
        rows.append({
            "brand": brand,
            "total": len(items),
            "positive": sentiments.get("positive", 0),
            "neutral": sentiments.get("neutral", 0),
            "negative": sentiments.get("negative", 0),
        })
    rows.sort(key=lambda r: -r["total"])
    max_count = max((r["total"] for r in rows), default=1) or 1

    bars_html = ""
    for r in rows:
        is_subject = (r["brand"] == SUBJECT[0])
        label_class = "brand-accent font-semibold" if is_subject else "text-white"
        count_class = "brand-accent font-bold" if is_subject else "text-white/70"
        if r["total"] == 0:
            inner = ""
        else:
            # Bar width is total/max; within the bar, segments are each sentiment's
            # share of the total (so each segment is colored proportionally).
            # We render three flex children with flex-basis as percentages.
            bar_width_pct = (r["total"] / max_count) * 100
            pos_share = (r["positive"] / r["total"]) * 100
            neu_share = (r["neutral"]  / r["total"]) * 100
            neg_share = (r["negative"] / r["total"]) * 100
            seg_pos = (
                f'<div class="bg-emerald-500/80 h-full" '
                f'style="width: {pos_share:.2f}%" '
                f'title="{r["positive"]} positive"></div>'
            ) if r["positive"] else ""
            seg_neu = (
                f'<div class="bg-white/30 h-full" '
                f'style="width: {neu_share:.2f}%" '
                f'title="{r["neutral"]} neutral"></div>'
            ) if r["neutral"] else ""
            seg_neg = (
                f'<div class="bg-rose-500/80 h-full" '
                f'style="width: {neg_share:.2f}%" '
                f'title="{r["negative"]} negative"></div>'
            ) if r["negative"] else ""
            inner = (
                f'<div class="flex h-full" style="width: {bar_width_pct:.2f}%">'
                f'{seg_pos}{seg_neu}{seg_neg}'
                f'</div>'
            )
        bars_html += f"""
<div class="flex items-center gap-3 sm:gap-4">
  <div class="w-24 sm:w-32 shrink-0 text-xs sm:text-sm {label_class} text-right">{escape(r['brand'])}</div>
  <div class="flex-1 bg-white/[0.04] rounded overflow-hidden h-6 sm:h-7">
    {inner}
  </div>
  <div class="w-10 text-right text-sm {count_class} tabular-nums">{r['total']}</div>
</div>
"""
    legend = """
<div class="flex flex-wrap gap-x-4 gap-y-2 text-xs text-white/60 mb-4">
  <span class="inline-flex items-center gap-2">
    <span class="inline-block w-3 h-3 rounded-sm bg-emerald-500/80"></span> Positive
  </span>
  <span class="inline-flex items-center gap-2">
    <span class="inline-block w-3 h-3 rounded-sm bg-white/30"></span> Neutral
  </span>
  <span class="inline-flex items-center gap-2">
    <span class="inline-block w-3 h-3 rounded-sm bg-rose-500/80"></span> Negative
  </span>
</div>
"""
    return f"""
<section>
  <div class="flex items-baseline gap-3 mb-3">
    <h2 class="text-2xl font-semibold text-white">Mention Volume</h2>
    <span class="text-xs text-white/40">Last 14 days · sentiment-stacked · syndicated coverage counts once</span>
  </div>
  {legend}
  <div class="bg-white/[0.02] border border-white/10 rounded-2xl p-4 sm:p-6 space-y-2">
    {bars_html}
  </div>
</section>
"""


def render_brand_chip(brand: str, slug: str, count: int, active: bool = False) -> str:
    """Render a filter chip used to filter the Buzz feed by brand."""
    base = "px-3 py-1.5 rounded-full text-xs font-medium border transition whitespace-nowrap cursor-pointer"
    if active:
        cls = f"{base} bg-brand-accent/15 text-[#74C947] border-brand-accent"
    else:
        cls = f"{base} bg-white/[0.03] text-white/70 border-white/10 hover:border-white/30 hover:text-white"
    count_str = f' <span class="text-white/40">{count}</span>' if count else ' <span class="text-white/25">0</span>'
    return (
        f'<button type="button" class="{cls}" data-brand-filter="{escape(slug)}" '
        f'onclick="setBrandFilter(this, \'{escape(slug)}\')">{escape(brand)}{count_str}</button>'
    )


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

    # Brand filter chips: "All" + each competitor brand. Acorns is in its own
    # section at top so isn't included in the Buzz brand row.
    chips_html = (
        f'<button type="button" data-brand-filter="all" '
        f'class="px-3 py-1.5 rounded-full text-xs font-semibold border bg-brand-accent/15 text-[#74C947] border-brand-accent transition whitespace-nowrap cursor-pointer" '
        f'onclick="setBrandFilter(this, \'all\')">All <span class="text-white/40">{len(buzz_items)}</span></button>'
    )
    buzz_by_brand: dict[str, int] = {}
    for it in buzz_items:
        buzz_by_brand[it["brand"]] = buzz_by_brand.get(it["brand"], 0) + 1
    for brand, slug in BRANDS:
        n = buzz_by_brand.get(brand, 0)
        chips_html += render_brand_chip(brand, slug, n, active=False)

    # Sentiment filter chips: "All" + Positive / Negative / Neutral. AND-combines
    # with the brand filter so users can drill down to e.g. only-negative-Kalshi.
    buzz_by_sentiment = {"positive": 0, "negative": 0, "neutral": 0}
    for it in buzz_items:
        buzz_by_sentiment[it.get("sentiment", "neutral")] += 1
    sentiment_chips_html = (
        f'<button type="button" data-sentiment-filter="all" '
        f'class="px-3 py-1.5 rounded-full text-xs font-semibold border bg-brand-accent/15 text-[#74C947] border-brand-accent transition whitespace-nowrap cursor-pointer" '
        f'onclick="setSentimentFilter(this, \'all\')">All</button>'
        f'<button type="button" data-sentiment-filter="positive" '
        f'class="px-3 py-1.5 rounded-full text-xs font-medium border bg-white/[0.03] text-emerald-400/90 border-emerald-400/30 hover:bg-emerald-400/10 transition whitespace-nowrap cursor-pointer" '
        f'onclick="setSentimentFilter(this, \'positive\')">▲ Positive <span class="text-white/40">{buzz_by_sentiment["positive"]}</span></button>'
        f'<button type="button" data-sentiment-filter="neutral" '
        f'class="px-3 py-1.5 rounded-full text-xs font-medium border bg-white/[0.03] text-white/60 border-white/15 hover:bg-white/[0.06] transition whitespace-nowrap cursor-pointer" '
        f'onclick="setSentimentFilter(this, \'neutral\')">Neutral <span class="text-white/40">{buzz_by_sentiment["neutral"]}</span></button>'
        f'<button type="button" data-sentiment-filter="negative" '
        f'class="px-3 py-1.5 rounded-full text-xs font-medium border bg-white/[0.03] text-rose-400/90 border-rose-400/30 hover:bg-rose-400/10 transition whitespace-nowrap cursor-pointer" '
        f'onclick="setSentimentFilter(this, \'negative\')">▼ Negative <span class="text-white/40">{buzz_by_sentiment["negative"]}</span></button>'
    )

    mentions_chart_html = render_mentions_chart(by_brand, refresh_date)

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
<main class="max-w-7xl mx-auto px-4 sm:px-6 py-8 sm:py-10 space-y-10 sm:space-y-12">

  <!-- 1. Mention Volume (chart) at top for at-a-glance overview -->
  {mentions_chart_html}

  <!-- 2. Acorns in the News -->
  <section>
    <div class="flex items-baseline gap-3 mb-5">
      <h2 class="text-2xl font-semibold text-white">Acorns in the News</h2>
      <span class="text-xs text-white/40">{len(acorns_items)} items · last 14 days</span>
    </div>
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-3">
      {acorns_html}
    </div>
  </section>

  <!-- 3. Official PR -->
  <section>
    <div class="flex items-baseline gap-3 mb-5">
      <h2 class="text-2xl font-semibold text-white">Official PR</h2>
      <span class="text-xs text-white/40">{len(official_items)} items · PR wires + SEC filings · competitors only</span>
    </div>
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-3">
      {official_html}
    </div>
  </section>

  <!-- 4. The Buzz (with brand filter chips) -->
  <section>
    <div class="flex items-baseline gap-3 mb-4">
      <h2 class="text-2xl font-semibold text-white">The Buzz</h2>
      <span class="text-xs text-white/40">{len(buzz_items)} items · click a brand below to filter</span>
    </div>
    <div class="space-y-3 mb-6">
      <div>
        <div class="text-[10px] uppercase tracking-widest text-white/40 mb-2">Filter by brand</div>
        <div id="buzz-brand-filters" class="flex flex-wrap gap-2">
          {chips_html}
        </div>
      </div>
      <div>
        <div class="text-[10px] uppercase tracking-widest text-white/40 mb-2">Filter by sentiment</div>
        <div id="buzz-sentiment-filters" class="flex flex-wrap gap-2">
          {sentiment_chips_html}
        </div>
      </div>
    </div>
    <div id="buzz-grid" class="grid grid-cols-1 lg:grid-cols-2 gap-3">
      {buzz_html}
    </div>
    <p id="buzz-empty" class="hidden text-white/50 text-sm mt-4">No items for this brand in the last 14 days.</p>
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
<main class="max-w-4xl mx-auto px-4 sm:px-6 py-10 sm:py-12">
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
<main class="max-w-4xl mx-auto px-4 sm:px-6 py-8 sm:py-10 space-y-8 sm:space-y-10">
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
