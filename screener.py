"""
興櫃篩選引擎 v5（最終版）
═══════════════════════════════════════════════════
資料來源：FinMind 公開 API（免費、不需 Token 也能用）
        無 token：600 次/小時
        免費註冊：1500 次/小時（建議註冊以增加額度）

執行：python screener.py
輸出：results.json
═══════════════════════════════════════════════════
"""

import json, time, sys, os, socket
from datetime import datetime, timedelta
import urllib.request, urllib.parse

socket.setdefaulttimeout(15)

# ═══════════════════════════════════════════════════
#  ★ 設定區
# ═══════════════════════════════════════════════════
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")  # 留空也能用，免費註冊可提高額度

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

# 執行控制
MAX_STOCKS    = 999
REQUEST_DELAY = 0.3   # FinMind 限流保護
OUTPUT_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
# ═══════════════════════════════════════════════════

API = "https://api.finmindtrade.com/api/v4/data"

HEADERS = {"User-Agent": "Mozilla/5.0"}
if FINMIND_TOKEN:
    HEADERS["Authorization"] = f"Bearer {FINMIND_TOKEN}"


def fm_get(dataset, stock_id=None, start=None, end=None, retries=2):
    """呼叫 FinMind API"""
    params = {"dataset": dataset}
    if stock_id: params["data_id"]    = stock_id
    if start:    params["start_date"] = start
    if end:      params["end_date"]   = end

    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8"))
                if data.get("status") == 200:
                    return data.get("data", [])
                if data.get("status") == 402:  # rate limit
                    print(f"     ⚠ FinMind 限流，等待 60 秒...", flush=True)
                    time.sleep(60)
                    continue
                return []
        except Exception as e:
            if attempt < retries:
                time.sleep(2)
            else:
                return []
    return []


def fv(*vals):
    for v in vals:
        if v not in (None, "", "—", "--"):
            try: return float(str(v).replace(",", ""))
            except: pass
    return 0.0


# ─── 取得興櫃股票清單 ─────────────────────────────

def get_emerging_stocks():
    print("① 取得台股總覽（含興櫃）...", flush=True)
    rows = fm_get("TaiwanStockInfo")
    emerging = [
        {"sid": r["stock_id"], "sname": r.get("stock_name", "")}
        for r in rows
        if r.get("type") == "emerging"
    ]
    print(f"   → 共 {len(rows)} 支股票，其中 {len(emerging)} 支為興櫃", flush=True)
    return emerging


# ─── 行情、法人、持股 ─────────────────────────────

def get_history(sid, days=35):
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return fm_get("TaiwanStockPrice", sid, start, end)

def get_inst(sid, days=14):
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return fm_get("TaiwanStockInstitutionalInvestorsBuySell", sid, start, end)

def get_holders(sid):
    """股權分散表（每週更新）"""
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=60)).strftime("%Y-%m-%d")
    rows  = fm_get("TaiwanStockShareholding", sid, start, end)
    if not rows:
        rows = fm_get("TaiwanStockHoldingSharesPer", sid, start, end)
    return rows


# ─── 評分 ─────────────────────────────────────────

def score_chip(history, inst):
    score, reasons = 0, []

    # 條件1：法人淨買超
    if inst:
        net = sum(fv(d.get("buy")) - fv(d.get("sell")) for d in inst)
        if net > 0:
            score += 1; reasons.append(f"法人淨買超 ✓")
        else:
            reasons.append("法人未買超 ✗")
    else:
        reasons.append("無法人資料 —")

    # 條件2：量縮後回升
    if len(history) >= 10:
        vols = [fv(d.get("Trading_Volume"), d.get("volume")) for d in history]
        avg = sum(vols[:-3]) / max(1, len(vols) - 3)
        trough = min(vols[-7:-3]) if len(vols) >= 7 else (vols[-4] if len(vols) >= 4 else avg)
        recent = sum(vols[-3:]) / 3
        if trough < avg * 0.75 and recent > trough * 1.1:
            score += 1; reasons.append("量縮回升 ✓")
        else:
            reasons.append("量能型態不符 ✗")
    else:
        reasons.append("歷史資料不足 —")

    # 條件3：橫盤收斂
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


def score_holders(holders):
    """從股權分散表計算前十大股東持股比例"""
    if not holders:
        return None
    # 取最新一週
    latest_date = max(d.get("date", "") for d in holders)
    latest = [d for d in holders if d.get("date") == latest_date]
    # FinMind 股權分散表級距 1=1-999, 2=1000-5000 ... 15=1000001以上
    # 通常前十大持股比例會在大級距（千張以上）累計
    big = sum(fv(d.get("percent")) for d in latest if int(d.get("HoldingSharesLevel", 0) or 0) >= 13)
    if big > 0:
        return round(min(big, 100), 1)
    # 退而求其次：累加全部 percent 中前10名
    return None


# ─── 期望值與持倉 ─────────────────────────────────

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


# ─── 主流程 ───────────────────────────────────────

