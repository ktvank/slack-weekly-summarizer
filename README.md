# Slack Weekly Article Summarizer

Reads the last 7 days of a Slack channel, pulls out any links people shared,
records new ones in a Google Sheet (link, title, date posted), and posts a
summary back to Slack. Runs on a schedule via GitHub Actions, so it doesn't
depend on any particular machine being on.

## 1. Create the Slack app

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**.
2. Name it (e.g. "Article Bot") and pick your workspace.
3. Under **OAuth & Permissions** → **Scopes** → **Bot Token Scopes**, add:
   - `channels:history` (read public channel messages)
   - `channels:read` (look up channel by name)
   - `chat:write` (post the summary)

   (This build only supports public channels. If you ever do need a private
   channel, add `groups:history`/`groups:read` and change `types=` in
   `resolve_channel_id()` in `summarize.py` back to
   `"public_channel,private_channel"`.)
4. Click **Install to Workspace** at the top of that page, approve, then copy
   the **Bot User OAuth Token** (starts with `xoxb-`).
5. In Slack, go to the target channel and run `/invite @Article Bot` (or
   whatever you named it) so the bot can actually see messages there.

## 2. Create the Google service account (for Sheets access)

1. Go to https://console.cloud.google.com/ → create a project (or use an
   existing one).
2. **APIs & Services → Library** → enable the **Google Sheets API**.
3. **APIs & Services → Credentials** → **Create Credentials** → **Service
   account**. Give it any name, no roles needed, done.
4. Open the new service account → **Keys** → **Add Key** → **Create new key**
   → JSON. This downloads a `.json` file. Keep it — you'll need its full
   contents in step 4 below. (Don't commit it; it's already git-ignored.)
5. Note the service account's email address (looks like
   `something@project-id.iam.gserviceaccount.com`).
6. Create (or open) the Google Sheet you want to use, and **Share** it with
   that service account email as an **Editor**.
7. Copy the sheet's ID from its URL:
   `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`

## 3. Test it locally first

```
cd slack-weekly-summarizer
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`:
- `SLACK_BOT_TOKEN` from step 1
- `SLACK_CHANNEL` (e.g. `#articles`)
- `GOOGLE_CREDENTIALS_PATH` → path to the downloaded service account JSON
  (e.g. save it as `service-account.json` in this folder)
- `GOOGLE_SHEET_ID` from step 2

Run it:

```
venv\Scripts\python.exe summarize.py
```

The first run creates a header row (`Link | Title | Date Posted`) in the
`Articles` tab if it doesn't exist yet, adds any links from the last 7 days,
and posts a summary message to the channel. Re-running it won't duplicate
links already in the sheet.

## 4. Push to GitHub and set up the scheduled run

This repo includes `.github/workflows/weekly-summary.yml`, which runs the
script every Monday at 13:00 UTC via GitHub Actions — no local machine
needs to be powered on.

1. Create a GitHub repo and push this folder to it (private repo is fine —
   GitHub Actions works the same either way, and free minutes are more than
   enough for a once-a-week job).
2. In the repo, go to **Settings → Secrets and variables → Actions → New
   repository secret** and add:
   - `SLACK_BOT_TOKEN` — from step 1
   - `SLACK_CHANNEL` — e.g. `#articles`
   - `SLACK_SUMMARY_CHANNEL` — optional; only add it if you want the summary
     posted somewhere other than `SLACK_CHANNEL`
   - `GOOGLE_SHEET_ID` — from step 2
   - `GOOGLE_SHEET_TAB` — optional; only add it if you don't want the
     default tab name `Articles`
   - `GOOGLE_CREDENTIALS_JSON` — paste the **entire contents** of the
     service account JSON key file as one secret
3. That's it. Check **Actions** tab → the workflow will run automatically
   on schedule, or click **Run workflow** to trigger it manually and verify
   it works end to end.

To change the schedule, edit the `cron` line in
`.github/workflows/weekly-summary.yml` (cron times are in UTC).

## Notes / easy extensions

- "Date posted" is the date the link was shared in Slack, not the article's
  own publish date (that's unreliable to scrape across sites).
- Titles are picked in this order: Slack's own link-preview title (from the
  message's unfurl data), then our own scrape of the page, then — if a site
  blocks scraping outright (e.g. Washington Post/NYT-style bot detection
  that stalls the request rather than erroring) — a readable title guessed
  from the URL's slug.
- Each run adds a bold, colored "Week of ..." row to the sheet before that
  week's links, so weeks are easy to tell apart at a glance — even in weeks
  with nothing new (it adds a "(no new links this week)" placeholder row).
- The weekly Slack summary includes a link back to the full sheet. For that
  link to actually work for other people in the channel, open the Sheet →
  **Share** → change general access to **Anyone with the link** (Viewer) —
  otherwise they'll hit a "request access" wall.
- Want to also record who shared each link? Add `msg.get("user")` in
  `extract_links()` in `summarize.py`, resolve it with
  `client.users_info(user=...)`, and add a column.
- GitHub Actions' cron doesn't auto-adjust for daylight saving; nudge the
  UTC hour twice a year if the exact local time matters to you.
