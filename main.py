"""
EUR/USD Multi-Timeframe Alert Bot (OANDA)
=========================================
This bot watches EUR/USD on:
- 5m
- 15m
- 30m
- 1h
- 4h

It sends Discord alerts when:
- 4H and 1H trend align
- 30M/15M confirm momentum
- 5M provides the trigger

Environment variables needed:
- OANDA_TOKEN
- OANDA_BASE_URL   (e.g. https://api-fxpractice.oanda.com or https://api-fxtrade.oanda.com)
- DISCORD_WEBHOOK_URL
"""

import os
import time
from typing import Dict, List, Optional

import requests
from flask import Flask
from threading import Thread

OANDA_TOKEN = os.environ.get("OANDA_TOKEN")
OANDA_BASE_URL = os.environ.get("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

INSTRUMENT = "EUR_USD"
SCAN_INTERVAL_SECONDS = 60
PRICE_COMPONENT = "M"
CANDLE_COUNT = 300

GRANULARITIES = {
    "5m": "M5",
    "15m": "M15",
    "30m": "M30",
    "1h": "H1",
    "4h": "H4",
}

EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
RSI_BULL_MIN = 52
RSI_BEAR_MAX = 48
ENTRY_COOLDOWN_SECONDS = 45 * 60
MIN_RANGE_PIPS_5M = 2.0
MIN_BODY_TO_RANGE = 0.45


def check_config():
    missing = []
    for name, value in [
        ("OANDA_TOKEN", OANDA_TOKEN),
        ("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL),
    ]:
        if not value:
            missing.append(name)

    if missing:
        print("❌ Missing environment variables:")
        for item in missing:
            print(f"   → {item}")
        raise SystemExit(1)

    print("✅ Config loaded")
    print(f"✅ OANDA base URL: {OANDA_BASE_URL}")
    print(f"✅ Instrument: {INSTRUMENT}")


def start_keep_alive():
    app = Flask("")

    @app.route("/")
    def home():
        return "EUR/USD OANDA bot is running ✅"

    Thread(target=lambda: app.run(host="0.0.0.0", port=8080), daemon=True).start()
    print("✅ Keep-alive server on port 8080")


def send_discord(msg: str, username: str = "EURUSD OANDA Bot"):
    try:
        r = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": msg, "username": username},
            timeout=10,
        )
        if r.status_code in (200, 204):
            print("  [✓] Discord alert sent")
        else:
            print(f"  [!] Discord error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  [!] Discord failed: {e}")


def oanda_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {OANDA_TOKEN}",
        "Content-Type": "application/json",
    }


def fetch_candles(granularity: str, count: int = CANDLE_COUNT) -> List[Dict]:
    url = f"{OANDA_BASE_URL}/v3/instruments/{INSTRUMENT}/candles"
    params = {
        "granularity": granularity,
        "count": count,
        "price": PRICE_COMPONENT,
    }
    r = requests.get(url, headers=oanda_headers(), params=params, timeout=15)
    r.raise_for_status()
    raw = r.json()

    candles = []
    for c in raw.get("candles", []):
        mid = c.get("mid", {})
        if not c.get("complete", False):
            continue
        candles.append(
            {
                "time": c["time"],
                "open": float(mid["o"]),
                "high": float(mid["h"]),
                "low": float(mid["l"]),
                "close": float(mid["c"]),
                "volume": float(c.get("volume", 0)),
            }
        )
    return candles


def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(alpha * v + (1 - alpha) * result[-1])
    return result


