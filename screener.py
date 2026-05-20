"""
興櫃篩選引擎 v7（混合模式 — 自動籌碼篩選 + 人工確認）
═══════════════════════════════════════════════════
階段一(本程式)：自動執行籌碼面 + 量價型態篩選
階段二(Dashboard)：你對候選股手動勾選承銷價、股東結構

資料來源：FinMind API（免費）
執行時間：5-10 分鐘
═══════════════════════════════════════════════════
"""

import json, time, sys, os, socket
from datetime import datetime, timedelta
import urllib.request, urllib.parse

socket.setdefaulttimeout(30)

# ═══════════════════════════════════════════════════
#  ★ 設定區
# ═══════════════════════════════════════════════════
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

# 期望值預設參數（會在 Dashboard 重算）
P_WIN           = 0.55
GAIN_MULTIPLIER = 1.5
STOP_PCT        = 0.15

MDD             = 0.10
TOTAL_CAPITAL   = 1_000_000
MAX_POS_PCT     = 0.30

# 籌碼篩選門檻
CHIP_SCORE_MIN  = 2

# 流動性過濾（去掉成交量過低的殭屍股，提高效率）
MIN_AVG_VOLUME  = 50     # 近 10 日平均成交張數最低門檻
MIN_PRICE       = 5      # 最低股價（過濾雞蛋水餃股）

REQUEST_DELAY = 0.4
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.json")
# ═══════════════════════════════════════════════════

API = "https://api.finmindtrade.com/api/v4/data"
HEADERS = {"User-Agent": "Mozilla/5.0"}
if FINMIND_TOKEN:
    HEADERS["Authorization"] = f"Bearer {FINMIND_TOKEN}"


def fm_get(dataset, **kwargs):
    params = {"dataset": dataset, **{k: v for k, v in kwargs.items() if v}}
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
                status = data.get("status")
                if status == 200:
                    return data.get("data", [])
                if status == 402:
                    wait = 65 if not FINMIND_TOKEN else 30
                    print(f"     ⚠ 限流，等待 {wait} 秒...", flush=True)
                    time.sleep(wait)
                    continue
                return []
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
    return []


def fv(*vals):
    for v in vals:
        if v not in (None, "", "—", "--"):
            try: return float(str(v).replace(",", ""))
            except: pass
    return 0.0


# ═══════════════════════════════════════════════════
#  資料抓取
# ═══════════════════════════════════════════════════

def get_emerging_stocks():
    print("① 取得興櫃股票清單...", flush=True)
    rows = fm_get("TaiwanStockInfo")
    emerging = [
        {"sid": r["stock_id"], "sname": r.get("stock_name", "")}
        for r in rows
        if r.get("type") == "emerging"
    ]
    print(f"   → 共 {len(emerging)} 支興櫃股票", flush=True)
    return emerging


def get_history(sid, days=45):
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return fm_get("TaiwanStockPrice", data_id=sid, start_date=start, end_date=end)


def get_inst(sid, days=14):
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return fm_get("TaiwanStockInstitutionalInvestorsBuySell",
                  data_id=sid, start_date=start, end_date=end)


# ═══════════════════════════════════════════════════
#  籌碼評分
# ═══════════════════════════════════════════════════

def score_chip(history, inst):
    score, reasons = 0, []

    # 條件1：法人淨買超
    if inst:
        net = sum(fv(d.get("buy")) - fv(d.get("sell")) for d in inst)
        if net > 0:
            score += 1
            reasons.append(f"法人淨買超 ✓")
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
        if avg > 0 and trough < avg * 0.75 and recent > trough * 1.1:
            score += 1
            reasons.append("量縮回升 ✓")
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
                score += 1
                reasons.append("橫盤收斂 ✓")
            else:
                reasons.append("尚未收斂 ✗")
    else:
        reasons.append("歷史資料不足 —")

    return score, reasons


# ═══════════════════════════════════════════════════
#  期望值計算（預設使用市價當承銷價，Dashboard 會重算）
# ═══════════════════════════════════════════════════

def calc_ev_default(cur):
    """預設期望值（用市價 * 1.5 當目標）"""
    target = round(cur * GAIN_MULTIPLIER, 2)
    stop   = round(cur * (1 - STOP_PCT),  2)
    gain   = target - cur
    loss   = cur - stop
    ev     = round(P_WIN * gain + (1 - P_WIN) * (-loss), 2)
    rr     = round(gain / loss, 2) if loss > 0 else 0
    return ev, rr, target, stop


# ═══════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════

