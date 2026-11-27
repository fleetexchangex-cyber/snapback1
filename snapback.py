"""Snapback - free market alert bot for large US stocks.

Every run it checks two things and sends a Telegram message for each new event:

1. Big price moves: an S&P 500 / Nasdaq 100 stock up or down DROP_PCT (default 7%)
   or more today versus yesterday's close (only while the US market is open).
2. Important SEC filings: a new 8-K from one of those companies with a material item
   (CEO/CFO change, deal, bankruptcy, accounting restatement, impairment, ...).

Each message is written so it can be pasted straight into Claude for a full analysis.
It gives facts only, never a trade call.

Environment variables:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  required unless DRY_RUN=1
  SEC_USER_AGENT     "Your Name your@email.com" - the SEC requires a contact in the user agent
  DROP_PCT           move size that triggers a price alert, in percent (default 7)
  PEER_PCT           a peer counts as "also down" at this fall, in percent (default 4)
  REALERT_STEP       alert again on the same day if the move grows by this many points (default 5)
  DIGEST_OVER        more price alerts than this in one run -> one summary message (default 10)
  DRY_RUN=1          print messages instead of sending them
Command line:
  python snapback.py            normal run
  python snapback.py --test     send a test message to Telegram
  python snapback.py --force    run the price scan even if the market is closed (uses last session)
"""
import json
import math
import os
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
NY = ZoneInfo("America/New_York")
STHLM = ZoneInfo("Europe/Stockholm")

DROP_PCT = float(os.getenv("DROP_PCT", "7"))
PEER_PCT = float(os.getenv("PEER_PCT", "4"))
REALERT_STEP = float(os.getenv("REALERT_STEP", "5"))
DIGEST_OVER = int(os.getenv("DIGEST_OVER", "10"))
DRY_RUN = os.getenv("DRY_RUN") == "1"
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
def _clean_header(value):
    """HTTP headers must be plain ASCII on one line: turn a/a/o-style letters into a/a/o and drop the rest."""
    import unicodedata
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return " ".join(value.split())


SEC_UA = _clean_header(os.getenv("SEC_USER_AGENT")) or "Snapback alert bot admin@example.com"

# 8-K items worth an alert. Routine items (results 2.02, Reg FD 7.01, other events 8.01,
# exhibits 9.01, shareholder votes 5.07, bylaws 5.03) are skipped to keep the noise down.
IMPORTANT_ITEMS = {
    "1.01": "Entered a material agreement",
    "1.02": "Ended a material agreement",
    "1.03": "Bankruptcy or receivership",
    "1.05": "Material cybersecurity incident",
    "2.01": "Completed an acquisition or sale of assets",
    "2.04": "Event that accelerates a debt obligation",
    "2.05": "Restructuring or exit costs (often layoffs)",
    "2.06": "Material impairment (write-down)",
    "3.01": "Delisting notice or failure to meet listing rules",
    "4.01": "Change of auditor",
    "4.02": "Past financial statements can no longer be relied on",
    "5.01": "Change in control of the company",
    "5.02": "Director or senior officer departure or appointment",
}
ITEM_NAMES = {**IMPORTANT_ITEMS, "2.02": "Results of operations (earnings)",
              "7.01": "Regulation FD disclosure", "8.01": "Other events", "9.01": "Exhibits"}

BACKTEST = (
    "Backtest base rates (S&P 500 + Nasdaq 100 stocks, 2014 to Nov 2025, 10 trading days "
    "after the drop vs the S&P 500): earnings drops -0.4%, 45% up; collateral drops 7%+ "
    "+1.6% to +2.6%, 56-57% up; company-specific roughly flat, about half up; headline-risk "
    "drops over 10% +0.4%, 39% up. About half fell another 5%+ first. Small samples."
)


# ---------------------------------------------------------------- helpers

def log(msg):
    print(f"[{datetime.now(STHLM):%H:%M:%S}] {msg}", flush=True)


def load_state():
    today = datetime.now(NY).date().isoformat()
    state = {"date": today, "price_alerted": {}, "filings_seen": []}
    if STATE_FILE.exists():
        try:
            old = json.loads(STATE_FILE.read_text())
            state["filings_seen"] = old.get("filings_seen", [])[-2000:]
            if old.get("date") == today:
                state["price_alerted"] = old.get("price_alerted", {})
        except Exception as e:  # corrupt file: start clean rather than crash
            log(f"could not read state.json ({e}); starting fresh")
    return state


def save_state(state):
    state["filings_seen"] = state["filings_seen"][-2000:]
    STATE_FILE.write_text(json.dumps(state, indent=1))


