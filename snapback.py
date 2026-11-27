"""Snapback - free market alert bot for large US stocks.

Every run it checks two things and sends a Telegram message for each new event:

1. Big price moves: an S&P 500 / Nasdaq 100 stock down DROP_PCT (default 7%) or up UP_PCT (default 7%)
   or more versus the last close - in the regular session, pre-market (from 4:00 New York)
   and after-hours (until 20:00 New York). Each alert ends with a Read line: sector move,
   company news, or no news found.
1b. Follow-up: a stock flagged with no news gets one "NEWS NOW OUT" message as soon as a
   filing or headline appears (checked for 36 hours).
2. Important SEC filings: a new 8-K from one of those companies with a material item
   (CEO/CFO change, deal, bankruptcy, accounting restatement, impairment, ...).

Each message is written so it can be pasted straight into Claude for a full analysis.
It gives facts only, never a trade call.

Environment variables:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  required unless DRY_RUN=1
  SEC_USER_AGENT     "Your Name your@email.com" - the SEC requires a contact in the user agent
  DROP_PCT           drop size that triggers a price alert, in percent (default 7)
  UP_PCT             rise size that triggers a price alert, in percent (default 7)
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
UP_PCT = float(os.getenv("UP_PCT", "7"))
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
    state = {"date": today, "price_alerted": {}, "filings_seen": [], "ext_alerted": {}, "no_news": {}}
    if STATE_FILE.exists():
        try:
            old = json.loads(STATE_FILE.read_text())
            state["filings_seen"] = old.get("filings_seen", [])[-2000:]
            state["ext_alerted"] = old.get("ext_alerted", {})
            state["no_news"] = old.get("no_news", {})
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


DEPART = ("resign", "retire", "step down", "stepping down", "will depart", "departure of", "no longer serve", "terminated as", "has been terminated")
SENIOR = ("chief executive officer", "chief financial officer", "chief operating officer", "president and")


def is_senior_departure(url):
    """For an 8-K item 5.02, check the filing text for a CEO/CFO/COO/President leaving."""
    try:
        text = sec_get(url).text.lower()
    except Exception:
        return True  # cannot check: alert rather than miss it
    # Look only at the 5.02 section, and require a departure word and a senior title close together,
    # so pay changes and director appointments do not trigger.
    start = text.find("5.02")
    section = text[start:start + 6000] if start >= 0 else text
    for d in DEPART:
        i = section.find(d)
        while i >= 0:
            window = section[max(0, i - 400): i + 400]
            if any(s in window for s in SENIOR):
                return True
            i = section.find(d, i + 1)
    return False


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


SESSION_LABEL = {"regular": "", "pre": " PRE-MARKET", "post": " AFTER-HOURS"}


def session_now():
    """regular 9:35-16:10, pre 4:00-9:35, post 16:10-20:00 New York time on weekdays, else None."""
    now = datetime.now(NY)
    if now.weekday() >= 5:
        return None
    m = now.hour * 60 + now.minute
    if 9 * 60 + 35 <= m <= 16 * 60 + 10:
        return "regular"
    if 4 * 60 <= m < 9 * 60 + 35:
        return "pre"
    if 16 * 60 + 10 < m <= 20 * 60:
        return "post"
    return None


def extended_moves(tickers, now=None):
    """Latest pre-market or after-hours price versus the last regular-session close.
    Returns (returns, latest price, reference close, number of 15-minute bars with trades) as Series by ticker.
    A stock needs trades in at least 2 bars this session and one in the last 45 minutes,
    so a single stray print does not trigger an alert. (Yahoo does not report extended-hours volume.)"""
    px = yf.download(tickers + ["SPY"], period="5d", interval="15m", prepost=True,
                     auto_adjust=False, progress=False, threads=True)
    cl, vo = px["Close"], px["Volume"]
    idx = cl.index.tz_convert(NY)
    cl.index = vo.index = idx
    mins = idx.hour * 60 + idx.minute
    regular = (mins >= 9 * 60 + 30) & (mins < 16 * 60)
    now = now or datetime.now(NY)
    today = now.date()
    if now.hour < 12:   # pre-market: bars from 4:00 today
        sess = (idx.date == today) & (mins >= 4 * 60) & (mins < 9 * 60 + 30)
    else:               # after-hours: bars from 16:00 today
        sess = (idx.date == today) & (mins >= 16 * 60)
    reg_cl = cl[regular & (idx < pd.Timestamp(now))]
    ref = reg_cl.ffill().iloc[-1] if len(reg_cl) else None
    s_cl, s_vo = cl[sess], vo[sess]
    if ref is None or s_cl.dropna(how="all").empty:
        log("no extended-hours trading data yet")
        return None
    last = s_cl.ffill().iloc[-1]
    last_time = s_cl.apply(lambda c: c.last_valid_index())
    fresh = last_time.apply(lambda ts: ts is not None and (pd.Timestamp(now) - ts) <= pd.Timedelta(minutes=45))
    bars = s_cl.notna().sum()
    ret = last / ref - 1
    ok = fresh & (bars >= 2)
    ok["SPY"] = True
    return ret.where(ok), last, ref, bars


def price_scan(state, uni, name, group, by_ticker, filed_earnings, close, vol, ret_now, spy_ret,
               session, ext=None, prior=None, store_key=None):
    """Send alerts for stocks moving DROP_PCT down / UP_PCT up. Returns the number of messages sent."""
    sent = 0
    alerted = state["ext_alerted"][store_key] if store_key else state["price_alerted"]
    label = SESSION_LABEL[session]
    when = {"regular": "today", "pre": "pre-market", "post": "after-hours"}[session]
    hits = []
    for t in uni.ticker:
        r = ret_now.get(t)
        if r is None or pd.isna(r) or r * 100 > -DROP_PCT and r * 100 < UP_PCT:
            continue
        prev = alerted.get(t)
        if prev is None and prior:
            prev = prior.get(t)   # already flagged earlier (pre-market / last night): only alert again if the move grows
        if prev is not None and abs(r) * 100 < abs(prev) + REALERT_STEP:
            continue
        hits.append((t, float(r), prev))
    hits.sort(key=lambda h: h[1])

    if len(hits) > DIGEST_OVER:
        lines = [f"SNAPBACK SUMMARY{label} - {len(hits)} large US stocks fell {DROP_PCT:.0f}%+ or rose {UP_PCT:.0f}%+ {when}",
                 f"S&P 500 (SPY) {pct(spy_ret)} | time {datetime.now(STHLM):%d %b %H:%M} Stockholm",
                 "Biggest: " + ", ".join(f"{t} {pct(r)}" for t, r, _ in hits[:20]),
                 "By sector: " + ", ".join(f"{g} {n}" for g, n in
                                          pd.Series([group[t] for t, _, _ in hits]).value_counts().head(6).items())]
        send("\n".join(lines))
        for t, r, _ in hits:
            alerted[t] = round(r * 100, 2)
        return 1

    for t, r, prev in hits:
        peers = [p for p in uni.ticker if group[p] == group[t] and p != t]
        pr = ret_now.reindex(peers).dropna()
        peers_down = pr[pr <= -PEER_PCT / 100].sort_values()
        peers_up = pr[pr >= PEER_PCT / 100].sort_values(ascending=False)
        same_way = peers_down if r < 0 else peers_up
        need = max(2, math.ceil(0.05 * len(pr)))
        c = close[t].dropna()
        month_base = c.iloc[-22] if len(c) > 22 else float("nan")
        direction = "DOWN" if r < 0 else "UP"
        again = ""
        if prev is not None:
            again = " (moving further)" if t in alerted else f" (flagged earlier at {prev:+.1f}%)"
        lines = [f"SNAPBACK ALERT - PRICE MOVE{label}{again}",
                 f"{t} {name[t]} {direction} {pct(r)} {when}"]
        if ext:
            ext_px, ext_ref, ext_vol = ext
            lines.append(f"Price {ext_px[t]:.2f} vs last close {ext_ref[t]:.2f}; traded in {int(ext_vol[t])} of the "
                         f"{when} 15-minute periods so far (thin trading - moves can change at the open)")
            month = ext_px[t] / month_base - 1 if month_base == month_base else float("nan")
        else:
            v = vol[t].dropna()
            avg_vol = v.iloc[-21:-1].mean()
            vol_x = v.iloc[-1] / avg_vol if avg_vol else float("nan")
            lines.append(f"Price {c.iloc[-1]:.2f} (yesterday's close {c.iloc[-2]:.2f}), volume so far {vol_x:.1f}x the 20-day average")
            month = c.iloc[-1] / month_base - 1 if month_base == month_base else float("nan")
        lines += [
            f"Time: {datetime.now(STHLM):%d %b %Y %H:%M} Stockholm",
            f"S&P 500 (SPY) {when} {pct(spy_ret)}; stock over the past month {pct(month)}",
            f"Sector group: {group[t]}. Peers {PEER_PCT:.0f}%+ {'down' if r < 0 else 'up'} {when}: "
            + (", ".join(f"{p} {pct(x)}" for p, x in same_way.head(6).items()) or "none")
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
        if len(same_way) >= need:
            lines.append(f"Read: SECTOR MOVE - {len(same_way)} peers moved the same way")
        elif not fl and not news:
            lines.append("Read: NO NEWS FOUND - the stock is moving on its own with no filing or headline yet. "
                         "Could be news not yet public or not yet picked up; the bot will message you if news appears")
            state["no_news"][t] = {"move": round(r * 100, 2), "at": datetime.now(NY).isoformat(timespec="minutes"),
                                   "when": when}
        else:
            lines.append("Read: COMPANY NEWS - see filings/headlines above")
        if r < 0:
            lines.append(BACKTEST)
        lines.append("Paste this into Claude for the full picture. Facts only, not advice.")
        send("\n".join(lines))
        sent += 1
        alerted[t] = round(r * 100, 2)
    return sent


def news_followups(state, name, by_ticker, today_ret, keep_hours=36):
    """For stocks alerted with no news: send one update as soon as a filing or headline shows up."""
    sent = 0
    now = datetime.now(NY)
    for t, info in list(state["no_news"].items()):
        at = datetime.fromisoformat(info["at"])
        if now - at > timedelta(hours=keep_hours):
            del state["no_news"][t]
            continue
        fl = [f for f in by_ticker.get(t, []) if f["date"] >= at.date().isoformat()]
        news = headlines(name.get(t, t), t, n=3)
        if not fl and not news:
            continue
        lines = ["SNAPBACK UPDATE - NEWS NOW OUT",
                 f"{t} {name.get(t, t)}: flagged {info['move']:+.1f}% {info['when']} at "
                 f"{at.astimezone(STHLM):%d %b %H:%M} Stockholm with no news found",
                 f"Now: {datetime.now(STHLM):%d %b %H:%M} Stockholm"]
        if today_ret is not None and t in today_ret and not pd.isna(today_ret[t]):
            lines.append(f"Stock today {pct(float(today_ret[t]))}")
        if fl:
            lines.append("New SEC 8-K filings:")
            for f in sorted(fl, key=lambda f: f["date"], reverse=True)[:3]:
                items = "; ".join(f"{i} {ITEM_NAMES.get(i, '')}".strip() for i in f["items"])
                lines.append(f"- {f['date']} {f['form']}: {items or 'items not listed'} {f['url']}")
        if news:
            lines.append("Headlines (check the times - the move came before or after these):")
            lines += [f"- {title} ({pub}) {link}" for title, link, pub in news]
        lines.append("Paste this into Claude for the full picture. Facts only, not advice.")
        send("\n".join(lines))
        sent += 1
        del state["no_news"][t]
    return sent


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

    # ---- 1. price moves (regular session, pre-market and after-hours)
    session = "regular" if force else session_now()
    if session:
        try:
            close, vol = load_prices(uni.ticker.tolist())
        except Exception as e:
            log(f"price download failed: {e}")
        if session == "regular" and close is not None and len(close) > 25:
            last_day = close.index[-1].date()
            if last_day != datetime.now(NY).date() and not force:
                log(f"latest price bar is {last_day}, not today - skipping price scan")
            else:
                rets = close.pct_change(fill_method=None)
                today_ret = rets.iloc[-1]
                spy_ret = float(today_ret.get("SPY", 0) or 0)
                pre_today = state["ext_alerted"].get(f"pre:{datetime.now(NY).date()}", {})
                sent += price_scan(state, uni, name, group, by_ticker, filed_earnings, close, vol,
                                   today_ret, spy_ret, "regular", prior=pre_today)
        elif session in ("pre", "post") and close is not None and len(close) > 25:
            try:
                ext = extended_moves(uni.ticker.tolist())
            except Exception as e:
                log(f"extended-hours download failed: {e}")
                ext = None
            if ext is not None:
                ext_ret, ext_px, ext_ref, ext_vol = ext
                key = f"{session}:{datetime.now(NY).date()}"
                state["ext_alerted"].setdefault(key, {})
                for k in sorted(state["ext_alerted"], key=lambda k: k.split(":")[1])[:-4]:
                    del state["ext_alerted"][k]
                spy_e = float(ext_ret.get("SPY", 0) or 0)
                prior = None
                if session == "pre":   # a stock flagged after-hours last night only alerts again if it moves further
                    posts = sorted(k for k in state["ext_alerted"] if k.startswith("post:"))
                    prior = state["ext_alerted"][posts[-1]] if posts else None
                sent += price_scan(state, uni, name, group, by_ticker, filed_earnings, close, vol,
                                   ext_ret, spy_e, session, ext=(ext_px, ext_ref, ext_vol), prior=prior, store_key=key)
    else:
        log("outside US trading hours (4:00-20:00 New York) - price scan skipped, filings still checked")

    # ---- 1b. stocks flagged with no news: has news come out since?
    sent += news_followups(state, name, by_ticker, today_ret)

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
