---
name: budget-data-split
description: "为 BudgetAgent 基于预算层统一多资产行情面板生成 train、validation 和 walk-forward 数据切分。"
license: Proprietary project skill
---

# Budget Data Split Skill

## 适用场景

在 `budget-data-panel` 已生成 `panel_ohlcv.parquet` 和 `returns_wide.parquet` 后使用本 skill。

预算层是多资产组合任务，因此切分必须按统一交易日期切分，而不是按长表行数切分。所有资产共享同一组 train、validation 和 walk-forward 日期窗口。

本 skill 会：

```text
1. 读取 budget_run_state.json。
2. 自动定位 data_panel.panel_ohlcv 和 data_panel.returns_wide。
3. 按 returns_wide 的日期索引生成全样本 train/validation 切分。
4. 生成扩展窗口 walk-forward 切分。
5. 同时写出 panel 长表切片和 returns 宽表切片。
6. 写出 split_manifest.json。
7. 更新 budget_run_state.json 的 data_split 和 artifacts.datasets.budget_splits。
```

## 命令

```powershell
python -m strategy_lab.cli budget split-data BUDGET_RUN_STATE_PATH [OPTIONS]
```

必填参数：

```text
BUDGET_RUN_STATE_PATH
  预算层任务状态文件路径，例如：
  artifacts\budget_runs\budget_xxx\budget_run_state.json
```

可选参数：

```text
--panel-ohlcv-path
  可选。覆盖 panel_ohlcv.parquet 路径。不传则从 budget_run_state.json 的 data_panel.panel_ohlcv 读取。

--returns-wide-path
  可选。覆盖 returns_wide.parquet 路径。不传则从 budget_run_state.json 的 data_panel.returns_wide 读取。

--output-dir
  可选。切分输出目录。不传则写入当前 budget run 的 data/splits。

--train-ratio
  全样本 train/validation 切分中的训练日期比例。
  默认 0.70。

--fold-count
  walk-forward fold 数量。
  默认 3。

--fold-train-ratio
  walk-forward 中每个 fold 的最小训练窗口日期比例。
  默认 0.60。

--fold-validation-ratio
  walk-forward 中每个 fold 的验证窗口日期比例。
  默认 0.20。

--min-train-dates
  每个训练窗口最少交易日数量。
  默认 120。

--min-validation-dates
  每个验证窗口最少交易日数量。
  默认 40。
```

## 输出目录

默认输出：

```text
artifacts/budget_runs/{budget_run_id}/data/splits/
  train_panel_ohlcv.parquet
  train_returns_wide.parquet
  validation_panel_ohlcv.parquet
  validation_returns_wide.parquet
  split_manifest.json
  walk_forward/
    fold_001/
      context_panel_ohlcv.parquet
      context_returns_wide.parquet
      train_panel_ohlcv.parquet
      train_returns_wide.parquet
      validation_panel_ohlcv.parquet
      validation_returns_wide.parquet
```

文件说明：

```text
train_panel_ohlcv.parquet
  训练区间的多资产 OHLCV 长表。

train_returns_wide.parquet
  训练区间的多资产收益率宽表。预算策略参数搜索通常读取这个文件。

validation_panel_ohlcv.parquet
  验证区间的多资产 OHLCV 长表。

validation_returns_wide.parquet
  验证区间的多资产收益率宽表。预算策略候选应在这里做样本外验证。

context_panel_ohlcv.parquet / context_returns_wide.parquet
  walk-forward fold 中的上下文数据，等于该 fold 的 train + validation。
  用于让动量、波动率、相关性等指标在验证段开头有足够历史窗口。

split_manifest.json
  切分清单。后续预算层画像、策略搜索和组合回测应优先读取它，避免各环节自行切分导致不一致。
```

## walk-forward 含义

walk-forward 是时间序列策略验证方法：

```text
fold_001: 用早期历史训练，随后一段验证。
fold_002: 训练窗口向后扩展，再用后面一段验证。
fold_003: 继续扩展训练窗口，再验证更靠后的区间。
```

它用于检查预算策略是否只在某一个时间段有效，还是在多个滚动样本外区间都有稳定表现。

## 调用示例

默认切分：

```powershell
python -m strategy_lab.cli budget split-data artifacts\budget_runs\budget_xxx\budget_run_state.json
```

增加 walk-forward fold 数：

```powershell
python -m strategy_lab.cli budget split-data artifacts\budget_runs\budget_xxx\budget_run_state.json --fold-count 5
```

调整训练/验证比例：

```powershell
python -m strategy_lab.cli budget split-data artifacts\budget_runs\budget_xxx\budget_run_state.json --train-ratio 0.75 --fold-train-ratio 0.65 --fold-validation-ratio 0.15
```

## BudgetAgent 使用要点

- 运行前确认 `budget_run_state.json -> data_panel.panel_ohlcv` 和 `data_panel.returns_wide` 已存在。
- 运行后读取 `split_manifest.json`，后续服务不要自行重新切分。
- 如果资产上市时间不同，`returns_wide` 早期可能有 NaN；这是正常情况，后续预算策略和回测需要把这些资产视为当时不可交易或不可评分。
