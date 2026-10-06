"""
KRX Open API 기반 '20일 이동평균 하락 -> 상승 전환' 스크리너 + 시각화 리포트

대상
  - 지수: KOSPI 시리즈 일별시세정보   (idx/kospi_dd_trd)  -> KOSPI, KOSPI 200, 섹터지수 등
  - ETF : ETF 일별매매정보            (etp/etf_bydd_trd)
          기본은 주식형·원자재 ETF만 본다. 채권/금리, 혼합, 통화, 리츠 ETF와 인버스 ETF는 제외.
          유형은 ETF 이름과 기초지수명으로 분류한다 (ETF_RULES 참고).

판정 (기본: 둘 다 충족)
  1) 종가 20일 이동평균(MA20)이 --min-fall 거래일 이상 하락하다가 상승으로 전환
  2) 거래량 20일 이동평균(VMA20)도 하락/보합에서 상승으로 전환
  전환은 최근 --lookback 거래일 안에 일어나야 하고, 그 뒤 오늘까지 상승이 유지돼야 한다.

전환 정도 지표
  하락 기간(일)          MA20이 연속으로 내려간 거래일 수 (전환 직전까지)
  하락 폭(%)             하락 시작 시점 MA20 대비 저점 MA20의 하락률
  반등 폭(%)             저점 MA20 대비 현재 MA20 상승률
  현재 기울기(%/일)      오늘 MA20 하루 변화율
  기울기 개선폭(%p)      현재 기울기 - 하락 구간 중 가장 가파른 하락 기울기  (클수록 강한 전환, 정렬 기준)
  종가/MA20(%)           종가가 MA20 위로 얼마나 올라섰는지
  VMA20 반등(%)          거래량 MA20 저점 대비 현재 상승률
  거래량/VMA20(배)       당일 거래량이 평균의 몇 배인지

데이터는 KRX Open API 한 경로로만 받는다. 호출이 실패하면 즉시 종료하며 다른 소스로 넘어가지 않는다.

인증키
  같은 폴더의 .env 파일에  KRX_API_KEY=발급받은키  형식으로 두거나,
  환경 변수 KRX_API_KEY 로 지정한다. (환경 변수가 우선)

사용법
  pip install requests pandas
  python krx_ma20_turn.py                    # 지수 + ETF, 최근 3거래일 내 전환
  python krx_ma20_turn.py --target index     # 지수만
  python krx_ma20_turn.py --target etf       # ETF만
  python krx_ma20_turn.py --lookback 5       # 최근 5거래일 내 전환
  python krx_ma20_turn.py --min-fall 10      # MA20이 10거래일 이상 하락했던 것만
  python krx_ma20_turn.py --price-only       # 주가 MA20 조건만
  python krx_ma20_turn.py --etf-types 원자재  # 원자재 ETF만 (주식,원자재,채권/금리,혼합,통화,리츠 중 선택)
  python krx_ma20_turn.py --include-inverse  # 인버스 ETF도 포함
  python krx_ma20_turn.py --no-open          # 리포트를 브라우저로 자동으로 열지 않음

일자별 응답은 cache/ 폴더에 저장해 재실행 시 API 호출을 줄인다.
결과: results/ma20_turn_YYYYMMDD.csv (데이터), results/ma20_turn_YYYYMMDD.html (시각화 리포트)
기술적 조건으로 걸러낸 목록일 뿐이며 투자 조언이 아니다.
"""
import argparse
import html
import json
import math
import os
import re
import sys
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
APIS = {
    "index": "idx/kospi_dd_trd",
    "etf": "etp/etf_bydd_trd",
}
LABELS = {"index": "지수", "etf": "ETF"}

# ETF 유형 분류 규칙 (이름 + 기초지수명 기준, 위에서부터 먼저 맞는 규칙으로 판정, 아무것도 안 맞으면 주식)
ETF_RULES = [
    ("혼합", r"혼합|TDF|TRF|자산배분|멀티에셋|밸런스|Balanced|Blend"),
    ("채권/금리", r"채권|국고채|국채|회사채|금융채|은행채|통안채|국공채|단기채|크레딧|하이일드|물가채|스트립|"
                 r"특수채|전단채|우선증권|만기|CD금리|CD1년|CD&|KOFR|SOFR|머니마켓|단기자금|MMF|금리|"
                 r"Treasury|T-Bond|Bond|Cash 지수|KRW Cash"),
    ("통화", r"달러선물|엔선물|엔화초단기|미국달러단기"),
    ("리츠", r"리츠|REIT|부동산|리얼티|Realty"),
    ("원자재", r"금현물|골드|금선물|국제금|금액티브|은선물|은액티브|금은선물|구리|원유선물|WTI|농산물|팔라듐|"
              r"백금|천연가스선물|탄소배출권|GSCI|Gold Spot|Silver Spot"),
]
ETF_TYPES = ["주식", "원자재", "채권/금리", "혼합", "통화", "리츠"]
ETF_TYPE_ALIASES = {"equity": "주식", "commodity": "원자재", "bond": "채권/금리",
                    "mixed": "혼합", "currency": "통화", "reit": "리츠"}


def classify_etf(name, index_name):
    text = f"{name} {index_name or ''}"
    for label, pat in ETF_RULES:
        if re.search(pat, text):
            return label
    return "주식"
MA = 20
PER_PAGE = 8          # 리포트 표/차트 한 페이지당 항목 수
HISTORY = 45          # MA20 계산 이후 확보할 거래일 수 (하락 구간 측정용)
HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / "cache"
RESULT_DIR = HERE / "results"


class KrxError(RuntimeError):
    pass


