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
TG_CHAT = os.getenv('TELEGRAM_CHAT_ID')


# --- 타임존 (DST 자동) ---
TZ_UTC = dt.timezone.utc
TZ_NY = ZoneInfo("America/New_York")
TZ_KST = dt.timezone(dt.timedelta(hours=9))


# --- 상수/규칙 ---
PRICE_MIN, PRICE_MAX = 0.10, 3.00
EXCH_ALLOW = {"NASDAQ", "NYSE", "NYSE American", "AMEX"}
EXCH_ALLOW_CODES = {"XNAS", "XNYS", "XASE", "ARCX"} # Polygon 코드형 허용
BL = re.compile(r"(-W|W$|WS$|WT$|\.W|U$|\.U|/WS|/W|/U|PR$|P$)")
TICKER_RGX = [
re.compile(r"(NASDAQ|Nasdaq|NYSE(?:\s+American)?|AMEX)[:\s-]*([A-Z]{1,5})"), # 괄호 없음
re.compile(r"\((NASDAQ|NYSE|NYSE\s+American|AMEX):\s*([A-Z]{1,5})\)"), # (NASDAQ: ABCD)
re.compile(r"\b([A-Z]{1,5})\b") # 최후 보조
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

