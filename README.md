# Restock Notifier — Setup Guide

Checks Amazon products every 5 minutes. Alerts you on Telegram + Gmail when a
product is back in stock AND at/under the price you set. Fully free.

## 1. Create the GitHub repo

1. Go to github.com → New repository → name it (e.g. `restock-notifier`) → can be Private.
2. Upload all files from this folder into the repo (drag-and-drop on github.com works,
   or `git push` if you're comfortable with git).

## 2. Turn on GitHub Pages (for the UI)

1. In the repo: Settings → Pages.
2. Source: "Deploy from a branch" → Branch: `main`, folder `/ (root)`.
3. Save. After a minute your UI is live at:
   `https://<your-username>.github.io/<repo-name>/`

## 3. Create a GitHub Personal Access Token (for the UI to edit the watchlist)

1. GitHub → Settings (your account, not the repo) → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token.
2. Repository access: "Only select repositories" → pick this repo.
3. Permissions: **Contents → Read and write**, and **Actions → Read and write**
   (Actions access is only needed for the "Send test notification" button).
4. Generate, copy the token (you won't see it again) — you'll paste this
   into the UI's setup screen the first time you open it.

## 4. Set up Telegram

1. In Telegram, message **@BotFather** → `/newbot` → follow prompts → copy the **bot token**.
2. Message your new bot anything (so it can message you back).
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser,
   find `"chat":{"id":...}` in the response — that number is your **chat ID**.

## 5. Set up Gmail

1. Google Account → Security → turn on 2-Step Verification (if not already on).
2. Security → App passwords → create one for "Mail" → copy the 16-character password.

## 6. Add secrets to the GitHub repo (for the checker script)

Repo → Settings → Secrets and variables → Actions → New repository secret.
Add each of these:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | bot token from step 4 |
| `TELEGRAM_CHAT_ID` | chat ID from step 4 |
| `GMAIL_ADDRESS` | your Gmail address |
| `GMAIL_APP_PASSWORD` | app password from step 5 |

(Optional: `GMAIL_TO` if you want alerts sent to a different address than
`GMAIL_ADDRESS`.)

## 7. Use it

1. Open your GitHub Pages URL from step 2.
2. First time: set a 4-digit passcode, enter your GitHub username, repo name,
   and the token from step 3.
3. Add a product: paste an ASIN or Amazon link, set max price, pick duration.
4. GitHub Actions checks every 5 minutes automatically — no button to press.
   You can watch it run under the repo's "Actions" tab.

## Troubleshooting a 404 on save

If "Add to watchlist" fails with a 404, use the new **"Test GitHub connection"**
button on the page — it shows GitHub's actual error message, which usually
points to one of:

- Owner/repo typed wrong in setup (case-sensitive, must match exactly)
- `watchlist.json` not at the root of the repo
- Token not scoped to this repo, or missing the Contents permission
- Repo name mismatch between the Pages URL and what you typed into setup

## Notes / limits

- Amazon's page structure changes over time and sometimes blocks repeated
  automated requests — if checks start failing, the script logic (price/stock
  parsing in `monitor.py`) may need small updates.
- 5 minutes is the practical minimum interval for GitHub Actions' free
  scheduler — actual runs can occasionally lag a few extra minutes during
  GitHub's busy periods.
- Everything here is free: GitHub Actions, GitHub Pages, Telegram, and Gmail
  SMTP all have free tiers that comfortably cover this use case.