def send(text):
    if DRY_RUN:
        print(text + "\n" + "-" * 70, flush=True)
        return
    for attempt in range(3):
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text[:4000], "disable_web_page_preview": True},
            timeout=20,
        )
        if r.status_code == 429:  # Telegram rate limit
            time.sleep(int(r.json().get("parameters", {}).get("retry_after", 3)) + 1)
            continue
        r.raise_for_status()
        time.sleep(0.5)
        return
    log("gave up sending a message after Telegram rate limits")


def pct(x):
    return f"{x * 100:+.1f}%"


def market_open_now():
    now = datetime.now(NY)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 35 <= minutes <= 16 * 60 + 10


def sec_get(url, **params):
    r = requests.get(url, params=params, headers={"User-Agent": SEC_UA}, timeout=30)
    r.raise_for_status()
    time.sleep(0.15)  # SEC asks for at most 10 requests per second
    return r


JUNK = ("stock price, news, quote", "stock forecast", "should you buy", "price forecast",
        "stock quote", "price target", "stock price today", "buy, sell, or hold", "stock analysis")


def _rss(url, max_age_h=48):
    """Return (title, link, published) from an RSS feed, newest first, skipping junk and old items."""
    try:
        root = ET.fromstring(requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).content)
    except Exception as e:
        log(f"feed failed: {url[:60]}... {e}")
        return []
    out, cutoff = [], pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=max_age_h)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        try:
            ts = pd.Timestamp(item.findtext("pubDate") or "")
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        except Exception:
            continue
        if not title or ts < cutoff or any(j in title.lower() for j in JUNK):
            continue
        out.append((ts, title, link))
    out.sort(reverse=True)
    return [(t, l, ts.tz_convert(STHLM).strftime("%d %b %H:%M")) for ts, t, l in out]


def headlines(name, ticker, n=4):
    """Recent headlines: Yahoo Finance's ticker feed (direct links) first, Google News to fill up."""
    items = _rss(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US")
    if len(items) < n:
        q = f'"{name}" when:2d'
        items += _rss("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
                      + "&hl=en-US&gl=US&ceid=US:en")
    # keep only headlines that name the company or ticker (Yahoo's feed also carries general market stories)
    short = name.lower()
    for suffix in (" inc.", " inc", " corporation", " corp.", " corp", " co.", " plc", " ltd", " holdings",
                   " group", " technologies", " company", ","):
        short = short.replace(suffix, "")
    short = short.strip().split(" ")[0] if len(short.strip().split(" ")[0]) >= 4 else short.strip()
    seen, out = set(), []
    for title, link, pub in items:
        base = title.rsplit(" - ", 1)[0] if " - " in title else title
        low = f" {base.lower()} "
        if short not in low and f"({ticker.lower()})" not in low and f" {ticker.lower()} " not in low:
            continue
        key = base.lower()[:60]
        if key not in seen:
            seen.add(key)
            out.append((title, link, pub))
        if len(out) >= n:
            break
    return out


DEPART = ("resign", "retire", "step down", "stepping down", "depart", "terminat", "separation", "transition")
SENIOR = ("chief executive", "chief financial", "chief operating", "president", "ceo", "cfo")


def is_senior_departure(url):
    """For an 8-K item 5.02, check the filing text for a CEO/CFO/COO/President leaving."""
    try:
        text = sec_get(url).text.lower()
    except Exception:
        return True  # cannot check: alert rather than miss it
    return any(d in text for d in DEPART) and any(s in text for s in SENIOR)


# ---------------------------------------------------------------- SEC filings

def load_universe():
    uni = pd.read_csv(HERE / "universe.csv")
    try:
        tick = sec_get("https://www.sec.gov/files/company_tickers.json").json()
    except Exception as e:  # filings are a bonus: never let the SEC stop the price alerts
        log(f"SEC company list failed ({e}); filing alerts skipped this run")
        uni["cik"] = float("nan")
        return uni
    cik_of = {v["ticker"].upper(): int(v["cik_str"]) for v in tick.values()}
    uni["cik"] = uni.ticker.str.upper().map(lambda t: cik_of.get(t) or cik_of.get(t.replace("-", ".")))
    missing = uni[uni.cik.isna()].ticker.tolist()
    if missing:
        log(f"no SEC CIK for {len(missing)} tickers: {missing[:10]}")
    return uni


def recent_8ks(days_back=3):
    """All 8-K filings from the last few days, one row per filing, from SEC full-text search."""
    end = datetime.now(NY).date()
    start = end - timedelta(days=days_back)
    filings, offset = {}, 0
    while True:
        data = sec_get("https://efts.sec.gov/LATEST/search-index", forms="8-K",
                       dateRange="custom", startdt=start.isoformat(), enddt=end.isoformat(),
                       **{"from": offset}).json()
        hits = data["hits"]["hits"]
        for h in hits:
            s = h["_source"]
            adsh = s.get("adsh") or h["_id"].split(":")[0]
            if adsh in filings or not s.get("ciks"):
                continue
            cik = int(s["ciks"][0])
            doc = h["_id"].split(":", 1)[1] if ":" in h["_id"] else ""
            folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}"
            filings[adsh] = {
                "adsh": adsh, "cik": cik, "form": s.get("form", "8-K"),
                "date": s.get("file_date", ""), "items": s.get("items") or [],
                "name": (s.get("display_names") or [""])[0],
                "url": f"{folder}/{doc}" if doc else folder,
                "index": f"{folder}/{adsh}-index.htm",
            }
        offset += len(hits)
        total = data["hits"]["total"]["value"]
        if not hits or offset >= total or offset >= 2000:
            break
    log(f"8-K filings fetched: {len(filings)} ({start} to {end})")
    return list(filings.values())


