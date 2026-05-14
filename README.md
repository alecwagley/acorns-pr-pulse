# Acorns PR Pulse

Weekly competitive PR intelligence dashboard for Acorns' PR team. Tracks press releases, news coverage, and SEC filings for 11 fintech competitors.

**Live:** https://acorns-pr.vscrl.co (HTTP Basic Auth, user: `acorns-pr`)
**Refresh:** Mondays at 7am ET (GitHub Actions cron → Vercel auto-deploy)

## Brands tracked

Chime · Robinhood · Alinea · Greenlight · Cash App · Betterment · Wealthfront · SoFi · Kalshi · Polymarket · Chase Bank

## Data sources

- **Google News RSS** per brand (free, wide net — catches official PR, tech press coverage, regulatory news, etc.)
- **SEC EDGAR JSON** for the 4 publicly-traded entities (Robinhood, SoFi, Block/Cash App, JPMorgan Chase) — pulls 8-K material events + 10-Q/10-K reports

Cost: $0/week. No API keys required.

## Project structure

- `pipeline/sources.py` — per-brand source config + noise filters (blocked publishers, title patterns)
- `pipeline/collect.py` — fetch + normalize + dedupe → writes `data/current.json`
- `pipeline/generate.py` — render HTML from `data/current.json` → writes `dashboard/index.html` + 11 per-brand pages
- `dashboard/` — static HTML served by Vercel
- `middleware.js` — Vercel Edge Middleware HTTP Basic Auth
- `deploy_full.sh` — one-command deploy (repo + Vercel + DNS)
- `.github/workflows/weekly.yml` — Monday 7am ET cron

## Local development

```bash
pip3 install -r pipeline/requirements.txt
python3 pipeline/collect.py     # fetch fresh data (1-2 min)
python3 pipeline/generate.py    # render dashboard
open dashboard/index.html       # preview locally
```

## Tuning noise

The biggest editorial decision in this project is the noise filter. Google News surfaces a lot of algorithmic stock-data, promo-code spam, and aggregator repackaging. Filtering happens in `pipeline/sources.py`:

- `BLOCKED_PUBLISHERS` — case-insensitive substring match against the publisher name
- `BLOCKED_TITLE_PATTERNS` — case-insensitive substring match against the article title
- `BRAND_TITLE_TERMS` (in `collect.py`) — relevance check; brand name must appear in the title

When the PR team flags noise that slipped through, add patterns here and re-run.

## Cadence

The weekly cron in `.github/workflows/weekly.yml` runs the collector + generator every Monday at 7am ET, commits the new `data/current.json` + `dashboard/*.html`, and Vercel auto-deploys the change. No manual intervention required.

To trigger a manual refresh: GitHub → Actions → "Weekly PR Refresh" → Run workflow.