# =====================================================================
# 1. KRX Open API
# =====================================================================
def load_api_key():
    key = os.environ.get("KRX_API_KEY", "").strip()
    env_file = HERE / ".env"
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("KRX_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        raise KrxError("KRX_API_KEY 가 없습니다. .env 파일이나 환경 변수에 인증키를 넣어 주세요.")
    return key


def fetch(session, api, bas_dd):
    """basDd(YYYYMMDD) 하루치 OutBlock_1 목록을 반환. 휴장일이면 빈 목록."""
    cache_file = CACHE_DIR / api / f"{bas_dd}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    url = f"{BASE_URL}/{APIS[api]}"
    try:
        r = session.get(url, params={"basDd": bas_dd}, timeout=20)
    except requests.RequestException as e:
        raise KrxError(f"[{api} {bas_dd}] 요청 실패: {e}") from e
    if r.status_code != 200:
        raise KrxError(f"[{api} {bas_dd}] HTTP {r.status_code}: {r.text[:300]}")
    try:
        body = r.json()
    except ValueError:
        raise KrxError(f"[{api} {bas_dd}] JSON 이 아닌 응답: {r.text[:300]}")
    if "OutBlock_1" not in body:
        raise KrxError(f"[{api} {bas_dd}] OutBlock_1 없음: {json.dumps(body, ensure_ascii=False)[:300]}")

    rows = body["OutBlock_1"] or []
    # 데이터가 있거나, 충분히 지난 날짜(휴장일 확정)만 캐시한다. 최근 날짜는 아직 미반영일 수 있음.
    if rows or datetime.strptime(bas_dd, "%Y%m%d").date() <= date.today() - timedelta(days=3):
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def collect(session, targets, n_days, max_calendar_days=200):
    """오늘부터 거꾸로 거래일 n_days 개를 모은다. 첫 대상 API로 거래일 여부를 판정한다."""
    data = {t: [] for t in targets}
    dates = []
    d = date.today()
    for _ in range(max_calendar_days):
        if len(dates) >= n_days:
            break
        if d.weekday() < 5:
            bas_dd = d.strftime("%Y%m%d")
            rows = fetch(session, targets[0], bas_dd)
            if rows:
                dates.append(bas_dd)
                data[targets[0]].extend(rows)
                for t in targets[1:]:
                    data[t].extend(fetch(session, t, bas_dd))
                print(f"\r  수신 중 {len(dates)}/{n_days} 거래일", end="", flush=True)
                time.sleep(0.05)
        d -= timedelta(days=1)
    print("\r" + " " * 40 + "\r", end="", flush=True)
    if len(dates) < MA + 5:
        raise KrxError(f"거래일 데이터가 {len(dates)}일뿐이라 MA{MA} 분석이 불가합니다.")
    return data, sorted(dates)


# =====================================================================
# 2. 정리
# =====================================================================
def to_num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", "").replace({"-": None, "": None}),
                         errors="coerce")


def tidy(api, rows):
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if api == "index":
        df["key"] = df["IDX_CLSS"].str.strip() + "|" + df["IDX_NM"].str.strip()
        df["code"] = df["IDX_CLSS"].str.strip()
        df["name"] = df["IDX_NM"].str.strip()
        df["close"] = to_num(df["CLSPRC_IDX"])
        df["type"] = "지수"
    else:
        df["key"] = df["ISU_CD"].str.strip()
        df["code"] = df["ISU_CD"].str.strip()
        df["name"] = df["ISU_NM"].str.strip()
        df["close"] = to_num(df["TDD_CLSPRC"])
        idx_nm = df["IDX_IND_NM"] if "IDX_IND_NM" in df else pd.Series("", index=df.index)
        latest = df.sort_values("BAS_DD").groupby("key").tail(1)
        types = {k: classify_etf(n, i) for k, n, i in
                 zip(latest["key"], latest["name"], idx_nm.loc[latest.index].fillna(""))}
        df["type"] = df["key"].map(types)
    df["volume"] = to_num(df["ACC_TRDVOL"])
    df["value"] = to_num(df["ACC_TRDVAL"])
    df["date"] = pd.to_datetime(df["BAS_DD"], format="%Y%m%d")
    return df[["key", "code", "name", "type", "date", "close", "volume", "value"]]


def filter_etf(df, etf_types, include_inverse):
    if df.empty:
        return df
    keep = df["type"].isin(etf_types)
    if not include_inverse:
        keep &= ~df["name"].str.contains("인버스")
    return df[keep]


# =====================================================================
# 3. 전환 판정 + 정도 측정
# =====================================================================
def find_turn(series, lookback):
    """MA20 기울기가 최근 lookback 거래일 안에 (<=0 -> >0)으로 바뀌고 지금까지 >0 이면
    (ma, slope, 전환 위치 i) 반환. slope.iloc[i] 가 첫 상승일. 아니면 None."""
    ma = series.rolling(MA).mean()
    slope = ma.diff().dropna()
    if len(slope) < lookback + 1:
        return None
    for k in range(lookback):
        i = len(slope) - 1 - k
        if slope.iloc[i] > 0 and slope.iloc[i - 1] <= 0 and (slope.iloc[i:] > 0).all():
            return ma, slope, i
    return None


def fall_phase(slope, i):
    """전환 직전 연속 하락 구간. (시작 위치 j: 하락 직전 고점 / None이면 데이터 시작까지, 하락 일수)"""
    j = i - 1
    while j >= 0 and slope.iloc[j] <= 0:
        j -= 1
    return (j if j >= 0 else None), i - 1 - j


