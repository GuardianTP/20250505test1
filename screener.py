"""
興櫃篩選引擎 v8 — 三指標替代承銷價
═══════════════════════════════════════════════════
取代「承銷價」的三個指標：
  ① 近高距離 ≥ 30%   現價距離近 6 月高點仍有折價空間（主力盈虧線下）
  ② 低點卷起 ≤ 20%   現價距離近 3 月低點不遠（主力仍在收貨期）
  ③ 本益比 < 30 倍   估值不過高

三個指標全部通過 = 第一關「價格便宜」通過

執行：python screener.py
輸出：results.json
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

# 期望值
P_WIN           = 0.55
GAIN_MULTIPLIER = 1.5
STOP_PCT        = 0.15

# 風控
MDD             = 0.10
TOTAL_CAPITAL   = 1_000_000
MAX_POS_PCT     = 0.30

# 「便宜貨」三指標門檻
HIGH_DROP_MIN   = 30.0    # 近 6 月高點下跌幅度需 ≥ 30%
LOW_RISE_MAX    = 20.0    # 距近 3 月低點漲幅 ≤ 20%
PER_MAX         = 30.0    # 本益比 < 30
CHEAP_SCORE_MIN = 2       # 三指標至少通過 N 項才算「便宜」

# 籌碼面門檻
CHIP_SCORE_MIN  = 2

# 股東集中度（仍需手動填，但保留欄位）
TOP10_MIN_PCT   = 40.0

# 流動性
MIN_AVG_VOLUME  = 50
MIN_PRICE       = 5

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
        except Exception:
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

def get_psb_stock_ids():
    """從櫃買中心取得戰略新板股票代號清單"""
    print("  ↳ 取得戰略新板清單以排除...", flush=True)
    url = "https://www.tpex.org.tw/openapi/v1/tpex_psb_listing_companies"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, list):
                psb_ids = set()
                for r in data:
                    # 嘗試不同欄位名
                    sid = (r.get("SecuritiesCompanyCode") or r.get("stockNo")
                           or r.get("公司代號") or r.get("Code") or "")
                    if sid:
                        psb_ids.add(str(sid).strip())
                print(f"  ↳ 找到 {len(psb_ids)} 支戰略新板股票", flush=True)
                return psb_ids
    except Exception as e:
        print(f"  ⚠ 無法取得戰略新板清單：{e}", flush=True)
        print(f"  ↳ 改用代號規則辨識（5 碼開頭為 9 視為戰略新板）", flush=True)
    return None


def get_emerging_stocks():
    print("① 取得興櫃股票清單（僅一般板）...", flush=True)
    rows = fm_get("TaiwanStockInfo")

    # 取得所有興櫃
    all_emerging = [{"sid": r["stock_id"],
                     "sname": r.get("stock_name", ""),
                     "industry": r.get("industry_category", "")}
                    for r in rows if r.get("type") == "emerging"]

    # 取得戰略新板清單
    psb_ids = get_psb_stock_ids()

    # 過濾
    if psb_ids:
        general = [s for s in all_emerging if s["sid"] not in psb_ids]
    else:
        # 備用過濾：依產業類別欄位含「戰略新板」字樣，或代號為 5 位且 9 開頭
        general = [s for s in all_emerging
                   if "戰略新板" not in s.get("industry", "")
                   and not (len(s["sid"]) == 5 and s["sid"].startswith("9"))]

    excluded = len(all_emerging) - len(general)
    print(f"   → 興櫃總數 {len(all_emerging)} 支，排除戰略新板 {excluded} 支，剩 {len(general)} 支一般板", flush=True)
    return general


def get_history(sid, days=200):
    """近 200 天行情（用於計算 6 個月高低點）"""
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return fm_get("TaiwanStockPrice", data_id=sid, start_date=start, end_date=end)


def get_inst(sid, days=14):
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return fm_get("TaiwanStockInstitutionalInvestorsBuySell",
                  data_id=sid, start_date=start, end_date=end)


def get_per(sid, days=30):
    """近 30 天 PER (取最新一筆)"""
    end   = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    return fm_get("TaiwanStockPER", data_id=sid, start_date=start, end_date=end)


# ═══════════════════════════════════════════════════
#  評分邏輯
# ═══════════════════════════════════════════════════

def score_chip(history, inst):
    """籌碼三指標（沿用 v7）"""
    score, reasons = 0, []

    if inst:
        net = sum(fv(d.get("buy")) - fv(d.get("sell")) for d in inst)
        if net > 0:
            score += 1; reasons.append("法人淨買超 ✓")
        else:
            reasons.append("法人未買超 ✗")
    else:
        reasons.append("無法人資料 —")

    if len(history) >= 10:
        recent = history[-30:] if len(history) >= 30 else history
        vols = [fv(d.get("Trading_Volume"), d.get("volume")) for d in recent]
        avg = sum(vols[:-3]) / max(1, len(vols) - 3)
        trough = min(vols[-7:-3]) if len(vols) >= 7 else (vols[-4] if len(vols) >= 4 else avg)
        rcv = sum(vols[-3:]) / 3
        if avg > 0 and trough < avg * 0.75 and rcv > trough * 1.1:
            score += 1; reasons.append("量縮回升 ✓")
        else:
            reasons.append("量能型態不符 ✗")
    else:
        reasons.append("歷史資料不足 —")

    if len(history) >= 16:
        recent = history[-20:]
        h = lambda d: fv(d.get("max"), d.get("high"))
        l = lambda d: fv(d.get("min"), d.get("low"))
        h1 = [h(d) for d in recent[:8] if h(d) > 0]
        l1 = [l(d) for d in recent[:8] if l(d) > 0]
        h2 = [h(d) for d in recent[8:] if h(d) > 0]
        l2 = [l(d) for d in recent[8:] if l(d) > 0]
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


def score_cheap(history, per):
    """便宜三指標：近高距離 + 低點卷起 + 本益比"""
    score, reasons = 0, []
    metrics = {}

    if not history:
        return 0, ["無歷史資料"], {}

    cur = fv(history[-1].get("close"))

    # 計算 6 個月高、3 個月低
    # 假設一個月約 22 個交易日
    high_window = history[-min(132, len(history)):]   # 約 6 個月
    low_window  = history[-min(66,  len(history)):]    # 約 3 個月

    highs = [fv(d.get("max"), d.get("high")) for d in high_window]
    highs = [h for h in highs if h > 0]
    lows  = [fv(d.get("min"), d.get("low"))  for d in low_window]
    lows  = [l for l in lows  if l > 0]

    # 指標1：近高距離
    if highs and cur > 0:
        high_6m = max(highs)
        drop_pct = (high_6m - cur) / high_6m * 100
        metrics["high6m"] = round(high_6m, 2)
        metrics["dropPct"] = round(drop_pct, 1)
        if drop_pct >= HIGH_DROP_MIN:
            score += 1
            reasons.append(f"距高點 {drop_pct:.0f}% ✓")
        else:
            reasons.append(f"距高點僅 {drop_pct:.0f}% ✗")
    else:
        reasons.append("無高點資料 —")

    # 指標2：低點卷起
    if lows and cur > 0:
        low_3m = min(lows)
        rise_pct = (cur - low_3m) / low_3m * 100 if low_3m > 0 else 999
        metrics["low3m"] = round(low_3m, 2)
        metrics["risePct"] = round(rise_pct, 1)
        if rise_pct <= LOW_RISE_MAX:
            score += 1
            reasons.append(f"距低點 +{rise_pct:.0f}% ✓")
        else:
            reasons.append(f"距低點 +{rise_pct:.0f}% ✗")
    else:
        reasons.append("無低點資料 —")

    # 指標3：本益比
    if per:
        latest_per = per[-1]
        per_val = fv(latest_per.get("PER"), latest_per.get("per"))
        metrics["per"] = round(per_val, 2) if per_val else None
        if per_val > 0:
            if per_val < PER_MAX:
                score += 1
                reasons.append(f"PER {per_val:.1f} ✓")
            else:
                reasons.append(f"PER {per_val:.1f} 過高 ✗")
        else:
            reasons.append("PER 異常或虧損 —")
    else:
        reasons.append("無 PER 資料 —")

    return score, reasons, metrics


# ═══════════════════════════════════════════════════
#  期望值與持倉
# ═══════════════════════════════════════════════════

def calc_ev(cur, target_price):
    """以「合理價推估」取代「承銷價 × 倍數」"""
    target = round(target_price, 2)
    stop   = round(cur * (1 - STOP_PCT), 2)
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
    print(f"  興櫃篩選引擎 v8（三指標替代承銷價）")
    print(f"  {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Token: {'✓ 已設定' if FINMIND_TOKEN else '✗ 未設定（限流 600/hr）'}")
    print(f"{'═'*62}\n", flush=True)

    stocks = get_emerging_stocks()
    if not stocks:
        print("❌ 無法取得興櫃清單"); sys.exit(1)

    print(f"\n開始篩選 {len(stocks)} 支興櫃股票\n{'─'*62}", flush=True)

    candidates  = []
    early_drops = []

    for i, s in enumerate(stocks):
        sid, sname = s["sid"], s["sname"]
        bar = f"[{i+1:3d}/{len(stocks)}]"

        # 行情（拉 200 天用於計算 6 月高低）
        history = get_history(sid, days=200)
        time.sleep(REQUEST_DELAY)
        if not history or len(history) < 10:
            continue

        cur = fv(history[-1].get("close"))
        if cur < MIN_PRICE:
            continue

        # 流動性
        vols = [fv(d.get("Trading_Volume")) for d in history[-10:]]
        avg_vol = sum(vols) / max(1, len(vols))
        if avg_vol < MIN_AVG_VOLUME * 1000:
            continue

        # 法人
        inst = get_inst(sid)
        time.sleep(REQUEST_DELAY)

        # PER
        per = get_per(sid)
        time.sleep(REQUEST_DELAY)

        # 評分
        chip_score, chip_reasons = score_chip(history, inst)
        chip_ok = chip_score >= CHIP_SCORE_MIN

        cheap_score, cheap_reasons, cheap_metrics = score_cheap(history, per)
        cheap_ok = cheap_score >= CHEAP_SCORE_MIN

        # 法人淨買超
        inst_net = sum(fv(d.get("buy")) - fv(d.get("sell")) for d in inst) if inst else 0

        # 期望值：目標價用「6個月高點」當合理價推估
        target_price = cheap_metrics.get("high6m", cur * GAIN_MULTIPLIER) or cur * GAIN_MULTIPLIER
        ev, rr, target, stop = calc_ev(cur, target_price)

        rec = dict(
            sid=sid, sname=sname,
            curPrice=round(cur, 2),
            avgVolume=round(avg_vol / 1000, 1),
            instNet=round(inst_net / 1000, 1),
            # 籌碼
            chipScore=chip_score, chipOk=chip_ok, chipReasons=chip_reasons,
            # 便宜（三指標）
            cheapScore=cheap_score, cheapOk=cheap_ok, cheapReasons=cheap_reasons,
            high6m=cheap_metrics.get("high6m"),
            low3m=cheap_metrics.get("low3m"),
            dropPct=cheap_metrics.get("dropPct"),
            risePct=cheap_metrics.get("risePct"),
            per=cheap_metrics.get("per"),
            # 股東（手動）
            top10Pct=None, holderOk=False,
            # 期望值
            ev=ev, rr=rr, target=target, stop=stop,
            evPass=ev > 0,
            status="fail",
        )

        # 兩關全通過(籌碼 + 便宜) → 進候選
        if chip_ok and cheap_ok:
            candidates.append(rec)
            rec["status"] = "candidate"
            print(f"{bar} {sid:<6s} {sname[:8]:<9s} 市價={cur:.0f} "
                  f"距高={cheap_metrics.get('dropPct','—')}% 卷起=+{cheap_metrics.get('risePct','—')}% "
                  f"PER={cheap_metrics.get('per','—')} 籌碼={chip_score}/3 → ✅ 候選",
                  flush=True)
        else:
            early_drops.append(rec)

    elapsed = round((datetime.now() - t0).total_seconds())
    print(f"\n{'─'*62}")
    print(f"第一階段完成：{len(candidates)} 支候選，{len(early_drops)} 支淘汰")
    print(f"耗時 {elapsed}s\n", flush=True)

    output = dict(
        generatedAt   = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        elapsedSec    = elapsed,
        totalScreened = len(candidates) + len(early_drops),
        totalCandidates = len(candidates),
        totalPassed   = 0,
        params = dict(
            pWin=P_WIN, gainMult=GAIN_MULTIPLIER, stopPct=STOP_PCT,
            mdd=MDD, capital=TOTAL_CAPITAL, maxPosPct=MAX_POS_PCT,
            highDropMin=HIGH_DROP_MIN, lowRiseMax=LOW_RISE_MAX, perMax=PER_MAX,
            cheapScoreMin=CHEAP_SCORE_MIN, chipScoreMin=CHIP_SCORE_MIN,
            top10MinPct=TOP10_MIN_PCT,
            minAvgVolume=MIN_AVG_VOLUME, minPrice=MIN_PRICE,
        ),
        candidates = sorted(candidates,  key=lambda x: x["ev"], reverse=True),
        all        = sorted(candidates + early_drops, key=lambda x: x["chipScore"], reverse=True),
    )

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"結果 → {OUTPUT_FILE}")
    print(f"下一步：到 Dashboard 對 {len(candidates)} 支候選股手動填股東集中度")
    print(f"{'═'*62}\n", flush=True)


if __name__ == "__main__":
    run()
