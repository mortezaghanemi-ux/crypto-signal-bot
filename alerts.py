"""Telegram alert bot - long-only Donchian trend following (paper trading).

Rules (same as the backtest, 4h candles, fixed values - not tuned):
- ENTRY : a closed 4h candle closes above the highest high of the previous 55 candles.
- STOP  : entry price - 2 x ATR(14).
- EXIT  : a closed 4h candle closes below the lowest low of the previous 20 candles.

It does not trade. It tracks "paper" positions in state.json, sends alerts to
Telegram, and logs finished paper trades to paper_trades.csv.
Standard library only. Educational tool, not financial advice.

Commands:
    python alerts.py            check once (sends to Telegram if configured)
    python alerts.py --dry      check once, only print (no Telegram, nothing saved)
    python alerts.py loop       keep running, check after every 4h candle closes
    python alerts.py test       send a test message
    python alerts.py chatid     find your Telegram chat id
    python alerts.py status     show open paper positions
    python alerts.py report     show paper-trading results

Secrets live in a file named .env next to this script (never commit it):
    BOT_SERVICE=telegram            (or: bale - works without a VPN inside Iran)
    TELEGRAM_TOKEN=123456:ABC...    (the bot token; same name is used for Bale)
    TELEGRAM_CHAT_ID=123456789
"""

import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

import bot

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP"]
N_IN = 55
N_OUT = 20
STOP_ATR = 2.0
ATR_PERIOD = 14
FEE = 0.001
CANDLE_MS = 4 * 3600 * 1000
MIN_CANDLES = 120

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
TRADES_FILE = os.path.join(HERE, "paper_trades.csv")
ENV_FILE = os.path.join(HERE, ".env")


# ---------------- Config & Telegram ----------------
def load_env():
    cfg = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "BOT_SERVICE"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


API_BASES = {"telegram": "https://api.telegram.org", "bale": "https://tapi.bale.ai"}


def tg_call(token, method, payload=None, service="telegram"):
    base = API_BASES.get(service, API_BASES["telegram"])
    url = f"{base}/bot{token}/{method}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def send(cfg, text):
    """Send a Telegram message. Returns True on success. Never prints the token."""
    token = cfg.get("TELEGRAM_TOKEN")
    chat_id = cfg.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        res = tg_call(
            token,
            "sendMessage",
            {"chat_id": chat_id, "text": text},
            service=cfg.get("BOT_SERVICE", "telegram").lower(),
        )
        return bool(res.get("ok"))
    except Exception as e:  # noqa: BLE001
        print("Telegram send failed:", str(e).replace(token, "***"))
        return False


# ---------------- Market data ----------------
def _kucoin(coin):
    end_s = int(time.time())
    start_s = end_s - 400 * 4 * 3600  # explicit range, same form the backtest used
    url = (
        "https://api.kucoin.com/api/v1/market/candles"
        f"?type=4hour&symbol={coin}-USDT&startAt={start_s}&endAt={end_s}"
    )
    rows = bot.http_get_json(url)["data"]  # newest first: [t_s, open, close, high, low]
    return [
        (int(r[0]) * 1000, float(r[1]), float(r[3]), float(r[4]), float(r[2]))
        for r in reversed(rows)
    ]


def _okx(coin):
    url = f"https://www.okx.com/api/v5/market/candles?instId={coin}-USDT&bar=4H&limit=300"
    rows = bot.http_get_json(url)["data"]  # newest first
    return [
        (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        for r in reversed(rows)
    ]


def _bybit(coin):
    url = (
        "https://api.bybit.com/v5/market/kline"
        f"?category=spot&symbol={coin}USDT&interval=240&limit=300"
    )
    rows = bot.http_get_json(url)["result"]["list"]  # newest first
    return [
        (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        for r in reversed(rows)
    ]


def _binance(coin):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={coin}USDT&interval=4h&limit=300"
    return [
        (int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]))
        for c in bot.http_get_json(url)
    ]


SOURCES = [("kucoin", _kucoin), ("okx", _okx), ("bybit", _bybit), ("binance", _binance)]


def fetch_closed(coin):
    """Closed 4h candles only (a candle is closed once start + 4h <= now)."""
    now_ms = int(time.time() * 1000)
    errors = []
    for name, fn in SOURCES:
        try:
            candles = [c for c in fn(coin) if c[0] + CANDLE_MS <= now_ms]
            if len(candles) < MIN_CANDLES:
                raise ValueError(f"only {len(candles)} closed candles")
            return candles
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e or type(e).__name__}")
    raise RuntimeError(f"{coin}: all sources failed | " + " | ".join(errors))