def run():
    t0 = datetime.now()
    print(f"\n{'═'*62}")
    print(f"  興櫃篩選引擎 v7（混合模式）")
    print(f"  {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Token: {'✓ 已設定' if FINMIND_TOKEN else '✗ 未設定（限流 600/hr）'}")
    print(f"{'═'*62}\n", flush=True)

    stocks = get_emerging_stocks()
    if not stocks:
        print("❌ 無法取得興櫃清單"); sys.exit(1)

    # === 預過濾：先批量取得最近一日全市場行情（用於成交量初篩） ===
    print("\n② 取得最近行情供流動性初篩...", flush=True)
    # 用近 3 個交易日的均量初篩
    today = datetime.today().strftime("%Y-%m-%d")
    days_back = 14
    start = (datetime.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    # 對每支興櫃股票檢查均量（避免處理沒人交易的殭屍股）
    print(f"\n第一階段：流動性 + 籌碼篩選（共 {len(stocks)} 支）\n{'─'*62}", flush=True)

    candidates = []
    early_drops = []

    for i, s in enumerate(stocks):
        sid, sname = s["sid"], s["sname"]
        bar = f"[{i+1:3d}/{len(stocks)}]"

        # 取行情
        history = get_history(sid)
        time.sleep(REQUEST_DELAY)
        if not history or len(history) < 10:
            continue

        cur = fv(history[-1].get("close"))
        if cur < MIN_PRICE:
            continue

        # 流動性初篩
        vols = [fv(d.get("Trading_Volume")) for d in history[-10:]]
        avg_vol = sum(vols) / max(1, len(vols))
        if avg_vol < MIN_AVG_VOLUME * 1000:  # 張轉股
            continue

        # 取法人
        inst = get_inst(sid)
        time.sleep(REQUEST_DELAY)

        # 籌碼評分
        chip_score, chip_reasons = score_chip(history, inst)
        chip_ok = chip_score >= CHIP_SCORE_MIN

        # 計算法人淨買超
        inst_net = sum(fv(d.get("buy")) - fv(d.get("sell")) for d in inst) if inst else 0

        # 預設期望值（Dashboard 會重算）
        ev, rr, target, stop = calc_ev_default(cur)

        rec = dict(
            sid=sid, sname=sname,
            curPrice=round(cur, 2),
            avgVolume=round(avg_vol / 1000, 1),  # 轉張
            instNet=round(inst_net / 1000, 1),    # 轉張
            chipScore=chip_score,
            chipOk=chip_ok,
            chipReasons=chip_reasons,
            # 以下欄位需要在 Dashboard 手動填寫
            ipoPrice=None,         # 承銷價（待手動填）
            top10Pct=None,          # 前十大持股（待手動填）
            # 預設期望值（用市價×1.5）
            ev=ev, rr=rr, target=target, stop=stop,
            evPass=ev > 0,
            status="candidate" if chip_ok else "fail",
            # 額外幫使用者準備的查詢連結
            goodinfoUrl=f"https://goodinfo.tw/tw/StockInfo.asp?STOCK_ID={sid}",
            mopsUrl=f"https://mops.twse.com.tw/mops/web/t05st08?co_id={sid}",
            yongfengUrl=f"https://www.sinotrade.com.tw/Stock/Stock_3_1_3?stockcode={sid}",
        )

        if chip_ok:
            candidates.append(rec)
            print(f"{bar} {sid:<6s} {sname[:8]:<9s} 市價={cur:.0f} "
                  f"均量={avg_vol/1000:.0f}張 籌碼={chip_score}/3 法人={inst_net/1000:+.0f}張 → ✅ 候選",
                  flush=True)
        else:
            early_drops.append(rec)

    elapsed = round((datetime.now() - t0).total_seconds())

    print(f"\n{'─'*62}")
    print(f"第一階段完成：{len(candidates)} 支候選，{len(early_drops)} 支淘汰")
    print(f"耗時 {elapsed}s\n", flush=True)

    # 輸出結果
    output = dict(
        generatedAt   = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        elapsedSec    = elapsed,
        totalScreened = len(candidates) + len(early_drops),
        totalCandidates = len(candidates),
        totalPassed   = 0,  # 需 Dashboard 確認才算通過
        params = dict(
            pWin=P_WIN, gainMult=GAIN_MULTIPLIER, stopPct=STOP_PCT,
            mdd=MDD, capital=TOTAL_CAPITAL, maxPosPct=MAX_POS_PCT,
            chipScoreMin=CHIP_SCORE_MIN,
            minAvgVolume=MIN_AVG_VOLUME, minPrice=MIN_PRICE,
        ),
        # 候選清單排在前面，方便 Dashboard 直接看
        candidates = sorted(candidates,  key=lambda x: x["chipScore"], reverse=True),
        all        = sorted(candidates + early_drops, key=lambda x: x["chipScore"], reverse=True),
    )

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"結果 → {OUTPUT_FILE}")
    print(f"下一步：到 Dashboard 對 {len(candidates)} 支候選股手動填承銷價、股東集中度")
    print(f"{'═'*62}\n", flush=True)


if __name__ == "__main__":
    run()
