#!/usr/bin/env python3
"""
Daily notification: emails Jinny at Acorns after each dashboard refresh.

Run after pipeline/generate.py in the daily cron. Uses Gmail SMTP with an app
password (no API key, no third-party service). Env vars required:
    GMAIL_USER     — sender Gmail address (e.g. alec@vscrl.co)
    GMAIL_APP_PASSWORD — 16-char app password from
                         https://myaccount.google.com/apppasswords

If either var is missing, the script logs a notice and exits 0 (so a missing
secret doesn't fail the workflow — dashboard still refreshes, just no email).
"""
from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage

RECIPIENT = "jinny.davoudi@acorns.com"
SUBJECT = "Acorns PR Pulse — daily update"
BODY = """Hey Jinny — daily has been updated. Check it out!

https://acorns-pr.vscrl.co
User: acorns-pr
Pass: Kq97KeaXKY2GdoET

Alec
"""


def main() -> int:
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        print("notify: GMAIL_USER or GMAIL_APP_PASSWORD not set; skipping email send.")
        print("To enable daily emails: add both to ~/.config/vscrl/secrets.env, then re-run deploy_full.sh.")
        return 0

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = RECIPIENT
    msg["Subject"] = SUBJECT
    msg.set_content(BODY)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(user, pw)
            smtp.send_message(msg)
        print(f"notify: sent daily email to {RECIPIENT}")
        return 0
    except Exception as e:
        print(f"notify: email send FAILED: {e}", file=sys.stderr)
        # Non-zero exit so the workflow surfaces the failure but doesn't roll back
        # the (successful) dashboard refresh that already committed.
        return 1


if __name__ == "__main__":
    sys.exit(main())
