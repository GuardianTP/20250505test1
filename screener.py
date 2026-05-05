"""
興櫃篩選引擎 v6（批量優化最終版）
═══════════════════════════════════════════════════
核心優化：用 3 次 API 批量抓取代替 400 支 × 4 次的逐查模式
預期執行時間：5-10 分鐘

資料來源：FinMind API
        無 Token：600 次/小時（v6 一次跑只用 ~10 次）
        免費註冊：1500 次/小時

執行：python screener.py
輸出：results.json
═══════════════════════════════════════════════════
"""

import json, time, sys, os, socket
from datetime import datetime, timedelta
from collections import defaultdict
import urllib.request, urllib.parse

socket.setdefaulttimeout(30)

# ═══════════════════════════════════════════════════
#  ★ 設定區
# ═══════════════════════════════════════════════════
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

# 期望值
P_WIN           = 0.55
GAIN_MULTIPLIER = 1.5
STOP_PCT        = 0.15

# 風控
MDD             = 0.10
TOTAL_CAPITAL   = 1_000_000
MAX_POS_PCT     = 0.30

# 篩選門檻
PRICE_RATIO_MAX = 1.05
TOP10_MIN_PCT   = 40.0
CHIP_SCORE_MIN  = 2

# 由於 FinMind 興櫃股東資料覆蓋率低，提供 fallback 模式：
# True  = 把「股東結構」改為「籌碼面外資持股比例」當作替代評估
# False = 保留原邏輯，覆蓋率不足時跳過該關
USE_FOREIGN_HOLDING_FALLBACK = True

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
# ═══════════════════════════════════════════════════

API = "https://api.finmindtrade.com/api/v4/data"

HEADERS = {"User-Agent": "Mozilla/5.0"}
if FINMIND_TOKEN:
    HEADERS["Authorization"] = f"Bearer {FINMIND_TOKEN}"


def fm_get(dataset, **kwargs):
    """呼叫 FinMind API（帶限流自動重試）"""
    params = {"dataset": dataset, **{k: v for k, v in kwargs.items() if v}}
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
                status = data.get("status")
                if status == 200:
                    return data.get("data", [])
                if status == 402:
                    wait = 65 if not FINMIND_TOKEN else 30
                    print(f"     ⚠ FinMind 限流，等待 {wait} 秒後重試...", flush=True)
                    time.sleep(wait)
                    continue
                print(f"     ⚠ FinMind 錯誤 status={status}: {data.get('msg','')}", flush=True)
                return []
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"     ⚠ {dataset} 請求失敗：{e}", flush=True)
                return []
    return []


def fv(*vals):
    for v in vals:
        if v not in (None, "", "—", "--"):
            try:
                return float(str(v).replace(",", ""))
            except:
                pass
    return 0.0


# ═══════════════════════════════════════════════════
#  批量資料抓取（核心優化）
# ═══════════════════════════════════════════════════

def fetch_all_stock_info():
    """1 次 API 取得全部股票清單"""
    print("① 取得台股總覽...", flush=True)
    rows = fm_get("TaiwanStockInfo")
    emerging = {r["stock_id"]: r.get("stock_name", "")
                for r in rows if r.get("type") == "emerging"}
    print(f"   → 共 {len(rows)} 支股票，興櫃 {len(emerging)} 支", flush=True)
    return emerging


def fetch_all_prices(start_date, end_date):
    """1 次 API 取得全部股票 35 天行情"""
    print(f"② 批量取得全市場行情（{start_date} ~ {end_date}）...", flush=True)
    rows = fm_get("TaiwanStockPrice", start_date=start_date, end_date=end_date)
    print(f"   → 共 {len(rows)} 筆行情", flush=True)
    grouped = defaultdict(list)
    for r in rows:
        sid = r.get("stock_id", "")
        if sid:
            grouped[sid].append(r)
    for sid in grouped:
        grouped[sid].sort(key=lambda x: x.get("date", ""))
    return grouped


def fetch_all_inst(start_date, end_date):
    """1 次 API 取得全部股票 14 天三大法人"""
    print(f"③ 批量取得全市場三大法人...", flush=True)
    rows = fm_get("TaiwanStockInstitutionalInvestorsBuySell",
                  start_date=start_date, end_date=end_date)
    print(f"   → 共 {len(rows)} 筆法人資料", flush=True)
    net_map = defaultdict(float)
    for r in rows:
        sid = r.get("stock_id", "")
        net_map[sid] += fv(r.get("buy")) - fv(r.get("sell"))
    return net_map


def fetch_all_holdings(date):
    """1 次 API 取得全市場股權分散表（最近一週）"""
    print(f"④ 批量取得全市場股權分散表...", flush=True)
    # 取最近兩週的資料以確保有資料
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=21)).strftime("%Y-%m-%d")
    rows = fm_get("TaiwanStockShareholding", start_date=start, end_date=date)
    print(f"   → 共 {len(rows)} 筆股權分散資料", flush=True)
    return rows


