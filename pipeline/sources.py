"""
Source config: per-brand data feeds for the PR pulse collector.

Two source types:
    1. SEC EDGAR JSON  — per-company filings (8-K for material announcements,
                          earnings releases, etc.). Only for the 4 public brands.
                          Free, no API key, rate-limited (10 req/sec generous cap).
    2. Google News RSS — wide-net news search by brand name. Indexes the
                          company's own press page + tech press + analyst coverage.
                          Free, no API key, real-time.

Architecture decision (2026-05-14):
    Most fintech newsrooms (Chime, Robinhood, SoFi, Greenlight, Betterment,
    Wealthfront, Chase, Cash App) do NOT publish RSS feeds. Custom HTML scrapers
    per brand are fragile and high-maintenance, so we lean on Google News RSS
    (which already crawls those pages) + SEC EDGAR for definitive material
    announcements. If Layer 2 misses a release that Google News didn't index,
    add a per-brand HTML scraper later.

Polymarket has a Substack RSS feed (news.polymarket.com/feed) but it's covered
adequately by Google News for the wide-net use case; we keep it simple and only
use the two source types above.
"""
from __future__ import annotations

# The subject: tracked separately, rendered in its own "Acorns in the News" section
# at the top of the dashboard. Same data shape, same collection, separate section.
SUBJECT = ("Acorns", "acorns")

# Competitors — display name → slug (used for URL paths + CSS classes)
BRANDS = [
    ("Chime",       "chime"),
    ("Robinhood",   "robinhood"),
    ("Alinea",      "alinea"),
    ("Greenlight",  "greenlight"),
    ("Cash App",    "cash-app"),
    ("Betterment",  "betterment"),
    ("Wealthfront", "wealthfront"),
    ("SoFi",        "sofi"),
    ("Kalshi",      "kalshi"),
    ("Polymarket",  "polymarket"),
    ("Chase",       "chase"),
]

# Everything we collect (subject + competitors). Order matters for the collector
# loop but display order is handled by the generator.
ALL_TRACKED = [SUBJECT] + BRANDS

# Publishers that count as "Official PR" — PR distribution wires, company newsrooms,
# and the SEC. Anything not in this set falls under "The Buzz" (tech press, analyst
# commentary, news coverage). Case-insensitive substring match (same convention as
# BLOCKED_PUBLISHERS).
OFFICIAL_PR_PUBLISHERS = {
    "globenewswire",         # PR wire
    "pr newswire",           # PR wire
    "prnewswire",            # PR wire (no space variant)
    "businesswire",          # PR wire
    "business wire",         # PR wire (with space)
    "cision",                # PR wire / press release distribution
    "sec edgar",             # SEC filings (set by our collector for filing items)
    "accesswire",            # PR wire
    "newsfile",              # PR wire
    "ein presswire",         # PR wire
    "ein newsdesk",          # PR wire
    "newswire",              # generic catch
}

# SEC CIKs for the 4 publicly-traded entities (10-digit zero-padded).
# Used to hit https://data.sec.gov/submissions/CIK{cik}.json for recent filings.
SEC_CIKS = {
    "Robinhood": "0001783879",
    "SoFi":      "0001818874",
    "Cash App":  "0001512673",   # Block, Inc. parent
    "Chase":     "0000019617",   # JPMorgan Chase parent
}

# Google News RSS search query per brand.
# Format: https://news.google.com/rss/search?q={QUERY}&hl=en-US&gl=US&ceid=US:en
#
# Query guidelines:
#   - Wrap brand name in double-quotes for exact match (avoids false positives
#     like "chime" matching wind chimes).
#   - For ambiguous brand names (Public, Native), add a disambiguator term.
#   - For brands owned by larger entities (Cash App / Block, Chase / JPMorgan),
#     query both names so we catch coverage filed under the parent.
GOOGLE_NEWS_QUERIES = {
    "Acorns":      '"Acorns" (fintech OR investing OR app)',
    "Chime":       '"Chime" (fintech OR banking OR neobank)',
    "Robinhood":   '"Robinhood" (fintech OR investing OR brokerage OR HOOD)',
    "Alinea":      '"Alinea" (investing OR fintech)',
    "Greenlight":  '"Greenlight" (fintech OR debit OR kids OR card)',
    "Cash App":    '"Cash App" OR "CashApp"',
    "Betterment":  '"Betterment" (investing OR fintech)',
    "Wealthfront": '"Wealthfront"',
    "SoFi":        '"SoFi" (banking OR investing OR fintech OR loans)',
    "Kalshi":      '"Kalshi" (prediction OR market OR betting)',
    "Polymarket":  '"Polymarket"',
    "Chase":       '"Chase Bank" OR "JPMorgan Chase"',
}


