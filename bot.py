"""
رادار العملات الرقمية — بوت توصيات تيليغرام
يشتغل على GitHub Actions كل 15 دقيقة، يفحص العملات، ينشر التوصيات ويتابع الأهداف ووقف الخسارة.
"""
import io
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------- الإعدادات ----------------
COINS = [
    ("BTC", "بيتكوين"), ("ETH", "إيثيريوم"), ("SOL", "سولانا"), ("XRP", "ريبل"),
    ("BNB", "بي إن بي"), ("DOGE", "دوجكوين"), ("ADA", "كاردانو"), ("TRX", "ترون"),
    ("TON", "تون"), ("SUI", "سوي"), ("AVAX", "أفالانش"), ("LINK", "تشين لينك"),
    ("DOT", "بولكادوت"), ("LTC", "لايتكوين"), ("BCH", "بيتكوين كاش"), ("PEPE", "بيبي"),
]
TIMEFRAME = "1h"            # إطار التحليل
MIN_SCORE = 2.5             # أقل قوة إشارة لنشر توصية (من 5)
REC_TTL_H = 24              # مدة صلاحية التوصية بالساعات
COOLDOWN_H = 1              # انتظار بعد إغلاق توصية قبل فتح جديدة على نفس العملة
MAX_LEV = 20                # سقف الرافعة المقترحة
DAILY_SUMMARY_UTC_HOUR = 18 # 18 UTC = 9 مساءً بتوقيت السعودية

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

BINANCE = ["https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com"]
OKX_BAR = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}


# ---------------- البيانات ----------------
def klines(sym, interval, limit=300):
    """شموع مرتبة من الأقدم للأحدث: [وقت الفتح ms, open, high, low, close, volume]"""
    for base in BINANCE:
        try:
            r = requests.get(f"{base}/api/v3/klines",
                             params={"symbol": sym + "USDT", "interval": interval, "limit": limit}, timeout=15)
            if r.ok:
                return [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in r.json()]
        except requests.RequestException:
            pass
    # بديل احتياطي: OKX
    out, after = [], None
    while len(out) < limit:
        params = {"instId": f"{sym}-USDT", "bar": OKX_BAR[interval], "limit": 300}
        if after:
            params["after"] = after
        r = requests.get("https://www.okx.com/api/v5/market/history-candles" if after else
                         "https://www.okx.com/api/v5/market/candles", params=params, timeout=15)
        data = r.json().get("data", []) if r.ok else []
        if not data:
            break
        out += [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in data]
        after = data[-1][0]
        if len(data) < 100:
            break
    if not out:
        raise RuntimeError(f"no data for {sym}")
    out.sort(key=lambda x: x[0])
    return out[-limit:]


# ---------------- المؤشرات (نفس منطق الصفحة) ----------------
def ema(a, n):
    k, o, e = 2 / (n + 1), [], None
    for i, v in enumerate(a):
        e = v if i == 0 else v * k + e * (1 - k)
        o.append(e)
    return o


def rsi(c, n=14):
    o = [None] * len(c)
    if len(c) <= n:
        return o
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n
    o[n] = 100 - 100 / (1 + (g / l if l else 1e9))
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        o[i] = 100 - 100 / (1 + (g / l if l else 1e9))
    return o


