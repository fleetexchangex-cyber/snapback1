# Snapback

Sends a Telegram alert when a large US stock drops sharply. It does not make trade calls.

## What triggers an alert

- The stock is down 7% or more from yesterday's close (setting: `DROP_PCT`)
- and the drop is at least 3x the stock's normal daily move (setting: `SIGMA`)
- Universe: 510 stocks, the S&P 500 plus the Nasdaq 100 (`universe.csv`)
- Scans every 15 minutes during US market hours, Monday to Friday
- One alert per stock per day, repeated only if it falls another 5 points
- If more than 10 stocks trigger in one scan, you get a single summary message

## What each alert says

- Size of the drop, price, day low, volume versus average
- How the S&P 500 and the stock's industry peers are doing
- Type: earnings reaction, sector-wide, company-specific, or market-wide
- One line on what similar drops did historically (2014 to Nov 2025 backtest)
- Top three headlines

## Setup (about 10 minutes)

1. In Telegram, message @BotFather, send `/newbot`, and copy the token it gives you.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the `chat` `id` number.
3. Create a new private GitHub repository and upload everything in this folder,
   including the `.github` folder.
4. In the repository: Settings > Secrets and variables > Actions > New repository secret.
   Add `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.
5. Open the Actions tab, enable workflows, choose "Snapback scan" and click "Run workflow" once to check it.

After that it runs on its own schedule.

## Changing settings

In the repository: Settings > Secrets and variables > Actions > Variables, add `DROP_PCT`
(for example `5` or `10`). Other settings are environment variables listed at the top of `snapback.py`.

## Running it by hand

```
pip install -r requirements.txt
DRY_RUN=1 python snapback.py --last-session   # print alerts for the most recent session
python snapback.py --test                     # send a test message to Telegram
```

## Limits

- Prices come from Yahoo Finance and can lag by up to 15 minutes; GitHub's scheduler can add a few more.
- Regular hours only. An overnight gap is alerted at the first scan after the open.
- Yahoo sometimes throttles requests. A throttled scan fails and the next one retries.
- The universe is a fixed list; refresh `universe.csv` when index membership changes.
