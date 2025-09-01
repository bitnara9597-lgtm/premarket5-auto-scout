import os, re, requests, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

TG_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN'); TG_CHAT=os.getenv('TELEGRAM_CHAT_ID')
FINN=os.getenv('FINNHUB_API_KEY')  # 선택(가격필터)
PRICE_MIN, PRICE_MAX = 0.10, 3.00

PR_RSS=[
  'https://www.prnewswire.com/rss/industry/business-technology-latest-news.rss',
  'https://www.businesswire.com/portal/site/home/news/rh/us/rss/industry/?vnsId=31326&newsLang=EN'
]

TICKER_RGX=[re.compile(r"\((NASDAQ|NYSE|NYSE\s+American|AMEX):\s*([A-Z]{1,5})\)"),
            re.compile(r"\b([A-Z]{1,5})\b")]
BL = re.compile(r"(-W|W$|WS$|WT$|\.W|U$|\.U|/WS|/W|/U|PR$|P$)")

def fetch_rss():
    rows=[]
    for url in PR_RSS:
        try:
            r=requests.get(url,timeout=10); r.raise_for_status()
            root=ET.fromstring(r.text)
            for it in root.iterfind('.//item'):
                rows.append({'title':it.findtext('title') or '',
                             'url':it.findtext('link') or '',
                             'published_at':it.findtext('pubDate') or ''})
        except Exception: pass
    return rows

def extract_tickers(title:str):
    hits=set(); title=title.replace('–','-')
    for rgx in TICKER_RGX:
        for m in rgx.finditer(title):
            s=(m.group(2) if m.lastindex and m.lastindex>=2 else m.group(1)) or ''
            s=s.strip().upper()
            if 1<=len(s)<=5: hits.add(s)
    return [s for s in hits if not BL.search(s)]

def finnhub_price_ok(sym:str)->bool:
    if not FINN: return True  # 키 없으면 가격필터 생략(관찰 전용)
    try:
        r=requests.get(f'https://finnhub.io/api/v1/quote?symbol={sym}&token={FINN}',timeout=8)
        if r.status_code!=200: return True
        c=r.json().get('pc') or r.json().get('c')
        if not c: return True
        return PRICE_MIN<=float(c)<=PRICE_MAX
    except Exception:
        return True

def build_candidates():
    rows=fetch_rss(); out=[]; seen=set()
    for r in rows:
        for sym in extract_tickers(r['title']):
            key=(sym, r['title'][:80])
            if key in seen: continue
            seen.add(key)
            if finnhub_price_ok(sym):
                out.append({**r,'symbol':sym})
    return out[:12]

def dual_time():
    now=datetime.now(timezone.utc)
    et=now.astimezone(timezone(timedelta(hours=-4)))
    kst=now.astimezone(timezone(timedelta(hours=9)))
    return f"{et:%Y-%m-%d %H:%M} ET / {kst:%Y-%m-%d %H:%M} KST"

def send_tg(text:str):
    if not (TG_TOKEN and TG_CHAT): return False
    url=f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
    return requests.post(url,json={'chat_id':TG_CHAT,'text':text}).status_code==200

def main():
    cands=build_candidates()
    lines=[f"🔔 Premarket-5 Auto-Scout(Light) @ {dual_time()}\n"]
    for i,c in enumerate(cands[:5],1):
        lines += [f"{i}) {c['symbol']} — {c['title'][:140]}",
                  f"📰 {c['url']}", "📶 Data Completeness — Low/Medium (라이트)", "—"]
    if len(cands)==0: lines.append("현재 후보 없음(라이트 모드).")
    send_tg("\n".join(lines))

if __name__=='__main__': main()
