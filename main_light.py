아래 전체를 그대로 붙여넣어 \*\*`main_light.py`\*\*로 저장하면 됩니다.

```python
# main_light.py — Premarket-5 Auto-Scout (DDM)
# - Polygon 분봉 기반 PHL/VWAP/RVOL 계산
# - DST 자동 변환
# - 사전 스캔(03:40 ET) 문턱 55%, 04:10 ET 이후 문턱 65%
# - 이벤트 기본 확률 상향(25 + 1.2*escore)
# - 텔레그램 전송 실패 시 프로세스 종료(깃허브 액션에서 실패로 표시)

import os, re
import datetime as dt
from typing import List, Dict, Any
from zoneinfo import ZoneInfo
import requests
import xml.etree.ElementTree as ET

# --- 환경변수 ---
POLY = os.getenv('POLYGON_API_KEY')
BENZ = os.getenv('BENZINGA_API_KEY')
FINN = os.getenv('FINNHUB_API_KEY')
TG_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TG_CHAT  = os.getenv('TELEGRAM_CHAT_ID')

# --- 타임존 (DST 자동) ---
TZ_UTC = dt.timezone.utc
TZ_NY  = ZoneInfo("America/New_York")
TZ_KST = dt.timezone(dt.timedelta(hours=9))

# --- 상수/규칙 ---
PRICE_MIN, PRICE_MAX = 0.10, 3.00
EXCH_ALLOW = {"NASDAQ", "NYSE", "NYSE American", "AMEX"}
EXCH_ALLOW_CODES = {"XNAS", "XNYS", "XASE", "ARCX"}  # Polygon 코드형 허용
BL = re.compile(r"(-W|W$|WS$|WT$|\.W|U$|\.U|/WS|/W|/U|PR$|P$)")
TICKER_RGX = [
    re.compile(r"(NASDAQ|Nasdaq|NYSE(?:\s+American)?|AMEX)[:\s-]*([A-Z]{1,5})"),  # 괄호 없음
    re.compile(r"\((NASDAQ|NYSE|NYSE\s+American|AMEX):\s*([A-Z]{1,5})\)"),         # (NASDAQ: ABCD)
    re.compile(r"\b([A-Z]{1,5})\b")                                                # 최후 보조
]

POS_KEYS = {
    "buyback": 35, "repurchase": 35, "stock repurchase": 35, "10b5-1": 12,
    "fda approval": 40, "clearance": 28, "de novo": 30, "510(k)": 26, "ce mark": 20,
    "contract": 26, "award": 22, "partnership": 20, "distribution": 18,
    "merger": 32, "definitive": 10, "acquisition": 26,
    "earnings": 18, "guidance raise": 24, "beats": 18, "record revenue": 16
}
NEG_KEYS = {"s-3":-25,"s-1":-18,"424b5":-25,"atm":-20,"registered direct":-22,"warrant":-12,"reverse split":-30}

PR_RSS = [
    "https://www.prnewswire.com/rss/industry/business-technology-latest-news.rss",
    "https://www.businesswire.com/portal/site/home/news/rh/us/rss/industry/?vnsId=31326&newsLang=EN",
    "https://www.globenewswire.com/RssFeed/industry/All-Press-Releases.xml",
    "https://www.accesswire.com/rss/latest.xml"
]

# --- 유틸 ---
now_utc = lambda: dt.datetime.now(TZ_UTC)

def fmt_dual(ts: dt.datetime) -> str:
    et = ts.astimezone(TZ_NY)
    kst = ts.astimezone(TZ_KST)
    return f"{et:%Y-%m-%d %H:%M} ET / {kst:%Y-%m-%d %H:%M} KST"

# --- 데이터 취득 ---
def poly_prev_close(sym: str) -> float | None:
    if not POLY: return None
    u = f"https://api.polygon.io/v2/aggs/ticker/{sym}/prev?adjusted=true&apiKey={POLY}"
    r = requests.get(u, timeout=10)
    if r.status_code != 200: return None
    try:
        return float(r.json()['results'][0]['c'])
    except Exception:
        return None

def finnhub_prev_price(sym: str) -> float | None:
    if not FINN: return None
    try:
        r = requests.get(f"https://finnhub.io/api/v1/quote?symbol={sym}&token={FINN}", timeout=8)
        if r.status_code != 200: return None
        js = r.json()
        return float(js.get("pc") or js.get("c") or 0) or None
    except Exception:
        return None

def poly_meta(sym: str) -> Dict[str, Any] | None:
    if not POLY: return None
    u = f"https://api.polygon.io/v3/reference/tickers/{sym}?apiKey={POLY}"
    r = requests.get(u, timeout=10)
    if r.status_code != 200: return None
    return r.json().get('results')

def poly_aggs_1min(sym: str, start: dt.datetime, end: dt.datetime) -> List[Dict[str, Any]]:
    if not POLY: return []
    s = int(start.timestamp() * 1000); e = int(end.timestamp() * 1000)
    u = f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/minute/{s}/{e}?adjusted=true&sort=asc&limit=50000&apiKey={POLY}"
    r = requests.get(u, timeout=12)
    if r.status_code != 200: return []
    return r.json().get('results', []) or []

def fetch_benzinga(since_iso: str) -> List[Dict[str, Any]]:
    if not BENZ: return []
    u = ("https://api.benzinga.com/api/v2/news?"
         f"token={BENZ}&displayOutput=full&pagesize=100&channels=General,Press%20Releases&sort=publishedAt:desc&from={since_iso}")
    r = requests.get(u, timeout=15)
    if r.status_code != 200: return []
    js = r.json()
    return js if isinstance(js, list) else []

def fetch_rss() -> List[Dict[str, Any]]:
    rows = []
    for url in PR_RSS:
        try:
            r = requests.get(url, timeout=10); r.raise_for_status()
            root = ET.fromstring(r.text)
            for it in root.iterfind('.//item'):
                rows.append({
                    'title': it.findtext('title') or '',
                    'url': it.findtext('link') or '',
                    'published_at': it.findtext('pubDate') or ''
                })
        except Exception:
            pass
    return rows

# --- 파싱/스코어 ---
def extract_tickers(title: str) -> List[str]:
    title = title.replace('–', '-'); hits = set()
    for rgx in TICKER_RGX:
        for m in rgx.finditer(title):
            s = (m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)) or ''
            s = s.strip().upper()
            if 1 <= len(s) <= 5 and not BL.search(s):
                hits.add(s)
    return list(hits)

def classify_event(text: str) -> int:
    t = text.lower(); score = 0
    for k, v in POS_KEYS.items():
        if k in t: score += v
    for k, v in NEG_KEYS.items():
        if k in t: score += v
    return score

def build_news_rows(hours: int = 12) -> List[Dict[str, Any]]:
    since = now_utc() - dt.timedelta(hours=hours)
    since_iso = since.isoformat(timespec='seconds').replace('+00:00', 'Z')
    rows: List[Dict[str, Any]] = []

    for it in fetch_benzinga(since_iso):
        title = it.get('title') or ''; url = it.get('url') or it.get('amp_url') or ''
        ts = it.get('created') or it.get('publishedAt') or ''
        tickers = it.get('stocks') or extract_tickers(title)
        syms = [(t.get('name', t).upper() if isinstance(t, dict) else str(t).upper())
                for t in (tickers if isinstance(tickers, list) else [])]
        for sym in syms:
            rows.append({'symbol': sym, 'title': title, 'url': url, 'published_at': ts})

    if not rows:
        for it in fetch_rss():
            title, url, ts = it['title'], it['url'], it['published_at']
            for sym in extract_tickers(title):
                rows.append({'symbol': sym, 'title': title, 'url': url, 'published_at': ts})

    # dedup
    uniq, seen = [], set()
    for r in rows:
        key = (r['symbol'], r['title'][:80])
        if key in seen: continue
        seen.add(key); uniq.append(r)
    return uniq

# --- 프리장 메트릭 계산 ---
def premkt_metrics(sym: str, prev_close: float) -> Dict[str, Any]:
    # 04:00 America/New_York부터 현재까지
    et_now = now_utc().astimezone(TZ_NY)
    start  = et_now.replace(hour=4, minute=0, second=0, microsecond=0).astimezone(TZ_UTC)
    end    = now_utc()
    bars   = poly_aggs_1min(sym, start, end)
    if not bars:
        return {"phl": None, "vwap": None, "rvol1": None, "rvol3": None, "rvol5": None, "dv5k": None, "last": prev_close}

    highs = [b.get('h', 0) for b in bars]
    vols  = [b.get('v', 0) for b in bars]
    closes= [b.get('c', 0) for b in bars]
    vws   = [b.get('vw', b.get('c', 0)) for b in bars]

    phl  = max(highs) if highs else None
    vwap = (sum(v * w for v, w in zip(vols, vws)) / sum(vols)) if sum(vols) > 0 else None
    last = closes[-1] if closes else prev_close

    # RVOL: 최근 1/3/5분 vs 직전 20분 평균
    def rvol(n: int) -> float | None:
        if len(vols) < (n + 20): return None
        recent = sum(vols[-n:]); base = sum(vols[-(n + 20):-n]) / 20
        if base <= 0: return None
        return round(recent / base, 2)

    r1, r3, r5 = rvol(1), rvol(3), rvol(5)

    # 5분 누적 대금($)
    dv5 = sum([(bars[-i]['v'] * bars[-i]['c']) for i in range(1, min(5, len(bars)) + 1)]) if bars else 0
    dv5k = int(dv5 / 1000)

    return {
        "phl":  round(phl, 4) if phl else None,
        "vwap": round(vwap, 4) if vwap else None,
        "rvol1": r1, "rvol3": r3, "rvol5": r5,
        "dv5k": dv5k, "last": last
    }

# --- 후보 생성 ---
def build_candidates() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in build_news_rows(12):
        sym = r['symbol'].upper()
        if BL.search(sym): continue

        # 가격 폴백(Polygon → Finnhub)
        price = poly_prev_close(sym) or finnhub_prev_price(sym)
        if price is None or not (PRICE_MIN <= price <= PRICE_MAX): continue

        # 거래소 필터(이름/코드 모두 수용)
        meta = poly_meta(sym) or {}
        exch_name = meta.get('primary_exchange_name') or meta.get('listing_exchange') or meta.get('primary_exchange') or ''
        exch_code = meta.get('primary_exchange') or ''
        allowed = any(e in str(exch_name) for e in EXCH_ALLOW) or str(exch_code) in EXCH_ALLOW_CODES
        if (exch_name or exch_code) and not allowed: continue

        # 이벤트 점수 → 기본 확률 (상향)
        es = classify_event(r['title'])
        prob_base = max(0, min(90, 25 + int(es * 1.2)))

        out.append({
            **r, 'symbol': sym, 'price': price,
            'exchange': str(exch_name or exch_code) or 'N/A',
            'escore': es, 'prob_base': prob_base
        })

    out.sort(key=lambda x: x['prob_base'], reverse=True)
    return out

# --- 포맷 & 발송 ---
def send_tg(text: str) -> bool:
    if not (TG_TOKEN and TG_CHAT): return False
    url = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
    payload = {'chat_id': TG_CHAT, 'text': text, 'parse_mode': 'Markdown', 'disable_web_page_preview': True}
    try:
        return requests.post(url, json=payload, timeout=10).status_code == 200
    except Exception:
        return False

def to_rows(cands: List[Dict[str, Any]]) -> str:
    nowline = fmt_dual(now_utc())
    lines = [f"🔔 Premarket-5 Auto-Scout(DDM) @ {nowline}\n"]

    et = now_utc().astimezone(TZ_NY)
    is_0410 = (et.hour > 4 or (et.hour == 4 and et.minute >= 10))

    picks = []
    for c in cands:
        m = premkt_metrics(c['symbol'], c['price']) if is_0410 else {
            "phl": None, "vwap": None, "rvol1": None, "rvol3": None, "rvol5": None, "dv5k": None, "last": c['price']
        }
        bonus = 0
        gap = (m['last'] - c['price']) / c['price'] * 100 if c['price'] else 0
        if is_0410:
            if m['rvol3'] and m['rvol3'] >= 2: bonus += 10
            if m['dv5k'] and 50 <= m['dv5k'] <= 250: bonus += 8
            if 0 <= gap <= 12: bonus += 4

        prob = max(0, min(95, c['prob_base'] + bonus))
        row = {**c, 'metrics': m, 'prob': prob}
        picks.append(row)

        # 디버그 로그(깃허브 액션 콘솔용)
        print(f"[BASE] {c['symbol']} es={c['escore']} base={c['prob_base']} prob={prob}")

    # 시간대별 문턱
    thresh = 55 if not is_0410 else 65
    picks = [p for p in picks if p['prob'] >= thresh][:5]
    print(f"[PICKS] threshold={thresh} count={len(picks)}")

    for i, p in enumerate(picks, 1):
        m = p['metrics']
        lines += [
            f"{i}) **{p['symbol']}** (${p['price']:.2f}) — {p['title'][:150]}",
            f"📰 뉴스 — {p['url']}",
            f"📊 기술적 레벨 — PHL: {m['phl'] if m['phl'] else 'N/A'}, VWAP: {m['vwap'] if m['vwap'] else 'N/A'}, ORH: N/A",
            f"   RVOL(1/3/5): {m['rvol1'] or '–'}/{m['rvol3'] or '–'}/{m['rvol5'] or '–'}, 5분 대금: {m['dv5k'] or 0}k",
            f"💵 희석·재무 — 키워드 기반 감점 적용(정밀 EDGAR 미적용)",
            f"🚀 1~3일 시나리오 — DDM 후보(트리거 대기)",
            f"🎯 전략 — T1 PHL 재돌파 | T2 VWAP 장악(+ORH는 장 시작 후) | T3 대금 임계치",
            f"✅ 확률(보수적) — ~{p['prob']}% (이벤트:{p['escore']})",
            f"📶 Data Completeness — {'High' if is_0410 else 'Medium'}",
            f"—"
        ]

    if not picks:
        lines.append('현재 기준 ≥ 문턱치 후보가 없습니다. (조건 미충족 또는 데이터 부족)')

    return "\n".join(lines)

def main():
    cands = build_candidates()
    msg = to_rows(cands)
    ok = send_tg(msg)
    print(msg)  # 로그 출력
    if not ok:
        raise SystemExit("❌ Telegram send failed. Check secrets/chat id/bot membership.")

if __name__ == '__main__':
    main()
```
