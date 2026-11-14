"""
اختبار استراتيجيات المعادن (الذهب والفضة والبلاتين والبالاديوم والنحاس) على سنتين من شموع الساعة الحقيقية (COMEX من Yahoo).
القواعد مثل البوت: إشارة عند إغلاق الشمعة، وقف خسارة يُفحص أولاً في كل شمعة، عمولة وانزلاق 0.05%.
نختار على أول 60% من الفترة، ونحكم على آخر 40% (بيانات لم تُستخدم في الاختيار).
يرسل النتيجة على تيليغرام.
"""
import os
import sys
import time

import requests

TICKERS = {"XAU": ("GC=F", "الذهب"), "XAG": ("SI=F", "الفضة"), "XPT": ("PL=F", "البلاتين"),
           "XPD": ("PA=F", "البالاديوم"), "COPPER": ("HG=F", "النحاس")}
FEE = 0.0005
H = 3_600_000


# ---------------- البيانات ----------------
def yahoo_1h(ticker):
    for attempt in range(3):
        for host in ("query1", "query2"):
            try:
                r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}",
                                 params={"interval": "60m", "range": "730d"},
                                 headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
                r.raise_for_status()
                res = r.json()["chart"]["result"][0]
                q = res["indicators"]["quote"][0]
                out = []
                for i, t in enumerate(res.get("timestamp") or []):
                    o, h, l, c = (q[k][i] for k in ("open", "high", "low", "close"))
                    if None in (o, h, l, c):
                        continue
                    out.append([int(t) * 1000, float(o), float(h), float(l), float(c)])
                if len(out) > 1000:
                    return out
            except Exception as e:  # noqa: BLE001
                print(f"{ticker} {host}: {e}")
        time.sleep(3)
    raise RuntimeError(f"no data {ticker}")


def aggregate(cs, ms):
    out = []
    for x in cs:
        b = x[0] - x[0] % ms
        if out and out[-1][0] == b:
            a = out[-1]; a[2] = max(a[2], x[2]); a[3] = min(a[3], x[3]); a[4] = x[4]
        else:
            out.append([b] + x[1:])
    return out


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
        d = c[i] - c[i - 1]; g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n
    o[n] = 100 - 100 / (1 + (g / l if l else 1e9))
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n; l = (l * (n - 1) + max(-d, 0)) / n
        o[i] = 100 - 100 / (1 + (g / l if l else 1e9))
    return o


def atr(h, l, c, n=14):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    o = [tr[0]] * len(c); a = sum(tr[:n]) / n
    for i in range(n, len(tr)):
        a = (a * (n - 1) + tr[i]) / n; o[i] = a
    return o


def adx(h, l, c, n=14):
    o = [0.0] * len(c)
    tr, pdm, ndm = [0.0], [0.0], [0.0]
    for i in range(1, len(c)):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0); ndm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    a_tr, p, m = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(ndm[1:n + 1])
    dxs, val = [], 0.0
    for i in range(n + 1, len(c)):
        a_tr = a_tr - a_tr / n + tr[i]; p = p - p / n + pdm[i]; m = m - m / n + ndm[i]
        pdi = 100 * p / a_tr if a_tr else 0; mdi = 100 * m / a_tr if a_tr else 0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0
        if len(dxs) < n:
            dxs.append(dx); val = sum(dxs) / len(dxs)
        else:
            val = (val * (n - 1) + dx) / n
        o[i] = val
    return o