def fetch_foreign_holding(start_date, end_date):
    """fallback：批量取得外資持股比例"""
    print(f"⑤ 批量取得外資持股比例（fallback）...", flush=True)
    rows = fm_get("TaiwanStockShareholdingClass",
                  start_date=start_date, end_date=end_date)
    if not rows:
        # 試試另一個端點
        rows = fm_get("TaiwanStockHoldingSharesPer",
                      start_date=start_date, end_date=end_date)
    print(f"   → 共 {len(rows)} 筆", flush=True)
    # 取每支股票最新一筆
    latest = {}
    for r in sorted(rows, key=lambda x: x.get("date", "")):
        sid = r.get("stock_id", "")
        if sid:
            latest[sid] = r
    return latest


# ═══════════════════════════════════════════════════
#  評分邏輯
# ═══════════════════════════════════════════════════

def score_chip(history, inst_net):
    score, reasons = 0, []

    # 1. 法人淨買超
    if inst_net != 0:
        if inst_net > 0:
            score += 1; reasons.append(f"法人淨買超 ✓")
        else:
            reasons.append("法人未買超 ✗")
    else:
        reasons.append("無法人資料 —")

    # 2. 量縮後回升
    if len(history) >= 10:
        vols = [fv(d.get("Trading_Volume"), d.get("volume")) for d in history]
        avg = sum(vols[:-3]) / max(1, len(vols) - 3)
        trough = min(vols[-7:-3]) if len(vols) >= 7 else (vols[-4] if len(vols) >= 4 else avg)
        recent = sum(vols[-3:]) / 3
        if avg > 0 and trough < avg * 0.75 and recent > trough * 1.1:
            score += 1; reasons.append("量縮回升 ✓")
        else:
            reasons.append("量能型態不符 ✗")
    else:
        reasons.append("歷史資料不足 —")

    # 3. 橫盤收斂
    if len(history) >= 16:
        h = lambda d: fv(d.get("max"), d.get("high"))
        l = lambda d: fv(d.get("min"), d.get("low"))
        h1 = [h(d) for d in history[-16:-8] if h(d) > 0]
        l1 = [l(d) for d in history[-16:-8] if l(d) > 0]
        h2 = [h(d) for d in history[-8:]    if h(d) > 0]
        l2 = [l(d) for d in history[-8:]    if l(d) > 0]
        if h1 and l1 and h2 and l2:
            r1 = max(h1) - min(l1)
            r2 = max(h2) - min(l2)
            if r1 > 0 and r2 < r1 * 0.8:
                score += 1; reasons.append("橫盤收斂 ✓")
            else:
                reasons.append("尚未收斂 ✗")
    else:
        reasons.append("歷史資料不足 —")

    return score, reasons


def score_holders(holdings_for_sid):
    """計算前十大股東持股集中度"""
    if not holdings_for_sid:
        return None
    # 取最新一筆日期
    latest_date = max(d.get("date", "") for d in holdings_for_sid)
    latest = [d for d in holdings_for_sid if d.get("date") == latest_date]
    # 大股東級距：13 級以上(千張以上)代表大戶
    big = sum(fv(d.get("percent")) for d in latest
              if int(d.get("HoldingSharesLevel", 0) or 0) >= 13)
    if big > 0:
        return round(min(big, 100), 1)
    return None


def score_foreign_holding(record):
    """fallback：用外資持股比例代替前十大集中度"""
    if not record:
        return None
    pct = fv(record.get("percent"), record.get("ForeignInvestSharesRatio"),
             record.get("ratio"))
    return round(pct, 1) if pct > 0 else None


# ═══════════════════════════════════════════════════
#  期望值與持倉
# ═══════════════════════════════════════════════════

def calc_ev(cur, ipo):
    target = round(ipo * GAIN_MULTIPLIER, 2)
    stop   = round(cur * (1 - STOP_PCT),  2)
    gain   = target - cur
    loss   = cur - stop
    ev     = round(P_WIN * gain + (1 - P_WIN) * (-loss), 2)
    rr     = round(gain / loss, 2) if loss > 0 else 0
    return ev, rr, target, stop


def calc_position(cur, stop):
    lps = cur - stop
    if lps <= 0: return 0, 0
    pct = min(TOTAL_CAPITAL * MDD / lps * cur / TOTAL_CAPITAL, MAX_POS_PCT)
    return round(pct * 100, 1), round(TOTAL_CAPITAL * pct)


# ═══════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════

