---
name: portfolio-evaluation
description: "指导 PortfolioAgent 对指定组合层版本执行融合策略评估：读取 fusion_policy.py，运行预算层与信号层，调用 PortfolioFusionEngine 生成最终权重并回测。"
license: Proprietary project skill
---

# Portfolio Evaluation Skill

## 何时使用

当组合层 run 已完成以下准备后，使用本 skill 评估某个组合层版本：

```text
1. portfolio-run 已创建组合层任务目录。
2. DataAgent 已准备 panel_ohlcv.parquet 和 returns_wide.parquet。
3. portfolio-data-split 已生成 split_manifest.json。
4. portfolio-profile 和 portfolio-signal-profile 已生成画像。
5. portfolio-fusion-version 创建新的组合层版本目录.
6. 已在新创建的某个版本目录下写好：
   fusion_manifest.json
   fusion_policy.py
   param_space.json
   fusion_policy_spec.md
   fusion_policy_meta.json
```

本 skill 只负责评估一个已经存在的组合层版本；不负责生成策略、不负责最终选择。

## 核心命令

```powershell
python -m strategy_lab.cli portfolio evaluate PORTFOLIO_RUN_STATE_PATH --version-id VERSION_ID [OPTIONS]
```

推荐始终使用 `--version-id VERSION_ID`。CLI 也兼容把 `VERSION_ID` 放在
`PORTFOLIO_RUN_STATE_PATH` 后面的旧写法，但 skill 内统一采用 `--version-id`，避免歧义。

示例：

```powershell
python -m strategy_lab.cli portfolio evaluate artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json --version-id v001_budget_direct --benchmark equal_weight_rebalance
```

## 评估对象

新版组合层评估以 `fusion_policy.py` 为正式策略文件。服务会：

```text
1. 加载 versions/{version_id}/fusion_policy.py。
2. 读取 param_space.json 中每个参数的 default，作为策略初始化参数。
3. 实例化 PortfolioFusionPolicy(params)。
4. 调用 PortfolioFusionPolicy.generate_weights(...)。
5. 校验返回的 weights 是否为非负 DataFrame、日期和资产能否对齐、单日总仓位是否不超过 1。
```

`fusion_policy.py` 不要自己读取文件，也不要写死路径。评估服务会自动传入：

```text
budget_weights
  预算层策略执行后的每日资产权重。

signal_targets
  每个资产信号层策略执行后的每日目标仓位 S。

returns
  对齐后的多资产收益率数据。

signal_profile
  portfolio-signal-profile 生成的结构化画像；如果尚未生成则为空 dict。

market_context
  portfolio-profile 生成的结构化画像；如果尚未生成则为空 dict。
```

`generate_weights` 必须返回：

```text
weights
  pandas.DataFrame，index 为日期，columns 为资产代码，数值为最终组合权重。

diagnostics
  pandas.DataFrame，可选但建议提供。至少建议包含：
  gross_exposure、cash_weight、turnover、budget_gross、signal_mean、signal_breadth。
```

如果 diagnostics 缺少标准字段，系统会自动补齐。

## 常用参数

```text
PORTFOLIO_RUN_STATE_PATH
  组合层状态文件，例如：
  artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json

--version-id
  要评估的组合层版本，例如：
  v001_budget_direct

--split-manifest-path
  可选。指定 split_manifest.json。
  不传时读取 portfolio_run_state.json 的 data.split_manifest。

--output-dir
  可选。指定评估输出目录。
  不传时写入 versions/{version_id}/evaluation。

--benchmark
  回测基准。
  可选：equal_weight_rebalance、equal_weight_buy_hold、simple_momentum_topk、cash。
  不传时使用预算层 run_state 的默认基准；若仍无则使用 equal_weight_rebalance。

--initial-cash
  可选。覆盖初始资金，例如 100000。

--commission
  可选。覆盖佣金比例，例如 0.0001。

--slippage-perc
  可选。覆盖滑点比例，例如 0.0001。

--chart / --no-chart
  是否生成策略与基准对比图。
  默认生成；快速测试时可使用 --no-chart。
```

## 内部执行流程

服务内部会自动完成：

```text
读取 portfolio_run_state.json
-> 如果 VERSION_ID 目录存在但尚未登记，自动登记该版本
-> 读取 fusion_manifest.json
-> 加载 fusion_policy.py 和 param_space.json 默认参数
-> 读取 split_manifest.json
-> 执行预算层策略，生成 daily_budget_weights
-> 执行每个资产的信号层策略，生成 daily_signal_targets
-> 调用 PortfolioFusionEngine 执行 fusion_policy.py，生成 daily_final_weights
-> 生成融合诊断文件，解释最终仓位、现金、换手、超预算使用等情况
-> 使用 daily_final_weights 做组合回测
-> 生成回测指标、报告、权重文件和图表
-> 更新 portfolio_run_state.json
```

## 输出文件

默认输出到：

```text
artifacts/portfolio_runs/{portfolio_run_id}/versions/{version_id}/evaluation/
```

主要文件：

```text
budget_execution/
  预算层策略执行产物，包括 daily_budget_weights.parquet、daily_scores.parquet、turnover.parquet 等。

daily_budget_weights.parquet
  每日预算层给出的资产权重。

daily_signal_targets.parquet
  每日信号层给出的资产目标仓位 S，通常在 0 到 1 之间。

daily_final_weights.parquet
  fusion_policy.py 生成并经系统校验后的最终组合权重。

fusion_diagnostics.parquet
  每日组合层融合诊断，包括 final_gross、cash_weight、turnover、over_budget_total、holding_count 等。

fusion_asset_diagnostics.parquet
  每日每个资产的预算权重、信号目标、最终权重和超预算权重。

fusion_diagnostics.json
  融合诊断摘要和警告，便于程序读取。

fusion_diagnostics.md
  融合诊断文字报告，便于 PortfolioAgent 或人工检查策略表现。

aligned_returns_wide.parquet
  与最终权重日期和资产列对齐后的收益率数据，用于回测。

backtest/
  组合回测产物，包括 equity_curve.parquet、orders.parquet、holdings.parquet、metrics.json、report.md、budget_vs_benchmark.png 等。

evaluation_manifest.json
  本次评估总清单，记录输入文件、输出文件、融合摘要、回测指标和警告。

evaluation_summary.md
  简要评估报告。
```

## 检查清单

执行后检查：

```text
1. CLI 返回成功，没有错误信息。
2. daily_final_weights.parquet 已生成，列为资产代码。
3. fusion_diagnostics.json 已生成，summary 中包含 average_final_gross、average_cash_weight、average_turnover。
4. backtest/metrics.json 已生成，包含 sharpe、total_return、max_drawdown 等指标。
5. backtest/budget_vs_benchmark.png 已生成，除非使用 --no-chart。
6. portfolio_run_state.json 的 versions[{version_id}].evaluation 已登记。
7. portfolio_run_state.json 的 artifacts.evaluations 已登记。
8. 如果评估前版本未登记，events 中会出现 portfolio_version_auto_registered。
```