# Publisher-name blocklist — known low-value sources that pollute Google News results.
# Google News URLs use `news.google.com` as the host (they're redirect URLs), so we
# can't filter by URL domain. Instead we match against the publisher name surfaced in
# the RSS `source` element or parsed from the headline tail ("Headline - Publisher").
#
# Match is case-insensitive substring — "stock titan" matches "Stock Titan" and also
# "stocktitan.net" if the publisher label leaked the domain. Generous on purpose;
# tighten if a real source gets accidentally blocked.
BLOCKED_PUBLISHERS = {
    "covers.com",            # sports betting affiliate promo content
    "rotowire",              # same
    "action network",        # same
    "stock titan",           # algorithmic SEC-filing repackager (we get filings direct from EDGAR)
    "motley fool",           # clickbait
    "nasdaq.com",            # algorithmic stock-data repackages (not the exchange's own PR)
    "seeking alpha",         # paywalled analyst content
    "marketbeat",            # algorithmic stock-tracker repackages
    "benzinga",              # algorithmic news repackages
    "yahoo finance",         # often repackages other sources
    "futu",                  # Chinese broker market-data scraper (also "富途")
    "富途",                  # same, Chinese characters
    "investing.com",         # algorithmic
    "tipranks",              # algorithmic analyst-rating aggregator
    "simply wall st",        # algorithmic
    "simplywall",            # same domain without spaces
    "tradingkey",            # algorithmic stock-movement noise
    "traders union",         # algorithmic trading content
    "etfdailynews",          # algorithmic
    "marketwatch",           # often algorithmic recaps; canonical sources usually better
    "stocktwits",            # social commentary
    "youtube",               # video content, not PR
    "reddit",                # social
    "tradingview",           # algorithmic stock-tracker
    "financefeeds",          # SEO-flavored finance content
    "stockstory",            # algorithmic stock recap
    "techstock",             # algorithmic
}

# Tier-1 publishers: the high-leverage outlets a PR team cares about most.
# Stories from these get a visible badge on the dashboard so eyeballs hit them
# first. Match is lowercased substring against the item's `source` field, so
# both "Reuters" and "reuters.com" hit. Keep this conservative — every name
# added dilutes the signal. Add only outlets the team would actively pitch.
TIER_1_PUBLISHERS = {
    "reuters",
    "bloomberg",
    "the wall street journal",
    "wsj",
    "the new york times",
    "nytimes",
    "associated press",
    "ap news",
    "apnews",
    "cnbc",
    "financial times",
    "ft.com",
    "the washington post",
    "axios",
    "forbes",
    "fortune",
    "barron",
    "business insider",
    "techcrunch",
    "the information",
}


def is_tier_1(publisher: str) -> bool:
    """Return True if the publisher matches a Tier-1 outlet. Case-insensitive
    substring match against TIER_1_PUBLISHERS so domain forms and full-name
    forms both hit."""
    if not publisher:
        return False
    p = publisher.lower()
    return any(t in p for t in TIER_1_PUBLISHERS)

# Title-keyword patterns to drop. Case-insensitive substring match.
# Targets affiliate promo-code spam (huge volume for prediction markets) and
# stock-data noise (options-volume scrapers, price-action recaps).
BLOCKED_TITLE_PATTERNS = [
    "promo code",
    "referral code",
    "invite code",
    "sign-up bonus",
    "sign up bonus",
    "bonus code",
    "deposit $",
    "options spot",
    "spot-on",
    "open interest",
    "implied volatility",
    "unusual options",
    "options volume",
    "options activity",
    "insider transactions",
    "insider trading",
    "price target",
    "downgraded to",
    "upgraded to",
    "consensus estimate",
    "wall street analysts",
    "wall street consensus",
    "etf weekly",
    "should you buy",
    "is now a good time",
    "(6 photos)",            # "Buying X Accounts: A Comprehensive (6 Photos)" SEO spam
    "(4 photos)",
    "buying cash app",       # crypto/payment account spam
    "buy verified",          # account-sale spam
    "for sale",              # account-sale spam
    # Clickbait analyst patterns — surfaced during live-site audit
    "stock guide",           # "Robinhood vs Schwab 2026 Stock Guide"
    "can reach $",           # "Can JPMorgan Stock Reach $400 in 2026?"
    "in focus across",       # "Why Is X Stock in Focus Across Wall Street"
    "stock in focus",        # same pattern
    "best stocks",
    "top stocks",
    "stocks to buy",
    "stocks to watch",
    "stock to watch",
]

