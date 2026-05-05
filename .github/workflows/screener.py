"""
興櫃篩選引擎 v3 — 完全免費，不需要任何 Token
═══════════════════════════════════════════════
資料來源：
  ① TPEx OpenAPI     https://www.tpex.org.tw/openapi/v1/
  ② MOPS 公開資訊觀測站  https://mops.twse.com.tw/
  ③ 政府資料開放平台      https://data.gov.tw/

執行：python screener.py
輸出：results.json（供 index.html 讀取）
═══════════════════════════════════════════════
"""

import json, time, sys, os, re
from datetime import datetime, timedelta
import urllib.request, urllib.parse, urllib.error

# ═══════════════════════════════════════════════
#  ★ 設定區
# ═══════════════════════════════════════════════
P_WIN           = 0.55    # 預估上漲機率
GAIN_MULTIPLIER = 1.5     # 目標價 = 承銷價 × N 倍
STOP_PCT        = 0.15    # 停損幅度 15%
MDD             = 0.10    # 帳戶最大可承受回撤
TOTAL_CAPITAL   = 1_000_000  # 帳戶總資金（元）
MAX_POS_PCT     = 0.30    # 單一持倉上限

PRICE_RATIO_MAX = 1.05    # 市價 ≤ 承銷價 × 105%
TOP10_MIN_PCT   = 40.0    # 前十大持股合計 ≥ 40%
CHIP_SCORE_MIN  = 2       # 籌碼三項至少通過 N 項
MAX_STOCKS      = 999     # 掃描上限（999 = 全部）
REQUEST_DELAY   = 0.5     # API 呼叫間隔（秒），過快會被 rate limit

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
# ═══════════════════════════════════════════════

TPEX   = "https://www.tpex.org.tw/openapi/v1"
MOPS   = "https://mops.twse.com.tw/mops/web"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": "https://www.tpex.org.tw/",
}

# ─── HTTP 工具 ────────────────────────────────

def get_json(url, params=None, retries=2):
    if params:
        url = url + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
            else:
                return None

def get_html(url, data=None, retries=2):
    for attempt in range(retries + 1):
        try:
            if data:
                post = urllib.parse.urlencode(data).encode()
                req  = urllib.request.Request(url, data=post, headers=HEADERS)
            else:
                req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
            else:
                return ""

def fv(*vals):
    """安全取第一個可轉 float 的值"""
    for v in vals:
        if v not in (None, "", "—", "--", "N/A"):
            try:
                return float(str(v).replace(",", ""))
            except:
                pass
    return 0.0

# ─── ① 興櫃股票清單（TPEx OpenAPI）─────────────

def get_stock_list():
    """
    端點：/tpex_esb_latest_statistics
    回傳全市場興櫃股票清單，含代號、名稱
    """
    print("① 取得興櫃股票清單...")
    data = get_json(f"{TPEX}/tpex_esb_latest_statistics")

    stocks = []
    if data and isinstance(data, list):
        for r in data:
            sid   = str(r.get("SecuritiesCompanyCode", "") or r.get("stockNo", "")).strip()
            sname = str(r.get("CompanyName", "") or r.get("name", "")).strip()
            if sid and sname:
                stocks.append({"sid": sid, "sname": sname})
    
    # 備援：從 mopsfin 取
    if not stocks:
        data2 = get_json(f"{TPEX}/mopsfin_t187ap03_2")
        if data2 and isinstance(data2, list):
            for r in data2:
                sid   = str(r.get("SecuritiesCompanyCode", "") or "").strip()
                sname = str(r.get("CompanyName", "") or "").strip()
                if sid:
                    stocks.append({"sid": sid, "sname": sname})

    print(f"   → 取得 {len(stocks)} 支興櫃股票")
    return stocks


# ─── ② 每日行情（TPEx OpenAPI）──────────────────

def get_daily_quotes_all():
    """
    一次取得全市場今日興櫃行情（含收盤價、成交量、最高最低）
    端點：/tpex_esb_latest_statistics  （含行情欄位）
    """
    data = get_json(f"{TPEX}/tpex_esb_latest_statistics")
    result = {}
    if data and isinstance(data, list):
        for r in data:
            sid = str(r.get("SecuritiesCompanyCode", "") or "").strip()
            if not sid:
                continue
            result[sid] = {
                "close":  fv(r.get("Close"), r.get("LatestPrice"), r.get("close")),
                "high":   fv(r.get("High"),  r.get("high")),
                "low":    fv(r.get("Low"),   r.get("low")),
                "volume": fv(r.get("TradeVolume"), r.get("volume")),
            }
    return result


