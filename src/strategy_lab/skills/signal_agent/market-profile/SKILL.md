---
name: market-profile
description: "为 SignalAgent 生成单资产市场画像，包含基础摘要、收益分布、回撤、趋势、波动、阶段划分、成交量画像和事实型文字描述。"
license: Proprietary project skill
---

# Market Profile Skill

## 适用场景

SignalAgent 在生成第一版策略前，应先生成市场画像。市场画像用于描述目标资产在样本区间内的事实特征，包括趋势、震荡、回撤、波动和分阶段表现。

## 输入

可以直接传 OHLCV 数据文件：

```powershell
python -m strategy_lab.cli signal market-profile artifacts\data_agent_workspace\data_files\000300_daily_2024.parquet
```

也可以只传 `run_state.json`，让服务从状态文件中读取：

```text
steps.data_acquisition.primary_dataset
```

命令：

```powershell
python -m strategy_lab.cli signal market-profile --run-state-path artifacts\signal_runs\signal_000300_SH_20260505_213000\run_state.json
```

如果同时传 `DATA_PATH` 和 `--run-state-path`，优先使用显式传入的 `DATA_PATH`。

## CLI 参数

```text
DATA_PATH
  可选。OHLCV 数据文件。若不传，则必须提供 --run-state-path，且 run_state.json 中必须有 primary_dataset。

--run-state-path PATH
  可选。signal run 的 run_state.json。提供后，输出目录默认使用 directories.market_profile，并自动更新 market_profile 状态。

--output-dir PATH
  可选。画像输出目录。
  不传时：
  - 有 --run-state-path：写入 run_state.json 的 directories.market_profile
  - 无 --run-state-path：写入 artifacts/market_profiles/{profile_id}_{timestamp}

--profile-id TEXT
  可选。画像文件名前缀，默认 market_profile。

--smooth-window INTEGER
  可选。自适应阶段划分的标签平滑窗口，单位为交易日，默认 5。

--min-segment-days INTEGER
  可选。自适应阶段划分的最短阶段长度，单位为交易日，默认 10。短于该长度的阶段会合并到相邻且更相近的阶段。

--output-format TEXT
  可选。json、md 或 both，默认 both。

--chart / --no-chart
  可选。是否生成走势与阶段划分 PNG 图像，默认生成。
```

## 输出

默认生成：

```text
market_profile.json
market_profile.md
market_profile_chart.png
```

`market_profile.json` 供 Agent 和程序读取，`market_profile.md` 供人工查看，`market_profile_chart.png` 供人工查看或后续多模态模型读取。

## 画像内容

```text
summary
  数据摘要：symbol、日期范围、行数、字段、缺失值、重复日期、起止价格、累计收益。

return_profile
  收益分布：日收益均值、波动、年化收益、年化波动、类 Sharpe、偏度、峰度、最好/最差单日、上涨日占比。

drawdown_profile
  回撤画像：最大回撤、最大回撤起止、当前回撤、主要回撤区间。

trend_profile
  趋势画像：20/60/120 日均线斜率、价格位于均线上方的比例、整体趋势标签和事实型趋势描述。

volatility_profile
  波动画像：20/60 日波动率、最新波动状态、高波动区间、ATR proxy。

regime_segments
  自适应走势阶段划分：根据每日走势状态自动形成阶段，不按固定窗口切分。每段包含起止日期、交易日数量、阶段标签、收益、波动、最大回撤、均线斜率、价格在均线上方比例和中文事实描述。

liquidity_profile
  成交量或成交额画像。若无 volume/amount 字段，会给出事实说明。

profile_flags
  事实标签：趋势标签、波动水平、回撤风险水平、收益方向。

risk_events
  主要回撤事件和最差单日。

fact_descriptions
  自动生成的事实型中文描述。

visualizations
  图像产物路径。默认包含 price_regime_chart 和 price_regime_chart_filename。
```

## 图像内容

默认生成 `{profile_id}_chart.png`，与 JSON/Markdown 放在同一目录。图像包含：

```text
1. 收盘价、MA20、MA60。
2. 自适应阶段背景色：绿色表示上行阶段，红色表示下行阶段，灰色表示震荡阶段，紫色表示高波动震荡阶段。
3. 最大回撤所在日期的竖线。
4. 样本期最差单日标记。
5. 回撤曲线。
6. 如果数据包含 volume 字段，则额外展示成交量。
```

Markdown 中会自动插入该图像，JSON 中会写入图像路径。

## 阶段标签

阶段划分可能出现以下标签：

```text
strong_uptrend
weak_uptrend
range
weak_downtrend
strong_downtrend
high_volatility_uptrend
high_volatility_downtrend
high_volatility_range
```

每段同时包含中文标签：

```text
强上行阶段
弱上行阶段
震荡阶段
弱下行阶段
强下行阶段
高波动上行阶段
高波动下行阶段
高波动震荡阶段
```

## 阶段划分方法

阶段划分使用确定性规则，不调用 LLM。计算过程：

```text
1. 每个交易日计算 20 日收益、60 日收益、MA20/MA60 斜率、收盘价是否位于 MA20/MA60 上方、20 日年化波动率。
2. 根据这些指标给每日打状态标签，例如 strong_uptrend、weak_downtrend、range。
3. 如果 20 日波动率高于样本内 75% 分位数，则把对应状态标记为高波动上行/下行/震荡。
4. 用 --smooth-window 做多数标签平滑，降低单日异常波动导致的阶段抖动。
5. 合并连续相同标签，形成真实走势阶段。
6. 短于 --min-segment-days 的阶段会合并到相邻且标签更相近的阶段。
```

每个阶段都会输出 `trading_days`，表示该阶段实际包含的交易日数量。

## 常用命令

直接从数据文件生成画像：

```powershell
python -m strategy_lab.cli signal market-profile artifacts\data_agent_workspace\data_files\000300_daily_2024.parquet
```

从 run_state.json 自动读取数据并写入本次 run 的 market_profile 目录：

```powershell
python -m strategy_lab.cli signal market-profile --run-state-path artifacts\signal_runs\signal_000300_SH_20260505_213000\run_state.json
```

调整自适应划分平滑窗口和最短阶段长度：

```powershell
python -m strategy_lab.cli signal market-profile artifacts\data_agent_workspace\data_files\000300_daily_2024.parquet --smooth-window 5 --min-segment-days 10
```

只输出 JSON：

```powershell
python -m strategy_lab.cli signal market-profile artifacts\data_agent_workspace\data_files\000300_daily_2024.parquet --output-format json
```

不生成图像：

```powershell
python -m strategy_lab.cli signal market-profile artifacts\data_agent_workspace\data_files\000300_daily_2024.parquet --no-chart
```
