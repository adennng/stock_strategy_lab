---
name: budget-policy-evaluation
description: "指导 BudgetAgent 一次性评估单个或多个预算层策略，生成参数搜索、回测摘要、阶段归因和横向排名产物。"
license: Proprietary project skill
---

# Budget Policy Evaluation Skill

## 何时使用

当 `budget-policy-authoring` 已经生成预算策略四件套后，使用本 skill 评估预算层策略。

预算策略四件套通常是：

```text
budget_policy_config.json
budget_policy_spec.md
param_space.json
budget_policy_meta.json
```

正式策略探索只使用两个入口：

```text
单策略评估：budget evaluate-policy
多策略批量评估：budget batch-evaluate-policies
```

底层调试、补文件、排查问题命令不要默认使用。确实需要时，再阅读：

```text
references/low_level_debug_commands.md
```

## 前置条件

必须已经完成：

```text
budget-run
budget-data-panel
budget-data-split
budget-profile
budget-policy-authoring
```

输入目录中应已经存在 `budget_run_state.json`，且其中登记了预算层数据面板、数据切分、市场画像等路径。

## 单策略一键评估

当只需要评估一个预算策略时，调用：

```powershell
python -m strategy_lab.cli budget evaluate-policy BUDGET_RUN_STATE_PATH --policy-dir POLICY_DIR [OPTIONS]
```

也可以显式传入两个核心文件：

```powershell
python -m strategy_lab.cli budget evaluate-policy BUDGET_RUN_STATE_PATH --policy-config-path path\to\budget_policy_config.json --param-space-path path\to\param_space.json
```

常用示例：

```powershell
python -m strategy_lab.cli budget evaluate-policy artifacts\budget_runs\budget_xxx\budget_run_state.json --policy-dir artifacts\budget_runs\budget_xxx\policies\generated\policy_batch_001\policy_a --search-method ga --max-candidates 20 --max-workers 20
```

快速测试示例：

```powershell
python -m strategy_lab.cli budget evaluate-policy artifacts\budget_runs\budget_xxx\budget_run_state.json --policy-dir path\to\policy_a --search-method grid --max-candidates 1 --no-chart --no-stage-chart
```

内部会自动完成：

```text
读取 budget_policy_config.json + param_space.json
  读取预算策略结构和可调参数范围。
-> 参数搜索
  生成多组候选参数，并分别做训练段、验证段和滚动窗口回测，选出综合得分最高的一组。
-> 最佳参数重跑 full / train / validation / walk-forward
  用最佳参数重新执行完整回测，保留可复盘的标准产物。
-> 生成 attempt_summary.json / attempt_summary.md
  汇总本次策略、参数、回测指标、walk-forward 表现和关键文件路径。
-> 生成 stage_attribution.json / stage_attribution.csv / stage_attribution.md / stage_return_comparison.png
  按市场阶段拆解策略收益、基准收益、超额收益、回撤、仓位和换手。
-> 登记到 budget_run_state.json
  把本次评估产物路径、最佳得分和关键摘要写回任务状态文件。
```

运行时间通常较长：即使是单策略，也会反复执行多次回测；正式参数搜索可能需要数十分钟。

## 多策略批量评估

当首次生成多个预算策略时，优先调用批量入口。只需要调用一次，系统会遍历每个策略目录，并对每个策略执行完整单策略评估。
批量评估会对每个策略都执行完整参数搜索和回测，运行时间通常更长；多个策略同时评估时建议先控制 `--max-candidates`，再逐步提高并行 worker 数。

```powershell
python -m strategy_lab.cli budget batch-evaluate-policies BUDGET_RUN_STATE_PATH --policies-dir POLICIES_DIR [OPTIONS]
```

常用示例：

```powershell
python -m strategy_lab.cli budget batch-evaluate-policies artifacts\budget_runs\budget_xxx\budget_run_state.json --policies-dir artifacts\budget_runs\budget_xxx\policies\generated\policy_batch_001 --search-method random --max-candidates 8 --batch-workers 4 --max-workers 8 --no-chart --no-stage-chart
```

第一轮多策略方向筛选不建议直接用 GA。优先用 `random` + 较小 `max-candidates` 快速判断哪类策略路线更值得继续；选出 primary/fallback 后，再对少数策略用 GA 精调。

快速测试示例：