def analyze(g, label, lookback, min_fall, price_only):
    g = g.sort_values("date").dropna(subset=["close"])
    g = g[g["volume"] > 0].set_index("date")
    if len(g) < MA + lookback + 2:
        return None

    close, vol = g["close"], g["volume"].astype(float)
    p = find_turn(close, lookback)
    if p is None:
        return None
    ma, slope, i = p
    j, fall_days = fall_phase(slope, i)
    if fall_days < min_fall:
        return None

    v = find_turn(vol, lookback)
    if v is None and not price_only:
        return None

    ma_valid = ma.dropna()
    peak_date = slope.index[j] if j is not None else ma_valid.index[0]
    trough_date = slope.index[i - 1]
    turn_date = slope.index[i]
    peak, trough, now = ma[peak_date], ma[trough_date], ma.iloc[-1]
    slope_pct = slope / ma.shift(1) * 100
    fall_slopes = slope_pct.loc[slope.index[(j + 1) if j is not None else 0]:trough_date]
    worst_fall = fall_slopes.min()
    slope_now = slope_pct.iloc[-1]

    vma = vol.rolling(MA).mean()
    if v is not None:
        _, vslope, vi = v
        v_turn_ago = len(vslope) - 1 - vi
        v_trough = vma[vslope.index[vi - 1]]
        v_rebound = (vma.iloc[-1] / v_trough - 1) * 100
    else:
        v_turn_ago, v_rebound = None, None

    last = g.iloc[-1]
    metrics = {
        "구분": label,
        "유형": last["type"],
        "코드/계열": last["code"],
        "이름": last["name"],
        "기준일": g.index[-1].strftime("%Y-%m-%d"),
        "종가": last["close"],
        "MA20": round(now, 2),
        "하락 기간(일)": fall_days,
        "하락 기간 데이터 시작부터": j is None,
        "하락 폭(%)": round((trough / peak - 1) * 100, 2),
        "전환(일 전)": len(slope) - 1 - i,
        "반등 폭(%)": round((now / trough - 1) * 100, 2),
        "현재 기울기(%/일)": round(slope_now, 3),
        "기울기 개선폭(%p)": round(slope_now - worst_fall, 3),
        "종가/MA20(%)": round((last["close"] / now - 1) * 100, 2),
        "거래량 전환(일 전)": v_turn_ago,
        "VMA20 반등(%)": None if v_rebound is None else round(v_rebound, 1),
        "거래량/VMA20(배)": round(last["volume"] / vma.iloc[-1], 2),
        "20일 평균 거래대금(억)": round(g["value"].tail(MA).mean() / 1e8, 1),
    }
    chart = {
        "dates": [d.strftime("%Y-%m-%d") for d in g.index],
        "close": close.round(4).tolist(),
        "ma": [None if pd.isna(x) else round(x, 4) for x in ma],
        "vol": vol.tolist(),
        "vma": [None if pd.isna(x) else round(x, 1) for x in vma],
        "peak": peak_date.strftime("%Y-%m-%d"),
        "trough": trough_date.strftime("%Y-%m-%d"),
        "turn": turn_date.strftime("%Y-%m-%d"),
        "vturn": None if v is None else v[1].index[v[2]].strftime("%Y-%m-%d"),
    }
    return metrics, chart


def screen(df, label, lookback, min_fall, price_only):
    rows, charts = [], []
    if df.empty:
        return rows, charts
    for _, g in df.groupby("key"):
        res = analyze(g, label, lookback, min_fall, price_only)
        if res:
            rows.append(res[0])
            charts.append(res[1])
    return rows, charts


# =====================================================================
# 4. HTML 리포트
# =====================================================================
def nice_ticks(lo, hi, n=4):
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=10 * mag)
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t <= hi + step * 0.001:
        if t >= lo - step * 0.001:
            ticks.append(round(t, 10))
        t += step
    while ticks[-1] < hi - step * 0.001:
        ticks.append(round(ticks[-1] + step, 10))
    if ticks[0] > lo + step * 0.001:
        ticks.insert(0, round(ticks[0] - step, 10))
    return ticks, step


def fmt_num(x, step=None):
    if x is None:
        return "-"
    ax = abs(x)
    if ax >= 1e8:
        return f"{x / 1e8:,.1f}억"
    if ax >= 1e4 and step is not None and step >= 1e4:
        return f"{x / 1e4:,.0f}만"
    if step is not None and step < 1:
        return f"{x:,.{max(0, -math.floor(math.log10(step)))}f}"
    return f"{x:,.0f}" if ax >= 100 else f"{x:,.2f}"


