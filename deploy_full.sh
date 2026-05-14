#!/usr/bin/env bash
# End-to-end Acorns PR Pulse deploy: GitHub repo create + push, Vercel project +
# env + deploy + domain, GoDaddy CNAME. Adapted from the canonical pulse-template
# script; this variant skips APIFY_API_TOKEN setup because the PR pulse uses
# only free unauthenticated sources (Google News RSS + SEC EDGAR).
#
# Prereqs:
#   - ~/.config/vscrl/secrets.env with these vars set:
#       GITHUB_TOKEN, VERCEL_TOKEN, VERCEL_SCOPE,
#       GODADDY_API_KEY, GODADDY_API_SECRET
#   - gh CLI authenticated (gh auth status)
#   - Repo already initialized + committed locally

set -euo pipefail

# ============ EDIT PER DEPLOY ============
REPO_NAME="acorns-pr-pulse"
SUBDOMAIN="acorns-pr"
APEX="vscrl.co"
# Optional: set SITE_PASSWORD before running, otherwise auto-generated
SITE_PASSWORD="${SITE_PASSWORD:-}"
# =========================================

set -a; source "$HOME/.config/vscrl/secrets.env"; set +a
for var in GITHUB_TOKEN VERCEL_TOKEN VERCEL_SCOPE GODADDY_API_KEY GODADDY_API_SECRET; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: $var not set in ~/.config/vscrl/secrets.env"
    echo "  Add it there. Don't put it in this script or any committed file."
    exit 1
  fi
done

GH_OWNER="$(gh api user --jq .login)"
[[ -z "$GH_OWNER" ]] && { echo "ERROR: gh CLI not authed"; exit 1; }

if [[ -z "$SITE_PASSWORD" ]]; then
  # Generate 16-char alphanumeric password. Using python3 avoids the SIGPIPE-with-pipefail
  # gotcha that hits `tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16` (tr writes to a
  # closed pipe after head exits, which pipefail surfaces as exit 141).
  SITE_PASSWORD="$(python3 -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(16)))")"
fi

echo "=== Deploy plan ==="
echo "  Repo:         $GH_OWNER/$REPO_NAME (private)"
echo "  Subdomain:    $SUBDOMAIN.$APEX"
echo "  Username:     $SUBDOMAIN"
echo "  Password:     $SITE_PASSWORD"
echo ""

# 1. GitHub repo create (no-op if it already exists)
if ! gh repo view "$GH_OWNER/$REPO_NAME" >/dev/null 2>&1; then
  gh repo create "$REPO_NAME" --private --source=. --remote=origin --push
else
  echo "✓ GitHub repo already exists. Pushing latest..."
  git push -u origin main 2>&1 | tail -3 || true
fi

# 2. GitHub Actions secret for ANTHROPIC_API_KEY (optional — LLM sentiment).
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  gh secret set ANTHROPIC_API_KEY --repo "$GH_OWNER/$REPO_NAME" --body "$ANTHROPIC_API_KEY"
  echo "✓ ANTHROPIC_API_KEY set on GitHub Actions (LLM sentiment enabled)"
fi

# 2b. GitHub Actions secrets for Gmail SMTP (daily email-to-Jinny step).
if [[ -n "${GMAIL_USER:-}" && -n "${GMAIL_APP_PASSWORD:-}" ]]; then
  gh secret set GMAIL_USER --repo "$GH_OWNER/$REPO_NAME" --body "$GMAIL_USER"
  gh secret set GMAIL_APP_PASSWORD --repo "$GH_OWNER/$REPO_NAME" --body "$GMAIL_APP_PASSWORD"
  echo "✓ GMAIL_USER + GMAIL_APP_PASSWORD set (daily email enabled)"
else
  echo "⚠ GMAIL_USER and/or GMAIL_APP_PASSWORD missing from secrets.env — daily email won't send."
  echo "  To enable: 1) generate an app password at https://myaccount.google.com/apppasswords"
  echo "             2) add to ~/.config/vscrl/secrets.env:"
  echo "                GMAIL_USER=alec@vscrl.co"
  echo "                GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx"
  echo "             3) re-run this script"
