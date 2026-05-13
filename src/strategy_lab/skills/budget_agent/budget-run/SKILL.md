---
name: budget-run
description: "为 BudgetAgent 创建预算层任务，扫描已有信号层 run 目录，复制信号层最终策略小文件，并生成标准 budget_run_state.json。"
license: Proprietary project skill
---

# Budget Run Skill

## 适用场景

当 BudgetAgent 收到新的预算层训练任务时，第一步使用本 skill。

本项目的预算层默认建立在已完成的信号层结果之上。用户通常会给出一个或多个信号层结果目录，例如：

```text
artifacts\signal_runs
```

也可以给出多个目录或单个 `run_state.json`，用于只纳入部分资产。

本 skill 会：

```text
1. 创建 budget run 标准目录。
2. 递归扫描 source_paths 下的所有 signal run_state.json。
3. 读取每个资产的代码、数据范围、原始主数据路径和最终策略信息。
4. 检查信号层最终策略材料是否完整。
5. 按资产代码分组复制信号层小文件到当前 budget run。
6. 生成 asset_pool_manifest.json 和 signal_artifacts_manifest.json。
7. 写入 budget_run_state.json。
```

## 创建命令

命令格式：

```powershell
python -m strategy_lab.cli budget new-run POOL_NAME --source-paths SOURCE_PATHS [OPTIONS]
```

必填参数：

```text
POOL_NAME
  资产池名称，例如 sector_etf_pool、macro_pool、custom_pool。

--source-paths
  信号层结果来源路径，逗号分隔。
  可以传：
  - 一个 signal_runs 总目录
  - 多个 signal_runs 子目录
  - 一个或多个 signal run_state.json 文件
```

常用可选参数：

```text
--mode
  预算层运行模式：
  standalone：预算层单独训练，默认模式。此时先令 S_i = 1。
  signal_joint：冻结信号层后联合训练，最终仓位为 R_i * S_i。
  final_verify：最终端到端验证。

--start-date / --end-date
  用来获取数据并训练预算层的数据范围，格式 YYYY-MM-DD。

  如果不传，系统会从所有完整信号层 run 的 data_range 自动取共同交集：
  start = 所有资产开始日期的最大值；end = 所有资产结束日期的最小值。
  这是默认推荐方式，因为预算层需要多资产横向比较，交集区间最稳定。

  如果传了，系统会尊重用户指定范围，并逐个资产检查信号层数据是否覆盖该范围。
  若用户范围超出某些资产已有数据范围，会在 manifest 和 budget_run_state.json 中列出缺口；
  此时不要自己猜测数据是否够用，应读取 data_panel.missing_data 和 data_panel.coverage。

--frequency
  数据频率，默认 1d。

--task
  自然语言任务描述。

--task-name
  可读任务名；不填则使用 budget_run_id。

--run-id
  可选。手动指定 budget_run_id；不填则系统按 pool_name 和时间戳生成。

--benchmark
  本次预算层评估主基准。可选值：

  equal_weight_rebalance
    等权定期再平衡。默认值，适合大多数多资产预算层任务。

  equal_weight_buy_hold
    初始等权买入后不再调仓，用于对比预算层是否优于静态持有。

  simple_momentum_topk
    简单动量 Top-K 基准，用于对比 BudgetAgent 生成策略是否优于朴素轮动。

  cash
    空仓/现金基准，用于观察绝对收益和风险暴露。

--strategy-max-iterations
  本次预算策略探索最多允许生成和评估的 attempt 数，写入
  budget_run_state.json 的 strategy_search.max_iterations。
  不填时读取 configs/budget.yaml，当前默认 20。
```

回测配置覆盖参数：

```text
--initial-cash
  覆盖本次预算层回测初始资金。
  默认值：100000。
  示例：--initial-cash 100000

--commission
  覆盖佣金比例。
  默认值：0.0001，即 0.01%。
  示例：--commission 0.0001

--slippage-perc
  覆盖滑点比例。
  默认值：0.0001，即 0.01%。
  示例：--slippage-perc 0.0001

--execution-price
  覆盖撮合价格口径。
  默认值：close。
  示例：--execution-price close

--allow-short / --no-allow-short
  覆盖是否允许做空。
  默认值：false。
  示例：--no-allow-short

--same-day-sell-cash / --no-same-day-sell-cash
  覆盖当日卖出资金是否可用于当日买入。
  默认值：false。
  示例：--no-same-day-sell-cash
```

## 调用示例

从一个信号层总目录创建预算层任务：