def card_svg(c, uid):
    W, L, R = 500, 50, 10
    PT, PH, GAP, VH, XB = 20, 150, 22, 56, 20
    H = PT + PH + GAP + VH + XB
    n = len(c["dates"])
    xw = (W - L - R) / max(n - 1, 1)
    X = lambda k: L + k * xw
    idx = {d: k for k, d in enumerate(c["dates"])}

    vals = [x for x in c["close"] + c["ma"] if x is not None]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or hi * 0.01
    pticks, pstep = nice_ticks(lo - pad, hi + pad)
    plo, phi = pticks[0], pticks[-1]
    if phi <= hi:
        phi = hi + pad
    if plo >= lo:
        plo = lo - pad
    Yp = lambda y: PT + PH - (y - plo) / (phi - plo) * PH

    vtop = PT + PH + GAP
    vmax = max(c["vol"] + [x for x in c["vma"] if x is not None]) or 1
    vticks, vstep = nice_ticks(0, vmax, 2)
    vhi = max(vticks[-1], vmax)
    Yv = lambda y: vtop + VH - y / vhi * VH

    def path(vs, Y):
        out, pen = [], False
        for k, y in enumerate(vs):
            if y is None:
                pen = False
                continue
            out.append(f"{'L' if pen else 'M'}{X(k):.1f},{Y(y):.1f}")
            pen = True
        return " ".join(out)

    s = [f'<svg viewBox="0 0 {W} {H}" class="chart" data-uid="{uid}" role="img" '
         f'aria-label="{html.escape(c["name"])} 종가와 MA20, 거래량과 VMA20">']
    # 하락 구간 음영
    x0, x1 = X(idx[c["peak"]]), X(idx[c["trough"]])
    s.append(f'<rect class="fall" x="{x0:.1f}" y="{PT}" width="{max(x1 - x0, 1):.1f}" height="{PH}"/>')
    s.append(f'<text class="note" x="{x0 + 4:.1f}" y="{PT + 12}">MA20 하락 구간</text>')
    # 가격 그리드
    for t in pticks:
        if plo <= t <= phi:
            y = Yp(t)
            s.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}"/>')
            s.append(f'<text class="tick" x="{L - 6}" y="{y + 3.5:.1f}" text-anchor="end">{fmt_num(t, pstep)}</text>')
    s.append(f'<path class="ln-close" d="{path(c["close"], Yp)}"/>')
    s.append(f'<path class="ln-ma" d="{path(c["ma"], Yp)}"/>')
    # 전환점
    tk = idx[c["turn"]]
    tx, ty = X(tk), Yp(c["ma"][tk])
    s.append(f'<circle class="dot-ma" cx="{tx:.1f}" cy="{ty:.1f}" r="5"/>')
    anchor = "end" if tx > W - 90 else "start"
    lx = tx - 8 if anchor == "end" else tx + 8
    s.append(f'<text class="lbl" x="{lx:.1f}" y="{ty + 16:.1f}" text-anchor="{anchor}">전환 {c["turn"][5:]}</text>')
    # 거래량
    bw = max(min(xw * 0.6, 8), 1.5)
    for k, v in enumerate(c["vol"]):
        y = Yv(v)
        s.append(f'<rect class="bar" x="{X(k) - bw / 2:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                 f'height="{max(vtop + VH - y, 0.5):.1f}"/>')
    s.append(f'<line class="axis" x1="{L}" x2="{W - R}" y1="{vtop + VH}" y2="{vtop + VH}"/>')
    s.append(f'<text class="tick" x="{L - 6}" y="{Yv(vhi) + 3.5:.1f}" text-anchor="end">{fmt_num(vhi, vhi)}</text>')
    s.append(f'<text class="tick" x="{L - 6}" y="{vtop + VH + 3.5:.1f}" text-anchor="end">0</text>')
    s.append(f'<path class="ln-vma" d="{path(c["vma"], Yv)}"/>')
    if c["vturn"]:
        vk = idx[c["vturn"]]
        s.append(f'<circle class="dot-vma" cx="{X(vk):.1f}" cy="{Yv(c["vma"][vk]):.1f}" r="4.5"/>')
    # x축 날짜
    for k in sorted({0, n // 2, n - 1}):
        a = "start" if k == 0 else "end" if k == n - 1 else "middle"
        s.append(f'<text class="tick" x="{X(k):.1f}" y="{H - 6}" text-anchor="{a}">{c["dates"][k][5:]}</text>')
    # 호버 레이어
    s.append(f'<line class="cross" x1="0" x2="0" y1="{PT}" y2="{vtop + VH}" visibility="hidden"/>')
    s.append(f'<rect class="hit" x="{L}" y="{PT}" width="{W - L - R}" height="{vtop + VH - PT}" '
             f'data-l="{L}" data-xw="{xw:.4f}" data-n="{n}"/>')
    s.append("</svg>")
    return "\n".join(s)


def scatter_svg(rows):
    W, H, L, R, T, B = 900, 300, 60, 16, 16, 44
    xs = [r["하락 폭(%)"] for r in rows]
    ys = [r["현재 기울기(%/일)"] for r in rows]
    xt, xstep = nice_ticks(min(xs + [0]), max(xs + [0]), 5)
    yt, ystep = nice_ticks(min(ys + [0]), max(ys), 4)
    X = lambda v: L + (v - xt[0]) / (xt[-1] - xt[0]) * (W - L - R)
    Y = lambda v: T + (H - T - B) - (v - yt[0]) / (yt[-1] - yt[0]) * (H - T - B)
    s = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" '
         f'aria-label="하락 폭 대비 현재 MA20 기울기 산점도">']
    for t in yt:
        s.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>')
        s.append(f'<text class="tick" x="{L - 6}" y="{Y(t) + 3.5:.1f}" text-anchor="end">{t:g}</text>')
    for t in xt:
        s.append(f'<text class="tick" x="{X(t):.1f}" y="{H - B + 16}" text-anchor="middle">{t:g}</text>')
    s.append(f'<line class="axis" x1="{L}" x2="{W - R}" y1="{H - B}" y2="{H - B}"/>')
    s.append(f'<text class="axt" x="{(L + W - R) / 2:.0f}" y="{H - 6}" text-anchor="middle">'
             f'MA20 하락 폭 (%) — 왼쪽일수록 깊게 하락</text>')
    s.append(f'<text class="axt" x="14" y="{(T + H - B) / 2:.0f}" text-anchor="middle" '
             f'transform="rotate(-90 14 {(T + H - B) / 2:.0f})">현재 MA20 기울기 (%/일)</text>')
    for k, r in enumerate(rows):
        cls = {"지수": "dot-a", "주식": "dot-b", "원자재": "dot-c"}.get(r["유형"], "dot-b")
        tip = html.escape(f'{r["이름"]} · 하락 {r["하락 폭(%)"]}% · 기울기 {r["현재 기울기(%/일)"]}%/일')
        s.append(f'<a href="#card-{k}"><circle class="{cls}" cx="{X(r["하락 폭(%)"]):.1f}" '
                 f'cy="{Y(r["현재 기울기(%/일)"]):.1f}" r="5" data-tip="{tip}"/></a>')
    s.append("</svg>")
    return "\n".join(s)


TABLE_COLS = [
    ("구분", "구분", "t"), ("유형", "유형", "t"), ("이름", "이름", "t"), ("코드/계열", "코드", "t"),
    ("하락 기간(일)", "하락 기간(일)", "n"), ("하락 폭(%)", "하락 폭(%)", "n"),
    ("전환(일 전)", "전환(일 전)", "n"), ("현재 기울기(%/일)", "현재 기울기(%/일)", "n"),
    ("기울기 개선폭(%p)", "기울기 개선폭(%p)", "bar"), ("반등 폭(%)", "반등 폭(%)", "n"),
    ("종가/MA20(%)", "종가/MA20(%)", "n"), ("거래량 전환(일 전)", "거래량 전환(일 전)", "n"),
    ("VMA20 반등(%)", "VMA20 반등(%)", "n"), ("거래량/VMA20(배)", "거래량/VMA20(배)", "n"),
    ("20일 평균 거래대금(억)", "거래대금(억)", "n"),
]

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--fall:rgba(11,11,11,.045);
--bar:#c3c2b7;--close:#898781}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--fall:rgba(255,255,255,.05);--bar:#4a4a46;--close:#898781}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;--fall:rgba(255,255,255,.05);
--bar:#4a4a46;--close:#898781}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 8px}
.meta{color:var(--ink2);margin:0}.meta b{color:var(--ink)}.meta a{color:var(--ink)}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px}
.legend{display:flex;flex-wrap:wrap;gap:16px;color:var(--ink2);font-size:12px;margin:8px 0 0}
.legend span{display:inline-flex;align-items:center;gap:6px}
.k{display:inline-block;width:16px;height:2px;border-radius:1px}
.k.sw{height:10px;width:10px;border-radius:2px}.k.dt{width:10px;height:10px;border-radius:50%}
.chart{width:100%;height:auto;display:block;overflow:visible}
.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.axt{fill:var(--ink2);font-size:12px}.note{fill:var(--muted);font-size:11px}
.lbl{fill:var(--ink);font-size:12px;font-weight:600}
.fall{fill:var(--fall)}
.ln-close{fill:none;stroke:var(--close);stroke-width:1.5;stroke-linejoin:round;stroke-linecap:round}
.ln-ma{fill:none;stroke:var(--s1);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.ln-vma{fill:none;stroke:var(--s2);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.bar{fill:var(--bar)}
.dot-ma,.dot-a{fill:var(--s1);stroke:var(--surface);stroke-width:2}
.dot-vma,.dot-b{fill:var(--s2);stroke:var(--surface);stroke-width:2}
.dot-c{fill:var(--s3);stroke:var(--surface);stroke-width:2}
.dot-a,.dot-b,.dot-c{cursor:pointer}.dot-a:hover,.dot-b:hover,.dot-c:hover{r:7}
.cross{stroke:var(--ink2);stroke-width:1}.hit{fill:transparent;cursor:crosshair}
.tw{overflow-x:auto}.sc{min-width:600px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:7px 10px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th{color:var(--ink2);font-weight:600;cursor:pointer;user-select:none}
th.t,td.t{text-align:left}th[aria-sort]::after{content:" ▾";color:var(--muted)}
th[aria-sort="ascending"]::after{content:" ▴"}
tbody tr:hover{background:var(--fall)}td a{color:var(--ink);text-decoration:none}td a:hover{text-decoration:underline}
.bc{display:inline-flex;align-items:center;gap:8px;justify-content:flex-end}
.bt{display:inline-block;width:70px;height:6px;border-radius:3px;background:var(--grid);position:relative;overflow:hidden}
.bf{position:absolute;left:0;top:0;bottom:0;border-radius:3px;background:var(--s1)}
.pager{display:flex;flex-wrap:wrap;align-items:center;gap:4px;margin:0 0 10px}
.pager button{min-width:32px;height:32px;padding:0 8px;border:1px solid var(--border);border-radius:6px;
background:var(--surface);color:var(--ink2);font:inherit;font-size:13px;font-variant-numeric:tabular-nums;cursor:pointer}
.pager button:hover:not(:disabled):not([aria-current]){background:var(--fall);color:var(--ink)}
.pager button[aria-current="page"]{background:var(--ink);border-color:var(--ink);color:var(--surface);font-weight:600}
.pager button:disabled{opacity:.35;cursor:default}
.pager .info{margin-left:auto;color:var(--muted);font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,480px),1fr));gap:16px}
.card h3{font-size:15px;margin:0}.card .sub{color:var(--muted);font-size:12px;margin:2px 0 10px}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:0 0 10px}
.stats div{font-size:11px;color:var(--ink2)}.stats b{display:block;font-size:15px;color:var(--ink);font-weight:600}
#tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--border);
border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;z-index:9;
font-variant-numeric:tabular-nums;white-space:nowrap}
#tip .r{display:flex;gap:12px;justify-content:space-between}#tip .r span:first-child{color:var(--ink2)}
.empty{color:var(--ink2);padding:24px 0}
.foot{color:var(--muted);font-size:12px;margin-top:32px}
@media (max-width:560px){.stats{grid-template-columns:repeat(3,1fr)}}
"""

JS = r"""
const tip=document.getElementById('tip');
const DATA=JSON.parse(document.getElementById('chart-data').textContent);
function show(e,h){tip.innerHTML=h;tip.style.display='block';
 const w=tip.offsetWidth,hh=tip.offsetHeight;let x=e.clientX+14,y=e.clientY+14;
 if(x+w>innerWidth-8)x=e.clientX-w-14;if(y+hh>innerHeight-8)y=e.clientY-hh-14;
 tip.style.left=x+'px';tip.style.top=y+'px';}