def atr(h, l, c, n=14):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[:n]) / n
    for i in range(n, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
    return a


def analyze(cs):
    if not cs or len(cs) < 60:
        return None
    h = [x[2] for x in cs]; l = [x[3] for x in cs]; c = [x[4] for x in cs]
    n = len(c) - 1; px = c[n]
    e20, e50 = ema(c, 20), ema(c, 50)
    R = rsi(c)[n]
    at = atr(h, l, c)
    m12, m26 = ema(c, 12), ema(c, 26)
    macd = [a - b for a, b in zip(m12, m26)]
    sig = ema(macd, 9)
    hist = [a - b for a, b in zip(macd, sig)]
    win = c[n - 19:n + 1]; mid = sum(win) / 20
    sd = (sum((x - mid) ** 2 for x in win) / 20) ** 0.5
    bbU, bbL = mid + 2 * sd, mid - 2 * sd
    hi, lo = max(h[n - 20:n]), min(l[n - 20:n])

    score, why = 0.0, []
    if px > e50[n]: score += 1; why.append("الاتجاه العام صاعد: السعر فوق EMA 50")
    else: score -= 1; why.append("الاتجاه العام هابط: السعر تحت EMA 50")
    if e20[n] > e50[n]: score += 1; why.append("تقاطع إيجابي: EMA 20 فوق EMA 50")
    else: score -= 1; why.append("تقاطع سلبي: EMA 20 تحت EMA 50")
    if R < 30: score += 1; why.append(f"تشبّع بيعي: RSI عند {R:.0f}")
    elif R > 70: score -= 1; why.append(f"تشبّع شرائي: RSI عند {R:.0f}")
    elif R >= 52: score += .5; why.append(f"زخم إيجابي: RSI عند {R:.0f}")
    elif R <= 48: score -= .5; why.append(f"زخم ضعيف: RSI عند {R:.0f}")
    if hist[n] > 0: score += 1; why.append("زخم MACD إيجابي")
    else: score -= 1; why.append("زخم MACD سلبي")
    score += .5 if hist[n] > hist[n - 1] else -.5
    if px > bbU: score -= .5
    if px < bbL: score += .5
    score = max(-5, min(5, score))

    if score >= 2:
        side, entry = "long", px
        sl = min(entry - 1.5 * at, max(lo - 0.2 * at, entry - 2.5 * at))
    elif score <= -2:
        side, entry = "short", px
        sl = max(entry + 1.5 * at, min(hi + 0.2 * at, entry + 2.5 * at))
    else:
        return {"side": "wait", "score": score, "px": px}
    d = 1 if side == "long" else -1
    r = abs(entry - sl)
    return {"side": side, "dir": d, "score": score, "px": px, "entry": entry, "sl": sl,
            "tp": [entry + d * k * r for k in (1, 2, 3)], "why": why, "rsi": R}


# ---------------- أدوات ----------------
def dec(p):
    p = abs(p)
    return 2 if p >= 100 else 3 if p >= 1 else 4 if p >= .1 else 5 if p >= .01 else 6 if p >= .0001 else 8


def fmt(p):
    return f"{p:,.{dec(p)}f}"


def pct(v):
    return f"{v:+.2f}%"


def suggest_lev(entry, sl):
    p = abs(entry - sl) / entry
    return max(1, min(MAX_LEV, int(1 / (p * 2))))


def pnl_pct(rec, exit_px):
    third, m = 1 / 3, 0.0
    hits = min(rec["hits"], 3)
    for i in range(hits):
        m += (rec["tp"][i] - rec["entry"]) / rec["entry"] * rec["dir"] * third
    m += (exit_px - rec["entry"]) / rec["entry"] * rec["dir"] * (1 - third * hits)
    return m * 100


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ---------------- تيليغرام ----------------
API = f"https://api.telegram.org/bot{TOKEN}"


def tg(method, files=None, **data):
    r = requests.post(f"{API}/{method}", data=data, files=files, timeout=30)
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not j.get("ok"):
        print(f"Telegram {method} failed: {r.status_code} {j.get('description', r.text[:200])}")
    return j.get("result")


def send(text, reply_to=None, photo=None):
    extra = {"parse_mode": "HTML"}
    if reply_to:
        extra["reply_to_message_id"] = reply_to
        extra["allow_sending_without_reply"] = "true"
    if photo is not None:
        res = tg("sendPhoto", files={"photo": ("chart.png", photo, "image/png")}, chat_id=CHAT_ID, caption=text, **extra)
        if res:
            return res.get("message_id")
    res = tg("sendMessage", chat_id=CHAT_ID, text=text, disable_web_page_preview="true", **extra)
    return res.get("message_id") if res else None


def discover_chat_id(st):
    """إذا ما في TELEGRAM_CHAT_ID، نأخذه من آخر رسالة أرسلتها للبوت."""
    if st.get("chat_id"):
        return st["chat_id"]
    ups = tg("getUpdates") or []
    for u in reversed(ups):
        msg = u.get("message") or u.get("channel_post") or {}
        if msg.get("chat", {}).get("id"):
            st["chat_id"] = str(msg["chat"]["id"])
            return st["chat_id"]
    return ""


# ---------------- صورة الشارت ----------------
def chart_png(sym, cs, rec):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except ImportError:
        return None
    d = cs[-60:]
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=120)
    bg, up, dn, ac = "#0f1418", "#2fbf8a", "#e5534b", "#d9a441"
    fig.patch.set_facecolor(bg); ax.set_facecolor(bg)
    for i, k in enumerate(d):
        col = up if k[4] >= k[1] else dn
        ax.plot([i, i], [k[3], k[2]], color=col, lw=1)
        ax.add_patch(Rectangle((i - .32, min(k[1], k[4])), .64, max(abs(k[4] - k[1]), 1e-12), color=col))
    x0, x1 = len(d) - 1, len(d) + 14
    e, sl, tp3 = rec["entry"], rec["sl"], rec["tp"][2]
    ax.add_patch(Rectangle((x0, min(e, tp3)), x1 - x0, abs(tp3 - e), color=up, alpha=.18))
    ax.add_patch(Rectangle((x0, min(e, sl)), x1 - x0, abs(sl - e), color=dn, alpha=.22))
    for v, col, lb in [(e, ac, "ENTRY"), (sl, dn, "SL"), (rec["tp"][0], up, "TP1"), (rec["tp"][1], up, "TP2"), (tp3, up, "TP3")]:
        ax.axhline(v, xmin=0, xmax=1, color=col, lw=.8, ls="--", alpha=.8)
        ax.text(x1 + .3, v, f" {lb} {fmt(v)}", color=bg, fontsize=8, va="center", family="monospace",
                bbox=dict(boxstyle="square,pad=0.25", fc=col, ec="none"))
    ax.set_xlim(-1, x1 + 9)
    ax.tick_params(colors="#8b9aa5", labelsize=8); ax.set_xticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.grid(axis="y", color="#26313a", lw=.6)
    side = "LONG" if rec["dir"] > 0 else "SHORT"
    ax.set_title(f"{sym}/USDT · 1H · {side}", color="#e7ecef", fontsize=11, loc="left", family="monospace")
    buf = io.BytesIO()
    fig.tight_layout(); fig.savefig(buf, format="png", facecolor=bg); plt.close(fig)
    return buf.getvalue()


