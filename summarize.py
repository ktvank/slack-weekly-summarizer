"""
Weekly Slack article summarizer.

Pulls the last 7 days of messages from a Slack channel, extracts any links
that were shared, records new ones (link, title, date posted) in a Google
Sheet, and posts a summary of the week's links back to Slack.

Run manually with:  python summarize.py
Configure via a .env file locally (see .env.example), or via environment
variables / secrets when run in GitHub Actions.
"""

import html
import os
import re
import sys
import warnings
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = os.environ["SLACK_CHANNEL"]
# `or` (not dict.get's default) because GitHub Actions sets an unset secret's
# env var to "" rather than leaving it absent.
SUMMARY_CHANNEL = os.environ.get("SLACK_SUMMARY_CHANNEL") or SLACK_CHANNEL
GOOGLE_CREDENTIALS_PATH = os.environ["GOOGLE_CREDENTIALS_PATH"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB") or "Articles"
DAYS_BACK = int(os.environ.get("DAYS_BACK") or "7")

LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|[^>]*)?>")
HEADER_ROW = ["Link", "Title", "Date Posted", "Slack Post"]

# Meeting links, not articles — don't record them at all.
IGNORED_DOMAINS = ("zoom.us",)
# Sites that require login and will never have a public title; skip our own
# scrape attempt against them instead of waiting out a timeout.
NO_SCRAPE_DOMAINS = ("slack.com",)

ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", re.IGNORECASE)


