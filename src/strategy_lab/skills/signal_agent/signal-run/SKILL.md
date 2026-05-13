---
name: signal-run
description: "为 SignalAgent 创建和理解信号层任务运行状态，包括标准 signal run 目录、run_state.json、任务元数据、数据目录、市场画像目录、attempts 目录、最终策略目录、run 级报告目录和 backtest_config。"
license: Proprietary project skill
---

# Signal Run Skill

## 适用场景

当 SignalAgent 收到新的单资产或单指数策略探索任务时，第一步应创建 signal run。

典型任务：

```text
为 000300.SH 在 2024-01-01 到 2024-12-31 探索一个信号层择时策略
```

此时先调用 `signal new-run`，生成标准目录和 `run_state.json`。后续数据获取、市场画像、策略生成、参数搜索、回测、复盘、最终选择都围绕这个状态文件和目录执行。

## 核心原则

- 一个 signal run 只对应一个目标资产或指数。
- SignalAgent 始终以 `run_state.json` 作为任务状态源。
- 顶层不放单次回测和单轮复盘；它们都属于某个 attempt。
- `reports/` 只放 run 级汇总报告，不放市场画像、不放单次回测报告、不放单轮复盘报告。
- 不要在项目中随意新建散乱目录；统一使用 signal run 标准目录。

## 创建命令

命令格式：

```powershell
python -m strategy_lab.cli signal new-run SYMBOL --start-date START_DATE --end-date END_DATE [OPTIONS]
```

必填参数：

```text
SYMBOL
  目标资产代码，例如 000300.SH、600519.SH、510300.SH。

--start-date
  数据开始日期，格式 YYYY-MM-DD。

--end-date
  数据结束日期，格式 YYYY-MM-DD。
```

常用可选参数：

```text
--asset-type
  资产类型，例如 index、stock、etf。默认 index。

--frequency
  数据频率，例如 1d、5m。默认 1d。

--task
  自然语言任务描述。

--task-name
  可读任务名；不填则使用 run_id。

--run-id
  可选。手动指定 run_id；不填则系统按 symbol 和时间戳生成。

--initial-cash
  覆盖本次任务初始资金。

--commission
  覆盖本次任务佣金比例。

--slippage-perc
  覆盖本次任务滑点比例。

--allow-short / --no-allow-short
  覆盖本次任务是否允许做空。

--strategy-max-iterations
  本次策略探索最多允许生成和评估的 attempt 数，写入
  run_state.json 的 steps.strategy_search.max_iterations。
  不填时默认 20。该参数是业务层策略探索次数限制，不是 DeepAgents recursion_limit。
```

## 生成目录

创建成功后会生成：

```text
artifacts/signal_runs/{run_id}/
  run_state.json
  data/
  market_profile/
  attempts/
  strategies/
  reports/
  logs/
```

目录用途：

```text
data
  主数据文件、过程数据文件、dataset_manifest.json。

market_profile
  市场画像产物，例如 market_profile.json、market_profile.md、market_profile_chart.png。

attempts
  每一轮策略探索的完整过程。每个 attempt 内部包含 strategy、optimization、backtests、review、logs。

strategies
  最终沉淀策略目录。推荐使用 strategies/accepted/ 保存最终入选策略。

reports
  run 级汇总报告，例如 run_summary.md、attempts_comparison.csv、final_selection_report.md、final_strategy_card.md。

logs
  全局执行日志、调试日志。
```

推荐 attempt 目录结构：

```text
attempts/attempt_001/
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
    validation_summary.json
    walk_forward_summary.json

  backtests/
    full/
    train/
    validation/
    walk_forward/
      fold_001/
      fold_002/

  review/
    critic_review.md
    critic_review.json
    next_strategy_brief.md

  logs/
```

## run_state.json 关键字段

```text
schema_version
  当前版本为 0.2.0。

run_id
  本次 signal run 的唯一 ID。

task
  任务描述、目标资产、数据范围、频率、优化目标。

directories
  本次任务所有标准目录：root、data、market_profile、attempts、strategies、reports、logs。

backtest_config
  本次任务默认回测参数，从 configs/backtest.yaml 复制，并可被 signal new-run 参数覆盖。

steps.data_acquisition
  数据获取状态和数据产物路径。

steps.market_profile
  市场画像状态和画像产物路径。

steps.strategy_search
  策略探索状态、当前 attempt、attempt 数量、最佳 attempt、最佳分数、最大迭代次数。

steps.final_selection
  最终选择的 attempt、策略路径和选择理由。

attempts
  每轮策略探索的索引列表。详细文件仍以 attempts/{attempt_id}/ 目录为准。

artifacts
  run 级产物索引，包括 datasets、market_profile、accepted_strategies、run_reports。

events
  任务过程事件日志。
```

## 后续调用建议

创建 signal run 后，应把 `run_state.json` 路径传给后续步骤：

```text
DataAgent
  获取数据时读取 run_state.json，并把最终主数据放到 directories.data。

MarketProfile
  读取 primary_dataset，生成画像文件到 directories.market_profile。

Backtest
  SignalAgent 主流程中应显式传 --output-dir，把回测结果写到 attempts/{attempt_id}/backtests/full、train、validation 或 walk_forward/fold_xxx。

CriticAgent
  读取 run_state.json 和 attempts/{attempt_id}/，生成复盘报告到 attempts/{attempt_id}/review。
```

## 注意事项

- `run_id` 推荐由系统生成，避免重名；只有在需要复现或测试时才手动指定。
- 如果已存在同名 run_id，创建命令会复用同名目录并覆盖 `run_state.json`，所以正式任务应避免重复 run_id。
- `backtest_config` 是本次任务级默认参数；单次回测命令显式传参仍可覆盖它。
- 顶层 `reports/` 只放 run 级汇总报告，不要把 attempt 级报告放进去。

## 最终选择登记

最终选择不要手动编辑 `run_state.json`。应使用：

```powershell
python -m strategy_lab.cli signal final-select RUN_STATE_PATH ATTEMPT_ID --reason "选择理由"
```

调用示例：

```powershell
python -m strategy_lab.cli signal final-select artifacts\signal_runs\signal_000300_20260508_120000\run_state.json attempt_003 --reason "attempt_003 已完成复盘；Full Sharpe、validation 交易有效性和 walk-forward 稳定性在候选中相对最好，最大回撤可接受，因此选为最终策略。"
```

可选参数：

```text
--strategy-path
  最终策略文件路径；不传则使用 attempts/{attempt_id}/strategy/strategy.py。

--metrics-path
  最终指标文件路径；不传则使用 attempts/{attempt_id}/backtests/full/metrics.json。

--reason
  最终选择理由，建议写明 Sharpe、walk-forward 稳定性、最大回撤、交易有效性和复盘结论。

--reason-file
  从文件读取较长的最终选择理由。

--score
  最终分数；不传则使用 run_state.json 中该 attempt 的 score。
```

该命令会统一更新：

```text
status
steps.final_selection
steps.strategy_search.status
steps.strategy_search.best_attempt_id
steps.strategy_search.best_score
artifacts.accepted_strategies.final
events
```

最终选择前，应确认拟选择的 attempt 已完成 CriticAgent 复盘；如果 review 目录没有 `critic_review.md` 和 `critic_review.json`，先补单 attempt 复盘。
