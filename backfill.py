"""
One-time backfill: pull the *entire* available Slack channel history, find
every article link ever shared, and rebuild the Google Sheet from scratch
with a week-separator row above each week that actually had links.

This does NOT post anything to Slack — sheet only. Safe to re-run; it always
rebuilds from the full history rather than appending.

Run with:  venv\\Scripts\\python.exe backfill.py

Note: on Slack's free plan, channel history is limited to the last ~90 days
regardless of how far back this script asks — that's a Slack-side limit,
not something this script can work around.
"""

import sys
import time
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import gspread
from google.oauth2.service_account import Credentials
from slack_sdk import WebClient

from summarize import (
    GOOGLE_CREDENTIALS_PATH,
    GOOGLE_SHEET_ID,
    GOOGLE_SHEET_TAB,
    HEADER_ROW,
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL,
    add_week_and_rows,
    extract_links,
    fetch_title,
    get_permalink,
    resolve_channel_id,
    set_column_widths,
)


def fetch_all_messages(client: WebClient, channel_id: str):
    messages = []
    cursor = None
    while True:
        resp = client.conversations_history(channel=channel_id, limit=200, cursor=cursor)
        messages.extend(resp["messages"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.5)  # be gentle with Slack's rate limits over a long history
    return messages


def most_recent_thursday(d):
    return d - timedelta(days=(d.weekday() - 3) % 7)


def style_header(sh, ws):
    last_col = chr(ord("A") + len(HEADER_ROW) - 1)
    ws.format(f"A1:{last_col}1", {
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        "backgroundColor": {"red": 0.20, "green": 0.25, "blue": 0.33},
    })
    ws.freeze(rows=1)
    set_column_widths(sh, ws, [280, 480, 110, 260])  # Link, Title, Date Posted, Slack Post


def main():
    client = WebClient(token=SLACK_BOT_TOKEN)
    channel_id = resolve_channel_id(client, SLACK_CHANNEL)

    print(f"Fetching full available history from {SLACK_CHANNEL}...")
    messages = fetch_all_messages(client, channel_id)
    messages.sort(key=lambda m: float(m.get("ts", 0)))
    print(f"Fetched {len(messages)} message(s).")

    links = extract_links(messages)
    print(f"Found {len(links)} unique link(s) across all history.")

    if not links:
        print("Nothing to backfill.")
        return

    permalink_cache = {}
    entries = []
    for i, link in enumerate(links, start=1):
        title = link["slack_title"] or fetch_title(link["url"])
        permalink = get_permalink(client, channel_id, link["message_ts"], permalink_cache)
        entries.append({
            "url": link["url"], "title": title, "date": link["date"], "permalink": permalink,
        })
        print(f"  [{i}/{len(links)}] {link['date']}  {title[:70]}")

    # Only backfill weeks up through the most recent past Thursday — the live
    # weekly job's next scheduled run will naturally cover anything newer
    # than that, so we don't want to double up.
    anchor = most_recent_thursday(datetime.now(timezone.utc).date())
    entries = [
        e for e in entries
        if datetime.strptime(e["date"], "%Y-%m-%d").date() <= anchor
    ]

    oldest_date = min(datetime.strptime(e["date"], "%Y-%m-%d").date() for e in entries)
    bins = []
    cur_end = anchor
    while True:
        cur_start = cur_end - timedelta(days=7)
        bins.append((cur_start, cur_end))
        if cur_start <= oldest_date:
            break
        cur_end = cur_start
    bins.reverse()  # oldest week first

    # Rebuild into a fresh worksheet, then swap it in, so leftover formatting
    # / merged cells from earlier test runs can't cause write errors.
    creds = Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)

    try:
        old_ws = sh.worksheet(GOOGLE_SHEET_TAB)
    except gspread.WorksheetNotFound:
        old_ws = None

    new_ws = sh.add_worksheet(title=f"{GOOGLE_SHEET_TAB}_rebuild", rows=100, cols=len(HEADER_ROW))
    new_ws.append_row(HEADER_ROW)
    style_header(sh, new_ws)

    week_count = 0
    for start, end in bins:
        week_entries = [
            e for e in entries
            if start < datetime.strptime(e["date"], "%Y-%m-%d").date() <= end
        ]
        if not week_entries:
            continue
        week_entries.sort(key=lambda e: e["date"])
        rows = [[e["url"], e["title"], e["date"], e["permalink"]] for e in week_entries]
        label = f"Week of {start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
        add_week_and_rows(new_ws, label, rows)
        week_count += 1

    if old_ws is not None:
        sh.del_worksheet(old_ws)
    new_ws.update_title(GOOGLE_SHEET_TAB)

    print(f"Backfill complete: {week_count} week(s), {len(entries)} link(s) total.")


if __name__ == "__main__":
    main()