function hide(){tip.style.display='none';}
const f=(v,d)=>v==null?'-':Number(v).toLocaleString('ko-KR',{maximumFractionDigits:d});
document.querySelectorAll('svg[data-uid]').forEach(svg=>{
 const c=DATA[svg.dataset.uid],hit=svg.querySelector('.hit'),cr=svg.querySelector('.cross');
 const L=+hit.dataset.l,xw=+hit.dataset.xw,n=+hit.dataset.n,dec=c.close[0]<1000?2:0;
 hit.addEventListener('mousemove',e=>{
  const p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;const q=p.matrixTransform(svg.getScreenCTM().inverse());
  const k=Math.max(0,Math.min(n-1,Math.round((q.x-L)/xw)));const x=L+k*xw;
  cr.setAttribute('x1',x);cr.setAttribute('x2',x);cr.setAttribute('visibility','visible');
  show(e,`<b>${c.dates[k]}</b><div class="r"><span>종가</span><span>${f(c.close[k],dec)}</span></div>`+
   `<div class="r"><span>MA20</span><span>${f(c.ma[k],2)}</span></div>`+
   `<div class="r"><span>거래량</span><span>${f(c.vol[k],0)}</span></div>`+
   `<div class="r"><span>VMA20</span><span>${f(c.vma[k],0)}</span></div>`);});
 hit.addEventListener('mouseleave',()=>{cr.setAttribute('visibility','hidden');hide();});
});
document.querySelectorAll('circle[data-tip]').forEach(d=>{
 d.addEventListener('mousemove',e=>show(e,d.dataset.tip));d.addEventListener('mouseleave',hide);});