fi

# 3. Vercel project (idempotent)
EXISTING=$(curl -sS "https://api.vercel.com/v10/projects/$REPO_NAME" \
  -H "Authorization: Bearer $VERCEL_TOKEN" \
  | python3 -c "import sys,json,re;r=sys.stdin.read();c=re.sub(r'[\\x00-\\x1f]',' ',r);d=json.loads(c);print(d.get('id',''))" 2>/dev/null || true)

if [[ -n "$EXISTING" && "$EXISTING" != "" ]]; then
  PROJECT_ID="$EXISTING"
  echo "✓ Vercel project already exists: $PROJECT_ID"
else
  PROJECT_ID=$(curl -sS -X POST "https://api.vercel.com/v10/projects" \
    -H "Authorization: Bearer $VERCEL_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$REPO_NAME\",\"gitRepository\":{\"type\":\"github\",\"repo\":\"$GH_OWNER/$REPO_NAME\"},\"outputDirectory\":\"dashboard\"}" \
    | python3 -c "import sys,json,re;r=sys.stdin.read();c=re.sub(r'[\\x00-\\x1f]',' ',r);print(json.loads(c).get('id',''))")
  echo "✓ Vercel project created: $PROJECT_ID"
fi
[[ -z "$PROJECT_ID" ]] && { echo "ERROR: project ID empty"; exit 1; }

# 4. SITE_PASSWORD env var
curl -sS -X POST "https://api.vercel.com/v10/projects/$PROJECT_ID/env" \
  -H "Authorization: Bearer $VERCEL_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"key\":\"SITE_PASSWORD\",\"value\":\"$SITE_PASSWORD\",\"target\":[\"production\",\"preview\",\"development\"],\"type\":\"encrypted\"}" \
  > /dev/null
echo "✓ SITE_PASSWORD env var set"

# 5. Trigger production deployment
DEPLOY_JSON=$(curl -sS -X POST "https://api.vercel.com/v13/deployments" \
  -H "Authorization: Bearer $VERCEL_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"$REPO_NAME\",\"project\":\"$PROJECT_ID\",\"target\":\"production\",\"gitSource\":{\"type\":\"github\",\"org\":\"$GH_OWNER\",\"repo\":\"$REPO_NAME\",\"ref\":\"main\"}}")
DEPLOY_URL=$(echo "$DEPLOY_JSON" | python3 -c "import sys,json,re;r=sys.stdin.read();c=re.sub(r'[\\x00-\\x1f]',' ',r);print(json.loads(c).get('url',''))")
echo "✓ Deploy triggered: https://$DEPLOY_URL"

# 6. Add custom domain
curl -sS -X POST "https://api.vercel.com/v10/projects/$PROJECT_ID/domains" \
  -H "Authorization: Bearer $VERCEL_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"$SUBDOMAIN.$APEX\"}" > /dev/null
echo "✓ Custom domain $SUBDOMAIN.$APEX added to project"

# 7. GoDaddy CNAME
curl -sS -X PUT "https://api.godaddy.com/v1/domains/$APEX/records/CNAME/$SUBDOMAIN" \
  -H "Authorization: sso-key $GODADDY_API_KEY:$GODADDY_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '[{"data":"cname.vercel-dns.com","ttl":3600}]' \
  -w "[HTTP %{http_code}]\n"
echo "✓ GoDaddy CNAME set: $SUBDOMAIN.$APEX -> cname.vercel-dns.com"

echo ""
echo "============================================================"
echo "DEPLOY COMPLETE"
echo "  Live URL:  https://$SUBDOMAIN.$APEX  (DNS propagates 1-5 min)"
echo "  Username:  $SUBDOMAIN"
echo "  Password:  $SITE_PASSWORD"
echo "============================================================"