# ---------------- Strategy (pure function, easy to test) ----------------
def fp(x):
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.5f}"


def process_coin(coin, closed, st):
    """Advance one coin's paper state using closed candles.

    st = {"last": candle_time_ms or None, "pos": None or {...}}
    Returns (new_state, events). Each event has "kind", "text" and optional "trade".
    """
    highs = [c[2] for c in closed]
    lows = [c[3] for c in closed]
    closes = [c[4] for c in closed]
    atrs = bot.atr(highs, lows, closes, ATR_PERIOD)

    last = st.get("last")
    pos = st.get("pos")
    if last is None:
        indices = [len(closed) - 1]  # first run: look only at the latest candle
    else:
        indices = [j for j in range(len(closed)) if closed[j][0] > last]

    events = []

    def finish(j, price, reason):
        nonlocal pos
        risk = pos["risk"]
        r = (price - pos["entry"]) / risk - 2 * FEE * pos["entry"] / risk
        trade = {
            "coin": coin,
            "entry_time": pos["t"],
            "entry": pos["entry"],
            "exit_time": closed[j][0],
            "exit": price,
            "reason": reason,
            "r": round(r, 3),
        }
        if reason == "STOP":
            text = (
                f"🔴 حد ضرر خورد (آزمایشی) | {coin}/USDT\n"
                f"ورود {fp(pos['entry'])} ← خروج {fp(price)}\n"
                f"نتیجه: {r:+.2f}R"
            )
        else:
            text = (
                f"🟡 سیگنال خروج (آزمایشی) | {coin}/USDT\n"
                f"کندل ۴ ساعته زیر کف {N_OUT} کندل اخیر بسته شد.\n"
                f"ورود {fp(pos['entry'])} ← قیمت مرجع خروج {fp(price)}\n"
                f"نتیجه تقریبی: {r:+.2f}R"
            )
        events.append({"kind": reason, "text": text, "trade": trade})
        pos = None

    for j in indices:
        if j < N_IN + 1 or atrs[j] is None:
            continue
        t, o, h, l, c = closed[j]

        if pos is not None:
            # 1) stop touched during this candle (gap-aware)
            if o <= pos["stop"]:
                finish(j, o, "STOP")
            elif l <= pos["stop"]:
                finish(j, pos["stop"], "STOP")
            # 2) channel exit on the close (exit at next open ~ reference close)
            elif c < min(lows[j - N_OUT : j]):
                finish(j, c, "EXIT")
                continue

        if pos is None and c > max(highs[j - N_IN : j]):
            risk = STOP_ATR * atrs[j]
            pos = {"entry": c, "stop": c - risk, "risk": risk, "t": t}
            chan_low = min(lows[j - N_OUT + 1 : j + 1])
            text = (
                f"🟢 سیگنال خرید (آزمایشی) | {coin}/USDT | ۴ ساعته\n"
                f"شکست سقف {N_IN} کندل اخیر\n"
                f"قیمت مرجع ورود: {fp(c)}\n"
                f"حد ضرر: {fp(pos['stop'])} ({risk / c * 100:.1f}٪ پایین‌تر)\n"
                f"خروج: بسته شدن یک کندل ۴ ساعته زیر {fp(chan_low)} "
                f"(کف {N_OUT} کندل اخیر؛ با گذشت زمان بالا می‌رود)\n"
                f"حجم با ریسک ۱٪ سرمایه: (سرمایه × 0.01) ÷ {fp(risk)}\n"
                "آموزشی است و توصیه مالی نیست."
            )
            events.append({"kind": "ENTRY", "text": text, "trade": None})

    return {"last": closed[-1][0], "pos": pos}, events


# ---------------- State, logging, commands ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def log_trade(trade):
    new = not os.path.exists(TRADES_FILE)
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["coin", "entry_time_utc", "entry", "exit_time_utc", "exit", "reason", "R"])
        fmt = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        w.writerow(
            [trade["coin"], fmt(trade["entry_time"]), trade["entry"],
             fmt(trade["exit_time"]), trade["exit"], trade["reason"], trade["r"]]
        )