```powershell
python -m strategy_lab.cli budget new-run sector_etf_pool --source-paths artifacts\signal_runs --start-date 2015-01-01 --end-date 2024-12-31 --task "基于已完成的信号层策略训练预算层。"
```

从多个信号层结果目录创建预算层任务：

```powershell
python -m strategy_lab.cli budget new-run selected_pool --source-paths artifacts\signal_runs\signal_512880_SH_xxx,artifacts\signal_runs\signal_512800_SH_xxx --start-date 2018-01-01 --end-date 2024-12-31
```

## 生成目录

创建成功后会生成：

```text
artifacts/budget_runs/{budget_run_id}/
  budget_run_state.json
  data/
    asset_pool_manifest.json
  profile/
  policies/
  attempts/
  reports/
  logs/
  signal_artifacts/
    signal_artifacts_manifest.json
    {symbol}/
      run_state.json
      strategy.py
      strategy_spec.md
      param_space.json
      strategy_meta.json
      signal_agent_memory.md
      metrics.json
```

说明：

```text
signal_artifacts/{symbol}/
  按资产代码分组保存信号层小文件。

primary_dataset
  不复制到预算层目录，只在 manifest 中登记原始路径。后续 budget-data-panel 读取这些路径汇总多资产数据。
```

信号层小文件说明：

```text
run_state.json
  该资产信号层任务的完整状态文件。预算层可从中查看资产代码、数据范围、最终选择的 attempt、数据文件路径、事件记录和各环节产物索引。

strategy.py
  信号层最终选中的可执行策略脚本。预算层不直接修改该文件；在 signal_joint 或 final_verify 场景中，可用它理解该资产自身信号 S_i 的生成逻辑。

strategy_spec.md
  信号层最终策略的自然语言说明，通常包括策略结构、Alpha 主信号、过滤器、出场/风控、仓位映射、适用市场环境和设计理由。预算层画像和复盘时应优先阅读这个文件理解单资产策略含义。

param_space.json
  信号层策略的参数空间或最终参数说明。预算层一般不重新搜索这些参数，但可读取它了解该策略有哪些关键可调参数、参数范围和最终搜索背景。

strategy_meta.json
  信号层策略元数据，通常包括策略名称、策略类名、策略结构标签、创建信息、依赖字段、适用约束等。预算层可用它做资产策略清单、策略类型统计和后续自动加载。

signal_agent_memory.md
  SignalAgent 在单资产策略探索过程中的简要记忆，记录探索阶段、每轮结果、保留或放弃的原因、最终选择理由等。预算层做跨资产分析时可用它理解每个资产策略是如何形成的。

metrics.json
  信号层最终策略的关键回测指标文件。该文件不是强制项；存在时会复制。预算层可用它比较不同资产的信号质量，例如收益、回撤、Sharpe、交易频率等。
```

## 信号层材料完整性要求

每个纳入预算层的资产，必须在对应 signal run 中存在：

```text
1. task.asset.symbol
2. steps.data_acquisition.primary_dataset 或 artifacts.datasets.primary.file
3. 最终选择的 strategy.py
4. strategy_spec.md
5. param_space.json
6. strategy_meta.json
7. reports/signal_agent_memory.md
```

如果存在 `metrics.json`，也会复制到对应资产目录，供预算层画像和后续复盘使用。

缺少任何必需材料时，该 signal run 不会作为完整资产纳入；缺失信息会写入：

```text
data/asset_pool_manifest.json
signal_artifacts/signal_artifacts_manifest.json
budget_run_state.json 的 asset_pool.error 和 signal_artifacts.error
```

状态含义：

```text
success
  所有扫描到的有效 signal run 均完整。

partial
  至少整理出一个完整资产，但存在不完整 signal run 或来源路径警告。

failed
  没有整理出任何完整资产。
```

## 数据范围和补数据提示

预算层数据范围写入：

```text
budget_run_state.json -> task.data_range
data/asset_pool_manifest.json -> data_range
signal_artifacts/signal_artifacts_manifest.json -> data_range
```

如果用户没有传 `--start-date / --end-date`：

```text
系统自动取所有完整资产信号层数据范围的交集。
这通常是最稳妥的默认行为，因为预算层需要多资产横向比较。
```

如果用户传了 `--start-date / --end-date`：

```text
系统以用户指定范围为准。
如果某些资产的信号层 primary_dataset 不覆盖该范围，系统不会自动补数据，而是返回 missing_data 清单。
BudgetAgent 应先向用户说明哪些资产缺少哪些时间段的数据。
如果用户要求继续补齐，BudgetAgent 再调用 DataAgent 获取对应缺失区间。
```

`missing_data` 中会说明：