```powershell
python -m strategy_lab.cli budget batch-evaluate-policies artifacts\budget_runs\budget_xxx\budget_run_state.json --policies-dir path\to\policy_batch_001 --search-method grid --max-candidates 1 --batch-workers 1 --max-workers 1 --no-chart --no-stage-chart
```

策略目录要求：

```text
POLICIES_DIR/
  policy_a/
    budget_policy_config.json
    param_space.json
    budget_policy_spec.md      可选
    budget_policy_meta.json    可选
  policy_b/
    budget_policy_config.json
    param_space.json
```

## 常用参数

```text
--policy-dir
  单个预算策略目录。目录内至少包含 budget_policy_config.json 和 param_space.json。

--policies-dir
  多个预算策略子目录的父目录。

--search-id
  单策略评估 ID。不传则自动生成。

--batch-id
  批量评估 ID。不传则自动生成。

--search-method
  grid、random 或 ga。
  第一轮多策略粗筛建议 random，速度快，能较快比较不同策略路线。
  少数候选策略精调建议 ga，效果通常更好但耗时更长。
  grid 只适合参数很少、每个参数取值很少的小参数空间。

--max-candidates
  最大候选参数数量。**它是影响速度的最关键参数**，为了提高速度，请适当降低。
  第一轮多策略粗筛建议 3-10。
  单策略 GA 精调建议 20-30，多策略请适当降低以提高速度。
  快速测试可 1-3。

--max-workers
  单个策略内部候选参数并行 worker 数。它控制同一个预算策略下，多个候选参数组合是否并行回测。
  常用建议是 5-20。

--batch-workers
  多个策略并行评估 worker 数。它控制多个策略目录是否并行评估。
  只有一个策略时该参数没有意义。
  多策略探索时常用建议是大于等于4。
  总并行压力大致等于 batch-workers * max-workers。

--benchmark
  可选，覆盖预算回测基准。不传则读取 budget_run_state.json 的 task.benchmark。

--chart / --no-chart
  是否为候选参数回测生成 budget_vs_benchmark.png。
  该图展示预算策略净值与基准净值、策略回撤与基准回撤。
  因为每个候选参数都可能生成图，数量多时会明显变慢。
  建议：正式大规模搜索保持默认 no-chart；最终最优参数重跑仍会生成关键图表。

--stage-chart / --no-stage-chart
  是否生成阶段归因图 stage_return_comparison.png。
  该图展示不同市场阶段内的策略收益、基准收益、超额收益，以及总仓位和换手变化。
  建议：正式评估保留 stage-chart；快速测试可关闭。

--update-run-state / --no-update-run-state
  是否登记到 budget_run_state.json。正式运行保持默认 update-run-state。
```

## 输出文件

单策略默认输出：

```text
artifacts/budget_runs/{budget_run_id}/policies/searches/{search_id}/
  search_result.json
  population_summary.csv
  population_summary.json
  best_individual.json
  walk_forward_summary.json
  attempt_summary.json
  attempt_summary.md
  best/
    budget_policy_config.json
    param_space.json
    full/
    train/
    validation/
    walk_forward/
  stage_attribution/
    stage_attribution.json
    stage_attribution.csv
    stage_attribution.md
    stage_return_comparison.png
```

单策略输出文件说明：

