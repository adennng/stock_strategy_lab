# Budget Policy Evaluation Low-Level Debug Commands

本文档只用于调试、补文件、排查问题。正式预算策略探索应优先调用：

```powershell
python -m strategy_lab.cli budget evaluate-policy ...
python -m strategy_lab.cli budget batch-evaluate-policies ...
```

不要在正常流程里让 BudgetAgent 手动串联以下命令。

## 1. 执行结构化预算策略配置

用途：只生成每日预算权重，不做组合回测。

```powershell
python -m strategy_lab.cli budget run-policy BUDGET_RUN_STATE_PATH POLICY_CONFIG_PATH [OPTIONS]
```

常用参数：

```text
--output-dir
  策略执行产物目录。不传则写入 artifacts/budget_runs/{budget_run_id}/policies/executions/{policy_id}

--panel-ohlcv-path
  覆盖 panel_ohlcv.parquet 路径。

--returns-wide-path
  覆盖 returns_wide.parquet 路径。

--policy-id
  本次策略执行 ID。

--update-run-state / --no-update-run-state
  是否登记到 budget_run_state.json。
```

默认输出：

```text
daily_budget_weights.parquet
daily_scores.parquet
gate_results.parquet
raw_weights.parquet
turnover.parquet
diagnostics.json
policy_execution_manifest.json
```

## 2. 对每日预算权重做组合回测

用途：已有 `daily_budget_weights.parquet` 后，单独补做组合回测。

```powershell
python -m strategy_lab.cli budget backtest-policy BUDGET_RUN_STATE_PATH --policy-execution-manifest-path POLICY_EXECUTION_MANIFEST_PATH [OPTIONS]
```

也可以直接传权重文件：

```powershell
python -m strategy_lab.cli budget backtest-policy BUDGET_RUN_STATE_PATH --weights-path path\to\daily_budget_weights.parquet
```

常用参数：

```text
--returns-wide-path
  覆盖 returns_wide.parquet 路径。

--output-dir
  回测输出目录。不传则写入 artifacts/budget_runs/{budget_run_id}/policies/backtests/{backtest_id}

--backtest-id
  本次回测 ID。

--benchmark
  回测基准。当前支持 equal_weight_rebalance、equal_weight_buy_hold、simple_momentum_topk、cash。

--initial-cash
  覆盖初始资金。

--commission
  覆盖佣金比例。

--slippage-perc
  覆盖滑点比例。

--chart / --no-chart
  是否生成策略与基准对比图。
```

默认输出：

```text
equity_curve.parquet
benchmark_curve.parquet
orders.parquet
holdings.parquet
metrics.json
report.md
budget_vs_benchmark.png
budget_backtest_manifest.json
```

## 3. 单独执行参数搜索

用途：只做预算策略参数搜索，不补 attempt summary 和阶段归因。

```powershell
python -m strategy_lab.cli budget search-policy BUDGET_RUN_STATE_PATH POLICY_CONFIG_PATH PARAM_SPACE_PATH [OPTIONS]
```

常用参数：

```text
--output-dir
  参数搜索输出目录。

--search-id
  本次搜索 ID。

--data-split-manifest-path
  覆盖 split_manifest.json 路径。

--search-method
  grid、random 或 ga。

--max-candidates
  最大候选参数数量。

--population-size
  GA 初始种群数量。

--generations
  GA 迭代代数。

--mutation-rate
  GA 变异概率。

--ga-patience
  GA early stopping patience。

--max-workers
  候选参数并行 worker 数。

--cache / --no-cache
  是否启用候选参数缓存。

--benchmark
  覆盖预算回测基准。

--chart / --no-chart
  是否为候选回测生成图表。
```

默认输出：

```text
search_request.json
population_summary.csv
population_summary.json
best_individual.json
walk_forward_summary.json
search_result.json
best/
  budget_policy_config.json
  param_space.json
  full/
  train/
  validation/
  walk_forward/
candidates/
candidate_cache/
```

## 4. 补生成参数搜索摘要

用途：已有 `search_result.json` 后，补生成 `attempt_summary.json` 和 `attempt_summary.md`。

```powershell
python -m strategy_lab.cli budget summarize-search BUDGET_RUN_STATE_PATH --search-id SEARCH_ID [OPTIONS]
```

也可以直接传：

```powershell
python -m strategy_lab.cli budget summarize-search BUDGET_RUN_STATE_PATH --search-result-path path\to\search_result.json
```

默认输出：

```text
attempt_summary.json
attempt_summary.md
```

## 5. 补生成阶段归因

用途：已有最佳参数 full 回测结果和 `budget_profile.json` 后，补生成阶段归因。

```powershell
python -m strategy_lab.cli budget stage-attribution BUDGET_RUN_STATE_PATH --search-id SEARCH_ID [OPTIONS]
```

也可以直接传：

```powershell
python -m strategy_lab.cli budget stage-attribution BUDGET_RUN_STATE_PATH --search-result-path path\to\search_result.json
```

常用参数：

```text
--profile-path
  覆盖 budget_profile.json 路径。

--output-dir
  阶段归因输出目录。

--chart / --no-chart
  是否生成阶段归因图。
```

默认输出：

```text
stage_attribution.json
stage_attribution.csv
stage_attribution.md
stage_return_comparison.png
```

## 调试建议

```text
1. 一次性入口失败时，先看错误信息是哪一步失败。
2. 如果策略配置执行失败，检查 budget_policy_config.json。
3. 如果参数搜索失败，检查 param_space.json 参数路径是否存在。
4. 如果回测失败，检查 daily_budget_weights.parquet 和 returns_wide.parquet 日期是否对齐。
5. 如果阶段归因失败，检查 budget_profile.json 是否有 regime_segments。
```