def run():
    t0 = datetime.now()
    print(f"\n{'═'*62}")
    print(f"  興櫃篩選引擎 v5（FinMind 真實資料版）")
    print(f"  {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Token: {'✓ 已設定' if FINMIND_TOKEN else '✗ 未設定（限流 600/hr）'}")
    print(f"{'═'*62}\n", flush=True)

    stocks = get_emerging_stocks()
    if not stocks:
        print("❌ FinMind 無法取得興櫃清單，請確認網路")
        sys.exit(1)

    total = min(len(stocks), MAX_STOCKS)
    print(f"\n第一階段：兩關過濾（共 {total} 支）\n{'─'*62}", flush=True)

    candidates  = []
    early_drops = []

    for i, s in enumerate(stocks[:total]):
        sid, sname = s["sid"], s["sname"]
        bar = f"[{i+1:3d}/{total}]"

        # 行情
        hist = get_history(sid)
        time.sleep(REQUEST_DELAY)
        if not hist:
            continue
        last = hist[-1]
        cur  = fv(last.get("close"), last.get("Close"))
        if cur <= 0:
            continue

        # 承銷價（取最早一筆收盤價當作參考，或用 IPO API）
        ipo_data = fm_get("TaiwanStockIPOPrice", sid)
        time.sleep(REQUEST_DELAY)
        if ipo_data:
            ipo = fv(ipo_data[-1].get("ipo_price"))
        else:
            ipo = fv(hist[0].get("close")) or cur
        if ipo <= 0: ipo = cur

        # 承銷價條件
        price_ratio = cur / ipo
        price_ok    = price_ratio <= PRICE_RATIO_MAX

        # 籌碼
        inst = get_inst(sid)
        time.sleep(REQUEST_DELAY)
        chip_score, chip_reasons = score_chip(hist, inst)
        chip_ok = chip_score >= CHIP_SCORE_MIN

        ev, rr, target, stop = calc_ev(cur, ipo)

        rec = dict(
            sid=sid, sname=sname,
            curPrice=round(cur, 2), ipoPrice=round(ipo, 2),
            priceRatioPct=round((price_ratio - 1) * 100, 1),
            priceOk=price_ok,
            chipScore=chip_score, chipOk=chip_ok, chipReasons=chip_reasons,
            top10Pct=None, holderOk=False,
            filterPass=False,
            ev=ev, rr=rr, target=target, stop=stop,
            evPass=ev > 0, finalPass=False,
            status="fail", posPct=0, posVal=0,
        )

        if price_ok and chip_ok:
            candidates.append(rec)
            print(f"{bar} {sid:<6s} {sname[:8]:<9s} 市價={cur:.0f} 承銷={ipo:.0f} "
                  f"籌碼={chip_score}/3 → ⏩ 候選", flush=True)
        else:
            early_drops.append(rec)

    print(f"\n第一階段完成：{len(candidates)} 支候選、{len(early_drops)} 支淘汰", flush=True)

    # ─── 第二階段：股東結構 ───
    print(f"\n第二階段：前十大股東查詢（共 {len(candidates)} 支）\n{'─'*62}", flush=True)

    passed = []
    for i, rec in enumerate(candidates):
        sid, sname = rec["sid"], rec["sname"]
        bar = f"[{i+1:3d}/{len(candidates)}]"
        print(f"{bar} {sid:<6s} {sname[:8]:<9s}", end=" ", flush=True)

        holders = get_holders(sid)
        time.sleep(REQUEST_DELAY)
        top10_pct = score_holders(holders)

        rec["top10Pct"]   = top10_pct
        rec["holderOk"]   = top10_pct is not None and top10_pct >= TOP10_MIN_PCT
        rec["filterPass"] = rec["priceOk"] and rec["chipOk"] and rec["holderOk"]
        rec["finalPass"]  = rec["filterPass"] and rec["evPass"]

        if rec["finalPass"]:
            rec["status"] = "pass"
            rec["posPct"], rec["posVal"] = calc_position(rec["curPrice"], rec["stop"])
            passed.append(rec)
            print(f"集中={top10_pct}% EV={rec['ev']:+.1f}  ✅", flush=True)
        elif rec["filterPass"]:
            rec["status"] = "warn"
            print(f"集中={top10_pct}% EV={rec['ev']:+.1f}  ⚠ EV不足", flush=True)
        else:
            rec["status"] = "fail"
            t = f"{top10_pct}%" if top10_pct is not None else "查無"
            print(f"集中={t} → 股東關未過", flush=True)

    # ─── 輸出 ───
    all_results = candidates + early_drops
    elapsed = round((datetime.now() - t0).total_seconds())

    output = dict(
        generatedAt   = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        elapsedSec    = elapsed,
        totalScreened = len(all_results),
        totalPassed   = len(passed),
        params = dict(
            pWin=P_WIN, gainMult=GAIN_MULTIPLIER, stopPct=STOP_PCT,
            mdd=MDD, capital=TOTAL_CAPITAL, maxPosPct=MAX_POS_PCT,
            priceRatioMax=PRICE_RATIO_MAX,
            top10MinPct=TOP10_MIN_PCT,
            chipScoreMin=CHIP_SCORE_MIN,
        ),
        passed = sorted(passed,      key=lambda x: x["ev"], reverse=True),
        all    = sorted(all_results, key=lambda x: x["ev"], reverse=True),
    )

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'═'*62}")
    print(f"完成：篩 {len(all_results)} 支，{len(passed)} 支通過，耗時 {elapsed}s")
    print(f"結果 → {OUTPUT_FILE}")
    print(f"{'═'*62}\n", flush=True)


if __name__ == "__main__":
    run()