# SEC filing forms worth surfacing. 8-K = material events (the gold). 10-Q/10-K
# = quarterly/annual reports. S-1 / S-1/A = registration statements (IPO-watch).
# 424B intentionally EXCLUDED — those are prospectus supplements (note pricings,
# bond issuance boilerplate) and don't have PR value.
SEC_FORMS = {"8-K", "8-K/A", "10-Q", "10-K", "S-1", "S-1/A"}


# Keyword lexicons for heuristic sentiment classification. Used on the Acorns
# section's items to render a positive / negative / neutral badge. Not
# bulletproof (a keyword list never is for news copy), but catches the obvious
# cases: funding rounds + partnerships + wins → positive; lawsuits + fines +
# regulatory probes + outages → negative; ambiguous lands at neutral.
SENTIMENT_POSITIVE = {
    "raises", "raised", "raising", "secures", "secured", "closes", "closed",
    "launches", "launched", "launching", "expands", "expanded", "growth",
    "growing", "wins", "won", "winning", "partners", "partnership", "deal",
    "agreement", "milestone", "record", "best", "top", "leading", "first",
    "exclusive", "premier", "honored", "honors", "awarded", "award", "boost",
    "boosts", "soars", "surges", "rally", "rallies", "beat", "beats",
    "exceeds", "exceeded", "outperforms", "outperformed", "innovative",
    "innovation", "breakthrough", "success", "successful", "approved",
    "acquires", "acquired", "completes", "completed",
}
SENTIMENT_NEGATIVE = {
    "loss", "losses", "lawsuit", "lawsuits", "sue", "sues", "sued", "suing",
    "fraud", "scandal", "investigation", "investigates", "probe", "fine",
    "fined", "penalty", "penalties", "penalized", "violation", "violations",
    "illegal", "warning", "warned", "decline", "declines", "declined",
    "drops", "fell", "plunges", "plunged", "crashed", "weak", "concerns",
    "concern", "concerned", "risk", "risks", "scrutiny", "criticism",
    "criticized", "facing", "faces", "breach", "leak", "outage", "shutdown",
    "layoffs", "layoff", "fired", "fires", "firing", "downgraded",
    "delisted", "bankrupt", "bankruptcy", "default", "defaulted",
    "miss", "missed", "missing", "subpoena", "subpoenaed",
}


# SEC 8-K item codes → plain-English descriptions. Used to turn "Item 5.02"
# into "Leadership change" in the TLDR. Source: SEC Form 8-K instructions.
# https://www.sec.gov/files/form8-k.pdf
SEC_8K_ITEMS = {
    "1.01": "Entered material agreement",
    "1.02": "Terminated material agreement",
    "1.03": "Bankruptcy / receivership",
    "1.04": "Mine safety violation",
    "1.05": "Material cybersecurity incident",
    "2.01": "Completed acquisition or disposition",
    "2.02": "Earnings release / financial results",
    "2.03": "Created direct financial obligation",
    "2.04": "Triggered direct financial obligation",
    "2.05": "Costs from exit / disposal activity",
    "2.06": "Material impairment",
    "3.01": "Listing standard / delisting notice",
    "3.02": "Unregistered equity sales",
    "3.03": "Modified rights of security holders",
    "4.01": "Changed accountant",
    "4.02": "Non-reliance on prior financial statements",
    "5.01": "Change in control",
    "5.02": "Leadership change (officers / directors)",
    "5.03": "Amended charter or bylaws",
    "5.04": "Insider-trading blackout period",
    "5.05": "Amended ethics code",
    "5.06": "Change in shell-company status",
    "5.07": "Shareholder vote results",
    "5.08": "Shareholder nominations notice",
    "6.01": "ABS informational disclosure",
    "6.02": "Change in servicer / trustee",
    "6.03": "Change in credit enhancement",
    "6.04": "Failure to make required distribution",
    "6.05": "Securities act updating disclosure",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other material event",
    "9.01": "Financial statements / exhibits",
}


def google_news_rss_url(brand: str) -> str:
    """Build the Google News RSS URL for a brand's search query."""
    from urllib.parse import quote
    query = GOOGLE_NEWS_QUERIES[brand]
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


def sec_filings_url(brand: str) -> str | None:
    """Build the SEC EDGAR submissions JSON URL for a brand (None if not public)."""
    cik = SEC_CIKS.get(brand)
    if not cik:
        return None
    return f"https://data.sec.gov/submissions/CIK{cik}.json"
