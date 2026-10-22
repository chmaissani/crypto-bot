"""
رادار العملات الرقمية — بوت توصيات تيليغرام
يشتغل على GitHub Actions كل 15 دقيقة، يفحص العملات، ينشر التوصيات ويتابع الأهداف ووقف الخسارة.

الاستراتيجية (الإصدار 2): الدخول عند التصحيح داخل الاتجاه
- اتجاه العملة على إطار 4 ساعات واضح (السعر و EMA20 فوق EMA50، أو العكس للبيع)
- اتجاه البيتكوين على 4 ساعات بنفس اتجاه الصفقة (تحديث 2.1: المحايد لم يعد مقبولاً)
- السعر فوق EMA200 على الساعة (أو تحته للبيع) وقوة الاتجاه ADX ≥ 25
- RSI الساعة نزل تحت 40 خلال آخر 4 شموع ثم ارتد وأغلق فوق 40 (أو العكس للبيع)
- كل القرارات على شموع مغلقة فقط
اختُبرت على شهرين (أغسطس–أكتوبر 2026) لـ 14 عملة: نسبة نجاح 63% مقابل 45% للإصدار الأول.
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
# المعادن من عقود بايننس الدائمة (Futures). تجريبية: الاستراتيجية لم تُختبر عليها بعد.
# المصادر بالترتيب: بايننس فيوتشرز ← بديل (PAXG لبايننس للذهب، ثم أسعار COMEX من Yahoo)
METALS = [("XAU", "الذهب"), ("XAG", "الفضة"), ("COPPER", "النحاس")]
METAL_SRC = {
    "XAU": ["fapi:XAUUSDT", "spot:PAXGUSDT", "yahoo:GC=F"],
    "XAG": ["fapi:XAGUSDT", "yahoo:SI=F"],
    "COPPER": ["fapi:COPPERUSDT", "yahoo:HG=F"],
}
ENABLE_METALS = True        # اجعلها False لإيقاف توصيات المعادن
TIMEFRAME = "1h"            # إطار الدخول
RSI_LEVEL = 40              # مستوى التصحيح في RSI (للشراء تحت 40، للبيع فوق 60)
MIN_ADX = 25                # أقل قوة اتجاه مقبولة
ALLOW_SHORTS = True         # اجعلها False لتوصيات شراء فقط
SL_ATR = (1.5, 2.5)         # وقف الخسارة بين 1.5 و 2.5 ضعف ATR خلف آخر قاع/قمة
TP_R = (1, 2, 4)            # الأهداف كمضاعفات للمخاطرة
REC_TTL_H = 48              # مدة صلاحية التوصية بالساعات
COOLDOWN_H = 3              # انتظار بعد إغلاق توصية قبل فتح جديدة على نفس العملة
MAX_LEV = 20                # سقف الرافعة المقترحة
DAILY_SUMMARY_UTC_HOUR = 18 # 18 UTC = 9 مساءً بتوقيت السعودية
# «الصفقة الذهبية»: البيتكوين بنفس الاتجاه + اتجاه قوي جداً + تصحيح عميق وصل لـ EMA 50
GOLD_MIN_ADX = 35

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

BINANCE = ["https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com"]
OKX_BAR = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}


# ---------------- البيانات ----------------
ALL_NAMES = dict(COINS + METALS)
LAST_SRC = {}  # آخر مصدر نجح لكل رمز


def _parse_binance(rows):
    return [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in rows]


def _yahoo(ticker, interval, limit):
    """شموع COMEX من Yahoo. الـ 4 ساعات تُبنى من شموع الساعة (مجمّعة على حدود UTC)."""
    yi = {"5m": ("5m", "5d"), "1h": ("60m", "60d"), "4h": ("60m", "60d")}[interval]
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                     params={"interval": yi[0], "range": yi[1]},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        out.append([int(t) * 1000, float(o), float(h), float(l), float(c), float(v or 0)])
    if interval == "4h":
        agg = []
        for x in out:
            b = x[0] - x[0] % TF_MS["4h"]
            if agg and agg[-1][0] == b:
                a = agg[-1]; a[2] = max(a[2], x[2]); a[3] = min(a[3], x[3]); a[4] = x[4]; a[5] += x[5]
            else:
                agg.append([b] + x[1:])
        out = agg
    if not out:
        raise RuntimeError(f"no yahoo data {ticker}")
    return out[-limit:]


def metal_klines(sym, interval, limit):
    errors = []
    for src in METAL_SRC[sym]:
        kind, ticker = src.split(":")
        try:
            if kind == "fapi":
                r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                                 params={"symbol": ticker, "interval": interval, "limit": limit}, timeout=15)
            elif kind == "spot":
                r = requests.get(f"{BINANCE[0]}/api/v3/klines",
                                 params={"symbol": ticker, "interval": interval, "limit": limit}, timeout=15)
            else:
                data = _yahoo(ticker, interval, limit)
                LAST_SRC[sym] = src
                return data
            if r.ok and r.json():
                LAST_SRC[sym] = src
                return _parse_binance(r.json())
            errors.append(f"{src}:{r.status_code}")
        except Exception as e:
            errors.append(f"{src}:{type(e).__name__}")
    raise RuntimeError(f"no data for {sym} ({', '.join(errors)})")


def klines(sym, interval, limit=300):
    """شموع مرتبة من الأقدم للأحدث: [وقت الفتح ms, open, high, low, close, volume]"""
    if sym in METAL_SRC:
        return metal_klines(sym, interval, limit)
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


TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def closed_klines(sym, interval, limit):
    """الشموع المغلقة فقط، حتى لا تتغير الإشارة قبل إغلاق الشمعة."""
    now = time.time() * 1000
    return [k for k in klines(sym, interval, limit + 1) if k[0] + TF_MS[interval] <= now][-limit:]


# ---------------- المؤشرات ----------------
def ema(a, n):
    k, o, e = 2 / (n + 1), [], None
    for i, v in enumerate(a):
        e = v if i == 0 else v * k + e * (1 - k)
        o.append(e)
    return o


def rsi(c, n=14):
    o = [50.0] * len(c)
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


def adx(h, l, c, n=14):
    tr, pdm, ndm = [0.0], [0.0], [0.0]
    for i in range(1, len(c)):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    a_tr, p, m = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(ndm[1:n + 1])
    dxs, val = [], 0.0
    for i in range(n + 1, len(c)):
        a_tr = a_tr - a_tr / n + tr[i]; p = p - p / n + pdm[i]; m = m - m / n + ndm[i]
        pdi = 100 * p / a_tr if a_tr else 0; mdi = 100 * m / a_tr if a_tr else 0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0
        if len(dxs) < n:
            dxs.append(dx)
            val = sum(dxs) / len(dxs)
        else:
            val = (val * (n - 1) + dx) / n
    return val


def trend_4h(cs4):
    """1 صاعد، -1 هابط، 0 بدون اتجاه واضح — على شموع 4 ساعات مغلقة."""
    if len(cs4) < 60:
        return 0
    c = [x[4] for x in cs4]
    e20, e50 = ema(c, 20)[-1], ema(c, 50)[-1]
    if c[-1] > e50 and e20 > e50:
        return 1
    if c[-1] < e50 and e20 < e50:
        return -1
    return 0


def setup(cs, cs4, btc_trend, diag=None, metal=False):
    """يرجع توصية إذا تحقق إعداد «التصحيح داخل الاتجاه» على آخر شمعة مغلقة، وإلا None.
    إذا مُرّر diag (قاموس) يُكتب فيه سبب عدم وجود إشارة."""
    diag = {} if diag is None else diag
    if len(cs) < 220:
        diag["why"] = "data"
        return None
    h = [x[2] for x in cs]; l = [x[3] for x in cs]; c = [x[4] for x in cs]
    px = c[-1]
    R = rsi(c)
    e200 = ema(c, 200)[-1]
    e50 = ema(c, 50)[-1]
    tr = trend_4h(cs4)
    strength = adx(h, l, c)
    at = atr(h, l, c)
    lvl = RSI_LEVEL
    d = 0
    if tr == 1 and min(R[-5:-1]) < lvl and R[-1] > R[-2] and R[-1] >= lvl and px > e200:
        d = 1
    elif ALLOW_SHORTS and tr == -1 and max(R[-5:-1]) > 100 - lvl and R[-1] < R[-2] and R[-1] <= 100 - lvl and px < e200:
        d = -1
    diag.update(trend=tr, adx=round(strength), rsi=round(R[-1]))
    if d == 0 or strength < MIN_ADX or btc_trend != d:
        want = tr if (tr == 1 or (tr == -1 and ALLOW_SHORTS)) else 0
        if tr == 0:
            diag["why"] = "no_trend"
        elif want == 0:
            diag["why"] = "shorts_off"
        elif btc_trend != want:
            diag["why"] = "btc"
        elif (want > 0 and px <= e200) or (want < 0 and px >= e200):
            diag["why"] = "ema200"
        elif strength < MIN_ADX:
            diag["why"] = "weak"
        elif (want > 0 and min(R[-5:-1]) >= lvl) or (want < 0 and max(R[-5:-1]) <= 100 - lvl):
            diag["why"] = "no_pullback"
        else:
            diag["why"] = "near"   # التصحيح حصل وننتظر الارتداد
        return None
    hi, lo = max(h[-13:]), min(l[-13:])
    smin, smax = SL_ATR
    if d > 0:
        sl = min(px - smin * at, max(lo - 0.2 * at, px - smax * at))
    else:
        sl = max(px + smin * at, min(hi + 0.2 * at, px + smax * at))
    r = abs(px - sl)
    dip = min(R[-5:-1]) if d > 0 else max(R[-5:-1])
    why = [
        "الاتجاه على 4 ساعات " + ("صاعد" if d > 0 else "هابط") + ": EMA 20 " + ("فوق" if d > 0 else "تحت") + " EMA 50",
        "السعر " + ("فوق" if d > 0 else "تحت") + " EMA 200 على الساعة",
        f"قوة الاتجاه: ADX عند {strength:.0f}",
        f"تصحيح انتهى: RSI {'نزل إلى' if d > 0 else 'صعد إلى'} {dip:.0f} ثم ارتد إلى {R[-1]:.0f}",
        ("المعادن لا تتبع البيتكوين، فلا يُطبَّق فلتره هنا" if metal else
         "اتجاه البيتكوين على 4 ساعات " + ("صاعد" if d > 0 else "هابط") + " ويدعم الصفقة"),
    ]
    gold = (not metal) and btc_trend == d and strength >= GOLD_MIN_ADX and (px - e50) * d <= 0
    if gold:
        why.append("تصحيح عميق وصل لمتوسط EMA 50")
    return {"dir": d, "gold": gold, "entry": px, "sl": sl, "r": r, "tp": [px + d * k * r for k in TP_R],
            "adx": strength, "rsi": R[-1], "why": why, "bar": cs[-1][0]}


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
    if rec.get("metal"):
        title = f"🧪 <b>توصية تجريبية</b> — {name}"
    elif rec.get("gold"):
        title = f"🔥 <b>الصفقة الذهبية</b> — {name}"
    else:
        title = f"📊 <b>توصية</b> — {name}"
    metal_note = ""
    if rec.get("metal"):
        e = rec["entry"]
        sl_p = abs(rec["sl"] - e) / e * 100
        tp_p = " / ".join(f"{abs(v - e) / e * 100:.2f}%" for v in rec["tp"])
        metal_note = ("\u200f🧪 المعادن جديدة على البوت ولم تُختبر الاستراتيجية عليها بعد، فنتائجها تُحسب منفصلة.\n"
                      "\u200f📌 العقد على بايننس فيوتشرز: " + rec["sym"] + "USDT\n")
        if not rec.get("src", "").startswith("fapi"):
            metal_note += (f"\u200f⚠️ الأسعار من مصدر بديل ({rec.get('src', '').split(':')[-1]}) وقد تختلف قليلاً عن بايننس. "
                           f"اعتمد النسب من سعر دخولك: الوقف {sl_p:.2f}% والأهداف {tp_p}\n")
    return (f"{title}\n\n"
            f"<b>SIGNAL</b>\n<b>{rec['sym']}/USDT</b>\n"
            f"Trade Type: <b>{side}</b>\n"
            f"Leverage: {rec['lev']}x (max)\n"
            f"Mode: ISOLATED\n"
            f"Entry Price: <code>{fmt(rec['entry'])}</code>\n"
            f"{tps}\n"
            f"Stop Loss: <code>{fmt(rec['sl'])}</code>\n\n"
            + metal_note
            + ("\u200f⭐ أقوى نوع توصيات عند البوت: نجح 3 من كل 4 في اختبار شهرين\n" if rec.get("gold") else "")
            + f"\u200f⏱ إطار الساعة · تصحيح داخل الاتجاه\n"
            f"\u200f📊 أسباب التوصية:\n"
            + "".join(f"\u200f• {w}\n" for w in rec.get("why", [])) +
            f"\u200f💡 أغلق ثلث الصفقة عند كل هدف، وانقل الوقف لنقطة الدخول بعد الهدف 1.")


# ---------------- المنطق الرئيسي ----------------
def track(rec, candles5, st, closes):
    """متابعة التوصية على شموع 5 دقائق منذ آخر فحص."""
    name = ALL_NAMES.get(rec["sym"], rec["sym"])
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


REASON_AR = {
    "btc": "اتجاهها لا يطابق اتجاه البيتكوين",
    "no_trend": "بدون اتجاه واضح على 4 ساعات",
    "weak": "الاتجاه ضعيف (ADX تحت الحد)",
    "ema200": "السعر في الجهة الخطأ من EMA 200",
    "no_pullback": "الاتجاه جيد وننتظر تصحيحاً",
    "near": "التصحيح حصل وننتظر الارتداد ⏳",
    "shorts_off": "اتجاه هابط والبيع (Short) مقفل",
    "error": "تعذر جلب بياناتها",
    "data": "بيانات غير كافية",
}


def market_status(st):
    m = st.get("market") or {}
    if not m:
        return ""
    btc = {1: "صاعد 📈", -1: "هابط 📉", 0: "محايد ↔️"}[m.get("btc", 0)]
    out = [f"\n\n🌡 <b>حالة السوق الآن</b>\n\u200fاتجاه البيتكوين على 4 ساعات: <b>{btc}</b>"]
    if m.get("btc_err"):
        out.append("\u200f⚠️ تعذر قراءة اتجاه البيتكوين في آخر فحص، لذلك توقفت التوصيات مؤقتاً")
    elif m.get("btc", 0) == 0:
        out.append("\u200fالبوت لا يفتح صفقات والبيتكوين محايد، وينتظر اتجاهاً واضحاً")
    elif m.get("btc") == -1 and not ALLOW_SHORTS:
        out.append("\u200fالبيتكوين هابط وتوصيات البيع مقفلة، فالبوت ينتظر عودة الصعود")
    groups = {}
    for sym, why in (m.get("coins") or {}).items():
        groups.setdefault(why, []).append(sym)
    for why in ["near", "no_pullback", "btc", "no_trend", "weak", "ema200", "shorts_off", "error", "data"]:
        if why in groups:
            out.append(f"\u200f• {REASON_AR[why]}: {', '.join(groups[why])}")
    last = st.get("last_signal")
    if last:
        days = (time.time() * 1000 - last) / 86400000
        if days >= 1:
            n = round(days)
            word = "يوم" if n == 1 else "يومين" if n == 2 else f"{n} أيام" if n <= 10 else f"{n} يوماً"
            out.append(f"\u200fآخر توصية جديدة كانت قبل {word}")
    return "\n".join(out)


def daily_summary(st):
    now = datetime.now(timezone.utc)
    key = now.strftime("%Y-%m-%d")
    if now.hour != DAILY_SUMMARY_UTC_HOUR or st.get("summary_day") == key:
        return
    st["summary_day"] = key
    day_ago = (time.time() - 86400) * 1000
    hist = [r for r in st.get("history", []) if r.get("closed_at", 0) >= day_ago]
    allh = [r for r in st.get("history", []) if r.get("v") == 2 and not r.get("metal")] or \
        [r for r in st.get("history", []) if not r.get("metal")]
    mh = [r for r in st.get("history", []) if r.get("metal")]
    wins = sum(1 for r in allh if r["res"] > 0)
    lines = [f"{'✅' if r['res'] > 0 else '❌'} {'🔥' if r.get('gold') else '🧪' if r.get('metal') else ''}{r['sym']} {'LONG' if r['dir'] > 0 else 'SHORT'}: {pct(r['res'])}" for r in hist]
    active = [f"⏳ {'🔥' if r.get('gold') else '🧪' if r.get('metal') else ''}{r['sym']} {'LONG' if r['dir'] > 0 else 'SHORT'} · أهداف {r['hits']}/3" for r in st.get("active", {}).values()]
    gh = [r for r in allh if r.get("gold")]
    market_txt = market_status(st)
    metal_line = (f"\nنسبة نجاح المعادن 🧪 (تجريبية): <b>{round(sum(1 for r in mh if r['res'] > 0) / len(mh) * 100)}%</b> من {len(mh)}") if mh else ""
    gold_line = (f"\nنسبة نجاح الصفقات الذهبية 🔥: <b>{round(sum(1 for r in gh if r['res'] > 0) / len(gh) * 100)}%</b> من {len(gh)}") if gh else ""
    send("📋 <b>ملخص اليوم</b>\n\n" + ("\n".join(lines) if lines else "لا توجد توصيات أُغلقت اليوم.") +
         ("\n\n<b>النشطة الآن:</b>\n" + "\n".join(active) if active else "") +
         (f"\n\nنسبة النجاح{' للإصدار 2' if allh[0].get('v') == 2 else ''}: <b>{round(wins / len(allh) * 100)}%</b> من {len(allh)} توصية" if allh else "") + gold_line + metal_line + market_txt)


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
             f"البوت يفحص {len(COINS)} عملة كل 15 دقيقة، "
             "وسيرسل لك هنا كل توصية جديدة، وكل هدف يتحقق، وكل وقف خسارة، وملخصاً يومياً الساعة 9 مساءً.")
        st["hello"] = True
    if st.get("version") != 2:
        send("🆕 <b>تحديث البوت — الإصدار 2</b>\n\n"
             "صار البوت يدخل فقط عند <b>التصحيح داخل اتجاه قوي</b>، مع فلتر لاتجاه البيتكوين، وقرارات على شموع مغلقة فقط.\n\n"
             "النتيجة على اختبار شهرين لـ14 عملة:\n"
             "\u200f• نسبة النجاح من 45% إلى 63%\n"
             "\u200f• عدد التوصيات من 16 إلى نحو 2–3 يومياً\n"
             "\u200f• أقصى تراجع متتالي أقل بعشر مرات\n\n"
             "التوصيات المفتوحة من الإصدار القديم سيكمل البوت متابعتها حتى تُغلق.")
        st["version"] = 2
    if ENABLE_METALS and not st.get("metals"):
        send("🧪 <b>إضافة المعادن: الذهب والفضة والنحاس</b>\n\n"
             "صار البوت يفحص عقود بايننس فيوتشرز: XAUUSDT و XAGUSDT و COPPERUSDT بنفس استراتيجية التصحيح داخل الاتجاه.\n\n"
             "\u200f• المعادن لا تتبع البيتكوين، فيُستخدم اتجاه المعدن نفسه بدل فلتر البيتكوين\n"
             "\u200f• الاستراتيجية لم تُختبر على المعادن بعد، لذلك توصياتها بعنوان «🧪 توصية تجريبية» ونتائجها تُحسب منفصلة\n"
             "\u200f• الملخص اليومي يعرض نسبة نجاح المعادن لحالها")
        st["metals"] = True
    if st.get("v21") and not st.get("v22"):
        send("🔧 <b>تحديث 2.2</b>\n\n"
             "صار الملخص اليومي يعرض <b>حالة السوق</b>: اتجاه البيتكوين، وسبب عدم وجود توصية لكل عملة، والعملات القريبة من إشارة.\n"
             "هيك بتعرف إذا السوق ساكت أو في مشكلة بالبوت.")
        st["v22"] = True
    if not st.get("v21"):
        send("🔧 <b>تحديث 2.1</b>\n\n"
             "صار البوت يشترط أن يكون <b>البيتكوين بنفس اتجاه الصفقة</b>، وما عاد يقبل البيتكوين المحايد.\n\n"
             "\u200f• آخر صفقتين خاسرتين (TRX و AVAX) فُتحتا والبيتكوين محايد، قبل هبوط السوق\n"
             "\u200f• على اختبار شهرين: نسبة النجاح من 62% إلى 68%، وتحسنت في كل فترات الاختبار\n"
             "\u200f• التوصيات أقل قليلاً: نحو 2 يومياً")
        st["v21"] = True
        st["v22"] = True

    active = st.setdefault("active", {})
    hist = st.setdefault("history", [])
    last_close = st.setdefault("last_close", {})
    now_ms = int(time.time() * 1000)

    market = st.setdefault("market", {})
    try:
        btc_trend = trend_4h(closed_klines("BTC", "4h", 120))
        market["btc_err"] = ""
    except Exception as e:
        print(f"BTC trend error {e}")
        btc_trend = 0
        market["btc_err"] = str(e)[:120]
    market["btc"] = btc_trend
    market["t"] = now_ms
    coin_status = market.setdefault("coins", {})
    print(f"BTC 4h trend: {btc_trend:+d}")

    universe = COINS + (METALS if ENABLE_METALS else [])
    for sym, name in universe:
        is_metal = sym in METAL_SRC
        try:
            rec = active.get(sym)
            if rec:
                closes = []
                track(rec, klines(sym, "5m", 300), st, closes)
                if rec["status"] != "closed" and now_ms - rec["t"] > REC_TTL_H * 3600e3:
                    last = klines(sym, "5m", 1)[-1][4]
                    close(rec, last, f"انتهت مدة التوصية ({REC_TTL_H} ساعة)", st, closes)
                if rec["status"] == "closed":
                    hist.insert(0, rec); del active[sym]; last_close[sym] = now_ms
                continue
            if now_ms - last_close.get(sym, 0) < COOLDOWN_H * 3600e3:
                continue
            cs = closed_klines(sym, TIMEFRAME, 400)
            if st.setdefault("last_bar", {}).get(sym) == cs[-1][0]:
                continue  # هذه الشمعة فُحصت سابقاً
            st["last_bar"][sym] = cs[-1][0]
            dg = {}
            cs4 = closed_klines(sym, "4h", 120)
            # المعادن لا تتبع البيتكوين: نستخدم اتجاهها هي على 4 ساعات بدل فلتر البيتكوين
            sig = setup(cs, cs4, trend_4h(cs4) if is_metal else btc_trend, dg, metal=is_metal)
            coin_status[sym] = dg.get("why", "signal")
            if not sig:
                print(f"{sym}: no setup ({dg})")
                continue
            st["last_signal"] = now_ms
            # لا ننشر إذا تحرك السعر كثيراً منذ إغلاق الشمعة
            live = klines(sym, "5m", 1)[-1][4]
            if abs(live - sig["entry"]) > 0.5 * sig["r"]:
                print(f"{sym}: setup skipped, price moved away ({fmt(live)})")
                continue
            rec = {"sym": sym, "dir": sig["dir"], "entry": sig["entry"], "sl": sig["sl"], "tp": sig["tp"],
                   "lev": suggest_lev(sig["entry"], sig["sl"]), "why": sig["why"], "v": 2, "gold": sig["gold"], "metal": is_metal, "src": LAST_SRC.get(sym, "binance"),
                   "t": now_ms, "last": cs[-1][0] + TF_MS[TIMEFRAME], "hits": 0, "status": "active"}
            rec["msg"] = send(signal_text(rec, name), photo=chart_png(sym, cs, rec))
            active[sym] = rec
            print(f"NEW {sym} {'LONG' if sig['dir'] > 0 else 'SHORT'} @ {fmt(sig['entry'])}{' GOLD' if sig['gold'] else ''}")
        except Exception as e:  # عملة وحدة ما توقف الباقي
            print(f"{sym}: error {e}")
            coin_status[sym] = "error"

    st["history"] = hist[:100]
    daily_summary(st)
    save_state(st)


if __name__ == "__main__":
    main()