def run():
    t0 = datetime.now()
    print(f"\n{'═'*62}")
    print(f"  興櫃篩選引擎 v6（批量優化版）")
    print(f"  {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Token: {'✓ 已設定' if FINMIND_TOKEN else '✗ 未設定（限流 600/hr）'}")
    print(f"{'═'*62}\n", flush=True)

    end_date   = datetime.today().strftime("%Y-%m-%d")
    price_start = (datetime.today() - timedelta(days=45)).strftime("%Y-%m-%d")
    inst_start  = (datetime.today() - timedelta(days=14)).strftime("%Y-%m-%d")

    # 一次性批量抓取
    emerging = fetch_all_stock_info()
    if not emerging:
        print("❌ 無法取得興櫃清單"); sys.exit(1)

    prices_by_sid = fetch_all_prices(price_start, end_date)
    inst_by_sid   = fetch_all_inst(inst_start, end_date)

    # 興櫃 IPO 承銷價（每支需單獨查，但只查興櫃約 400 支）
    print(f"⑤ 取得承銷掛牌價...", flush=True)
    ipo_data_all = fm_get("TaiwanStockIPOPrice")
    ipo_map = {}
    for r in ipo_data_all:
        sid = r.get("stock_id", "")
        ipo = fv(r.get("ipo_price"))
        if sid and ipo > 0:
            ipo_map[sid] = ipo
    print(f"   → 取得 {len(ipo_map)} 支股票的承銷價", flush=True)

    # 股東資料
    if USE_FOREIGN_HOLDING_FALLBACK:
        foreign_map = fetch_foreign_holding(inst_start, end_date)
        holdings_by_sid = {}
    else:
        holdings_rows = fetch_all_holdings(end_date)
        holdings_by_sid = defaultdict(list)
        for r in holdings_rows:
            sid = r.get("stock_id", "")
            if sid:
                holdings_by_sid[sid].append(r)
        foreign_map = {}

    # ─── 開始篩選 ───
    print(f"\n開始篩選 {len(emerging)} 支興櫃股票\n{'─'*62}", flush=True)

    results, passed = [], []

    for i, (sid, sname) in enumerate(emerging.items()):
        bar = f"[{i+1:3d}/{len(emerging)}]"

        # 行情
        history = prices_by_sid.get(sid, [])
        if not history:
            continue
        cur = fv(history[-1].get("close"))
        if cur <= 0:
            continue

        # 承銷價
        ipo = ipo_map.get(sid) or fv(history[0].get("close")) or cur
        if ipo <= 0:
            ipo = cur

        # 三大條件
        price_ratio = cur / ipo
        price_ok    = price_ratio <= PRICE_RATIO_MAX

        chip_score, chip_reasons = score_chip(history, inst_by_sid.get(sid, 0))
        chip_ok = chip_score >= CHIP_SCORE_MIN

        # 股東關
        if USE_FOREIGN_HOLDING_FALLBACK:
            top10_pct = score_foreign_holding(foreign_map.get(sid))
        else:
            top10_pct = score_holders(holdings_by_sid.get(sid))

        holder_ok = top10_pct is not None and top10_pct >= TOP10_MIN_PCT
        filter_pass = price_ok and chip_ok and holder_ok

        # 期望值
        ev, rr, target, stop = calc_ev(cur, ipo)
        ev_pass = ev > 0
        final = filter_pass and ev_pass

        pos_pct, pos_val = calc_position(cur, stop) if final else (0, 0)
        status = "pass" if final else ("warn" if filter_pass else "fail")
        icon   = "✅" if final else ("⚠ " if filter_pass else "✗ ")

        # 只 print 通過或候選的
        if filter_pass or chip_ok:
            top10_str = f"{top10_pct}%" if top10_pct is not None else "—"
            print(f"{bar} {sid:<6s} {sname[:8]:<9s} 市價={cur:.0f} 承銷={ipo:.0f} "
                  f"籌碼={chip_score}/3 集中={top10_str} EV={ev:+.1f}  {icon}",
                  flush=True)

        rec = dict(
            sid=sid, sname=sname,
            curPrice=round(cur, 2), ipoPrice=round(ipo, 2),
            priceRatioPct=round((price_ratio - 1) * 100, 1),
            priceOk=price_ok,
            chipScore=chip_score, chipOk=chip_ok, chipReasons=chip_reasons,
            top10Pct=top10_pct, holderOk=holder_ok,
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
            holderField="外資持股比例" if USE_FOREIGN_HOLDING_FALLBACK else "前十大股東",
        ),
        passed = sorted(passed,  key=lambda x: x["ev"], reverse=True),
        all    = sorted(results, key=lambda x: x["ev"], reverse=True),
    )

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'═'*62}")
    print(f"完成：篩 {len(results)} 支，{len(passed)} 支通過，耗時 {elapsed}s")
    print(f"結果 → {OUTPUT_FILE}")
    print(f"{'═'*62}\n", flush=True)


if __name__ == "__main__":
    run()