# ---------------------------------------------------------------- prices

def load_prices(tickers):
    px = yf.download(tickers + ["SPY"], period="3mo", interval="1d", auto_adjust=False,
                     progress=False, threads=True)
    close = px["Close"].dropna(how="all")
    vol = px["Volume"]
    return close, vol


def classify(t, ret, spy_ret, peers_down, need, earn_8k):
    if earn_8k:
        return "Earnings (results filed with the SEC in the last 2 days)"
    if spy_ret <= -0.02:
        return "Market-wide (S&P 500 down 2% or more today)"
    if ret < 0 and len(peers_down) >= need:
        return "Collateral? (several same-sector stocks also down; check whether the cause is someone else's news)"
    return "Company-specific? (no earnings filing, peers mostly calm; check the cause)"


# ---------------------------------------------------------------- main

def main(force=False):
    state = load_state()
    uni = load_universe()
    name = dict(zip(uni.ticker, uni.name))
    group = dict(zip(uni.ticker, uni.group))
    ticker_of_cik = {int(c): t for t, c in zip(uni.ticker, uni.cik) if not pd.isna(c)}

    try:
        filings = recent_8ks()
    except Exception as e:
        log(f"SEC filing search failed: {e}")
        filings = []
    ours = [f for f in filings if f["cik"] in ticker_of_cik]
    by_ticker = {}
    for f in ours:
        by_ticker.setdefault(ticker_of_cik[f["cik"]], []).append(f)
    two_days_ago = (datetime.now(NY).date() - timedelta(days=2)).isoformat()

    def filed_earnings(t):
        return any("2.02" in f["items"] and f["date"] >= two_days_ago for f in by_ticker.get(t, []))

    sent = 0
    today_ret = spy_ret = None
    close = vol = None

    # ---- 1. price moves
    if market_open_now() or force:
        try:
            close, vol = load_prices(uni.ticker.tolist())
        except Exception as e:
            log(f"price download failed: {e}")
        if close is not None and len(close) > 25:
            last_day = close.index[-1].date()
            if last_day != datetime.now(NY).date() and not force:
                log(f"latest price bar is {last_day}, not today - skipping price scan")
            else:
                rets = close.pct_change(fill_method=None)
                today_ret = rets.iloc[-1]
                spy_ret = float(today_ret.get("SPY", 0) or 0)
                hits = []
                for t in uni.ticker:
                    r = today_ret.get(t)
                    if r is None or pd.isna(r) or abs(r) * 100 < DROP_PCT:
                        continue
                    prev = state["price_alerted"].get(t)
                    if prev is not None and abs(r) * 100 < abs(prev) + REALERT_STEP:
                        continue
                    hits.append((t, float(r), prev))
                hits.sort(key=lambda h: h[1])

                if len(hits) > DIGEST_OVER:
                    lines = [f"SNAPBACK SUMMARY - {len(hits)} large US stocks moved {DROP_PCT:.0f}%+ today",
                             f"S&P 500 (SPY) {pct(spy_ret)} | time {datetime.now(STHLM):%d %b %H:%M} Stockholm",
                             "Biggest: " + ", ".join(f"{t} {pct(r)}" for t, r, _ in hits[:20]),
                             "By sector: " + ", ".join(f"{g} {n}" for g, n in
                                                      pd.Series([group[t] for t, _, _ in hits]).value_counts().head(6).items())]
                    send("\n".join(lines))
                    sent += 1
                    for t, r, _ in hits:
                        state["price_alerted"][t] = round(r * 100, 2)
                    hits = []

                for t, r, prev in hits:
                    peers = [p for p in uni.ticker if group[p] == group[t] and p != t]
                    pr = today_ret[peers].dropna()
                    peers_down = pr[pr <= -PEER_PCT / 100].sort_values()
                    need = max(2, math.ceil(0.05 * len(pr)))
                    c = close[t].dropna()
                    v = vol[t].dropna()
                    avg_vol = v.iloc[-21:-1].mean()
                    vol_x = v.iloc[-1] / avg_vol if avg_vol else float("nan")
                    month = c.iloc[-1] / c.iloc[-22] - 1 if len(c) > 22 else float("nan")
                    direction = "DOWN" if r < 0 else "UP"
                    lines = [
                        f"SNAPBACK ALERT - PRICE MOVE{' (moving further)' if prev else ''}",
                        f"{t} {name[t]} {direction} {pct(r)} today",
                        f"Price {c.iloc[-1]:.2f} (yesterday's close {c.iloc[-2]:.2f}), volume so far {vol_x:.1f}x the 20-day average",
                        f"Time: {datetime.now(STHLM):%d %b %Y %H:%M} Stockholm",
                        f"S&P 500 (SPY) today {pct(spy_ret)}; stock over the past month {pct(month)}",
                        f"Sector group: {group[t]}. Peers {PEER_PCT:.0f}%+ down today: "
                        + (", ".join(f"{p} {pct(x)}" for p, x in peers_down.head(6).items()) or "none")
                        + f" (of {len(pr)})",
                    ]
                    if r < 0:
                        lines.append("First-pass drop type: " + classify(t, r, spy_ret, peers_down, need, filed_earnings(t)))
                    fl = by_ticker.get(t, [])
                    if fl:
                        lines.append("SEC 8-K filings, last 3 days:")
                        for f in sorted(fl, key=lambda f: f["date"], reverse=True)[:3]:
                            items = "; ".join(f"{i} {ITEM_NAMES.get(i, '')}".strip() for i in f["items"])
                            lines.append(f"- {f['date']} {f['form']}: {items or 'items not listed'} {f['url']}")
                    news = headlines(name[t], t)
                    if news:
                        lines.append("Headlines:")
                        lines += [f"- {title} ({pub}) {link}" for title, link, pub in news]
                    else:
                        lines.append("Headlines: none found in the last 2 days")
                    if r < 0:
                        lines.append(BACKTEST)
                    lines.append("Paste this into Claude for the full picture. Facts only, not advice.")
                    send("\n".join(lines))
                    sent += 1
                    state["price_alerted"][t] = round(r * 100, 2)
    else:
        log("US market closed - price scan skipped, filings still checked")

    # ---- 2. important filings
    seen = set(state["filings_seen"])
    first_run = not seen
    if first_run:
        log("first run: marking existing filings as seen without alerting")
    for f in sorted(ours, key=lambda f: f["date"]):
        if first_run and os.getenv("ALERT_BACKLOG") != "1":
            state["filings_seen"].append(f["adsh"])
            continue
        if f["adsh"] in seen:
            continue
        state["filings_seen"].append(f["adsh"])
        important = [i for i in f["items"] if i in IMPORTANT_ITEMS]
        if not important:
            continue
        # Item 5.02 on its own is mostly board and pay changes: keep it only for a senior executive leaving.
        if important == ["5.02"] and (f["form"].endswith("/A") or not is_senior_departure(f["url"])):
            continue
        # Item 1.01 together with 2.03 (new debt) is almost always a routine loan or bond deal.
        if important == ["1.01"] and "2.03" in f["items"]:
            continue
        t = ticker_of_cik[f["cik"]]
        lines = [
            "SNAPBACK ALERT - SEC FILING",
            f"{t} {name.get(t, f['name'])} filed a {f['form']} on {f['date']}",
            "Items: " + "; ".join(f"{i} {ITEM_NAMES.get(i, '')}".strip() for i in f["items"]),
            f"Filing: {f['url']}",
            f"All documents: {f['index']}",
            f"Time: {datetime.now(STHLM):%d %b %Y %H:%M} Stockholm",
        ]
        if today_ret is not None and t in today_ret and not pd.isna(today_ret[t]):
            lines.append(f"Stock today {pct(float(today_ret[t]))}; S&P 500 (SPY) {pct(spy_ret)}")
        news = headlines(name.get(t, t), t, n=3)
        if news:
            lines.append("Headlines:")
            lines += [f"- {title} ({pub}) {link}" for title, link, pub in news]
        lines.append("Paste this into Claude for the full picture. Facts only, not advice.")
        send("\n".join(lines))
        sent += 1

    save_state(state)
    log(f"done: {sent} message(s) sent")


if __name__ == "__main__":
    if "--test" in sys.argv:
        send("Snapback test message: the bot can reach this chat.")
    else:
        if not DRY_RUN and not (TOKEN and CHAT_ID):
            # Exit cleanly so GitHub does not email a failure every 15 minutes before setup is done.
            log("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are not set yet - add them as repository secrets. Nothing sent.")
            sys.exit(0)
        main(force="--force" in sys.argv)
