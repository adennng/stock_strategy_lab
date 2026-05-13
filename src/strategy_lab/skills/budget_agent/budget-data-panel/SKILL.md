---
name: budget-data-panel
description: "为 BudgetAgent 汇总已登记的信号层资产行情数据，生成预算层统一 OHLCV 面板、收益率宽表和数据清单。"
license: Proprietary project skill
---

# Budget Data Panel Skill

## 适用场景

在 `budget-run` 已完成后使用本 skill。它读取 `budget_run_state.json` 和
`signal_artifacts_manifest.json`，把每个资产的信号层 `primary_dataset` 汇总为预算层统一数据。

本 skill 会：

```text
1. 读取 budget_run_state.json。
2. 读取 signal_artifacts_manifest.json 中登记的每个资产 primary_dataset。
3. 如存在 data/supplemental/{symbol} 补充数据，也一并读取。
4. 按 task.data_range 截取。
5. 标准化字段并生成小数收益率 return。
6. 写出 panel_ohlcv.parquet、returns_wide.parquet、data_panel_manifest.json。
7. 更新 budget_run_state.json 的 data_panel 和 artifacts.datasets.budget_panel。
```

## 命令

```powershell
python -m strategy_lab.cli budget data-panel BUDGET_RUN_STATE_PATH [OPTIONS]
```

必填参数：

```text
BUDGET_RUN_STATE_PATH
  预算层任务状态文件路径，例如：
  artifacts\budget_runs\budget_xxx\budget_run_state.json
```

可选参数：

```text
--output-dir
  输出目录。不传时默认写入当前 budget run 的 data 目录。

--allow-pending-missing-data
  当 budget_run_state.json 中仍存在 data_panel.missing_data 时，允许继续生成临时面板。
  默认不允许。正常流程应先让 DataAgent 补齐缺失数据，再运行本 skill。

--include-supplemental / --no-include-supplemental
  是否读取 data/supplemental/{symbol} 下的补充数据。
  默认读取。

--min-rows-per-asset
  每个资产最少有效行数。低于该值不会中止，但会写入 warning。
  默认 20。
```

## 输出文件

默认生成在：

```text
artifacts/budget_runs/{budget_run_id}/data/
  panel_ohlcv.parquet
  returns_wide.parquet
  data_panel_manifest.json
```

文件说明：

```text
panel_ohlcv.parquet
  多资产长表。每行是一个资产在一个交易日的 OHLCV 数据。
  标准字段包括：
  symbol, datetime, open, high, low, close, volume, pctchange, return, source

returns_wide.parquet
  多资产收益率宽表。index 是 datetime，columns 是 symbol，值是小数收益率 return。
  预算层画像、参数搜索和组合回测优先读取这个文件。

data_panel_manifest.json
  数据面板清单。记录每个资产读取了哪些文件、实际覆盖日期、行数、缺失警告、最终输出路径和字段。
```

## 收益率说明

```text
pctchange
  保留原始数据中的涨跌幅字段，通常是百分数单位，例如 -2.54 表示 -2.54%。

return
  统一生成的小数收益率字段，例如 -0.0254。
  returns_wide.parquet 使用 return 字段。
```

如果原始数据缺少 `pctchange`，服务会用 `close.pct_change()` 计算。

## 缺数据处理

如果 `budget_run_state.json -> data_panel.missing_data` 仍有内容，默认会中止，并提示先补数据。

正常流程：

```text
1. 阅读 data_panel.missing_data。
2. 调用 DataAgent 按 suggested_output_dir 和 suggested_file_name 补齐数据。
3. 重新运行 budget-data-panel。
```

临时分析流程：

```text
如果用户明确接受部分资产前段或后段缺失，可以加 --allow-pending-missing-data。
这会生成 status=partial 的面板，manifest 中会保留 warning。
后续预算层回测必须能处理某些资产在部分日期不可交易的情况。
```

## 调用示例

正常生成：

```powershell
python -m strategy_lab.cli budget data-panel artifacts\budget_runs\budget_xxx\budget_run_state.json
```

允许缺数据时生成临时面板：

```powershell
python -m strategy_lab.cli budget data-panel artifacts\budget_runs\budget_xxx\budget_run_state.json --allow-pending-missing-data
```

指定输出目录：

```powershell
python -m strategy_lab.cli budget data-panel artifacts\budget_runs\budget_xxx\budget_run_state.json --output-dir artifacts\budget_runs\budget_xxx\data
```

## BudgetAgent 使用要点

- 运行前先检查 `budget_run_state.json -> data_panel.status`。
- 如果状态是 `needs_data`，优先补齐数据；只有用户明确允许时才使用 `--allow-pending-missing-data`。
- 运行后阅读 `data_panel_manifest.json`，确认每个资产的实际覆盖日期和 warning。
- 后续 `budget-data-split` 和 `budget-profile` 应读取本 skill 生成的 `panel_ohlcv.parquet` 和 `returns_wide.parquet`。