```text
search_result.json
  本次参数搜索的主索引文件。记录 search_id、输入策略文件、参数空间、最佳参数、最佳得分、各分段回测路径和关键产物路径。

population_summary.csv
  所有候选参数组合的扁平表格，便于排序和人工查看。每行通常对应一个候选参数组合及其 train、validation、walk-forward 表现。

population_summary.json
  与 population_summary.csv 类似，但保留更完整的结构化字段，便于后续程序读取。

best_individual.json
  最佳候选参数的详细记录，包含 best_params、综合评分、评分组成、分段表现等。

walk_forward_summary.json
  最佳候选参数在滚动窗口验证中的表现汇总，包含各 fold 的指标、均值、波动和稳定性信息。

attempt_summary.json
  给后续复盘智能体读取的结构化摘要。汇总任务信息、数据面板、市场画像、最佳策略配置、参数搜索结果、full/train/validation/walk-forward 回测指标和关键文件路径。

attempt_summary.md
  人类可读摘要。用于快速查看最佳参数、评分组成、核心回测指标、walk-forward 表格、Top candidates 和关键文件路径。

best/budget_policy_config.json
  已写入最佳参数后的最终预算策略配置。后续复盘、最终选择或模拟交易应优先读取这个文件。

best/param_space.json
  本次评估使用的参数空间副本，方便复盘时知道搜索范围。

best/full/
  最佳参数在完整训练数据范围上的回测产物目录。

best/train/
  最佳参数在 train 切片上的回测产物目录。

best/validation/
  最佳参数在 validation 切片上的回测产物目录。

best/walk_forward/
  最佳参数在滚动窗口验证各 fold 上的回测产物目录。

best/*/equity_curve.parquet
  对应分段的组合净值、收益、换手、成本、仓位、基准收益和超额收益序列。

best/*/benchmark_curve.parquet
  对应分段的基准净值序列。

best/*/orders.parquet
  对应分段的调仓记录，包含资产、买卖方向、权重变化、交易金额、交易成本等。

best/*/holdings.parquet
  对应分段的每日持仓权重和估算持仓市值。

best/*/metrics.json
  对应分段的核心指标，例如 total_return、annual_return、sharpe、max_drawdown、benchmark_total_return、excess_total_return、average_turnover 等。

best/*/report.md
  对应分段的人类可读回测报告。

best/*/budget_vs_benchmark.png
  预算策略与基准的净值和回撤对比图。候选参数是否批量生成该图由 --chart 控制；最终最佳参数回测通常会保留关键图表。

stage_attribution/stage_attribution.json
  阶段归因结构化结果。按 budget-profile 的市场阶段拆解策略收益、基准收益、超额收益、回撤、波动、换手、成本、仓位和主要持仓。

stage_attribution/stage_attribution.csv
  阶段归因扁平表格，便于排序筛选。

stage_attribution/stage_attribution.md
  人类可读阶段归因报告，用于复盘策略在哪些市场阶段表现好或差。

stage_attribution/stage_return_comparison.png
  阶段收益对比图。由 --stage-chart 控制是否生成。
```

批量评估额外输出：

```text
artifacts/budget_runs/{budget_run_id}/policies/batch_evaluations/{batch_id}/
  batch_policy_evaluation_summary.json
  batch_policy_evaluation_summary.md
  batch_policy_evaluation_summary.csv
```

批量评估输出文件说明：

```text
batch_policy_evaluation_summary.json
  批量评估主索引文件。记录 batch_id、成功/失败数量、最佳策略、每个策略的排名、得分、核心指标和对应 search 产物路径。

batch_policy_evaluation_summary.md
  人类可读横向比较报告，用于快速查看多个预算策略的排名和核心表现。

batch_policy_evaluation_summary.csv
  扁平化横向比较表格，便于人工排序、筛选和后续程序读取。
```

其中每个策略自己的完整评估产物仍写入：

```text
artifacts/budget_runs/{budget_run_id}/policies/searches/{search_id}/
```

## 执行后检查

单策略评估完成后检查：

```text
1. search_result.json 是否存在。
2. attempt_summary.json 和 attempt_summary.md 是否存在。
3. stage_attribution.json、stage_attribution.csv、stage_attribution.md 是否存在。
4. best_score、full_sharpe、validation_sharpe、walk_forward_mean_score 是否有值。
5. budget_run_state.json 的 artifacts.policies.searches.{search_id} 是否登记完整路径。
```

批量评估完成后检查：

```text
1. batch_policy_evaluation_summary.json 是否存在。
2. batch_policy_evaluation_summary.csv 是否存在。
3. success_count、failed_count 是否合理。
4. rank=1 的策略是否有 best_score、attempt_summary_path、stage_attribution_path。
5. budget_run_state.json 的 artifacts.policies.batch_evaluations.{batch_id} 是否登记。
6. 每个成功策略是否登记到 artifacts.policies.searches.{search_id}。
```

## 给 BudgetAgent 的决策规则

```text
如果只有一个预算策略，调用 evaluate-policy。
如果有多个预算策略，调用 batch-evaluate-policies。
不要手动拆分执行内部步骤，除非一次性入口失败且需要调试。
评估完成后，优先阅读 attempt_summary.md、stage_attribution.md 和批量 summary.md。
根据 best_score、validation_sharpe、walk_forward_mean_score、最大回撤、阶段归因结果决定下一轮优化方向。
```
