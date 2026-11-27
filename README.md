# Snapback

A free alert bot for large US stocks. Every 15 minutes on weekdays it checks the
S&P 500 and Nasdaq 100 (510 stocks, listed in `universe.csv`) and sends a Telegram
message when something important happens. Each message is written to be pasted
into Claude, which then writes the full analysis.

It gives facts only, never a trade call.

## What triggers an alert

**1. Big price moves** (only while the US market is open)
- A stock down 7% or more, or up 7% or more, today versus yesterday's close.
- Alerted once per stock per day, and again only if the move grows by another 5 points.
- If more than 10 stocks trigger at once (a market-wide day), you get one summary instead.

Each price alert contains: the move, price, volume against normal, the S&P 500's move,
same-sector stocks also down 4% or more, a first-pass drop type, any SEC 8-K filings from
the last 3 days, recent headlines with links, and (for drops) the backtest base rates.

**2. Important SEC filings** (also before the open)
A new 8-K from one of these companies with one of these items:

| Item | Meaning |
|---|---|
| 1.01 / 1.02 | Entered or ended a material agreement (routine loan deals are skipped) |
| 1.03 | Bankruptcy or receivership |
| 1.05 | Material cybersecurity incident |
| 2.01 | Completed an acquisition or sale of assets |
| 2.04 | Event that accelerates a debt obligation |
| 2.05 | Restructuring or exit costs |
| 2.06 | Material impairment |
| 3.01 | Delisting notice |
| 4.01 | Change of auditor |
| 4.02 | Past financial statements can no longer be relied on |
| 5.01 | Change in control |
| 5.02 | CEO, CFO, COO or President leaving (board and pay changes are skipped) |

Earnings releases (item 2.02) are not alerted on their own; the Claude morning briefing covers earnings.

## Where the data comes from (all free)

- Prices: Yahoo Finance (via the `yfinance` library)
- Filings: SEC EDGAR full-text search
- Headlines: Yahoo Finance and Google News RSS feeds

## Setup

1. **Telegram bot.** In Telegram, message @BotFather, send `/newbot` and copy the token.
   Send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the `chat` `id`.
2. **Secrets.** In this repository: Settings > Secrets and variables > Actions > New repository secret.
   - `TELEGRAM_TOKEN` - the bot token
   - `TELEGRAM_CHAT_ID` - the chat id
   - `SEC_USER_AGENT` - your name and an email address, for example `Jane Doe jane@example.com`.
     The SEC requires automated tools to identify themselves this way; it is only sent to sec.gov.
3. **Test.** Actions tab > Snapback scan > Run workflow, with mode `test`. You should get a
   Telegram message within a minute. Mode `dry` prints what it would send to the run log.

After that it runs on its own schedule.

## Settings

Repository variables (Settings > Secrets and variables > Actions > Variables):
- `DROP_PCT` - size of drop that triggers a price alert, in percent (default 7)
- `UP_PCT` - size of rise that triggers a price alert, in percent (default 7)

Other settings are listed at the top of `snapback.py`.

## Using it with Claude

Copy a whole alert message and paste it into a Claude chat. The "Snapback alert analysis"
skill turns it into the full picture: what happened, why it matters, what the headline leaves
out, who else is affected, the drop type with its historical base rates, and what to watch next.

## Costs and limits

- Free. GitHub gives private repositories 2,000 Actions minutes a month; this schedule uses
  roughly 1,000-1,300. Making the repository public removes the limit.
- GitHub's scheduler can start runs a few minutes late, and Yahoo prices can lag up to 15 minutes,
  so expect alerts 5-30 minutes after a move.
- Yahoo occasionally blocks requests; a failed price scan is retried by the next run.
- `universe.csv` is a fixed list; update it when index membership changes.
- `state.json` records what has already been alerted, so nothing is sent twice. Deleting it
  makes the next run treat all recent filings as already seen (no flood of old alerts).
