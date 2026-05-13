---
name: data-split
description: "为 SignalAgent 生成时间序列训练集、验证集和 walk-forward 验证切分，供参数搜索、遗传算法评估、复盘和稳健性检查使用。"
license: Proprietary project skill
---

# Data Split Skill

## 适用场景

当 SignalAgent 已经拿到目标资产的 OHLCV 主数据文件后，应先生成标准数据切分，再进入策略参数搜索或遗传算法评估。后续服务应读取同一份 `split_manifest.json`，避免各环节自行切分导致口径不一致。

## 命令

```powershell
python -m strategy_lab.cli signal split-data [DATA_PATH] --run-state-path RUN_STATE_PATH [OPTIONS]
```

如果传了 `DATA_PATH`，服务使用该文件作为源数据；如果不传 `DATA_PATH`，服务从 `run_state.json` 的 `steps.data_acquisition.primary_dataset` 读取主数据文件。

常用参数：

```text
--run-state-path
  signal run 的 run_state.json。推荐传入。

--output-dir
  切分产物输出目录。不传且提供 run_state_path 时，默认写入 artifacts/signal_runs/{run_id}/data/splits。

--train-ratio
  全样本 train/validation 切分中的训练集比例，默认 0.70。

--fold-count
  walk-forward fold 数量，默认 3。

--fold-train-ratio
  walk-forward 中每个 fold 的最小训练窗口比例，默认 0.60。

--fold-validation-ratio
  walk-forward 中每个 fold 的验证窗口比例，默认 0.20。

--min-train-rows
  每个训练窗口最少行数，默认 60。

--min-validation-rows
  每个验证窗口最少行数，默认 20。
```

## 输出产物

默认输出目录：

```text
artifacts/signal_runs/{run_id}/data/splits/
  train.parquet
  validation.parquet
  split_manifest.json
  walk_forward/
    fold_001/
      context.parquet
      train.parquet
      validation.parquet
    fold_002/
      context.parquet
      train.parquet
      validation.parquet
```

`context.parquet = train + validation`。它用于 walk-forward 回测：训练段数据只作为策略计算指标的历史上下文，真正下单和计分只发生在 `evaluation_start` 到 `evaluation_end`。

`split_manifest.json` 会记录源数据、切分参数、每个数据文件路径、每段日期范围、行数、字段列表、`context_path`、`evaluation_start` 和 `evaluation_end`。后续参数搜索服务应优先读取这个 manifest。

## 切分原则

- 所有切分都按 `datetime` 升序执行，不随机打乱。
- `train.parquet` 和 `validation.parquet` 是全样本的一次性训练/验证切分。
- `walk_forward` 使用扩展训练窗口：越往后的 fold 使用越长的历史训练区间，并用随后的连续区间做验证。
- 每个 walk-forward fold 都会生成 `context.parquet`，用于避免均线、RSI、波动率等指标在验证段开头缺少历史。

## 使用示例

只传 `run_state.json`，让服务自动读取主数据并写入标准目录：

```powershell
python -m strategy_lab.cli signal split-data --run-state-path artifacts/signal_runs/signal_000300_20260506_210000/run_state.json
```

显式指定源数据和 fold 数：

```powershell
python -m strategy_lab.cli signal split-data artifacts/signal_runs/signal_000300_20260506_210000/data/000300.parquet --run-state-path artifacts/signal_runs/signal_000300_20260506_210000/run_state.json --fold-count 5
```