# ---------------- الرسائل ----------------
def signal_text(rec, name):
    side = "LONG/BUY 🟢" if rec["dir"] > 0 else "SHORT/SELL 🔴"
    tps = "\n".join(f"Take-Profit Target {i + 1}: <code>{fmt(v)}</code>" for i, v in enumerate(rec["tp"]))
    return (f"🔥 <b>الصفقة الذهبية</b> — {name}\n\n"
            f"<b>SIGNAL</b>\n<b>{rec['sym']}/USDT</b>\n"
            f"Trade Type: <b>{side}</b>\n"
            f"Leverage: {rec['lev']}x (max)\n"
            f"Mode: ISOLATED\n"
            f"Entry Price: <code>{fmt(rec['entry'])}</code>\n"
            f"{tps}\n"
            f"Stop Loss: <code>{fmt(rec['sl'])}</code>\n\n"
            f"\u200f⏱ قوة الإشارة {abs(rec['score']):.1f}/5 على إطار الساعة\n"
            f"\u200f📊 أسباب التوصية:\n"
            + "".join(f"\u200f• {w}\n" for w in rec.get("why", [])[:4]) +
            f"\u200f💡 بعد الهدف 1 انقل الوقف لنقطة الدخول.")


# ---------------- المنطق الرئيسي ----------------
def track(rec, candles5, st, closes):
    """متابعة التوصية على شموع 5 دقائق منذ آخر فحص."""
    name = dict(COINS)[rec["sym"]]
    for k in candles5:
        if k[0] < rec["last"]:
            continue
        rec["last"] = k[0]
        hi, lo = k[2], k[3]
        stop = rec["entry"] if rec["hits"] >= 1 else rec["sl"]
        hit_stop = (lo <= stop) if rec["dir"] > 0 else (hi >= stop)
        if hit_stop:
            why = f"خروج على التعادل بعد الهدف {rec['hits']}" if rec["hits"] else "ضرب وقف الخسارة"
            close(rec, stop, why, st, closes)
            return
        while rec["hits"] < 3 and ((hi >= rec["tp"][rec["hits"]]) if rec["dir"] > 0 else (lo <= rec["tp"][rec["hits"]])):
            rec["hits"] += 1
            i = rec["hits"]
            tail = "انقل وقف الخسارة إلى نقطة الدخول 🔒" if i == 1 else "خذ جزءاً من الربح واترك الباقي للهدف 3" if i == 2 else ""
            send(f"🎯 <b>{rec['sym']}/USDT</b> — تحقق الهدف {i} ✅\n"
                 f"السعر: <code>{fmt(rec['tp'][i - 1])}</code> ({pct((rec['tp'][i - 1] - rec['entry']) / rec['entry'] * rec['dir'] * 100)}"
                 f" · ×{rec['lev']} = {pct((rec['tp'][i - 1] - rec['entry']) / rec['entry'] * rec['dir'] * 100 * rec['lev'])})\n{tail}",
                 reply_to=rec.get("msg"))
        if rec["hits"] >= 3:
            close(rec, rec["tp"][2], "تحققت كل الأهداف 🏆", st, closes)
            return


