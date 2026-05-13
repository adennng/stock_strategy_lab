---
name: portfolio-profile
description: "指导 PortfolioAgent 在编写组合层 fusion policy 前生成组合层画像：资产池、预算层独立表现、信号层输出、预算/信号关系和相关性事实。"
license: Proprietary project skill
---

# Portfolio Profile Skill

## 何时使用

在已经完成以下步骤后使用：

```text
1. portfolio-run 已创建组合层 run。
2. DataAgent 已准备 panel_ohlcv.parquet 和 returns_wide.parquet。
3. portfolio-data-split 已生成 split_manifest.json。
```

PortfolioAgent 编写第一版组合层策略前必须先使用本 skill。  
本 skill 不生成组合层策略、不创建版本、不回测最终组合。它只生成事实画像，帮助 LLM 判断预算层和信号层该如何融合。

## 核心命令

```powershell
python -m strategy_lab.cli portfolio profile PORTFOLIO_RUN_STATE_PATH [OPTIONS]
```

常用示例：

```powershell
python -m strategy_lab.cli portfolio profile artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json
```

不生成图表的快速模式：

```powershell
python -m strategy_lab.cli portfolio profile artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json --no-chart
```

## 参数说明

```text
PORTFOLIO_RUN_STATE_PATH
  组合层 portfolio_run_state.json 路径。

--split-manifest-path
  可选。指定 split_manifest.json。不传时读取 portfolio_run_state.json 的 data.split_manifest。

--output-dir
  可选。画像输出目录。不传时写入当前组合层 run 的 profile/。

--chart / --no-chart
  是否生成图表。默认生成。
```

## 内部执行流程

服务内部会自动完成：

```text
1. 读取 portfolio_run_state.json。
2. 读取 split_manifest.json，定位 panel_ohlcv.parquet 和 returns_wide.parquet。
3. 执行预算层最终策略，生成 daily_budget_weights.parquet。
4. 执行每个资产的信号层最终策略，生成 daily_signal_targets.parquet。
5. 统计资产收益、波动、回撤、成交量和相关性。
6. 统计预算层每日权重、总敞口、持仓数量和资产排名。
7. 统计信号层每日 target_S、信号广度、强信号比例和资产排名。
8. 用横截面份额、排名、Top-K 重合、条件响应来度量预算层和信号层的关系。
9. 单独评估预算层策略表现，并与等权再平衡、等权买入持有基准比较。
10. 输出事实提示，帮助 LLM 决定第一版 fusion policy 的方向。
11. 更新 portfolio_run_state.json 的 profile 字段。
```

注意：预算权重 `R_i` 和信号目标 `S_i` 不是同一量纲，不要直接比较绝对数值。本画像重点使用份额、排名、Top-K 重合和条件响应。

## 输出文件

默认输出到：

```text
artifacts/portfolio_runs/{portfolio_run_id}/profile/
```

主要文件：

```text
portfolio_profile.json
  完整结构化画像。后续自动分析和策略编写优先读取这个文件。

portfolio_profile.md
  给 LLM 和用户阅读的文字版画像摘要。

asset_summary.csv
  每个资产的收益、波动、最大回撤、成交量等摘要。

budget_summary.csv
  每个资产在预算层中的平均权重、最大权重、零权重比例、活跃比例和平均排名。

signal_summary.csv
  每个资产在信号层中的平均 target_S、强信号比例、零信号比例和信号变化次数。

budget_signal_alignment.csv
  每个资产的预算/信号关系。重点看 share_gap_mean、rank_gap_mean、budget_high_signal_low_days_ratio、signal_high_budget_low_days_ratio 和 alignment_type。

daily_budget_signal_alignment.parquet
  每日横截面预算/信号关系。包含排名相关性、Top3/Top5 重合、份额 L1 差异、预算高信号低数量、信号高预算低数量。

budget_benchmark_metrics.json
  预算层独立表现与基准表现的结构化指标。

budget_benchmark_metrics.csv
  预算层独立表现与基准表现的表格版指标。

budget_benchmark_equity.parquet
  budget_only、equal_weight_rebalance、equal_weight_buy_hold 的净值曲线。

correlation_matrix.csv
  资产收益率相关性矩阵。

daily_budget_weights.parquet
  画像阶段执行预算层策略得到的每日预算权重。

daily_signal_targets.parquet
  画像阶段执行信号层策略得到的每日信号目标仓位。

charts/
  budget_signal_scatter.png
  gross_exposure_comparison.png
  budget_vs_benchmarks.png
  daily_alignment_timeseries.png
  budget_signal_rank_gap_bar.png
  correlation_heatmap.png
```

## LLM 写策略前必须阅读

创建第一版组合层策略前，至少阅读：

```text
profile/portfolio_profile.md
profile/portfolio_profile.json
profile/budget_signal_alignment.csv
profile/daily_budget_signal_alignment.parquet
profile/budget_benchmark_metrics.json
```

重点判断：

```text
预算层独立表现是否优于等权基准。
预算层平均总敞口是多少。
信号层平均动用比例和信号广度是多少。
预算 Top-K 和信号 Top-K 的日均重合度如何。
预算/信号排名相关性是否稳定。
是否存在高预算低信号资产。
是否存在高信号低预算资产。
预算层给 0 权重时，是否经常出现强信号资产。
资产相关性是否很高。
```

## 如何使用画像指导 fusion policy

如果预算层独立表现明显弱于等权基准：

```text
不要过度相信预算权重，可降低 budget_weight 权重，提升 signal_strength、risk_adjusted_signal 或 equal_weight_anchor 的作用。
```

如果预算 Top-K 和信号 Top-K 重合度低：

```text
需要显式处理两层冲突，不要简单让某一层完全支配。可以考虑 score_blend、signal_veto、soft_budget_cap 或 over_budget_pool。
```

如果高预算低信号资产较多：

```text
考虑对预算权重做 signal discount 或 veto，但不要无条件把所有低信号资产归零，避免过度空仓。
```

如果高信号低预算资产较多：

```text
考虑有限预算软突破、闲置资金再分配或 signal_quality_bonus。是否允许 zero_override_enabled 取决于后续回测证据。
```

如果信号广度低：

```text
target_gross 不宜过激，或设计 idle_cash_reallocation，把被信号层压低的资金分配给同日更强、更可靠的资产。
```

如果资产相关性高：

```text
max_weight、over_budget_pool 和集中度约束不宜过松。
```

## 检查清单

执行后必须检查：

```text
1. portfolio_profile.json 存在。
2. portfolio_profile.md 存在。
3. budget_signal_alignment.csv 存在。
4. daily_budget_signal_alignment.parquet 存在。
5. budget_benchmark_metrics.json 存在。
6. budget_benchmark_equity.parquet 存在。
7. daily_budget_weights.parquet 存在。
8. daily_signal_targets.parquet 存在。
9. portfolio_run_state.json 的 profile.status 为 success。
10. profile.summary.daily_top5_overlap_mean、profile.summary.daily_budget_signal_rank_corr_mean 有值或有合理缺失说明。
11. 如有 warnings，阅读并判断是否影响后续策略生成。
```

## 后续步骤

完成组合层画像后，下一步使用 `portfolio-signal-profile` skill，生成每个资产的信号层策略画像。之后再使用 `portfolio-policy-authoring` skill 创建第一版组合层策略五件套。
