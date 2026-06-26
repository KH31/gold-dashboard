#!/usr/bin/env python3
"""
黄金量化看板系统 v2.0
三层架构：价格信号 + 资金流 + 长期驱动
"""

import datetime
import json
import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from fredapi import Fred
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv(Path(__file__).parent / ".env")

# ── 配置 ──
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", Path(__file__).parent))
VAULT_DAILY_DIR = Path(os.environ.get("VAULT_DAILY_DIR", OUTPUT_DIR.parent.parent / "daily"))
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
CACHE_FILE = OUTPUT_DIR / ".gold_cache.json"
MA = 20
LOOKBACK = 120

TICKERS = {"DXY": "DX-Y.NYB", "Gold": "GC=F", "Silver": "SI=F", "Copper": "HG=F"}
HISTORY_YEARS = 10  # 用于分位计算的历史年数

# ═══════════════ 工具函数 ═══════════════

def load_cache():
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}

def save_cache(data):
    try:
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, default=str))
    except Exception:
        pass

def ma_trend(series):
    if series is None or len(series) < MA:
        return None, None, "数据不足", "数据不足"
    latest = round(float(series.iloc[-1]), 4)
    ma_val = round(float(series.iloc[-MA:].mean()), 4)
    pos = "高于MA20" if latest > ma_val else "低于MA20"
    slope = "↑" if series.iloc[-10:].mean() > series.iloc[-20:-10].mean() else "↓"
    return latest, ma_val, pos, slope

def compute_percentiles():
    """下载 10 年历史数据，计算 DXY/金银比/铜金比的 3Y/5Y/10Y 分位"""
    try:
        data = yf.download(["GC=F", "SI=F", "HG=F", "DX-Y.NYB"], period=f"{HISTORY_YEARS}y", progress=False)
        if data is None or data.empty:
            return {}

        gold = data["Close"]["GC=F"].dropna()
        silver = data["Close"]["SI=F"].dropna()
        copper = data["Close"]["HG=F"].dropna()
        dxy = data["Close"]["DX-Y.NYB"].dropna()

        gs = (gold / silver).dropna()
        cg = (copper / gold).dropna()

        result = {}
        today = pd.Timestamp.now()
        for name, series, fmt in [("DXY", dxy, ".2f"), ("GS", gs, ".4f"), ("CG", cg, ".4f")]:
            current = float(series.iloc[-1])
            low = float(series.min())
            high = float(series.max())
            result[name] = {"current": current, "low": low, "high": high, "fmt": fmt}
            for yrs, label in [(3, "3Y"), (5, "5Y"), (10, "10Y")]:
                cutoff = today - pd.DateOffset(years=yrs)
                sub = series[series.index >= cutoff]
                if len(sub) > 100:
                    pct = int((sub < current).sum() / len(sub) * 100)
                else:
                    pct = None
                result[name][label] = pct
                result[name][f"{label}_bar"] = percentile_bar(pct)
        return result
    except Exception as e:
        print(f"[WARN] 历史分位计算失败: {e}")
        return {}

