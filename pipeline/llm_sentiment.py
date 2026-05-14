"""
LLM-based sentiment classifier for PR Pulse items.

Calls Claude Haiku 4.5 to classify each article's sentiment from the
perspective of the brand's PR team. Cached per-URL so daily refreshes only
classify newly-seen articles.

Cost (Haiku 4.5 pricing):
  ~$1/MTok input, ~$5/MTok output
  Per call: ~200 input + ~10 output tokens = ~$0.00025
  150 items × first run: ~$0.04
  Daily refresh, ~20-30 new items: ~$0.005-$0.008
  Annual: well under $5

Falls back to a keyword-list heuristic if ANTHROPIC_API_KEY is missing, so the
dashboard always renders sentiment badges. The heuristic is noticeably worse for
nuanced headlines, but at least it ships sentiment data unconditionally.
"""
from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SENTIMENT_CACHE_PATH = ROOT / "data" / "sentiment_cache.json"

# Claude Haiku 4.5 — cheapest current model, sufficient accuracy for tone
# classification on news headlines. Per the project's standing model guidance.
MODEL = "claude-haiku-4-5-20251001"

# Parallel API calls — sentiment classification is embarrassingly parallel
# (each item independent). 8 concurrent keeps us well under rate limits while
# bringing total run time down from ~30s sequential to ~5s.
MAX_WORKERS = 8

SYSTEM_PROMPT = """You classify news article sentiment from the perspective of the company's PR team.

Read the headline and summary. Decide whether the article is good news, bad news, or factual/balanced for the company.

POSITIVE = good news for the company. Funding rounds raised, product launches, partnerships announced, market wins, positive earnings, awards, growth metrics, executive hires that signal momentum, regulatory approval.

NEGATIVE = bad news for the company. Lawsuits filed, regulatory investigations, fines, executive departures under pressure, layoffs, security breaches, missed earnings, downgraded ratings, customer complaints, public scandals, competitive losses.

NEUTRAL = factual coverage that isn't clearly good or bad. Market roundups that simply list the company. Earnings calendar announcements. Mentions where the company is just one of many. Speculative analyst pieces with mixed signals.

When unsure between two labels, prefer NEUTRAL.

Respond with exactly one word: POSITIVE, NEGATIVE, or NEUTRAL. No explanation. No punctuation."""


# ---------------------------------------------------------------------------
# CACHE
# ---------------------------------------------------------------------------

def load_cache() -> dict:
    if SENTIMENT_CACHE_PATH.exists():
        try:
            return json.loads(SENTIMENT_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    SENTIMENT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENTIMENT_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# LLM PATH (preferred)
# ---------------------------------------------------------------------------

def _classify_one_llm(client, item: dict) -> tuple[str, str]:
    """Returns (cache_key, sentiment_label) for one item."""
    cache_key = item.get("url", "") or item.get("title", "")
    user_prompt = (
        f"Brand: {item.get('brand','')}\n"
        f"Headline: {item.get('title','')}\n"
        f"Summary: {item.get('summary','')}"
    )
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=10,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = resp.content[0].text.strip().lower() if resp.content else ""
        if "positive" in text:
            return cache_key, "positive"
        if "negative" in text:
            return cache_key, "negative"
        return cache_key, "neutral"
    except Exception as e:
        print(f"  ! sentiment classify failed for {cache_key[:50]}: {e}", file=sys.stderr)
        return cache_key, "neutral"


def classify_all_llm(items: list[dict], cache: dict) -> int:
    """Apply cached sentiment labels to items in place. If ANTHROPIC_API_KEY is
    set, also fire LLM classification for items not in cache. Returns the count
    of NEW LLM classifications made this run (0 if no API key, even though
    cached items still get their labels applied).

    Cache application happens UNCONDITIONALLY — that's the whole point of the
    cache. The API-key check only gates whether we hit Anthropic for items we
    don't already know about.
    """
    # 1. Apply cached labels first (unconditional). Items without a cache entry
    #    are collected as `pending` for either LLM classification or heuristic
    #    fallback in the caller.
    pending = []
    for item in items:
        key = item.get("url", "") or item.get("title", "")
        if key in cache:
            item["sentiment"] = cache[key]
        else:
            pending.append(item)

    if not pending:
        return 0

    # 2. If no API key, return without LLM classification — caller will heuristic-
    #    classify the still-pending items.
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return 0

    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)

    new_count = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_classify_one_llm, client, it): it for it in pending}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                key, label = fut.result()
                it["sentiment"] = label
                cache[key] = label
                new_count += 1
            except Exception as e:
                print(f"  ! sentiment future failed: {e}", file=sys.stderr)
                it["sentiment"] = "neutral"
    return new_count


# ---------------------------------------------------------------------------
# HEURISTIC FALLBACK (when ANTHROPIC_API_KEY is missing)
# ---------------------------------------------------------------------------

# Imported lazily to avoid hard dependency on sources during testing.
def _heuristic(text: str) -> str:
    """Keyword-list sentiment with light negation handling. Not bulletproof; the
    LLM path is preferred when ANTHROPIC_API_KEY is set."""
    from pipeline.sources import SENTIMENT_POSITIVE, SENTIMENT_NEGATIVE
    import re as _re
    lower = text.lower()

    # Tokenize so we can do negation lookback. Split on word boundaries.
    tokens = _re.findall(r"[a-z']+", lower)

    NEGATORS = {"not", "no", "never", "without", "fails", "failed", "failing",
                "denies", "denied", "refused", "refuses"}

    def is_negated(idx: int) -> bool:
        """Look back up to 3 tokens for a negator."""
        for k in range(max(0, idx - 3), idx):
            if tokens[k] in NEGATORS:
                return True
        return False

    pos = neg = 0
    for i, tok in enumerate(tokens):
        if tok in SENTIMENT_POSITIVE:
            if is_negated(i):
                neg += 1   # "fails to grow" → negative
            else:
                pos += 1
        elif tok in SENTIMENT_NEGATIVE:
            if is_negated(i):
                pos += 1   # "no losses" → positive
            else:
                neg += 1

    # Margin threshold — small differences land in neutral to avoid jitter.
    if abs(pos - neg) < 1:
        return "neutral"
    return "positive" if pos > neg else "negative"


def classify_all(items: list[dict]) -> dict[str, int]:
    """Classify all items. Tries LLM first; if no API key, falls back to
    heuristic. Returns a dict of {"llm_classified": N, "heuristic_classified": N,
    "cached": N} for logging."""
    cache = load_cache()
    cached_before = len([i for i in items if (i.get("url","") or i.get("title","")) in cache])

    new_llm = classify_all_llm(items, cache)

    # Items still without sentiment (LLM unavailable or failed) get heuristic.
    heuristic_count = 0
    for item in items:
        if item.get("sentiment"):
            continue
        text = item.get("title", "") + " " + item.get("summary", "")
        item["sentiment"] = _heuristic(text)
        heuristic_count += 1

    save_cache(cache)
    return {
        "cached": cached_before,
        "llm_new": new_llm,
        "heuristic": heuristic_count,
    }