def get_history_single(sid, days=30):
    """
    取得個股歷史日行情
    端點：/tpex_esb_stock_daily_trading_info
    """
    ym = datetime.today().strftime("%Y/%m")
    data = get_json(f"{TPEX}/tpex_esb_stock_daily_trading_info",
                    {"date": ym, "stockNo": sid})
    if data and isinstance(data, list) and len(data) >= 3:
        return data
    # 備援：前一個月
    prev = (datetime.today().replace(day=1) - timedelta(days=1))
    ym2  = prev.strftime("%Y/%m")
    data2 = get_json(f"{TPEX}/tpex_esb_stock_daily_trading_info",
                     {"date": ym2, "stockNo": sid})
    combined = (data2 or []) + (data or [])
    return combined if combined else []


# ─── ③ 三大法人（TPEx OpenAPI）──────────────────

def get_inst_all():
    """
    一次取得全市場今日三大法人買賣超（興櫃）
    端點：/tpex_esb_3insti
    回傳 dict: {sid: net_amount}
    """
    today = datetime.today().strftime("%Y/%m/%d")
    data  = get_json(f"{TPEX}/tpex_esb_3insti", {"date": today})
    result = {}
    if data and isinstance(data, list):
        for r in data:
            sid = str(r.get("SecuritiesCompanyCode", "") or r.get("stockNo", "")).strip()
            if not sid:
                continue
            buy  = fv(r.get("ForeignInvestorBuyVolume"),  r.get("foreignBuy"),  r.get("Buy"))
            sell = fv(r.get("ForeignInvestorSellVolume"), r.get("foreignSell"), r.get("Sell"))
            result[sid] = result.get(sid, 0) + (buy - sell)
    return result


# ─── ④ 承銷掛牌價（TPEx OpenAPI）────────────────

_ipo_cache = {}

def get_ipo_price_all():
    """
    一次取得全部興櫃承銷掛牌價
    端點：/tpex_esb_ipo  或 /mopsfin_t187ap03_2
    """
    global _ipo_cache
    if _ipo_cache:
        return _ipo_cache

    data = get_json(f"{TPEX}/tpex_esb_ipo")
    if not data:
        data = get_json(f"{TPEX}/mopsfin_t187ap03_2")

    if data and isinstance(data, list):
        for r in data:
            sid = str(r.get("SecuritiesCompanyCode", "") or r.get("stockNo", "") or r.get("StockCode", "")).strip()
            price = fv(r.get("IpoPrice"), r.get("ipo_price"), r.get("承銷價格"), r.get("ListingPrice"))
            if sid and price > 0:
                _ipo_cache[sid] = price

    return _ipo_cache


def get_ipo_price_single(sid):
    """單支股票承銷價備援查詢"""
    cache = get_ipo_price_all()
    if sid in cache:
        return cache[sid]
    # 個別查
    data = get_json(f"{TPEX}/tpex_esb_ipo", {"stockNo": sid})
    if data and isinstance(data, list):
        for r in data:
            v = fv(r.get("IpoPrice"), r.get("ipo_price"), r.get("承銷價格"))
            if v > 0:
                _ipo_cache[sid] = v
                return v
    return None


# ─── ⑤ 前十大股東（MOPS 公開資訊觀測站）──────────

_holder_cache = {}

def get_top10_holder(sid):
    """
    從 MOPS 公開資訊觀測站取得前十大股東資料
    資料來源：mops.twse.com.tw（每季更新）
    """
    if sid in _holder_cache:
        return _holder_cache[sid]

    # MOPS 股東持股比例查詢
    url  = f"{MOPS}/ajax_t04st12"
    year = datetime.today().year - 1911  # 民國年
    quarter = (datetime.today().month - 1) // 3  # 0-3
    if quarter == 0:
        year -= 1
        quarter = 4

    form_data = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "keyword4": "",
        "code1": "",
        "TYPEK2": "",
        "checkbtn": "",
        "queryName": "co_id",
        "inpuType": "co_id",
        "TYPEK": "emb",   # emb = 興櫃
        "isnew": "false",
        "co_id": sid,
        "year": str(year),
        "season": str(quarter),
    }

    html = get_html(url, data=form_data)
    pct  = _parse_top10_pct(html)
    _holder_cache[sid] = pct
    return pct


