---
name: portfolio-data-split
description: "指导 PortfolioAgent 对组合层已有多资产行情面板生成 full-only 或 train/validation/walk-forward 数据切分。"
license: Proprietary project skill
---

# Portfolio Data Split Skill

## 何时使用

当 PortfolioAgent 已经通过 DataAgent 获取或整理好组合层数据后，使用本 skill。

本 skill 不负责拉取数据。数据应由 DataAgent 保存到组合层 run 的 `data/` 目录，或由调用方显式传入文件路径。

组合层数据通常包括：

```text
panel_ohlcv.parquet
returns_wide.parquet
```

格式约定：
- `panel_ohlcv.parquet` 是长表，至少包含 `symbol, datetime, open, high, low, close, volume, pctchange`。
- `returns_wide.parquet` 是收益率宽表，推荐 `datetime` 作为 index，列名为完整资产代码，例如 `159819.SZ`、`512880.SH`。
- 服务会自动兼容 `returns_wide.parquet` 中 `datetime` 是普通列的情况，并在读取时转为 index。
- 服务会参考组合层已复制的信号层资产代码，把 `159819`、`512880` 这类无后缀代码尽量映射回 `159819.SZ`、`512880.SH`。
- 尽管服务有容错，DataAgent 生成数据时仍应优先直接产出标准格式。

## 两种模式

```text
full-only
  只生成全样本 manifest，不拆 train/validation/walk-forward。
  适合用户只想对某个时间范围做完整融合回测。

train-validation-walk-forward
  生成 train、validation 和 walk-forward。
  适合后续要优化预算层或信号层。
```

## 命令

```powershell
python -m strategy_lab.cli portfolio split-data PORTFOLIO_RUN_STATE_PATH [OPTIONS]
```

必填参数：

```text
PORTFOLIO_RUN_STATE_PATH
  组合层状态文件，例如：
  artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json
```

常用参数：

```text
--panel-ohlcv-path
  可选。覆盖 panel_ohlcv.parquet 路径。
  如果不传，服务会从 portfolio_run_state.json 的 data.panel_ohlcv 读取。

--returns-wide-path
  可选。覆盖 returns_wide.parquet 路径。
  如果不传，服务会从 portfolio_run_state.json 的 data.returns_wide 读取。

--output-dir
  可选。切分输出目录。不传则写入当前 portfolio run 的 data/splits。

--split-mode
  full-only 或 train-validation-walk-forward。
  默认 train-validation-walk-forward。

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

## 调用示例

纯完整回测，不做切分：

```powershell
python -m strategy_lab.cli portfolio split-data artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json --panel-ohlcv-path artifacts\portfolio_runs\portfolio_xxx\data\panel_ohlcv.parquet --returns-wide-path artifacts\portfolio_runs\portfolio_xxx\data\returns_wide.parquet --split-mode full-only
```

优化任务，生成完整切分：

```powershell
python -m strategy_lab.cli portfolio split-data artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json --panel-ohlcv-path artifacts\portfolio_runs\portfolio_xxx\data\panel_ohlcv.parquet --returns-wide-path artifacts\portfolio_runs\portfolio_xxx\data\returns_wide.parquet --split-mode train-validation-walk-forward --fold-count 3
```

## 输出目录

默认输出：

```text
artifacts/portfolio_runs/{portfolio_run_id}/data/splits/
  split_manifest.json
  train_panel_ohlcv.parquet                 train-validation-walk-forward 模式才有
  train_returns_wide.parquet                train-validation-walk-forward 模式才有
  validation_panel_ohlcv.parquet            train-validation-walk-forward 模式才有
  validation_returns_wide.parquet           train-validation-walk-forward 模式才有
  walk_forward/                             train-validation-walk-forward 模式才有
    fold_001/
      context_panel_ohlcv.parquet
      context_returns_wide.parquet
      train_panel_ohlcv.parquet
      train_returns_wide.parquet
      validation_panel_ohlcv.parquet
      validation_returns_wide.parquet
```

## split_manifest.json

`split_manifest.json` 是后续组合层评估的统一数据入口。

两种模式都会包含：

```text
split_mode
source_panel_ohlcv_path
source_returns_wide_path
full_panel_path
full_returns_path
summary
```

`train-validation-walk-forward` 模式还会包含：

```text
train_panel_path
train_returns_path
validation_panel_path
validation_returns_path
walk_forward_dir
folds
```

## PortfolioAgent 使用要点

- 如果用户只是要求“完整跑一遍组合回测”，优先使用 `--split-mode full-only`，速度更快。
- 如果用户要求优化预算层或信号层，必须使用 `--split-mode train-validation-walk-forward`。
- 后续 `portfolio-evaluation` 应优先读取本 skill 生成的 `split_manifest.json`，不要自行临时切分。
- 如果 DataAgent 已经把数据文件路径写入 `portfolio_run_state.json` 的 `data.panel_ohlcv` 和 `data.returns_wide`，可以不传 `--panel-ohlcv-path` 和 `--returns-wide-path`。

## 检查清单

执行后检查：

```text
1. CLI 返回 split_mode 和 manifest_path。
2. split_manifest.json 存在。
3. portfolio_run_state.json 的 data.split_manifest 已更新。
4. artifacts.datasets.portfolio_splits 已登记。
5. full-only 模式下 full_panel_path / full_returns_path 有值。
6. train-validation-walk-forward 模式下 train、validation 和 folds 文件路径完整。
```
