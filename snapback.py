"""Snapback - alerts on unusual single-stock drops.

Scans the universe, finds stocks that are down sharply versus yesterday's close,
tags the likely type of drop, attaches headlines and sends a Telegram message.
It makes no trade calls. Run it on a schedule during US market hours.

Environment variables:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   required unless DRY_RUN=1
  DROP_PCT        minimum drop in percent to alert (default 7)
  SIGMA           drop must also be this many times the stock's normal daily move (default 3)
  REALERT_STEP    alert again each time the drop deepens by this many points (default 5)
  DIGEST_OVER     if more than this many stocks trigger in one scan, send one summary (default 10)
  DRY_RUN=1       print alerts instead of sending
"""
import json
import math
import os
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

HERE = Path(__file__).parent
STATE_FILE = HERE / "state.json"
DROP_PCT = float(os.getenv("DROP_PCT", "7"))
SIGMA = float(os.getenv("SIGMA", "3"))
REALERT_STEP = float(os.getenv("REALERT_STEP", "5"))
DIGEST_OVER = int(os.getenv("DIGEST_OVER", "10"))
DRY_RUN = os.getenv("DRY_RUN") == "1"
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# What happened after similar drops, 2014 to Nov 2025 (10-day return versus the S&P 500).
HISTORY = {
    "EARNINGS": "Earnings drops kept drifting lower: -0.4% vs market over 10 days, 45% up.",
    "COLLATERAL": "Sector-wide drops bounced modestly: about +1% to +2% vs market over 10 days, 55% up.",
    "COMPANY": "Company-specific drops showed no bounce on average: about 0% vs market, 47% up.",
    "MARKET": "Whole-market selloff day. Stock is moving with the index.",
}
LABEL = {
    "EARNINGS": "Earnings reaction",
    "COLLATERAL": "Sector-wide / collateral damage",
    "COMPANY": "Company-specific",
    "MARKET": "Market-wide selloff",
}


def load_state(today):
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        if state.get("date") == today:
            return state
    return {"date": today, "alerted": {}}


def send(text):
    if DRY_RUN:
        print(text + "\n" + "-" * 60)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()


def headlines(name, ticker, n=3):
    query = urllib.parse.quote(f'"{name}" stock when:1d' if name != ticker else f"{ticker} stock when:1d")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        root = ET.fromstring(requests.get(url, timeout=15).content)
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if title:
                out.append((title, link))
            if len(out) >= n:
                break
        return out
    except Exception:
        return []


def reported_earnings(ticker, today):
    """True if the company reported today before the close or yesterday after it."""
    try:
        dates = yf.Ticker(ticker).get_earnings_dates(limit=8)
    except Exception:
        return False
    if dates is None or len(dates) == 0:
        return False
    for ts in dates.index:
        ts = ts.tz_convert("America/New_York")
        day = ts.date()
        if day == today and ts.hour < 16:
            return True
        if 0 < (today - day).days <= 4 and ts.hour >= 16:
            # after-close report: reaction is the next trading day
            gap = pd.bdate_range(day, today)
            if len(gap) == 2:
                return True
    return False


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    uni = pd.read_csv(HERE / "universe.csv")
    tickers = uni.ticker.tolist()
    name = dict(zip(uni.ticker, uni.name))
    group = dict(zip(uni.ticker, uni.group))

    px = yf.download(tickers + ["SPY"], period="6mo", auto_adjust=True, progress=False, threads=True)
    close = px["Close"].dropna(how="all")
    low = px["Low"]
    volume = px["Volume"]
    if len(close) < 70:
        sys.exit("not enough price history returned")

    last_day = close.index[-1].date()
    ny_today = datetime.now(ZoneInfo("America/New_York")).date()
    if last_day != ny_today and "--last-session" not in sys.argv:
        print(f"latest bar is {last_day}, no US session today yet - nothing to do")
        return

    ret = close.pct_change(fill_method=None)
    today_ret = ret.iloc[-1]
    sigma = ret.iloc[-61:-1].std()
    spy_ret = float(today_ret.get("SPY", float("nan")))

    state = load_state(str(last_day))
    hits = []
    for t in tickers:
        r = today_ret.get(t)
        if r is None or pd.isna(r) or pd.isna(sigma.get(t)):
            continue
        drop = -r * 100
        if drop < DROP_PCT or -r < SIGMA * sigma[t]:
            continue
        prev = state["alerted"].get(t)
        if prev is not None and drop < prev + REALERT_STEP:
            continue
        hits.append((t, r, prev))

    if len(hits) > DIGEST_OVER:
        ranked = sorted(hits, key=lambda h: h[1])
        by_group = pd.Series([group[t] for t, _, _ in ranked]).value_counts()
        lines = [
            f"<b>{len(hits)} stocks down {DROP_PCT:.0f}%+ at once</b>  (S&P 500 {spy_ret * 100:+.1f}%)",
            "Broad selloff, sending one summary instead of separate alerts.",
            "By group: " + ", ".join(f"{g} {n}" for g, n in by_group.head(6).items()),
            "Biggest: " + ", ".join(f"{t} {r * 100:+.0f}%" for t, r, _ in ranked[:15]),
        ]
        send("\n".join(lines))
        for t, r, _ in hits:
            state["alerted"][t] = round(-r * 100, 2)
        hits_to_detail = []
    else:
        hits_to_detail = hits

    for t, r, prev in sorted(hits_to_detail, key=lambda h: h[1]):
        peers = [p for p in tickers if group[p] == group[t] and p != t]
        peer_ret = today_ret[peers].dropna()
        down = peer_ret[peer_ret <= -0.04].sort_values()
        need = max(2, math.ceil(0.05 * len(peer_ret)))

        if reported_earnings(t, last_day):
            kind = "EARNINGS"
        elif spy_ret <= -0.02:
            kind = "MARKET"
        elif len(down) >= need:
            kind = "COLLATERAL"
        else:
            kind = "COMPANY"

        price = close[t].iloc[-1]
        day_low = low[t].iloc[-1]
        avg_vol = volume[t].iloc[-21:-1].mean()
        vol_x = volume[t].iloc[-1] / avg_vol if avg_vol else float("nan")

        lines = [
            f"<b>{t}  {r * 100:+.1f}%</b>  {esc(name[t])}" + ("  (drop deepening)" if prev else ""),
            f"Price {price:.2f}, day low {day_low:.2f}, {abs(r) / sigma[t]:.1f}x its normal daily move, volume {vol_x:.1f}x average so far",
            f"S&P 500 {spy_ret * 100:+.1f}% | {group[t]} peers: median {peer_ret.median() * 100:+.1f}%, {len(down)} of {len(peer_ret)} down 4%+",
        ]
        if len(down):
            lines.append("Also down: " + ", ".join(f"{p} {v * 100:+.0f}%" for p, v in down.head(5).items()))
        lines.append(f"<b>Type: {LABEL[kind]}</b>")
        lines.append(HISTORY[kind])
        news = headlines(name[t], t)
        if news:
            lines.append("")
            lines += [f'- <a href="{esc(link)}">{esc(title)}</a>' for title, link in news]
        send("\n".join(lines))
        state["alerted"][t] = round(-r * 100, 2)

    STATE_FILE.write_text(json.dumps(state, indent=1))
    print(f"{last_day}: {len(hits)} alert(s) sent, SPY {spy_ret * 100:+.2f}%")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        send("Snapback test message: the bot can reach this chat.")
    else:
        main()