def run_once(dry=False):
    cfg = load_env()
    can_send = bool(cfg.get("TELEGRAM_TOKEN") and cfg.get("TELEGRAM_CHAT_ID"))
    if not dry and not can_send:
        print("Telegram is not configured (.env missing) -> running as --dry")
        dry = True

    state = load_state()
    for coin in COINS:
        try:
            closed = fetch_closed(coin)
        except Exception as e:  # noqa: BLE001
            print(f"{coin}: skipped ({e})")
            continue
        new_st, events = process_coin(coin, closed, state.get(coin, {"last": None, "pos": None}))
        ok = True
        for ev in events:
            print(ev["text"], "\n")
            if not dry and not send(cfg, ev["text"]):
                ok = False
                break
        if dry:
            continue
        if ok:  # only advance state if every alert was delivered (else retry next run)
            state[coin] = new_st
            for ev in events:
                if ev["trade"]:
                    log_trade(ev["trade"])
        else:
            print(f"{coin}: alert not delivered, will retry next run")
    if not dry:
        save_state(state)
    status_line(state)
    return state


def status_line(state):
    open_pos = [c for c in COINS if state.get(c, {}).get("pos")]
    print(f"Checked {len(COINS)} coins | open paper positions: {', '.join(open_pos) or 'none'}")


def cmd_status():
    state = load_state()
    if not state:
        print("No state yet. Run: python alerts.py")
        return
    for coin in COINS:
        pos = state.get(coin, {}).get("pos")
        if pos:
            print(f"{coin}: entry {fp(pos['entry'])} | stop {fp(pos['stop'])}")
        else:
            print(f"{coin}: no open position")


def cmd_report():
    if not os.path.exists(TRADES_FILE):
        print("No finished paper trades yet.")
        return
    with open(TRADES_FILE, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rs = [float(r["R"]) for r in rows]
    wins = [x for x in rs if x > 0]
    print(f"Finished paper trades: {len(rs)}")
    print(f"Win rate: {len(wins) / len(rs) * 100:.0f}% | total {sum(rs):+.1f}R | avg {sum(rs) / len(rs):+.2f}R")
    for r in rows[-5:]:
        print(f"  {r['coin']} {r['entry_time_utc']} -> {r['exit_time_utc']} {r['reason']} {float(r['R']):+.2f}R")


def cmd_test():
    cfg = load_env()
    ok = send(cfg, "✅ ربات سیگنال متصل شد. (پیام آزمایشی)")
    print("Sent!" if ok else "Failed. Check .env and your VPN / internet.")


def cmd_chatid():
    cfg = load_env()
    token = cfg.get("TELEGRAM_TOKEN")
    if not token:
        print("Put TELEGRAM_TOKEN in .env first.")
        return
    try:
        res = tg_call(token, "getUpdates", service=cfg.get("BOT_SERVICE", "telegram").lower())
    except Exception as e:  # noqa: BLE001
        print("Failed:", str(e).replace(token, "***"))
        return
    ids = {m["message"]["chat"]["id"] for m in res.get("result", []) if "message" in m}
    if not ids:
        print("No messages found. Open your bot in Telegram, send it /start, then run this again.")
    for i in ids:
        print("Your chat id:", i)


def sleep_until_next_candle():
    now_ms = int(time.time() * 1000)
    target = ((now_ms // CANDLE_MS) + 1) * CANDLE_MS + 120_000  # 2 min after the close
    while True:
        remaining = target / 1000 - time.time()
        if remaining <= 0:
            return
        time.sleep(min(30, remaining))


def cmd_loop():
    cfg = load_env()
    last_heartbeat = None
    print("Loop started. Press Ctrl+C to stop.")
    while True:
        try:
            state = run_once()
            now = datetime.now(timezone.utc)
            if now.hour == 0 and last_heartbeat != now.date():
                open_pos = [c for c in COINS if state.get(c, {}).get("pos")]
                send(cfg, f"✅ ربات فعال است. پوزیشن‌های باز آزمایشی: {', '.join(open_pos) or 'ندارد'}")
                last_heartbeat = now.date()
        except Exception as e:  # noqa: BLE001
            print("Error in loop:", e)
            time.sleep(300)
            continue
        sleep_until_next_candle()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "loop":
        cmd_loop()
    elif arg == "test":
        cmd_test()
    elif arg == "chatid":
        cmd_chatid()
    elif arg == "status":
        cmd_status()
    elif arg == "report":
        cmd_report()
    else:
        run_once(dry=(arg == "--dry"))