def is_ignored_domain(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if any(host == d or host.endswith("." + d) for d in IGNORED_DOMAINS):
        return True
    # The bot's own summary messages link back to this sheet — don't let a
    # future run pick that link back up as if it were a shared article.
    if f"/spreadsheets/d/{GOOGLE_SHEET_ID}" in url:
        return True
    return False


def resolve_channel_id(client: WebClient, channel: str) -> str:
    if channel.startswith(("C", "G")) and channel.isalnum():
        return channel
    name = channel.lstrip("#")
    cursor = None
    while True:
        resp = client.conversations_list(
            types="public_channel", limit=200, cursor=cursor
        )
        for ch in resp["channels"]:
            if ch["name"] == name:
                return ch["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    raise ValueError(f"Could not find a channel named '{channel}'. Is the bot invited to it?")


def fetch_week_messages(client: WebClient, channel_id: str, days: int):
    oldest = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    messages = []
    cursor = None
    while True:
        resp = client.conversations_history(
            channel=channel_id, oldest=str(oldest), limit=200, cursor=cursor
        )
        messages.extend(resp["messages"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages


def extract_links(messages):
    found = []
    seen_on_this_run = set()
    for msg in messages:
        text = msg.get("text", "")
        ts = float(msg.get("ts", 0))
        posted_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

        # Slack unfurls links it recognizes and stores the resolved title on
        # the message's attachments — use that if present, since Slack's own
        # crawler is often let through by sites that block ours.
        slack_titles = {}
        for att in msg.get("attachments", []):
            src = att.get("from_url") or att.get("original_url")
            if src and att.get("title"):
                slack_titles[html.unescape(src)] = att["title"].strip()

        for match in LINK_RE.finditer(text):
            # Slack's message text HTML-escapes URLs, so "&" in a query
            # string comes through as "&amp;" and must be unescaped.
            url = html.unescape(match.group(1))
            if url in seen_on_this_run:
                continue
            if is_ignored_domain(url):
                continue
            seen_on_this_run.add(url)
            found.append({
                "url": url,
                "date": posted_date,
                "slack_title": slack_titles.get(url),
                "message_ts": msg.get("ts"),
            })
    return found


def get_permalink(client: WebClient, channel_id: str, ts: str, cache: dict) -> str:
    if ts in cache:
        return cache[ts]
    try:
        resp = client.chat_getPermalink(channel=channel_id, message_ts=ts)
        cache[ts] = resp["permalink"]
    except SlackApiError as e:
        print(f"Warning: couldn't get permalink for ts={ts}: {e.response['error']}", file=sys.stderr)
        cache[ts] = ""
    return cache[ts]


def title_from_slug(url: str) -> str:
    """Best-effort readable title derived from the URL itself, used when a
    site can't be scraped (e.g. Akamai/PerimeterX-protected news sites that
    stall bot-like requests rather than erroring). News URLs are often
    slugified from their headline, so this gets surprisingly close."""
    path = urlparse(url).path.strip("/")
    segment = path.rsplit("/", 1)[-1] if path else ""
    segment = re.sub(r"\.(html?|php|aspx?)$", "", segment, flags=re.IGNORECASE)
    words = [w for w in re.split(r"[-_]+", segment) if w and not w.isdigit()]
    if words:
        return " ".join(w.capitalize() for w in words)
    return urlparse(url).netloc or url


def fetch_arxiv_title(arxiv_id: str) -> str | None:
    """arxiv.org/abs and /pdf pages either aren't HTML (PDF) or lack a plain
    <title>, so the slug is just the paper ID. Query arXiv's own metadata
    API instead, which is fast, public, and reliable."""
    try:
        resp = requests.get(
            "http://export.arxiv.org/api/query",
            params={"id_list": arxiv_id},
            timeout=8,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        entry = soup.find("entry")
        if entry and entry.find("title"):
            return " ".join(entry.find("title").text.split())
    except Exception:
        pass
    return None


def fetch_title(url: str) -> str:
    host = urlparse(url).netloc.lower()

    arxiv_match = ARXIV_RE.search(url)
    if arxiv_match:
        title = fetch_arxiv_title(arxiv_match.group(1))
        if title:
            return title

    if any(host == d or host.endswith("." + d) for d in NO_SCRAPE_DOMAINS):
        return title_from_slug(url)

    try:
        resp = requests.get(
            url,
            timeout=8,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return og_title["content"].strip()
        twitter_title = soup.find("meta", attrs={"name": "twitter:title"})
        if twitter_title and twitter_title.get("content"):
            return twitter_title["content"].strip()
        if soup.title and soup.title.string:
            return soup.title.string.strip()
    except Exception:
        pass
    return title_from_slug(url)


def get_sheet():
    creds = Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(GOOGLE_SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=GOOGLE_SHEET_TAB, rows=100, cols=len(HEADER_ROW))
        ws.append_row(HEADER_ROW)
    if ws.row_values(1) != HEADER_ROW:
        ws.insert_row(HEADER_ROW, index=1)
    ws.format(f"A1:{chr(ord('A') + len(HEADER_ROW) - 1)}1", {
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        "backgroundColor": {"red": 0.20, "green": 0.25, "blue": 0.33},
    })
    ws.freeze(rows=1)
    set_column_widths(sh, ws, [280, 480, 110, 260])  # Link, Title, Date Posted, Slack Post
    return ws


def set_column_widths(sh, ws, widths_px: list[int]):
    sh.batch_update({
        "requests": [
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": i,
                        "endIndex": i + 1,
                    },
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            }
            for i, width in enumerate(widths_px)
        ]
    })


def add_week_and_rows(ws, week_label: str, data_rows: list[list[str]]):
    """Append a bold/colored week-separator row followed by this week's
    data rows (or a placeholder if there were none), so weeks are visually
    obvious when scrolling the sheet."""
    last_col = chr(ord("A") + len(HEADER_ROW) - 1)
    blank_tail = [""] * (len(HEADER_ROW) - 1)
    next_row = len(ws.get_all_values()) + 1
    rows_to_add = [[week_label] + blank_tail] + (
        data_rows if data_rows else [["(no new links this week)"] + blank_tail]
    )
    ws.append_rows(rows_to_add, value_input_option="USER_ENTERED")
    ws.format(f"A{next_row}:{last_col}{next_row}", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.85, "green": 0.90, "blue": 0.98},
    })
    ws.merge_cells(f"A{next_row}:{last_col}{next_row}")


def main():
    client = WebClient(token=SLACK_BOT_TOKEN)

    channel_id = resolve_channel_id(client, SLACK_CHANNEL)
    summary_channel_id = resolve_channel_id(client, SUMMARY_CHANNEL)

    print(f"Fetching last {DAYS_BACK} days of messages from {SLACK_CHANNEL}...")
    messages = fetch_week_messages(client, channel_id, DAYS_BACK)
    links = extract_links(messages)
    print(f"Found {len(links)} link(s) posted this week.")

    ws = get_sheet()
    all_values = ws.get_all_values()
    already_have = {row[0] for row in all_values[1:] if row}

    permalink_cache = {}
    new_rows = []
    new_entries = []
    for link in links:
        if link["url"] in already_have:
            continue
        title = link["slack_title"] or fetch_title(link["url"])
        permalink = get_permalink(client, channel_id, link["message_ts"], permalink_cache)
        new_rows.append([link["url"], title, link["date"], permalink])
        new_entries.append({"url": link["url"], "title": title, "date": link["date"]})
        already_have.add(link["url"])

    run_end = datetime.now(timezone.utc).date()
    run_start = run_end - timedelta(days=DAYS_BACK)
    week_label = f"Week of {run_start.strftime('%b %d')} – {run_end.strftime('%b %d, %Y')}"
    add_week_and_rows(ws, week_label, new_rows)
    print(f"Recorded '{week_label}' with {len(new_rows)} new row(s).")

    sheet_url = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit"
    if new_entries:
        lines = [f"• <{e['url']}|{e['title']}> — {e['date']}" for e in new_entries]
        summary = (
            f"*Weekly article roundup* ({len(new_entries)} new link"
            f"{'s' if len(new_entries) != 1 else ''} from the last {DAYS_BACK} days):\n"
            + "\n".join(lines)
            + f"\n\n<{sheet_url}|View the full list>"
        )
    else:
        summary = (
            f"*Weekly article roundup*: no new links were shared in the last {DAYS_BACK} days.\n"
            f"<{sheet_url}|View the full list>"
        )

    try:
        client.chat_postMessage(channel=summary_channel_id, text=summary)
        print("Posted weekly summary to Slack.")
    except SlackApiError as e:
        print(f"Failed to post summary to Slack: {e.response['error']}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