document.querySelectorAll('table.sortable').forEach(t=>{
 t.querySelectorAll('th').forEach((th,i)=>th.addEventListener('click',()=>{
  const asc=th.getAttribute('aria-sort')==='descending';
  t.querySelectorAll('th').forEach(h=>h.removeAttribute('aria-sort'));
  th.setAttribute('aria-sort',asc?'ascending':'descending');
  const rows=[...t.tBodies[0].rows];
  rows.sort((a,b)=>{const x=a.cells[i].dataset.v,y=b.cells[i].dataset.v;
   const nx=parseFloat(x),ny=parseFloat(y);
   const r=(isNaN(nx)||isNaN(ny))?String(x).localeCompare(String(y),'ko'):nx-ny;return asc?r:-r;});
  rows.forEach(r=>t.tBodies[0].appendChild(r));
  PAGERS.tbl&&PAGERS.tbl.go(1);}));});

/* ---- 페이지 넘김 ---- */
const PER=__PER_PAGE__, BLOCK=10, PAGERS={};
function makePager(nav, getItems){
 let cur=1;
 const pages=()=>Math.max(1,Math.ceil(getItems().length/PER));
 function btn(label,page,opts={}){const b=document.createElement('button');b.type='button';b.textContent=label;
  if(opts.title)b.title=opts.title;if(opts.current)b.setAttribute('aria-current','page');
  if(opts.disabled)b.disabled=true;else b.addEventListener('click',()=>go(page,true));return b;}
 function render(){
  const n=pages(),items=getItems();nav.innerHTML='';
  const start=Math.floor((cur-1)/BLOCK)*BLOCK+1,end=Math.min(n,start+BLOCK-1);
  nav.append(btn('‹',cur-1,{disabled:cur===1,title:'이전 페이지'}));
  if(start>1)nav.append(btn('…',start-1,{title:`${start-1}페이지`}));
  for(let p=start;p<=end;p++)nav.append(btn(String(p),p,{current:p===cur}));
  if(end<n)nav.append(btn('…',end+1,{title:`${end+1}페이지`}));
  nav.append(btn('›',cur+1,{disabled:cur===n,title:'다음 페이지'}));
  const a=(cur-1)*PER+1,b=Math.min(cur*PER,items.length);
  const info=document.createElement('span');info.className='info';
  info.textContent=`${items.length}개 중 ${a}–${b}`;nav.append(info);
  if(n<=1)nav.style.display='none';}
 function go(p,scroll){cur=Math.min(Math.max(1,p),pages());
  getItems().forEach((el,i)=>{el.hidden=!(i>=(cur-1)*PER&&i<cur*PER);});render();
  if(scroll)nav.scrollIntoView({block:'start',behavior:'smooth'});}
 go(1);return {go,pageOf:i=>Math.floor(i/PER)+1};}
const tbody=document.querySelector('table.sortable tbody');
if(tbody)PAGERS.tbl=makePager(document.getElementById('pager-tbl'),()=>[...tbody.rows]);
const cardEls=[...document.querySelectorAll('.cards > .card')];
if(cardEls.length)PAGERS.cards=makePager(document.getElementById('pager-cards'),()=>cardEls);
/* 표 이름·산점도 점 → 해당 차트 페이지로 이동 */
document.querySelectorAll('a[href^="#card-"]').forEach(a=>a.addEventListener('click',e=>{
 const el=document.getElementById(a.getAttribute('href').slice(1));if(!el||!PAGERS.cards)return;
 e.preventDefault();PAGERS.cards.go(PAGERS.cards.pageOf(cardEls.indexOf(el)));
 el.scrollIntoView({block:'start',behavior:'smooth'});}));