def close(rec, exit_px, why, st, closes):
    rec["status"] = "closed"; rec["exit"] = exit_px; rec["why_close"] = why
    rec["closed_at"] = int(time.time() * 1000); rec["res"] = pnl_pct(rec, exit_px)
    icon = "🏁" if rec["res"] > 0 else "⛔"
    send(f"{icon} <b>{rec['sym']}/USDT</b> — انتهت التوصية\n{why}\n"
         f"النتيجة: <b>{pct(rec['res'])}</b> على السعر (×{rec['lev']} = {pct(rec['res'] * rec['lev'])})",
         reply_to=rec.get("msg"))
    closes.append(rec["sym"])


def daily_summary(st):
    now = datetime.now(timezone.utc)
    key = now.strftime("%Y-%m-%d")
    if now.hour != DAILY_SUMMARY_UTC_HOUR or st.get("summary_day") == key:
        return
    st["summary_day"] = key
    day_ago = (time.time() - 86400) * 1000
    hist = [r for r in st.get("history", []) if r.get("closed_at", 0) >= day_ago]
    allh = st.get("history", [])
    wins = sum(1 for r in allh if r["res"] > 0)
    lines = [f"{'✅' if r['res'] > 0 else '❌'} {r['sym']} {'LONG' if r['dir'] > 0 else 'SHORT'}: {pct(r['res'])}" for r in hist]
    active = [f"⏳ {r['sym']} {'LONG' if r['dir'] > 0 else 'SHORT'} · أهداف {r['hits']}/3" for r in st.get("active", {}).values()]
    send("📋 <b>ملخص اليوم</b>\n\n" + ("\n".join(lines) if lines else "لا توجد توصيات أُغلقت اليوم.") +
         ("\n\n<b>النشطة الآن:</b>\n" + "\n".join(active) if active else "") +
         (f"\n\nنسبة النجاح الكلية: <b>{round(wins / len(allh) * 100)}%</b> من {len(allh)} توصية" if allh else ""))


def main():
    global CHAT_ID
    if not TOKEN:
        sys.exit("❌ TELEGRAM_BOT_TOKEN غير موجود. أضفه في Settings → Secrets and variables → Actions.")
    st = load_state()
    if not CHAT_ID:
        CHAT_ID = discover_chat_id(st)
    if not CHAT_ID:
        sys.exit("❌ ما قدرت أعرف محادثتك. افتح بوتك في تيليغرام واضغط Start أو أرسل له أي رسالة، ثم شغّل الـ workflow مرة ثانية.")

    if os.environ.get("TEST_MODE") == "true" or not st.get("hello"):
        send("✅ <b>رادار العملات الرقمية</b> متصل!\n"
             f"البوت يفحص {len(COINS)} عملة كل 15 دقيقة على إطار الساعة، "
             "وسيرسل لك هنا كل توصية جديدة، وكل هدف يتحقق، وكل وقف خسارة، وملخصاً يومياً الساعة 9 مساءً.")
        st["hello"] = True

    active = st.setdefault("active", {})
    hist = st.setdefault("history", [])
    last_close = st.setdefault("last_close", {})
    now_ms = int(time.time() * 1000)

    for sym, name in COINS:
        try:
            cs = klines(sym, TIMEFRAME, 300)
            a = analyze(cs)
            if not a:
                continue
            rec = active.get(sym)
            if rec:
                closes = []
                track(rec, klines(sym, "5m", 300), st, closes)
                if rec["status"] != "closed":
                    if now_ms - rec["t"] > REC_TTL_H * 3600e3:
                        close(rec, a["px"], "انتهت مدة التوصية (24 ساعة)", st, closes)
                    elif a["side"] in ("long", "short") and a["dir"] != rec["dir"] and abs(a["score"]) >= 3:
                        close(rec, a["px"], "انعكاس الإشارة", st, closes)
                if rec["status"] == "closed":
                    hist.insert(0, rec); del active[sym]; last_close[sym] = now_ms
                continue
            if a["side"] in ("long", "short") and abs(a["score"]) >= MIN_SCORE \
                    and now_ms - last_close.get(sym, 0) > COOLDOWN_H * 3600e3:
                rec = {"sym": sym, "dir": a["dir"], "entry": a["entry"], "sl": a["sl"], "tp": a["tp"],
                       "lev": suggest_lev(a["entry"], a["sl"]), "score": a["score"], "why": a["why"],
                       "t": now_ms, "last": now_ms - now_ms % 300000, "hits": 0, "status": "active"}
                rec["msg"] = send(signal_text(rec, name), photo=chart_png(sym, cs, rec))
                active[sym] = rec
                print(f"NEW {sym} {a['side']} @ {fmt(a['entry'])}")
            else:
                print(f"{sym}: {a['side']} ({a['score']:+.1f})")
        except Exception as e:  # عملة وحدة ما توقف الباقي
            print(f"{sym}: error {e}")

    st["history"] = hist[:100]
    daily_summary(st)
    save_state(st)


if __name__ == "__main__":
    main()