class Series:
    def __init__(self, cs):
        self.t = [x[0] for x in cs]; self.o = [x[1] for x in cs]; self.h = [x[2] for x in cs]
        self.l = [x[3] for x in cs]; self.c = [x[4] for x in cs]
        self.e20 = ema(self.c, 20); self.e50 = ema(self.c, 50); self.e200 = ema(self.c, 200)
        self.rsi = rsi(self.c); self.atr = atr(self.h, self.l, self.c); self.adx = adx(self.h, self.l, self.c)
        self.atr22 = atr(self.h, self.l, self.c, 22); self.e25 = ema(self.c, 25)
        # عرض بولنجر (5، 2) نسبةً للسعر ومتوسطه على 42 شمعة — لفكرة «الانضغاط ثم الانفجار»
        bw = []
        for i in range(len(self.c)):
            w = self.c[max(0, i - 4):i + 1]
            m = sum(w) / len(w)
            sd = (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5
            bw.append(4 * sd / m if m else 0)
        self.bw = bw
        self.bw_avg = [sum(bw[max(0, i - 41):i + 1]) / min(42, i + 1) for i in range(len(bw))]

    def trend(self, j):
        """اتجاه الشمعة j: 1 صاعد، -1 هابط، 0 محايد (مثل البوت)."""
        if j < 55:
            return 0
        if self.c[j] > self.e50[j] and self.e20[j] > self.e50[j]:
            return 1
        if self.c[j] < self.e50[j] and self.e20[j] < self.e50[j]:
            return -1
        return 0


def closed_index(base_t, higher, ms, base_ms=H):
    """لكل شمعة دنيا: رقم آخر شمعة عليا مغلقة عند إغلاقها (بدون نظر للمستقبل)."""
    idx, j = [], -1
    for t in base_t:
        end = t + base_ms
        while j + 1 < len(higher.t) and higher.t[j + 1] + ms <= end:
            j += 1
        idx.append(j)
    return idx


# ---------------- الإشارات ----------------
def sig_pullback(s, i, d_trend, p):
    lvl = p.get("lvl", 40)
    if s.adx[i] < p.get("adx", 25) or i < 210:
        return 0
    R = s.rsi
    if d_trend == 1 and min(R[i - 4:i]) < lvl and R[i] > R[i - 1] and R[i] >= lvl and s.c[i] > s.e200[i]:
        return 1
    if p.get("shorts") and d_trend == -1 and max(R[i - 4:i]) > 100 - lvl and R[i] < R[i - 1] and R[i] <= 100 - lvl and s.c[i] < s.e200[i]:
        return -1
    return 0


def sig_breakout(s, i, d_trend, p):
    n = p.get("don", 20)
    if i < max(n, 210):
        return 0
    if d_trend == 1 and s.c[i] > max(s.c[i - n:i]):
        return 1
    if p.get("shorts") and d_trend == -1 and s.c[i] < min(s.c[i - n:i]):
        return -1
    return 0


def sig_squeeze(s, i, d_trend, p):
    """تقريب لفكرة مؤشر Sami_algo Liquidity: انضغاط بولنجر خلال آخر 9 شموع ثم كسر نطاق 12 شمعة،
    بنفس اتجاه EMA 25 (فلتر الفريم الأعلى). تقييم الإشارة من 6 غير معروف لأن الكود مقفل، فلا يُطبَّق."""
    if i < 60:
        return 0
    squeezed = any(s.bw[k] < p.get("sens", 0.75) * s.bw_avg[k] for k in range(i - 9, i))
    if not squeezed:
        return 0
    n = p.get("range", 12)
    up = s.c[i] > max(s.h[i - n:i]); dn = s.c[i] < min(s.l[i - n:i])
    trend = 1 if s.c[i] > s.e25[i] else -1
    if up and trend == 1 and (not p.get("need_trend") or d_trend == 1):
        return 1
    if dn and trend == -1 and p.get("shorts", True) and (not p.get("need_trend") or d_trend == -1):
        return -1
    return 0


# ---------------- المحاكاة ----------------
def run(s, trend_of, kind, p):
    trades, i, n = [], 210, len(s.c)
    last_exit = -999
    while i < n - 1:
        if i - last_exit < p.get("cool", 3):
            i += 1; continue
        tr = trend_of(i)
        d = {"pullback": sig_pullback, "breakout": sig_breakout, "squeeze": sig_squeeze}[kind](s, i, tr, p)
        if p.get("daily") and d:
            if p["daily"](i) != d:
                d = 0
        if not d:
            i += 1; continue
        e, at = s.c[i], s.atr[i]
        if kind == "pullback":
            sw = 12
            hi, lo = max(s.h[i - sw:i + 1]), min(s.l[i - sw:i + 1])
            smin, smax = p.get("sl", (1.5, 2.5))
            sl = min(e - smin * at, max(lo - 0.2 * at, e - smax * at)) if d > 0 else max(e + smin * at, min(hi + 0.2 * at, e + smax * at))
        elif kind == "squeeze":
            sl = e - d * p.get("stop", 2.2) * s.atr22[i]
        else:
            sl = e - d * p.get("stop", 2.5) * at
        r = abs(e - sl)
        tps = [e + d * k * r for k in p.get("tps", (1, 2, 4))]
        hits, stop, j, res, best = 0, sl, i + 1, None, e
        while j < n and j <= i + p.get("ttl", 48):
            hi, lo = s.h[j], s.l[j]
            if (lo <= stop) if d > 0 else (hi >= stop):
                res = (min(stop, s.o[j]) if d > 0 else max(stop, s.o[j])); break
            if kind in ("pullback", "squeeze"):
                while hits < 3 and ((hi >= tps[hits]) if d > 0 else (lo <= tps[hits])):
                    hits += 1
                    if hits == 1:
                        stop = e
                if hits == 3:
                    res = tps[2]; break
            else:
                best = max(best, s.c[j]) if d > 0 else min(best, s.c[j])
                trail = best - d * p.get("trail", 3.0) * s.atr[j]
                stop = max(stop, trail) if d > 0 else min(stop, trail)
            j += 1
        j = min(j, n - 1)
        if res is None:
            res = s.c[j]
        if kind in ("pullback", "squeeze"):
            gain = sum((tps[k] - e) * d / 3 for k in range(min(hits, 3))) + (res - e) * d * (1 - min(hits, 3) / 3)
        else:
            gain = (res - e) * d
        trades.append({"t": s.t[i], "pct": gain / e * 100 - FEE * 100, "d": d, "bars": j - i})
        last_exit = j
        i = j + 1
    return trades


def stats(tr):
    if not tr:
        return None
    w = [x for x in tr if x["pct"] > 0]
    gp = sum(x["pct"] for x in w); gl = -sum(x["pct"] for x in tr if x["pct"] <= 0)
    return {"n": len(tr), "win": len(w) / len(tr) * 100, "avg": sum(x["pct"] for x in tr) / len(tr),
            "pf": gp / gl if gl else 99, "sum": sum(x["pct"] for x in tr)}


def main():
    data = {}
    for sym, (tk, name) in TICKERS.items():
        cs = yahoo_1h(tk)
        h1 = Series(cs); h4 = Series(aggregate(cs, 4 * H)); d1 = Series(aggregate(cs, 24 * H))
        data[sym] = (name, h1, h4, d1)
        print(f"{sym}: {len(cs)} hourly candles, {len(h4.c)} x 4h, {len(d1.c)} days")
        time.sleep(1)

    def strategies(h1, h4, d1):
        i4 = closed_index(h1.t, h4, 4 * H)
        i1d = closed_index(h1.t, d1, 24 * H)
        i4d = closed_index(h4.t, d1, 24 * H, 4 * H)
        t1 = lambda i: h4.trend(i4[i])
        d_of_1h = lambda i: d1.trend(i1d[i])
        d_of_4h = lambda i: d1.trend(i4d[i])
        return [x for x in [
            ("الحالية: تصحيح ساعة، شراء وبيع", h1, t1, "pullback", {"shorts": True}),
            ("تصحيح ساعة، شراء فقط", h1, t1, "pullback", {}),
            ("تصحيح ساعة، شراء فقط + اتجاه يومي", h1, t1, "pullback", {"daily": d_of_1h}),
            ("تصحيح ساعة، شراء فقط، وقف أوسع", h1, t1, "pullback", {"sl": (2.5, 3.5)}),
            ("تصحيح 4 ساعات، شراء فقط", h4, d_of_4h, "pullback", {"adx": 20, "ttl": 30}),
            ("تصحيح 4 ساعات، شراء وبيع", h4, d_of_4h, "pullback", {"adx": 20, "ttl": 30, "shorts": True}),
            ("اختراق 4 ساعات + وقف متحرك، شراء فقط", h4, d_of_4h, "breakout", {"ttl": 60, "cool": 1}),
            ("اختراق 4 ساعات + وقف متحرك، شراء وبيع", h4, d_of_4h, "breakout", {"ttl": 60, "cool": 1, "shorts": True}),
            ("اختراق ساعة + وقف متحرك، شراء فقط", h1, t1, "breakout", {"don": 24, "ttl": 72, "cool": 3}),
            ("فكرة مؤشر سامي: انضغاط + كسر، ساعة", h1, t1, "squeeze", {"tps": (0.9, 2, 3), "ttl": 48, "cool": 0}),
            ("فكرة مؤشر سامي: انضغاط + كسر، 4 ساعات", h4, d_of_4h, "squeeze", {"tps": (0.9, 2, 3), "ttl": 30, "cool": 0}),
            ("فكرة مؤشر سامي + اتجاه 4 ساعات، ساعة", h1, t1, "squeeze", {"tps": (0.9, 2, 3), "ttl": 48, "cool": 0, "need_trend": True}),
        ] if x[0].startswith("اختراق 4 ساعات")]  # الطريقة الرابحة فقط

    names = None
    rows = {}
    split_t = None
    per_metal = {}
    for sym, (name, h1, h4, d1) in data.items():
        split_t = h1.t[int(len(h1.t) * 0.6)]
        for label, s, trend_of, kind, p in strategies(h1, h4, d1):
            tr = run(s, trend_of, kind, p)
            rows.setdefault(label, []).extend(tr)
            per_metal.setdefault(label, {})[sym] = tr
        names = names or [x[0] for x in strategies(h1, h4, d1)]

    def f(st):
        if not st:
            return "لا صفقات"
        return f"{st['n']} صفقة · نجاح {st['win']:.0f}% · متوسط {st['avg']:+.2f}% · PF {st['pf']:.2f}"

    scored = []
    for label in names:
        tr = rows[label]
        a = stats([x for x in tr if x["t"] < split_t]); b = stats([x for x in tr if x["t"] >= split_t])
        score = min(a["avg"] if a else -9, b["avg"] if b else -9)
        scored.append((score, label, a, b))
        print(label, a, b)
    scored.sort(key=lambda x: -x[0])
    out = ["🥇 <b>اختبار استراتيجيات المعادن</b>",
           "‏الذهب والفضة والبلاتين والبالاديوم والنحاس · سنتان من شموع الساعة · عمولة 0.05%",
           "‏✅ = رابح في الفترتين (الأولى ← الثانية)",
           "‏مرتبة حسب أضعف أداء بين الفترتين (الأثبت أولاً). النسب على السعر بدون رافعة.\n"]
    for k, (score, label, a, b) in enumerate(scored, 1):
        pm = per_metal[label]
        out.append(f"‏<b>{k}. {label}</b>\n‏   الكل، الفترة الأولى: {f(a)}\n‏   الكل، الفترة الثانية: <b>{f(b)}</b>")
        for m in pm:
            ma = stats([x for x in pm[m] if x["t"] < split_t]); mb = stats([x for x in pm[m] if x["t"] >= split_t])
            ok = ma and mb and ma["avg"] > 0 and mb["avg"] > 0
            g = lambda st: f"{st['avg']:+.2f}% ({st['n']} صفقة، نجاح {st['win']:.0f}%)" if st else "-"
            out.append(f"‏   {'✅' if ok else '❌'} {data[m][0]}: {g(ma)} ← {g(mb)}")
        out.append("")
    out.append("‏PF = مجموع الأرباح ÷ مجموع الخسائر (فوق 1.3 جيد، تحت 1 خاسر)")
    text = "\n".join(out)
    print(text)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("⚠️ لا يوجد TELEGRAM_BOT_TOKEN، النتيجة في السجل فقط"); return
    import bot
    bot.TOKEN, bot.API = token, f"https://api.telegram.org/bot{token}"
    st = bot.load_state()
    bot.CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "") or st.get("chat_id") or bot.discover_chat_id(st)
    if not bot.CHAT_ID or not bot.send(text):
        sys.exit("❌ فشل الإرسال لتيليغرام (النتيجة موجودة أعلاه في السجل)")
    print("✅ أُرسلت النتيجة إلى تيليغرام")


if __name__ == "__main__":
    main()
