# Gold Quant Dashboard

每天自动抓取 DXY、黄金/白银/铜期货、10Y 实际利率 → 评分 → 生成黄金看板。

## 架构

```
trading/gold-dashboard/
├── gold_dashboard.py       # 主程序
├── requirements.txt        # 依赖
├── .env.example            # 环境变量模板
├── .github/workflows/      # GitHub Actions 自动运行
├── 黄金看板.md             # 输出看板
└── README.md               # 本文件
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
cp .env.example .env
```

编辑 `.env`，填入 FRED API Key（免费申请：https://fred.stlouisfed.org/docs/api/api_key.html）

### 3. 运行

```bash
python gold_dashboard.py
```

输出文件：`黄金看板.md`

## 评分系统

| 指标 | 条件 | 得分 |
|------|------|------|
| DXY | 低于 20 日均线 → 美元走弱 → 利多黄金 | +2 |
| | 高于 20 日均线 → 美元走强 → 利空黄金 | -2 |
| 10Y 实际利率 | 低于 20 日均线 → 持有黄金机会成本降低 | +2 |
| | 高于 20 日均线 → 持有黄金机会成本升高 | -2 |
| 金银比 | 下降 → 白银跑赢 → 风险偏好回升 → 利多 | +1 |
| | 上升 → 黄金跑赢 → 避险情绪 → 利空 | -1 |
| 铜金比 | 上升 → 经济扩张预期 → 通胀上行 → 利多 | +1 |
| | 下降 → 经济收缩预期 → 利空 | -1 |

### 综合判断

| 总得分 | 结论 |
|------|------|
| ≥ +3 | 🟢 利多黄金 — 多项指标共振看多 |
| -2 ~ +2 | 🟡 中性 — 信号分歧，观望为主 |
| ≤ -3 | 🔴 利空黄金 — 多项指标共振看空 |

## 自动化

### 本地定时（macOS）

```bash
# 每天 9:00 运行
crontab -e
# 添加：
0 9 * * * cd /path/to/gold-dashboard && python gold_dashboard.py
```

### GitHub Actions

Fork 到 GitHub → 设置 `FRED_API_KEY` Secret → Actions 每天自动运行。

## 数据源

- Yahoo Finance: DX-Y.NYB (DXY), GC=F (黄金), SI=F (白银), HG=F (铜)
- FRED (Federal Reserve Economic Data): DFII10 (10Y TIPS 实际利率)

## 兼容性

- macOS (Intel + Apple Silicon M 系列)
- Linux
- Windows

Python 3.9+