def percentile_bar(pct):
    """生成 ASCII 分位条"""
    if pct is None:
        return "░░░░░░░░░░░░░░░░░░░░"
    return "█" * (pct // 5) + "░" * (20 - pct // 5)
    return bar

def ratio_trend(a, b):
    if a is None or b is None or len(a) < MA or len(b) < MA:
        return None, "数据不足", "数据不足"
    common = a.index.intersection(b.index)
    if len(common) < MA:
        return None, "数据不足", "数据不足"
    ratio = a[common] / b[common]
    return round(float(ratio.iloc[-1]), 4), "↑" if ratio.iloc[-1] > ratio.iloc[-MA:].mean() else "↓", None

def trend_direction(series):
    """返回 ↑ ↓ →"""
    if series is None or len(series) < 5:
        return "→", "数据不足"
    short = series.iloc[-5:].mean()
    long = series.iloc[-min(20, len(series)):].mean()
    if short > long * 1.005:
        return "↑", "机构增持"
    elif short < long * 0.995:
        return "↓", "机构减持"
    return "→", "持仓持平"

# ═══════════════ 第一层：价格信号 ═══════════════

def fetch_yahoo():
    """抓取 DXY/金银铜"""
    cache = load_cache()
    closes = {}
    for name, ticker in TICKERS.items():
        ok = False
        for _ in range(2):
            try:
                t = yf.Ticker(ticker)
                h = t.history(period=f"{LOOKBACK + 30}d", auto_adjust=True)
                if h is not None and not h.empty and "Close" in h.columns:
                    closes[name] = h["Close"].dropna()
                    ok = True
                    break
            except Exception:
                pass
            import time; time.sleep(3)
        if not ok and name in cache:
            r = cache.get(name, {})
            if isinstance(r, dict) and "series" in r:
                closes[name] = pd.Series({pd.Timestamp(k): v for k, v in r["series"].items()})
        if name not in closes:
            closes[name] = pd.Series(dtype=float)
    return closes

def fetch_real_yield():
    if not FRED_API_KEY:
        return pd.Series(dtype=float)
    try:
        fred = Fred(api_key=FRED_API_KEY)
        s = fred.get_series("DFII10")
        s.index = pd.to_datetime(s.index)
        return s.dropna()
    except Exception as e:
        print(f"[WARN] FRED: {e}")
        return pd.Series(dtype=float)

# ═══════════════ 第二层：资金流 ═══════════════

def fetch_gld_holdings():
    """抓取 GLD ETF 持仓（吨）"""
    try:
        # SPDR Gold Shares 官方 CSV
        url = "https://www.spdrgoldshares.com/assets/dynamic/holdings/GLD-Primary-Holdings.csv"
        df = pd.read_csv(url, skiprows=2, names=["Date","GLD_Oz","GLD_Tons"], usecols=[0,2])
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        s = df["GLD_Tons"]
        # 移除逗号并转为float
        s = pd.to_numeric(s.astype(str).str.replace(",", ""), errors="coerce").dropna()
        return s
    except Exception as e:
        print(f"[WARN] GLD Holdings: {e}")
        return pd.Series(dtype=float)

def fetch_cot_report():
    """抓取 CFTC COT 黄金 Managed Money 净多头"""
    import urllib.request
    try:
        url = "https://www.cftc.gov/dea/newcot/c_disagg.txt"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as f:
            for line in f.readlines():
                line = line.decode("utf-8", errors="ignore")
                if "GOLD - COMMODITY EXCHANGE INC." in line:
                    cols = line.strip().split(",")
                    if len(cols) >= 14:
                        mgd_long = int(cols[12].strip().replace('"',''))
                        mgd_short = int(cols[13].strip().replace('"',''))
                        net = mgd_long - mgd_short
                        return net, mgd_long
        return None, None
    except Exception as e:
        print(f"[WARN] COT: {e}")
        return None, None

# ═══════════════ 第三层：长期驱动 ═══════════════

PBOC_DATA = {
    # 中国央行黄金储备（万盎司）——公开数据，每月更新
    "2026-05": 7370,
    "2026-04": 7370,
    "2026-03": 7370,
    "2026-02": 7350,
    "2026-01": 7350,
    "2025-12": 7320,
    "2025-11": 7296,
    "2025-10": 7296,
    "2025-09": 7280,
    "2025-08": 7280,
    "2025-07": 7264,
    "2025-06": 7264,
}

WGC_DATA = {
    # 全球央行净购金（吨）——World Gold Council 季度数据
    "2026-Q1": 188,
    "2025-Q4": 253,
    "2025-Q3": 218,
    "2025-Q2": 183,
    "2025-Q1": 290,
    "2024-Q4": 229,
}

# ═══════════════ 评分系统 ═══════════════

class Scorer:
    def __init__(self):
        self.scores = {}
        self.weight = {}

    def add(self, name, score, w):
        self.scores[name] = score
        self.weight[name] = w

    def total(self, weighted=True):
        if weighted:
            return sum(self.scores[k] * self.weight[k] for k in self.scores)
        return sum(self.scores.values())

    def verdict(self):
        t = self.total()
        if t >= 6:
            return "🟢 强烈利多 — 多层信号共振看多"
        elif t >= 3:
            return "🟢 偏多 — 多数信号指向利多"
        elif t >= -2:
            return "🟡 中性 — 信号分歧，观望为主"
        elif t >= -5:
            return "🔴 偏空 — 多数信号指向利空"
        return "🔴 强烈利空 — 多层信号共振看空"

# ═══════════════ 生成看板 ═══════════════

def generate_report(scorer, price_data, flow_data, driver_data):
    s = scorer
    t = s.total()
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

    def fmt(v):
        return str(v) if v is not None else "—"

    md = f"""# 黄金看板

> 自动生成 · {now}

---

## 第一层：价格信号

| 指标 | 现值 | MA20 | 方向 | 得分 | 权重 |
|------|------|------|------|------|------|
| DXY | {fmt(price_data.get('dxy_val'))} | {fmt(price_data.get('dxy_ma'))} | {price_data.get('dxy_pos','—')} {price_data.get('dxy_slope','')} | {_ps('DXY'):+d} | ★★★★ |
| 10Y实际利率 | {fmt(price_data.get('real_val'))}% | {fmt(price_data.get('real_ma'))}% | {price_data.get('real_pos','—')} {price_data.get('real_slope','')} | {_ps('Real_Yield'):+d} | ★★★★ |
| 金银比 | {fmt(price_data.get('gs_ratio'))} | | {price_data.get('gs_dir','—')} | {_ps('Gold_Silver'):+d} | ★★★ |
| 铜金比 | {fmt(price_data.get('cg_ratio'))} | | {price_data.get('cg_dir','—')} | {_ps('Copper_Gold'):+d} | ★★★ |
| **价格层小计** | | | | **{_psl():+d}** | |

## 第二层：资金流

| 指标 | 现值 | 趋势 | 信号 | 得分 | 权重 |
|------|------|------|------|------|------|
| GLD持仓(吨) | {fmt(flow_data.get('gld_tons'))} | {flow_data.get('gld_trend','→')} | {flow_data.get('gld_signal','数据不足')} | {_ps('GLD'):+d} | ★★★★ |
| COMEX净多头 | {fmt(flow_data.get('cot_net_long'))} | {flow_data.get('cot_trend','→')} | {flow_data.get('cot_signal','数据不足')} | {_ps('COT'):+d} | ★★★ |
| **资金流小计** | | | | **{_psf():+d}** | |

## 第三层：长期驱动

| 指标 | 现值 | 上月 | 趋势 | 得分 | 权重 |
|------|------|------|------|------|------|
| 中国央行黄金(万盎司) | {fmt(driver_data.get('pboc_now'))} | {fmt(driver_data.get('pboc_prev'))} | {driver_data.get('pboc_signal','数据不足')} | {_ps('PBOC'):+d} | ★★★ |
| 全球央行购金(吨/Q) | {fmt(driver_data.get('wgc_now'))} | — | {driver_data.get('wgc_signal','数据不足')} | {_ps('WGC'):+d} | ★★★ |
| **长期层小计** | | | | **{_psd():+d}** | |

---

## 综合评分

```
第一层（价格信号）：  {_psl():+d}
第二层（资金流）：    {_psf():+d}
第三层（长期驱动）：  {_psd():+d}
                    ─────
加权总分：           {t:+d}
```

## 交易结论

**{s.verdict()}**

---
> 数据源：Yahoo Finance · FRED (DFII10) · SPDR Gold Shares · CFTC COT · 中国外管局 · World Gold Council
> 自动生成 · 非投资建议
"""
    # Python f-string can't use functions directly in braces easily, so use template
    return md.replace("{_ps('DXY')", "")\
              .replace("{_ps('Real_Yield')", "")\
              .replace("{_ps('Gold_Silver')", "")\
              .replace("{_ps('Copper_Gold')", "")\
              .replace("{_ps('GLD')", "")\
              .replace("{_ps('COT')", "")\
              .replace("{_ps('PBOC')", "")\
              .replace("{_ps('WGC')", "")\
              .replace("{_psl()", str(scorer.scores.get('DXY', 0) + scorer.scores.get('Real_Yield', 0) + scorer.scores.get('Gold_Silver', 0) + scorer.scores.get('Copper_Gold', 0)) + "")\
              .replace("{_psf()", str(scorer.scores.get('GLD', 0) + scorer.scores.get('COT', 0)) + "")\
              .replace("{_psd()", str(scorer.scores.get('PBOC', 0) + scorer.scores.get('WGC', 0)) + "")

# Helper: generate report_v2
def _make_report(scorer, price_data, flow_data, driver_data):
    # Simplified version using string replacement
    s = scorer
    t_val = s.total()

    # Compute sub-scores (WEIGHTED)
    p_sub = sum(s.scores.get(k, 0) * s.weight.get(k, 1) for k in ['DXY','Real_Yield','Gold_Silver','Copper_Gold'])
    f_sub = sum(s.scores.get(k, 0) * s.weight.get(k, 1) for k in ['GLD','COT'])
    d_sub = sum(s.scores.get(k, 0) * s.weight.get(k, 1) for k in ['PBOC','WGC'])

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    v = s.verdict()

    def fmt(x):
        return str(x) if x is not None else "—"

    dxy_s = price_data.get('dxy_val') or "—"
    dxy_m = price_data.get('dxy_ma') or "—"
    dxy_p = price_data.get('dxy_pos') or "—"
    dxy_sl = price_data.get('dxy_slope') or ""
    rv = price_data.get('real_val') or "—"
    rm = price_data.get('real_ma') or "—"
    rp = price_data.get('real_pos') or "—"
    rs = price_data.get('real_slope') or ""
    gsr = price_data.get('gs_ratio') or "—"
    gsd = price_data.get('gs_dir') or "—"
    gsz = price_data.get('gs_zone') or "—"
    cgr = price_data.get('cg_ratio') or "—"
    cgd = price_data.get('cg_dir') or "—"
    cgz = price_data.get('cg_zone') or "—"
    # 铜金比放大显示
    cgr_txt = f"{cgr}" if cgr == "—" else f"{cgr} (×10000={round(float(cgr)*10000,1)})"

    # 历史分位（从 pct_data dict 提取）
    pct_data = price_data.get('pct_data') or {}
    def _p(name, yr):
        d = pct_data.get(name, {})
        return d.get(yr)
    def _pb(name, yr):
        d = pct_data.get(name, {})
        return d.get(f"{yr}_bar", "░░░░░░░░░░░░░░░░░░░░")
    def _v(name, key):
        d = pct_data.get(name, {})
        val = d.get(key)
        if val is None:
            return "—"
        fmt = d.get("fmt", ".2f")
        return f"{val:{fmt}}"

    # 金属建议——基于10年分位
    metal_advice = ""
    metal_score = 0
    gs10 = _p("GS", "10Y")
    cg10 = _p("CG", "10Y")

    # 预计算报告变量（f-string不能用反斜杠和复杂函数调用）
    dxy_3y, dxy_5y, dxy_10y = _p("DXY","3Y"), _p("DXY","5Y"), _p("DXY","10Y")
    dxy_3b, dxy_5b, dxy_10b = _pb("DXY","3Y"), _pb("DXY","5Y"), _pb("DXY","10Y")
    dxy_lo, dxy_hi = _v("DXY","low"), _v("DXY","high")
    gs_3y, gs_5y, gs_10y = _p("GS","3Y"), _p("GS","5Y"), _p("GS","10Y")
    gs_3b, gs_5b, gs_10b = _pb("GS","3Y"), _pb("GS","5Y"), _pb("GS","10Y")
    gs_lo, gs_hi = _v("GS","low"), _v("GS","high")
    cg_3y, cg_5y, cg_10y = _p("CG","3Y"), _p("CG","5Y"), _p("CG","10Y")
    cg_3b, cg_5b, cg_10b = _pb("CG","3Y"), _pb("CG","5Y"), _pb("CG","10Y")
    cg_lo, cg_hi = _v("CG","low"), _v("CG","high")
    ry_3y, ry_5y, ry_10y = _p("RY","3Y"), _p("RY","5Y"), _p("RY","10Y")
    ry_3b, ry_5b, ry_10b = _pb("RY","3Y"), _pb("RY","5Y"), _pb("RY","10Y")
    ry_lo = str(pct_data.get("RY",{}).get("low","—")) if pct_data.get("RY",{}).get("low") is not None else "—"
    ry_hi = str(pct_data.get("RY",{}).get("high","—")) if pct_data.get("RY",{}).get("high") is not None else "—"
    if gs10 is not None and cg10 is not None:
        if gs10 > 80:
            metal_advice += "🥈 金银比10年分位极高→**白银**被严重低估。"
            metal_score += 2
        elif gs10 > 60:
            metal_advice += "🥈 金银比10年分位偏高→**白银**相对便宜。"
            metal_score += 1
        elif gs10 < 20:
            metal_advice += "🥇 金银比10年分位极低→**黄金**被低估。"
            metal_score -= 2
        elif gs10 < 40:
            metal_advice += "🥇 金银比10年分位偏低→**黄金**相对便宜。"
            metal_score -= 1
        else:
            metal_advice += "⚖️ 金银比分位中性→金银均可。"
            metal_score += 0

        if cg10 < 15:
            metal_advice += "\n🟤 铜金比10年分位极低→**铜**被严重低估。"
            metal_score += 1
        elif cg10 < 30:
            metal_advice += "\n🟤 铜金比10年分位偏低→**铜**相对便宜。"
            metal_score += 0
        elif cg_pct_int > 80:
            metal_advice += "\n🟤 铜金比分位极高→铜偏贵，暂避。"
            metal_score -= 1
        else:
            metal_advice += "\n⚖️ 铜金比分位中性→铜价合理。"
            metal_score += 0

        # 综合结论
        if metal_score >= 3:
            metal_verdict = "🥈+🟤 **强烈建议买白银+铜，暂避黄金**"
        elif metal_score >= 1:
            metal_verdict = "🥈 **偏向白银**（分位比价支持）"
        elif metal_score >= -1:
            metal_verdict = "⚖️ **三者无明显偏向**，以黄金为主"
        elif metal_score >= -3:
            metal_verdict = "🥇 **偏向黄金**（分位比价支持）"
        else:
            metal_verdict = "🥇 **强烈建议买黄金，暂避其他**"
        # 融合全看板评分
        if t_val <= -10:
            metal_verdict += f" | 看板加权总分 {t_val:+d}，整体偏空，仓位控制为优先。"
        elif t_val >= 3:
            metal_verdict += f" | 看板加权总分 {t_val:+d}，多指标共振，可适度加仓。"
    else:
        metal_advice = "分位数据不足，无法判断。"
        metal_verdict = "⚪ 待数据更新"

    gld_t = flow_data.get('gld_tons') or "—"
    gld_tr = flow_data.get('gld_trend') or "→"
    gld_sig = flow_data.get('gld_signal') or "数据不足"
    cot_nl = flow_data.get('cot_net_long') or "—"
    cot_tr = flow_data.get('cot_trend') or "→"
    cot_sig = flow_data.get('cot_signal') or "数据不足"

    pboc_n = driver_data.get('pboc_now') or "—"
    pboc_p = driver_data.get('pboc_prev') or "—"
    pboc_s = driver_data.get('pboc_signal') or "数据不足"
    wgc_n = driver_data.get('wgc_now') or "—"
    wgc_s = driver_data.get('wgc_signal') or "数据不足"

    # 黄金现价 & 业绩（从 price_data 获取）
    gold_spot = price_data.get('gold_spot', '—')
    gold_ytd = price_data.get('gold_ytd', '—')
    gold_1y = price_data.get('gold_1y', '—')

    return f"""# 黄金看板

> 自动生成 · {now}

| 🥇 现货 | 📅 YTD | 📆 1年 |
|------|------|------|
| {gold_spot} | {gold_ytd} | {gold_1y} |

---

## 交易结论

**{v}**

### 综合评分

```
第一层（价格信号）：  {p_sub:+d}
第二层（资金流）：    {f_sub:+d}
第三层（长期驱动）：  {d_sub:+d}
                    ─────
加权总分：           {t_val:+d}
```

### 买金·买银·买铜

{metal_advice}

> **综合建议**：{metal_verdict}

---

## 第一层：价格信号

| 指标 | 现值 | MA20 | 方向 | 得分 | 权重 |
|------|------|------|------|------|------|
| DXY | {dxy_s} | {dxy_m} | {dxy_p} {dxy_sl} | {s.scores.get('DXY',0):+d} | ★★★★ |
| | 分位: 3Y {dxy_3y}% {dxy_3b} 5Y {dxy_5y}% {dxy_5b} 10Y {dxy_10y}% {dxy_10b} | 低 {dxy_lo} | 高 {dxy_hi} | | |
| 10Y实际利率 | {rv}% | {rm}% | {rp} {rs} | {s.scores.get('Real_Yield',0):+d} | ★★★★ |
| | 分位: 3Y {ry_3y}% {ry_3b} 5Y {ry_5y}% {ry_5b} 10Y {ry_10y}% {ry_10b} | 低 {ry_lo} | 高 {ry_hi} | | |
| 金银比 | {gsr} | {gsz} | {gsd} | {s.scores.get('Gold_Silver',0):+d} | ★★★ |
| | 分位: 3Y {gs_3y}% {gs_3b} 5Y {gs_5y}% {gs_5b} 10Y {gs_10y}% {gs_10b} | 低 {gs_lo} | 高 {gs_hi} | | |
| 铜金比 | {cgr_txt} | {cgz} | {cgd} | {s.scores.get('Copper_Gold',0):+d} | ★★★ |
| | 分位: 3Y {cg_3y}% {cg_3b} 5Y {cg_5y}% {cg_5b} 10Y {cg_10y}% {cg_10b} | 低 {cg_lo} | 高 {cg_hi} | | |
| **价格层小计** | | | | **{p_sub:+d}** | |

## 第二层：资金流

| 指标 | 现值 | 趋势 | 信号 | 得分 | 权重 |
|------|------|------|------|------|------|
| GLD持仓(吨) | {gld_t} | {gld_tr} | {gld_sig} | {s.scores.get('GLD',0):+d} | ★★★★ |
| COMEX净多头 | {cot_nl} | {cot_tr} | {cot_sig} | {s.scores.get('COT',0):+d} | ★★★ |
| **资金流小计** | | | | **{f_sub:+d}** | |

## 第三层：长期驱动

| 指标 | 现值 | 上月 | 趋势 | 得分 | 权重 |
|------|------|------|------|------|------|
| 中国央行黄金(万盎司) | {pboc_n} | {pboc_p} | {pboc_s} | {s.scores.get('PBOC',0):+d} | ★★★ |
| 全球央行购金(吨/Q) | {wgc_n} | — | {wgc_s} | {s.scores.get('WGC',0):+d} | ★★★ |
| **长期层小计** | | | | **{d_sub:+d}** | |

---

### 比值入场阈值

| 比值 | 区间 | 得分 | 含义 | 当前 |
|------|------|------|------|------|
| **金银比** | > 85 | +3 | 🔴 极端恐惧 | |
| | 75-85 | +1 | 🟡 高恐惧 | |
| | **45-75** | **0** | 🟡 中性 | ← **{gsr}** |
| | 30-45 | -1 | 🟢 低恐惧 | |
| | < 30 | -2 | 🟢 贪婪 | |
| **铜金比** (×10000) | > 25 | +1 | 🟢 增长区 | |
| | 15-25 | 0 | 🟡 中性 | |
| | **10-15** | **-1** | 🔴 衰退恐惧 | ← **{cgr_txt}** |
| | < 10 | -2 | 🔴 严重收缩 | |

> **入场规则**：金银比 > 85 且铜金比(×10000) < 10 同时触发 → 逆向买入黄金胜率最高（金银比极端=恐惧到顶+铜金比极端=衰退到底）。历史上 2008.10、2015.12、2020.3 三次触发后黄金涨 30%+。

---
> 自动生成 · 非投资建议

### 数据源链接

| 指标 | 来源 | 验证链接 |
|------|------|------|
| DXY | Yahoo Finance | [DX-Y.NYB](https://finance.yahoo.com/quote/DX-Y.NYB/) |
| 10Y实际利率 | FRED DFII10 | [DFII10](https://fred.stlouisfed.org/series/DFII10) |
| 金银比 | Yahoo GC=F / SI=F | [GC=F](https://finance.yahoo.com/quote/GC=F/) |
| 铜金比 | Yahoo HG=F / GC=F | [HG=F](https://finance.yahoo.com/quote/HG=F/) |
| GLD持仓 | SPDR Gold Shares | [GLD Holdings](https://www.spdrgoldshares.com/) |
| COMEX净多头 | CFTC COT | [COT Report](https://www.cftc.gov/dea/newcot/c_disagg.txt) |
| 中国央行黄金 | 外管局 | [SAFE](http://m.safe.gov.cn/) |
| 全球央行购金 | World Gold Council | [WGC GoldHub](https://www.gold.org/goldhub/data/gold-demand-trends) |
| 黄金现价/图表 | GoldPrice.org | [GoldPrice](https://goldprice.org/) |
| 技术图表 | TradingView | [TradingView Gold](https://www.tradingview.com/symbols/COMEX-GC1%21/) |
"""


# ═══════════════ 主流程 ═══════════════

def main():
    scorer = Scorer()

    print("[1/5] 第一层：价格信号...")
    closes = fetch_yahoo()
    real_yield = fetch_real_yield()
    print("[*] 计算历史分位...")
    pct_data = compute_percentiles()  # dict with DXY/GS/CG → {3Y,5Y,10Y}_pct, low, high

    # 实际利率 3/5/10Y 分位（FRED 数据单独计算）
    ry_pct = {}
    if len(real_yield) > 500:
        today = pd.Timestamp.now()
        current_ry = float(real_yield.iloc[-1])
        for yrs, label in [(3,"3Y"),(5,"5Y"),(10,"10Y")]:
            cutoff = today - pd.DateOffset(years=yrs)
            sub = real_yield[real_yield.index >= cutoff]
            if len(sub) > 100:
                ry_pct[label] = int((sub < current_ry).sum() / len(sub) * 100)
                ry_pct[f"{label}_bar"] = percentile_bar(ry_pct[label])
            else:
                ry_pct[label] = None
                ry_pct[f"{label}_bar"] = "░░░░░░░░░░░░░░░░░░░░"
    else:
        for label in ["3Y","5Y","10Y"]:
            ry_pct[label] = None
            ry_pct[f"{label}_bar"] = "░░░░░░░░░░░░░░░░░░░░"
    ry_pct["low"] = round(float(real_yield.min()), 2) if len(real_yield) > 0 else None
    ry_pct["high"] = round(float(real_yield.max()), 2) if len(real_yield) > 0 else None
    pct_data["RY"] = ry_pct

    dxy_val, dxy_ma, dxy_pos, dxy_slope = ma_trend(closes.get("DXY"))
    real_val, real_ma, real_pos, real_slope = ma_trend(real_yield)
    gs_ratio, gs_dir, _ = ratio_trend(closes.get("Gold"), closes.get("Silver"))
    cg_ratio, cg_dir, _ = ratio_trend(closes.get("Copper"), closes.get("Gold"))

    scorer.add("DXY", +2 if dxy_pos == "低于MA20" else -2, 4)
    scorer.add("Real_Yield", +2 if real_pos == "低于MA20" else -2, 4)
    # 金银比 —— 带阈值评分
    gs_score = 0
    gs_zone = ""
    if gs_ratio is not None and isinstance(gs_ratio, (int, float)):
        if gs_ratio > 85:
            gs_score, gs_zone = +3, "极端恐惧区"
        elif gs_ratio > 75:
            gs_score, gs_zone = +1, "高恐惧区"
        elif gs_ratio > 45:
            gs_score, gs_zone = 0, "中性区"
        elif gs_ratio > 30:
            gs_score, gs_zone = -1, "低恐惧区"
        else:
            gs_score, gs_zone = -2, "贪婪区"
    else:
        gs_score = +1 if gs_dir == "↓" else -1
        gs_zone = "趋势: " + str(gs_dir)
    scorer.add("Gold_Silver", gs_score, 3)

    # 铜金比 —— 带阈值评分
    cg_score = 0
    cg_zone = ""
    if cg_ratio is not None and isinstance(cg_ratio, (int, float)):
        cg_scaled = cg_ratio * 10000  # 放大到可读范围
        if cg_scaled > 25:
            cg_score, cg_zone = +1, "增长区"
        elif cg_scaled > 15:
            cg_score, cg_zone = 0, "中性区"
        elif cg_scaled > 10:
            cg_score, cg_zone = -1, "衰退恐惧区"
        else:
            cg_score, cg_zone = -2, "严重收缩区"
    else:
        cg_score = +1 if cg_dir == "↑" else -1
        cg_zone = "趋势: " + str(cg_dir)
    scorer.add("Copper_Gold", cg_score, 3)

    # 黄金现价 & 业绩（复用分位数据里已下载的黄金序列 / falls back to closes）
    gold_spot, gold_ytd, gold_1y = "—", "—", "—"
    gc_for_spot = closes.get("Gold", pd.Series()).dropna()
    if len(gc_for_spot) < 100:
        # 分位计算里已下载 10 年数据——直接复用
        try:
            data = yf.download("GC=F", period="2y", progress=False)
            if not data.empty:
                gc_for_spot = data["Close"].dropna()
        except:
            pass
    if len(gc_for_spot) > 100:
        gold_spot = f"${gc_for_spot.iloc[-1]:.0f}"
        try:
            ytd_open = gc_for_spot[gc_for_spot.index >= str(gc_for_spot.index[-1].year)]
            gold_ytd = f"{((gc_for_spot.iloc[-1] / ytd_open.iloc[0]) - 1) * 100:+.1f}%" if len(ytd_open) > 0 else "—"
            yr_ago = gc_for_spot.index[-1] - pd.DateOffset(years=1)
            yr_data = gc_for_spot[gc_for_spot.index >= yr_ago]
            gold_1y = f"{((gc_for_spot.iloc[-1] / yr_data.iloc[0]) - 1) * 100:+.1f}%" if len(yr_data) > 0 else "—"
        except:
            pass

    price_data = {
        "dxy_val": dxy_val, "dxy_ma": dxy_ma, "dxy_pos": dxy_pos, "dxy_slope": dxy_slope,
        "real_val": real_val, "real_ma": real_ma, "real_pos": real_pos, "real_slope": real_slope,
        "gs_ratio": gs_ratio, "gs_dir": gs_dir, "gs_zone": gs_zone,
        "cg_ratio": cg_ratio, "cg_dir": cg_dir, "cg_zone": cg_zone,
        "pct_data": pct_data,
        "gold_spot": gold_spot, "gold_ytd": gold_ytd, "gold_1y": gold_1y,
    }

    print("[2/5] 第二层：资金流...")
    # GLD holdings
    gld = fetch_gld_holdings()
    gld_tons_now = None
    gld_trend, gld_signal = "→", "数据不足"
    if len(gld) >= 5:
        gld_tons_now = round(float(gld.iloc[-1]), 1)
        gld_trend, gld_signal = trend_direction(gld)
        if gld_trend == "↑":
            scorer.add("GLD", +1, 4)
        elif gld_trend == "↓":
            scorer.add("GLD", -1, 4)
        else:
            scorer.add("GLD", 0, 4)
    else:
        scorer.add("GLD", 0, 4)

    # COT — CFTC Managed Money
    cot_net, cot_long = fetch_cot_report()
    cot_net_str = "需手动查看CFTC"
    cot_trend, cot_signal = "→", "数据不足"
    if cot_net is not None:
        cot_net_str = f"{cot_net:+,}"
        if cot_net > 0:
            cot_trend, cot_signal = "↑", "净多头—利多"
            scorer.add("COT", +1, 3)
        else:
            cot_trend, cot_signal = "↓", "净空头—利空"
            scorer.add("COT", -1, 3)
    else:
        scorer.add("COT", 0, 3)

    flow_data = {
        "gld_tons": gld_tons_now, "gld_trend": gld_trend, "gld_signal": gld_signal,
        "cot_net_long": cot_net_str, "cot_trend": cot_trend, "cot_signal": cot_signal,
    }

    print("[3/5] 第三层：长期驱动...")
    # PBOC
    pboc_keys = sorted(PBOC_DATA.keys(), reverse=True)
    pboc_now = PBOC_DATA.get(pboc_keys[0]) if pboc_keys else None
    pboc_prev = PBOC_DATA.get(pboc_keys[1]) if len(pboc_keys) > 1 else None
    if pboc_now and pboc_prev:
        if pboc_now > pboc_prev:
            pboc_signal = "增持—利多"
            scorer.add("PBOC", +2, 3)
        elif pboc_now < pboc_prev:
            pboc_signal = "减持—利空"
            scorer.add("PBOC", -2, 3)
        else:
            pboc_signal = "持平"
            scorer.add("PBOC", 0, 3)
    else:
        pboc_signal = "数据不足"
        scorer.add("PBOC", 0, 3)

    # WGC
    wgc_keys = sorted(WGC_DATA.keys(), reverse=True)
    wgc_now = WGC_DATA.get(wgc_keys[0]) if wgc_keys else None
    if wgc_now and wgc_now > 200:
        wgc_signal = f"{wgc_now}吨—高于平均"
        scorer.add("WGC", +1, 3)
    elif wgc_now:
        wgc_signal = f"{wgc_now}吨—低于平均"
        scorer.add("WGC", 0, 3)
    else:
        wgc_signal = "数据不足"
        scorer.add("WGC", 0, 3)

    driver_data = {
        "pboc_now": pboc_now, "pboc_prev": pboc_prev, "pboc_signal": pboc_signal,
        "wgc_now": wgc_now, "wgc_signal": wgc_signal,
    }

    print("[4/5] 生成报告...")
    report = _make_report(scorer, price_data, flow_data, driver_data)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "黄金看板.md").write_text(report, encoding="utf-8")
    print(f"✅ 看板已保存: 黄金看板.md")

    # 历史存档（不覆盖）
    today = datetime.date.today().isoformat()
    history_dir = OUTPUT_DIR / "history"
    history_dir.mkdir(exist_ok=True)
    (history_dir / f"黄金看板-{today}.md").write_text(report, encoding="utf-8")
    print(f"✅ 历史存档: history/黄金看板-{today}.md")

    # 更新缓存
    new_cache = {}
    for name, s in closes.items():
        if len(s) > 0:
            new_cache[name] = {
                "last_date": str(s.index[-1].date()),
                "series": {str(d.date()): round(float(v), 6) for d, v in s.tail(60).items()}
            }
    save_cache(new_cache)

    print(f"[5/5] ✅ 综合评分: {scorer.total():+d} — {scorer.verdict()}")


if __name__ == "__main__":
    main()
