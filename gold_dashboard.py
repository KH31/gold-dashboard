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

    # Compute sub-scores
    p_sub = sum(s.scores.get(k, 0) for k in ['DXY','Real_Yield','Gold_Silver','Copper_Gold'])
    f_sub = sum(s.scores.get(k, 0) for k in ['GLD','COT'])
    d_sub = sum(s.scores.get(k, 0) for k in ['PBOC','WGC'])

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

    return f"""# 黄金看板

> 自动生成 · {now}

---

## 第一层：价格信号

| 指标 | 现值 | MA20 | 方向 | 得分 | 权重 |
|------|------|------|------|------|------|
| DXY | {dxy_s} | {dxy_m} | {dxy_p} {dxy_sl} | {s.scores.get('DXY',0):+d} | ★★★★ |
| 10Y实际利率 | {rv}% | {rm}% | {rp} {rs} | {s.scores.get('Real_Yield',0):+d} | ★★★★ |
| 金银比 | {gsr} | {gsz} | {gsd} | {s.scores.get('Gold_Silver',0):+d} | ★★★ |
| 铜金比 | {cgr_txt} | {cgz} | {cgd} | {s.scores.get('Copper_Gold',0):+d} | ★★★ |
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

## 综合评分

```
第一层（价格信号）：  {p_sub:+d}
第二层（资金流）：    {f_sub:+d}
第三层（长期驱动）：  {d_sub:+d}
                    ─────
加权总分：           {t_val:+d}
```

## 交易结论

**{v}**

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

### 比值入场阈值

| 比值 | 区间 | 得分 | 含义 | 当前 |
|------|------|------|------|------|
| **金银比** | > 85 | +3 | 🔴 极端恐惧——白银被严重低估，黄金+白银都值得买 | |
| | 75-85 | +1 | 🟡 高恐惧——避险情绪高涨 | |
| | **45-75** | **0** | 🟡 中性 | ← **{gsr}** |
| | 30-45 | -1 | 🟢 低恐惧——黄金偏贵 | |
| | < 30 | -2 | 🟢 贪婪——白银过热 | |
| **铜金比** (×10000) | > 25 | +1 | 🟢 增长区——经济扩张，通胀预期上行，利多黄金 | |
| | 15-25 | 0 | 🟡 中性 | |
| | **10-15** | **-1** | 🔴 衰退恐惧——增长放缓，利空黄金 | ← **{cgr_txt}** |
| | < 10 | -2 | 🔴 严重收缩——通缩风险 | |

> **入场规则**：金银比 > 85 且铜金比(×10000) < 10 同时触发 → 逆向买入黄金胜率最高（金银比极端=恐惧到顶+铜金比极端=衰退到底）。历史上这两个条件同时触发发生在 2008.10、2015.12、2020.3——三次都是黄金大底。
"""


# ═══════════════ 主流程 ═══════════════

def main():
    scorer = Scorer()

    print("[1/5] 第一层：价格信号...")
    closes = fetch_yahoo()
    real_yield = fetch_real_yield()

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

    price_data = {
        "dxy_val": dxy_val, "dxy_ma": dxy_ma, "dxy_pos": dxy_pos, "dxy_slope": dxy_slope,
        "real_val": real_val, "real_ma": real_ma, "real_pos": real_pos, "real_slope": real_slope,
        "gs_ratio": gs_ratio, "gs_dir": gs_dir, "gs_zone": gs_zone,
        "cg_ratio": cg_ratio, "cg_dir": cg_dir, "cg_zone": cg_zone,
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

    if VAULT_DAILY_DIR.exists():
        today = datetime.date.today().isoformat()
        (VAULT_DAILY_DIR / f"黄金看板-{today}.md").write_text(report, encoding="utf-8")

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
