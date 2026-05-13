---
name: attempt-evaluation
description: "SignalAgent 写好策略文件后，评估单个或批量 attempt，完成策略保存、参数搜索、回测、摘要和阶段归因。"
license: Proprietary project skill
---

# Attempt Evaluation Skill

## 适用场景

本 skill 用于把 SignalAgent 生成的策略变成可交给 CriticAgent 复盘的完整 attempt。

支持两个场景：

```text
场景 A：单 attempt 评估
  输入一组 strategy.py / strategy_spec.md / param_space.json / strategy_meta.json。
  用于阶段 2/3 的单策略改进，或对某个策略做独立评估。

场景 B：批量 attempt 评估
  输入多个策略目录。
  用于阶段 1 Alpha 批量探索，一次评估最多 4 个不同 Alpha 路线。
```

## 前置条件

调用前通常应已完成：

```text
1. signal-run 已创建 run_state.json。
2. DataAgent 已获取数据，并写入主数据路径。
3. data-split 已生成 train / validation / walk-forward。
4. market-profile 已生成市场画像。
5. SignalAgent 已生成策略四件套。
```

缺少 data-split 会导致参数搜索失败；缺少 market-profile 会导致阶段归因失败。

## 策略四件套

正式策略必须包含：

```text
strategy.py
strategy_spec.md
param_space.json
strategy_meta.json
```

### strategy.py

默认策略类名是 `Strategy`。建议继承：

```python
from strategy_lab.signals.base import BaseSignalStrategy
```

必须实现：

```python
def suggest(self, history, current_position_in_budget=0.0) -> float:
    ...
```

返回值必须是目标预算占用比例：

```text
target_S ∈ [0, 1]
```

不要返回加减仓动作。不要在 `suggest()` 中读取外部全量数据、写文件、联网或执行慢操作。

如果不继承 `BaseSignalStrategy`，策略类也必须满足：

```text
1. 构造函数可接收 params 字典。
2. 实例上有 self.params。
3. 实现 suggest(history, current_position_in_budget=0.0)。
```

### strategy_spec.md

供 CriticAgent 理解策略设计意图。应写清楚：

```text
Alpha
Filters
ExitPolicy
PositionMapper
StateRules
参数含义
适合的市场阶段
可能失效的市场阶段
```

### param_space.json

参数搜索空间。推荐用离散 `values` 控制组合数量：

```json
{
  "ma_window": {"type": "int", "values": [20, 60, 120], "default": 60},
  "band": {"type": "float", "values": [0.0, 0.005, 0.01], "default": 0.0}
}
```

也支持 `low/high/step`。如果策略没有可调参数，可以写：

```json
{}
```

空参数空间会只评估一个 `{}` 候选，流程兼容。

### strategy_meta.json

建议包含：

```json
{
  "strategy_name": "my_strategy_name",
  "strategy_class_name": "Strategy",
  "created_by": "SignalAgent",
  "strategy_structure": {
    "alpha": "...",
    "filters": [],
    "exit_policy": [],
    "position_mapper": "...",
    "state_rules": []
  },
  "primary_metric": "sharpe"
}
```

注意：实际加载策略类使用 CLI 的 `--strategy-class-name`，默认 `Strategy`。`strategy_meta.json` 里的 `strategy_class_name` 主要用于记录和复盘，不会自动改变加载类名。

## 单 Attempt 评估

命令：

```powershell
python -m strategy_lab.cli signal evaluate-attempt RUN_STATE_PATH --strategy-path STRATEGY_PATH --strategy-spec-path STRATEGY_SPEC_PATH --param-space-path PARAM_SPACE_PATH --strategy-meta-path STRATEGY_META_PATH --strategy-name STRATEGY_NAME --search-method ga --max-candidates 30 --max-workers 2
```

关键参数：

