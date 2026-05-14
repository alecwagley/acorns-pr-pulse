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

import json
import os
import smtplib
import sys
from collections import Counter
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "current.json"

RECIPIENT = "jinny.davoudi@acorns.com"
SUBJECT = "Acorns PR Pulse, daily update"


def build_tldr() -> str:
    """Pick 3-4 standout items from today's data to summarize the dashboard.

    Selection rules (prioritized):
      1. Acorns items first — anything in their own feed matters most.
      2. Most-syndicated story across competitors (highest group_size).
      3. Loudest negative-sentiment story across competitors.
      4. Loudest positive-sentiment story across competitors.

    Skip any selection that would duplicate one we already picked.
    """
    try:
        data = json.loads(DATA_PATH.read_text())
    except Exception:
        return ""

    bullets: list[str] = []
    picked_urls: set[str] = set()

    def add(item: dict, prefix: str = "") -> None:
        if item.get("url") in picked_urls:
            return
        picked_urls.add(item.get("url", ""))
        title = item.get("title", "")[:120]
        brand = item.get("brand", "")
        n = item.get("group_size", 1)
        if n > 1:
            tail = f" ({n} outlets covering)"
        else:
            tail = ""
        bullets.append(f"• {prefix}[{brand}] {title}{tail}")

    # 1. Acorns items
    acorns_items = [i for i in data if i.get("brand") == "Acorns"]
    for item in acorns_items[:1]:
        add(item)

    competitors = [i for i in data if i.get("brand") != "Acorns"]

    # 2. Most-syndicated competitor story. Skip items that are clearly
    # procedural-filing-syndication (e.g. "Guarantor: ...", "Form FWP ...")
    # which inflate group counts without being real news.
    def is_substantive(item: dict) -> bool:
        title = item.get("title", "").lower()
        if title.startswith(("guarantor:", "form ", "amendment ")):
            return False
        if "8-k filing" in title or "10-q filing" in title or "10-k filing" in title:
            return False
        return len(title) >= 30
    grouped = sorted(
        [c for c in competitors if is_substantive(c)],
        key=lambda x: -x.get("group_size", 1),
    )
    for item in grouped[:1]:
        if item.get("group_size", 1) > 1:
            add(item)

    # 3. Loudest negative
    negatives = sorted(
        [i for i in competitors if i.get("sentiment") == "negative"],
        key=lambda x: -x.get("group_size", 1),
    )
    for item in negatives[:1]:
        add(item, prefix="▼ ")

    # 4. Loudest positive
    positives = sorted(
        [i for i in competitors if i.get("sentiment") == "positive"],
        key=lambda x: -x.get("group_size", 1),
    )
    for item in positives[:1]:
        add(item, prefix="▲ ")

    return "\n".join(bullets[:4])


def build_body() -> str:
    tldr = build_tldr()
    tldr_block = ""
    if tldr:
        tldr_block = f"\nTLDR. What's notable today:\n{tldr}\n"

    return (
        "Hey Jinny, daily has been updated. Check it out!\n"
        + tldr_block
        + "\nhttps://acorns-pr.vscrl.co\n"
        "User: acorns-pr\n"
        "Pass: Kq97KeaXKY2GdoET\n"
        "\n"
        "Yell if anything is off!\n"
        "\n"
        "Alec\n"
    )


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
    msg.set_content(build_body())

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