"""


def build_report(rows, charts, dates, args, path):
    fmt = lambda x: f"{x[:4]}-{x[4:6]}-{x[6:]}"
    cond = "주가 MA20" if args.price_only else "주가 MA20 + 거래량 VMA20"
    order = sorted(range(len(rows)), key=lambda k: -rows[k]["기울기 개선폭(%p)"])
    rows = [rows[k] for k in order]
    charts = [charts[k] for k in order]
    for r, c in zip(rows, charts):
        c["name"] = r["이름"]
    n_idx = sum(r["구분"] == "지수" for r in rows)
    n_etf = len(rows) - n_idx
    n_eq = sum(r["유형"] == "주식" for r in rows)
    n_cm = sum(r["유형"] == "원자재" for r in rows)
    n_other = n_etf - n_eq - n_cm

    h = ["<!doctype html><html lang='ko'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<title>MA20 전환 리포트</title>", f"<style>{CSS}</style></head><body><main>",
         "<h1>MA20 하락 → 상승 전환</h1>",
         f"<p class='meta'>데이터 기준일 <b>{fmt(dates[-1])}</b> · 사용 기간 {fmt(dates[0])} ~ {fmt(dates[-1])} "
         f"(거래일 {len(dates)}일) · 실행 {datetime.now():%Y-%m-%d %H:%M}"
         + (f" · <a class='archive' href='{html.escape(args.archive_link)}'>지난 리포트</a>" if args.archive_link else "")
         + "</p>",
         f"<p class='meta'>조건: {cond} 상승 전환, 최근 {args.lookback}거래일 이내, "
         f"MA20 하락 {args.min_fall}거래일 이상 · ETF 유형: {html.escape(', '.join(args.etf_types))}"
         f"{'' if args.include_inverse else ' (인버스 제외)'} · 결과 <b>{len(rows)}개</b> "
         f"(지수 {n_idx}, ETF {n_etf})</p>"]

    if not rows:
        h.append("<p class='empty'>조건을 만족하는 대상이 없습니다.</p>")
    else:
        # 개요 산점도
        h.append("<h2>하락 깊이와 반등 기울기</h2><div class='panel'><div class='tw'><div class='sc'>")
        h.append(scatter_svg(rows))
        h.append("</div></div>")
        h.append("<div class='legend'>"
                 + ("<span><i class='k dt' style='background:var(--s1)'></i>지수</span>" if n_idx else "")
                 + ("<span><i class='k dt' style='background:var(--s2)'></i>주식 ETF</span>" if n_eq else "")
                 + ("<span><i class='k dt' style='background:var(--s3)'></i>원자재 ETF</span>" if n_cm else "")
                 + ("<span><i class='k dt' style='background:var(--s2)'></i>기타 ETF</span>" if n_other else "")
                 + "<span>점을 누르면 해당 차트로 이동</span></div></div>")

        # 표
        top = max(r["기울기 개선폭(%p)"] for r in rows) or 1
        h.append("<h2>전환 정도 (기울기 개선폭 순)</h2><nav class='pager' id='pager-tbl' aria-label='표 페이지'></nav>"
                 "<div class='panel tw'><table class='sortable'><thead><tr>")
        for key, lab, kind in TABLE_COLS:
            cls = "t" if kind == "t" else ""
            sort = " aria-sort='descending'" if key == "기울기 개선폭(%p)" else ""
            h.append(f"<th class='{cls}'{sort}>{html.escape(lab)}</th>")
        h.append("</tr></thead><tbody>")
        for k, r in enumerate(rows):
            h.append("<tr>")
            for key, lab, kind in TABLE_COLS:
                v = r[key]
                dv = "" if v is None else html.escape(str(v))
                if key == "이름":
                    h.append(f"<td class='t' data-v='{dv}'><a href='#card-{k}'>{html.escape(str(v))}</a></td>")
                elif kind == "t":
                    h.append(f"<td class='t' data-v='{dv}'>{html.escape(str(v))}</td>")
                elif kind == "bar":
                    w = max(0, min(100, v / top * 100))
                    h.append(f"<td data-v='{dv}'><span class='bc'>{v:.3f}<i class='bt'>"
                             f"<i class='bf' style='width:{w:.0f}%'></i></i></span></td>")
                else:
                    txt = "-" if v is None else (f"{v}+" if key == "하락 기간(일)" and r["하락 기간 데이터 시작부터"] else f"{v}")
                    h.append(f"<td data-v='{dv if dv else -1e18}'>{txt}</td>")
            h.append("</tr>")
        h.append("</tbody></table></div>")

        # 개별 차트
        h.append("<h2>개별 차트</h2><nav class='pager' id='pager-cards' aria-label='차트 페이지'></nav>")
        h.append("<div class='legend' style='margin:0 0 12px'>"
                 "<span><i class='k' style='background:var(--close)'></i>종가</span>"
                 "<span><i class='k' style='background:var(--s1)'></i>MA20</span>"
                 "<span><i class='k dt' style='background:var(--s1)'></i>MA20 전환점</span>"
                 "<span><i class='k sw' style='background:var(--bar)'></i>거래량</span>"
                 "<span><i class='k' style='background:var(--s2)'></i>거래량 MA20</span>"
                 "<span><i class='k dt' style='background:var(--s2)'></i>거래량 MA20 전환점</span>"
                 "<span><i class='k sw' style='background:var(--fall);outline:1px solid var(--grid)'></i>MA20 하락 구간</span>"
                 "</div><div class='cards'>")
        for k, (r, c) in enumerate(zip(rows, charts)):
            fall = f'{r["하락 기간(일)"]}{"+" if r["하락 기간 데이터 시작부터"] else ""}일'
            price_txt = format(r["종가"], ",.2f" if r["구분"] == "지수" else ",.0f")
            h.append(f"<section class='panel card' id='card-{k}'>"
                     f"<h3>{html.escape(r['이름'])}</h3>"
                     f"<p class='sub'>{r['구분'] if r['구분'] == '지수' else r['유형'] + ' ETF'} · {html.escape(str(r['코드/계열']))} · 종가 "
                     f"{price_txt} · 평균 거래대금 {r['20일 평균 거래대금(억)']}억</p>"
                     "<div class='stats'>"
                     f"<div>하락 기간<b>{fall}</b></div>"
                     f"<div>하락 폭<b>{r['하락 폭(%)']}%</b></div>"
                     f"<div>반등 폭<b>{r['반등 폭(%)']:+.2f}%</b></div>"
                     f"<div>기울기 개선<b>{r['기울기 개선폭(%p)']:.3f}%p</b></div>"
                     f"<div>거래량/평균<b>{r['거래량/VMA20(배)']}배</b></div>"
                     "</div>")
            h.append(card_svg(c, str(k)))
            h.append("</section>")
        h.append("</div>")

    h.append("<p class='foot'>이 화면은 “한국거래소 통계정보”(KRX Open API)를 사용한 결과입니다. "
             "기술적 조건으로 걸러낸 결과이며 투자 권유가 아닙니다.</p>")
    data_json = json.dumps({str(k): c for k, c in enumerate(charts)}, ensure_ascii=False).replace("</", "<\\/")
    h.append(f"</main><div id='tip'></div><script id='chart-data' type='application/json'>{data_json}</script>")
    h.append(f"<script>{JS.replace('__PER_PAGE__', str(PER_PAGE))}</script></body></html>")
    path.write_text("\n".join(h), encoding="utf-8")
    return rows


# =====================================================================
# 5. main
# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["all", "index", "etf"], default="all")
    ap.add_argument("--lookback", type=int, default=3, help="전환 인정 기간(거래일)")
    ap.add_argument("--min-fall", type=int, default=5, help="전환 전 MA20 최소 하락 거래일 수")
    ap.add_argument("--price-only", action="store_true", help="주가 MA20 조건만 사용")
    ap.add_argument("--etf-types", default="주식,원자재",
                    help="볼 ETF 유형, 쉼표로 구분 (" + ",".join(ETF_TYPES) + ")")
    ap.add_argument("--include-inverse", action="store_true", help="인버스 ETF도 포함")
    ap.add_argument("--no-open", action="store_true", help="리포트를 브라우저로 열지 않음")
    ap.add_argument("--archive-link", default="", help="리포트 상단에 '지난 리포트' 링크 추가 (사이트 배포용)")
    args = ap.parse_args()
    args.etf_types = [ETF_TYPE_ALIASES.get(t.strip().lower(), t.strip())
                      for t in args.etf_types.split(",") if t.strip()]
    bad = [t for t in args.etf_types if t not in ETF_TYPES]
    if bad:
        ap.error(f"알 수 없는 ETF 유형: {', '.join(bad)} (가능: {', '.join(ETF_TYPES)})")

    targets = ["index", "etf"] if args.target == "all" else [args.target]
    n_days = MA + HISTORY

    try:
        session = requests.Session()
        session.headers.update({"AUTH_KEY": load_api_key()})
        raw, dates = collect(session, targets, n_days)
    except KrxError as e:
        print(f"\n[실패] {e}", file=sys.stderr)
        sys.exit(1)

    fmt = lambda x: f"{x[:4]}-{x[4:6]}-{x[6:]}"
    print("=" * 60)
    print(f" 데이터 기준일: {fmt(dates[-1])}  (KRX에 반영된 최근 거래일)")
    print(f" 사용 기간   : {fmt(dates[0])} ~ {fmt(dates[-1])}, 거래일 {len(dates)}일")
    print(f" 실행 시각   : {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 60)

    rows, charts = [], []
    for t in targets:
        df = tidy(t, raw[t])
        if t == "etf":
            df = filter_etf(df, args.etf_types, args.include_inverse)
        r, c = screen(df, LABELS[t], args.lookback, args.min_fall, args.price_only)
        rows += r
        charts += c

    RESULT_DIR.mkdir(exist_ok=True)
    stem = RESULT_DIR / f"ma20_turn_{dates[-1]}"
    html_path = stem.with_suffix(".html")
    rows = build_report(rows, charts, dates, args, html_path)

    cond = "주가" if args.price_only else "주가+거래량"
    if rows:
        out = pd.DataFrame(rows)
        out.to_csv(stem.with_suffix(".csv"), index=False, encoding="utf-8-sig")
        pd.set_option("display.width", 250)
        pd.set_option("display.max_columns", 30)
        pd.set_option("display.unicode.east_asian_width", True)
        show = ["유형", "이름", "코드/계열", "하락 기간(일)", "하락 폭(%)", "전환(일 전)", "현재 기울기(%/일)",
                "기울기 개선폭(%p)", "반등 폭(%)", "종가/MA20(%)", "거래량/VMA20(배)"]
        for t in targets:
            part = out[out["구분"] == LABELS[t]]
            print(f"\n■ {LABELS[t]}: {cond} MA20 하락→상승 전환 {len(part)}개 (기울기 개선폭 순)")
            if not part.empty:
                print(part[show].to_string(index=False))
        print(f"\n데이터: {stem.with_suffix('.csv')}")
    else:
        print(f"\n최근 {args.lookback}거래일 내 {cond} MA20 하락→상승 전환 대상이 없습니다.")
    print(f"리포트: {html_path}")
    if not args.no_open:
        webbrowser.open(html_path.as_uri())


if __name__ == "__main__":
    main()