def rsi(values: List[float], period: int = 14) -> List[float]:
    if len(values) < period + 1:
        return []
    gains = []
    losses = []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0))
        losses.append(abs(min(delta, 0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis = [50.0] * period

    if avg_loss == 0:
        rsis.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsis.append(100 - (100 / (1 + rs)))

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        if avg_loss == 0:
            rsis.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsis.append(100 - (100 / (1 + rs)))

    while len(rsis) < len(values):
        rsis.insert(0, 50.0)
    return rsis[-len(values):]


def pip_distance(price_a: float, price_b: float) -> float:
    return abs(price_a - price_b) / 0.0001


def summarize_tf(candles: List[Dict]) -> Dict:
    closes = [c["close"] for c in candles]
    ema_fast_vals = ema(closes, EMA_FAST)
    ema_slow_vals = ema(closes, EMA_SLOW)
    rsi_vals = rsi(closes, RSI_PERIOD)

    last = candles[-1]
    prev = candles[-2] if len(candles) >= 2 else candles[-1]

    body = abs(last["close"] - last["open"])
    rng = max(last["high"] - last["low"], 1e-9)
    body_to_range = body / rng

    bull = (
        last["close"] > ema_fast_vals[-1] > ema_slow_vals[-1]
        and (rsi_vals[-1] if rsi_vals else 50.0) >= RSI_BULL_MIN
    )
    bear = (
        last["close"] < ema_fast_vals[-1] < ema_slow_vals[-1]
        and (rsi_vals[-1] if rsi_vals else 50.0) <= RSI_BEAR_MAX
    )

    return {
        "last_close": last["close"],
        "prev_close": prev["close"],
        "ema_fast": ema_fast_vals[-1],
        "ema_slow": ema_slow_vals[-1],
        "rsi": rsi_vals[-1] if rsi_vals else 50.0,
        "bull": bull,
        "bear": bear,
        "body_to_range": body_to_range,
        "last_time": last["time"],
        "last_open": last["open"],
        "last_high": last["high"],
        "last_low": last["low"],
    }


def build_signal(tf: Dict[str, Dict]) -> Optional[Dict]:
    t4 = tf["4h"]
    t1 = tf["1h"]
    t30 = tf["30m"]
    t15 = tf["15m"]
    t5 = tf["5m"]

    long_bias = t4["bull"] and t1["bull"]
    short_bias = t4["bear"] and t1["bear"]

    long_confirm = t30["bull"] and t15["bull"]
    short_confirm = t30["bear"] and t15["bear"]

    bullish_trigger = (
        t5["bull"]
        and t5["body_to_range"] >= MIN_BODY_TO_RANGE
        and pip_distance(t5["last_high"], t5["last_low"]) >= MIN_RANGE_PIPS_5M
    )
    bearish_trigger = (
        t5["bear"]
        and t5["body_to_range"] >= MIN_BODY_TO_RANGE
        and pip_distance(t5["last_high"], t5["last_low"]) >= MIN_RANGE_PIPS_5M
    )

    if long_bias and long_confirm and bullish_trigger:
        entry = t5["last_close"]
        stop = min(t5["last_low"], t15["last_low"])
        risk = entry - stop
        if risk <= 0:
            return None
        tp1 = entry + (risk * 1.5)
        tp2 = entry + (risk * 2.5)
        return {
            "side": "LONG",
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "reason": "4H + 1H bullish, 30M/15M confirm, 5M trigger",
        }

    if short_bias and short_confirm and bearish_trigger:
        entry = t5["last_close"]
        stop = max(t5["last_high"], t15["last_high"])
        risk = stop - entry
        if risk <= 0:
            return None
        tp1 = entry - (risk * 1.5)
        tp2 = entry - (risk * 2.5)
        return {
            "side": "SHORT",
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "tp2": tp2,
            "reason": "4H + 1H bearish, 30M/15M confirm, 5M trigger",
        }

    return None


def format_signal(signal: Dict, tf: Dict[str, Dict]) -> str:
    side_emoji = "🟢" if signal["side"] == "LONG" else "🔴"
    return f"""{side_emoji} **EUR/USD {signal['side']} SETUP**
────────────────────────
📌 Reason: {signal['reason']}
💰 Entry: {signal['entry']:.5f}
🛡 Stop:  {signal['stop']:.5f}
✅ TP1:   {signal['tp1']:.5f}
✅ TP2:   {signal['tp2']:.5f}
────────────────────────
4H RSI:  {tf['4h']['rsi']:.1f}
1H RSI:  {tf['1h']['rsi']:.1f}
30M RSI: {tf['30m']['rsi']:.1f}
15M RSI: {tf['15m']['rsi']:.1f}
5M RSI:  {tf['5m']['rsi']:.1f}
────────────────────────
⏰ {tf['5m']['last_time']}
NFA · Alerts only"""


def main():
    check_config()
    start_keep_alive()

    last_signal = None
    last_signal_ts = 0

    print("[BOT] Starting EUR/USD OANDA scan loop")

    while True:
        try:
            tf_raw = {
                "5m": fetch_candles(GRANULARITIES["5m"], CANDLE_COUNT),
                "15m": fetch_candles(GRANULARITIES["15m"], CANDLE_COUNT),
                "30m": fetch_candles(GRANULARITIES["30m"], CANDLE_COUNT),
                "1h": fetch_candles(GRANULARITIES["1h"], CANDLE_COUNT),
                "4h": fetch_candles(GRANULARITIES["4h"], CANDLE_COUNT),
            }

            if any(len(v) < 60 for v in tf_raw.values()):
                print("[SCAN] Not enough candle data yet")
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            tf = {name: summarize_tf(candles) for name, candles in tf_raw.items()}
            signal = build_signal(tf)

            now_ts = time.time()
            if signal:
                signature = (
                    signal["side"],
                    round(signal["entry"], 5),
                    tf["5m"]["last_time"],
                )
                if signature != last_signal or (now_ts - last_signal_ts) > ENTRY_COOLDOWN_SECONDS:
                    msg = format_signal(signal, tf)
                    print(f"[SIGNAL] {signal['side']} EUR/USD @ {signal['entry']:.5f}")
                    send_discord(msg, "EURUSD OANDA Bot")
                    last_signal = signature
                    last_signal_ts = now_ts
                else:
                    print("[SCAN] Signal found but still on cooldown")
            else:
                print("[SCAN] No setup right now")

        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