```text
symbol
missing_start
missing_end
reason
primary_dataset
suggested_output_dir
suggested_file_name
expected_columns
expected_frequency
```

示例：

```json
{
  "symbol": "512880.SH",
  "missing_start": "2014-01-01",
  "missing_end": "2015-01-01",
  "reason": "用户要求的开始日期早于信号层数据开始日期",
  "primary_dataset": "artifacts/signal_runs/signal_xxx/data/512880_daily.parquet",
  "suggested_output_dir": "artifacts/budget_runs/budget_xxx/data/supplemental/512880_SH",
  "suggested_file_name": "512880_SH_20140101_20150101_supplement.parquet",
  "expected_columns": ["symbol", "datetime", "open", "high", "low", "close", "volume", "pctchange"],
  "expected_frequency": "1d"
}
```

`data_panel.coverage` 会按资产列出完整覆盖状态：

```text
covered
  该资产信号层 primary_dataset 已覆盖预算层要求范围。
missing
  该资产存在一个或多个缺失分段，详见 missing_segments。
unknown
  signal run 没有可判断的 data_range，需要人工或 DataAgent 补查。
```

补数据时应遵守：

```text
1. 每个资产的补充数据保存到对应 suggested_output_dir。
2. 推荐文件名使用 suggested_file_name；如确有需要也可使用其他文件名，但必须放在该资产的 suggested_output_dir 下。
3. 补充数据字段至少包含 expected_columns 中列出的字段。
4. 频率应与 expected_frequency 一致，当前预算层默认是日线 1d。
5. 后续 budget-data-panel 会同时读取信号层 primary_dataset 和 supplemental 目录下的补充数据，并在 task.data_range 内合并为统一面板。
```

调用 DataAgent 的自然语言任务示例：

```text
请为预算层任务补齐 512880.SH 的日线 OHLCV 数据，时间范围 2014-01-01 至 2015-01-01。
字段至少包含 symbol、datetime、open、high、low、close、volume、pctchange。
请优先使用 MiniQMT 获取，保存到 artifacts/budget_runs/budget_xxx/data/supplemental/512880_SH，
推荐文件名为 512880_SH_20140101_20150101_supplement.parquet。
保存完成后请返回最终数据文件路径、行数、字段列表和实际覆盖日期。
```

## budget_run_state.json 关键字段

```text
schema_version
  当前版本为 0.1.0。

budget_run_id
  本次 budget run 的唯一 ID。

mode
  standalone、signal_joint 或 final_verify。

task
  任务描述、资产池名称、基准、数据范围、优化目标。

directories
  本次任务所有标准目录：root、data、profile、policies、attempts、reports、logs、signal_artifacts。

asset_pool
  已从信号层结果中整理出的资产列表、来源路径、asset_pool_manifest.json 和缺失信息。

signal_artifacts
  已复制/登记的信号层小文件 manifest 和缺失信息。

data_panel
  后续 budget-data-panel 生成的多资产统一行情数据路径。

data_split
  后续在 task.data_range 范围内生成的 train、validation、walk-forward 切分清单。

budget_profile
  后续生成的资产池画像文件、图表和摘要。

strategy_search
  预算策略探索状态、当前轮次、attempt 列表、最佳 attempt、最大迭代次数。

final_selection
  最终选择的预算策略 attempt、策略路径、选择理由和选择时间。

backtest_config
  本次任务默认回测参数，从 configs/budget.yaml 复制，并可被 budget new-run 参数覆盖。

artifacts
  run 级产物索引，包括 datasets、profile、policies、run_reports。

events
  任务过程事件日志。
```

## 后续流程

创建 budget run 后，不再需要单独资产池整理环节。下一步直接进入：

```text
budget-data-panel
  读取 signal_artifacts_manifest.json 中登记的 primary_dataset 路径，
  汇总为 panel_ohlcv.parquet 和 returns_wide.parquet。

budget-data-split
  在统一面板上生成 train、validation 和 walk-forward 切分。

budget-profile
  基于统一面板生成资产池画像。
```

如果 `budget-run` 报告某些资产缺数据，BudgetAgent 可以调用 DataAgent 补齐数据，然后放入对应资产目录或更新后续数据面板输入；但正常流程下，信号层完成后这些数据应已存在。

## 注意事项

- 用户应传入已经完成信号层训练的目录。
- 如果用户不想纳入某些资产，应传入筛选后的目录，而不是让预算层猜测。
- 同一资产存在多个完整 signal run 时，系统默认选择 `updated_at` 较新的 run。
- 原始主数据不复制，避免大文件冗余；信号层小文件会复制，保证预算层 run 具备独立复查能力。