```text
RUN_STATE_PATH
  必填。本次 signal run 的 run_state.json。

--attempt-id
  可选。不传时自动生成 attempt_XXX。

--strategy-path / --strategy-spec-path / --param-space-path / --strategy-meta-path
  正式单 attempt 评估必填。

--strategy-name
  可选。不传时尝试从 strategy_meta.json 读取。

--strategy-class-name
  实际加载的策略类名，默认 Strategy。

--template
  仅用于基线或 smoke test，例如 ma_crossover；正式 SignalAgent 策略不建议使用。
```

快速测试：

```powershell
python -m strategy_lab.cli signal evaluate-attempt RUN_STATE_PATH --attempt-id attempt_eval_smoke --template ma_crossover --strategy-name ma_crossover_eval_smoke --search-method grid --max-candidates 5 --max-workers 1 --no-stage-chart
```

## 批量 Attempt 评估

用于阶段 1 Alpha 批量探索。SignalAgent 先生成多个策略目录，再统一评估。

命令：

```powershell
python -m strategy_lab.cli signal evaluate-attempts RUN_STATE_PATH --strategies-dir STRATEGIES_DIR --attempt-prefix attempt_alpha --search-method ga --max-candidates 30 --max-workers 2 --batch-workers 1
```

`STRATEGIES_DIR` 下每个子目录必须包含：

```text
strategy.py
strategy_spec.md
param_space.json
strategy_meta.json
```

如果策略类名不是 `Strategy`，建议用 manifest：

```json
{
  "strategies": [
    {
      "strategy_dir": "artifacts/signal_runs/.../strategies/generated/alpha_batch_001/trend_ma",
      "attempt_id": "attempt_alpha_001_trend_ma",
      "strategy_name": "trend_ma",
      "strategy_class_name": "Strategy"
    }
  ]
}
```

manifest 命令：

```powershell
python -m strategy_lab.cli signal evaluate-attempts RUN_STATE_PATH --strategy-manifest-path STRATEGY_MANIFEST_PATH --search-method ga --max-candidates 30 --max-workers 2 --batch-workers 1
```

批量模式会生成：

```text
reports/batch_evaluation_{timestamp}/batch_evaluation_summary.json
reports/batch_evaluation_{timestamp}/batch_evaluation_summary.md
```

批量模式仍然会为每个策略生成标准 attempt 目录。

## 参数搜索

搜索方法：

```text
grid
  参数组合少、需要透明复现时使用。

random
  参数空间较大、只想粗筛时使用。

ga
  默认正式探索方式，适合较大参数空间。
```

常用参数：

```text
--max-candidates
  每个策略最多评估的候选参数数。调试 3-10，正常迭代 20-50。

--max-workers
  单个策略内部候选参数并行 worker 数。普通机器建议 1-2。

--batch-workers
  多个策略 attempt 并行 worker 数。首次建议 1，需要提速时可设 2。

--cache / --no-cache
  默认启用缓存。修改评分公式、回测逻辑或怀疑缓存不一致时用 --no-cache。

--quantstats-html
  默认关闭。参数搜索阶段不建议开启，最终确认策略后再补 HTML。
```

GA 参数：

```text
--population-size
--generations
--mutation-rate
--ga-patience
--min-improvement
--random-seed
```

建议组合：

```text
快速调试：
--search-method grid --max-candidates 5 --max-workers 1

正常迭代：
--search-method ga --max-candidates 30 --population-size 10 --generations 5 --max-workers 2
```

## 评分逻辑

单区间分数：

```text
metric_score =
  2.00 * sharpe
+ 0.50 * calmar
+ 0.80 * total_return
+ 0.50 * excess_total_return
- 0.80 * abs(max_drawdown)
```

候选参数总分：

```text
candidate_score =
  0.20 * train_score
+ 0.35 * validation_score
+ 0.45 * walk_forward_mean_score
- overfit_penalty
- walk_forward_instability_penalty
```

含义：