def _parse_top10_pct(html):
    """從 MOPS HTML 中解析前十大股東合計持股比例"""
    if not html:
        return None
    # 尋找「合計」行的百分比數字
    patterns = [
        r'合計.*?([\d.]+)\s*%',
        r'前十大.*?合計.*?([\d.]+)',
        r'10\s*大.*?([\d.]+)\s*%',
        r'持股比例.*?([\d.]+)',
    ]
    for p in patterns:
        m = re.search(p, html, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if 0 < v <= 100:
                    return round(v, 1)
            except:
                pass
    # 找所有百分比，取最大（通常是合計）
    nums = re.findall(r'(\d{1,3}\.\d{1,2})\s*%', html)
    if nums:
        vals = [float(x) for x in nums if 0 < float(x) <= 100]
        if vals:
            return round(max(vals), 1)
    return None


# ─── ⑥ 籌碼評分 ──────────────────────────────────

def score_chip(history, inst_net):
    score   = 0
    reasons = []

    # 條件1：法人淨買超
    if inst_net is not None:
        if inst_net > 0:
            score += 1
            reasons.append(f"法人淨買超 {inst_net:+,.0f} 張 ✓")
        else:
            reasons.append(f"法人淨賣超 {inst_net:+,.0f} 張 ✗")
    else:
        reasons.append("法人資料無 —")

    # 條件2：量縮後回升
    if len(history) >= 10:
        vols = [fv(d.get("TradeVolume"), d.get("成交量"), d.get("volume")) for d in history]
        avg  = sum(vols[:-3]) / max(1, len(vols) - 3)
        trough = min(vols[-7:-3]) if len(vols) >= 7 else (vols[-4] if len(vols) >= 4 else avg)
        recent = sum(vols[-3:]) / 3
        if trough < avg * 0.75 and recent > trough * 1.1:
            score += 1
            reasons.append("量縮後回升 ✓")
        else:
            reasons.append("量能型態不符 ✗")
    else:
        reasons.append("歷史資料不足 —")

    # 條件3：橫盤收斂（近 16 日高低差縮小）
    if len(history) >= 16:
        def gh(d): return fv(d.get("High"), d.get("最高"), d.get("high"))
        def gl(d): return fv(d.get("Low"),  d.get("最低"), d.get("low"))
        h1 = [gh(d) for d in history[-16:-8] if gh(d) > 0]
        l1 = [gl(d) for d in history[-16:-8] if gl(d) > 0]
        h2 = [gh(d) for d in history[-8:]    if gh(d) > 0]
        l2 = [gl(d) for d in history[-8:]    if gl(d) > 0]
        if h1 and l1 and h2 and l2:
            r1 = max(h1) - min(l1)
            r2 = max(h2) - min(l2)
            if r1 > 0 and r2 < r1 * 0.8:
                score += 1
                reasons.append("橫盤收斂 ✓")
            else:
                reasons.append("尚未收斂 ✗")
        else:
            reasons.append("高低資料缺失 —")
    else:
        reasons.append("歷史資料不足 —")

    return score, reasons


# ─── ⑦ 期望值 & 持倉 ─────────────────────────────

def calc_ev(cur, ipo):
    target  = round(ipo * GAIN_MULTIPLIER, 2)
    stop    = round(cur * (1 - STOP_PCT),  2)
    gain    = target - cur
    loss    = cur    - stop
    ev      = round(P_WIN * gain + (1 - P_WIN) * (-loss), 2)
    rr      = round(gain / loss, 2) if loss > 0 else 0
    return ev, rr, target, stop

def calc_position(cur, stop):
    lps  = cur - stop
    if lps <= 0: return 0, 0
    pct  = min(TOTAL_CAPITAL * MDD / lps * cur / TOTAL_CAPITAL, MAX_POS_PCT)
    return round(pct * 100, 1), round(TOTAL_CAPITAL * pct)


# ─── ⑧ 主流程 ────────────────────────────────────

def run():
    t0 = datetime.now()
    print(f"\n{'═'*62}")
    print(f"  興櫃篩選引擎 v3（免費公開資料版）")
    print(f"  {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*62}\n")

    # 一次性批量取得（減少 API 呼叫次數）
    stocks    = get_stock_list()
    if not stocks:
        print("❌ 無法取得興櫃清單，請確認網路連線")
        sys.exit(1)

    print("② 批量取得今日行情...")
    all_quotes = get_daily_quotes_all()
    time.sleep(REQUEST_DELAY)

    print("③ 批量取得三大法人...")
    inst_map   = get_inst_all()
    time.sleep(REQUEST_DELAY)

    print("④ 批量取得承銷掛牌價...")
    ipo_map    = get_ipo_price_all()
    time.sleep(REQUEST_DELAY)

    print(f"\n開始逐一分析（共 {min(len(stocks), MAX_STOCKS)} 支）\n{'─'*62}")

    results, passed = [], []
    total = min(len(stocks), MAX_STOCKS)

    for i, s in enumerate(stocks[:total]):
        sid, sname = s["sid"], s["sname"]
        bar = f"[{i+1:3d}/{total}]"
        print(f"{bar} {sid:<6s} {sname[:8]:<9s}", end=" ", flush=True)

        # 行情
        q = all_quotes.get(sid, {})
        cur = q.get("close", 0)

        # 若批量沒有，查個別
        if not cur:
            hist_single = get_history_single(sid)
            if hist_single:
                cur = fv(hist_single[-1].get("Close"), hist_single[-1].get("close"),
                         hist_single[-1].get("收盤價"))
            time.sleep(REQUEST_DELAY)

        if not cur or cur <= 0:
            print("→ 無收盤價，跳過")
            continue

        # 承銷價
        ipo = ipo_map.get(sid) or get_ipo_price_single(sid) or cur

        # 承銷價條件
        price_ratio = cur / ipo
        price_ok    = price_ratio <= PRICE_RATIO_MAX

        # 籌碼（歷史行情 + 法人）
        hist = get_history_single(sid)
        time.sleep(REQUEST_DELAY)
        inst_net  = inst_map.get(sid)
        chip_score, chip_reasons = score_chip(hist, inst_net)
        chip_ok   = chip_score >= CHIP_SCORE_MIN

        # 前十大股東（MOPS）
        top10_pct = get_top10_holder(sid)
        time.sleep(REQUEST_DELAY)
        holder_ok = top10_pct is not None and top10_pct >= TOP10_MIN_PCT

        filter_pass = price_ok and chip_ok and holder_ok

        # 期望值
        ev, rr, target, stop = calc_ev(cur, ipo)
        ev_pass = ev > 0
        final   = filter_pass and ev_pass

        # 持倉
        pos_pct, pos_val = calc_position(cur, stop) if final else (0, 0)

        status = "pass" if final else ("warn" if filter_pass else "fail")
        icon   = "✅" if final else ("⚠ " if filter_pass else "✗ ")

        print(f"市價={cur:.0f} 承銷={ipo:.0f} "
              f"籌碼={chip_score}/3 集中={top10_pct}% "
              f"EV={ev:+.1f}  {icon}")

        rec = dict(
            sid=sid, sname=sname,
            curPrice=round(cur,  2),
            ipoPrice=round(ipo,  2),
            priceRatioPct=round((price_ratio - 1) * 100, 1),
            priceOk=price_ok,
            chipScore=chip_score, chipOk=chip_ok, chipReasons=chip_reasons,
            top10Pct=top10_pct,   holderOk=holder_ok,
            filterPass=filter_pass,
            ev=ev, rr=rr, target=target, stop=stop,
            evPass=ev_pass, finalPass=final, status=status,
            posPct=pos_pct, posVal=pos_val,
        )
        results.append(rec)
        if final:
            passed.append(rec)

    elapsed = round((datetime.now() - t0).total_seconds())
    output = dict(
        generatedAt   = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        elapsedSec    = elapsed,
        totalScreened = len(results),
        totalPassed   = len(passed),
        params = dict(
            pWin=P_WIN, gainMult=GAIN_MULTIPLIER, stopPct=STOP_PCT,
            mdd=MDD, capital=TOTAL_CAPITAL, maxPosPct=MAX_POS_PCT,
            priceRatioMax=PRICE_RATIO_MAX,
            top10MinPct=TOP10_MIN_PCT,
            chipScoreMin=CHIP_SCORE_MIN,
        ),
        passed = sorted(passed,  key=lambda x: x["ev"], reverse=True),
        all    = sorted(results, key=lambda x: x["ev"], reverse=True),
    )

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'═'*62}")
    print(f"完成：篩 {len(results)} 支，{len(passed)} 支通過，耗時 {elapsed}s")
    print(f"結果 → {OUTPUT_FILE}")
    print(f"{'═'*62}\n")

if __name__ == "__main__":
    run()