```text
Sharpe 是当前最关键指标。
validation 比 train 更重要。
walk-forward 权重最高，用来检查不同时间段的稳定性。
训练集好但验证集差会扣过拟合分。
walk-forward 波动大或某个 fold 为负会扣稳定性分。
```

## 产物结构

每个 attempt 写入：

```text
artifacts/signal_runs/{run_id}/attempts/{attempt_id}/
```

关键产物：

```text
strategy/
  strategy.py
  strategy_spec.md
  param_space.json
  strategy_meta.json

optimization/
  optimization_config.json
  population_summary.csv
  population_summary.json
  best_individual.json
  search_result.json
  validation_summary.json
  walk_forward_summary.json
  attempt_summary.json
  attempt_summary.md

backtests/full/
backtests/train/
backtests/validation/
backtests/walk_forward/fold_XXX/

analysis/stage_attribution/
  stage_attribution.json
  stage_attribution.csv
  stage_attribution.md
  stage_return_comparison.png
```

每个回测目录通常包含：

```text
metrics.json
report.md
equity_curve.parquet
benchmark_curve.parquet
daily_signals.parquet
orders.parquet
strategy_vs_benchmark.png
backtest_request.json
```

阶段归因说明：

```text
stage_attribution.json
  完整结构化结果。每个市场阶段包含收益、回撤、基准对比、trade_summary 和 trades 明细。

stage_attribution.csv
  一行一个市场阶段，包含交易汇总字段，不展开每笔交易。

stage_attribution.md
  给人和 CriticAgent 阅读的阶段归因报告，包含阶段内交易明细。
```

## 输出

单 attempt 默认输出摘要：

```json
{
  "attempt_id": "...",
  "strategy_name": "...",
  "best_score": 0.0,
  "best_params": {},
  "status": "ready_for_review",
  "full_backtest_dir": "...",
  "attempt_summary_path": "...",
  "stage_attribution_path": "..."
}
```

`status=ready_for_review` 后，SignalAgent 可以调用 CriticAgent 做单 attempt 复盘。

批量模式默认输出：

```json
{
  "status": "success",
  "attempted_count": 4,
  "success_count": 4,
  "failed_count": 0,
  "summary_json_path": "...",
  "summary_md_path": "...",
  "results": []
}
```

批量评估后，SignalAgent 应调用 CriticAgent 做多 attempt 横向比较。

## 失败处理

单 attempt 任一关键阶段失败会中止后续流程，并返回结构化 JSON：

```json
{
  "status": "failed",
  "attempt_id": "...",
  "failed_step": "save_strategy",
  "completed_steps": [],
  "error_type": "...",
  "error": "...",
  "error_path": ".../logs/attempt_evaluation_error.json",
  "next_action": "..."
}
```

失败时会：

```text
1. attempt.status 更新为 failed。
2. 写入 logs/attempt_evaluation_error.json。
3. run_state.json 写入 failed_step 和 attempt_evaluation_error_path。
4. events 追加 attempt_evaluation_failed。
```

常见失败阶段：

```text
save_strategy
  策略路径、类名、语法、suggest 接口或四件套文件有问题。

parameter_search
  缺少 data-split，param_space 不合理，suggest 运行报错，或所有候选失败。

attempt_summary
  参数搜索没有生成必要回测产物。

stage_attribution
  缺少 market-profile，或 full 回测目录缺少 equity_curve/orders/daily_signals/metrics。
```

失败 attempt 不要交给 CriticAgent 复盘，应先根据 `error_path` 修复后重跑。

## 最终检查

调用成功后确认：

```text
1. 命令状态成功。
2. 单 attempt 输出 status=ready_for_review。
3. attempt_summary_path 存在。
4. stage_attribution_path 存在。
5. full_backtest_dir 下存在 metrics.json。
6. run_state.json 中对应 attempt 已写入 best_params、best_individual_path、attempt_summary_path、stage_attribution_path。
```
