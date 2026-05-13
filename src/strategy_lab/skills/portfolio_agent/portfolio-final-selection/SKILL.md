---
name: portfolio-final-selection
description: "指导 PortfolioAgent 从已评估的组合层版本中选择最终版本，登记 final_selection，并复制标准化最终文件到 final/ 目录。"
license: Proprietary project skill
---

# Portfolio Final Selection Skill

## 何时使用

当组合层已经产生一个或多个完成评估的 version，并且 PortfolioAgent 需要确定当前最终方案时，使用本 skill。

本 skill 会同时完成两件事：

```text
1. 在 portfolio_run_state.json 中登记最终版本。
2. 将最终版本的关键文件复制到固定 final/ 目录，供后续组合模拟、提交、实盘前验证统一读取。
```

目标版本必须已经完成 `portfolio-evaluation`。如果只生成了五件套但还没评估，不能直接 final-select。

## 核心命令

```powershell
python -m strategy_lab.cli portfolio final-select PORTFOLIO_RUN_STATE_PATH VERSION_ID --reason "最终选择理由"
```

示例：

```powershell
python -m strategy_lab.cli portfolio final-select artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json v003_soft_budget_cap --reason "该版本在 Sharpe、最大回撤、仓位利用率和融合诊断上更均衡，因此登记为组合层最终版本。"
```

## 参数说明

```text
PORTFOLIO_RUN_STATE_PATH
  组合层 portfolio_run_state.json 路径。

VERSION_ID
  选择为最终版的组合层版本 ID。

--reason
  最终选择理由，必填。
  应说明为什么选该版本，而不是其他版本。

--selection-report-path
  可选。最终选择报告输出路径。
  不传时自动写入 reports/portfolio_final_selection_{version_id}_{timestamp}.md。
```

## 内部执行流程

命令会自动完成：

```text
1. 读取 portfolio_run_state.json。
2. 检查 VERSION_ID 是否存在于 versions。
3. 检查该版本是否已经完成 portfolio-evaluation。
4. 检查该版本目录的关键文件是否完整。
5. 生成最终选择报告 portfolio_final_selection_*.md。
6. 创建临时 final_tmp_{version_id}_{timestamp}/。
7. 将所选版本文件复制到临时 final 目录。
8. 生成 final_manifest.json。
9. 如果已有 final/，自动移动到 final_history/final_{old_version_id}_{timestamp}/。
10. 将临时 final 目录切换为正式 final/。
11. 更新 portfolio_run_state.json：
    - final_selection
    - best_version
    - current_version
    - status
    - artifacts.final.portfolio_version
    - artifacts.run_reports
    - events
```

## final/ 标准目录

成功后，当前最终版本始终在：

```text
artifacts/portfolio_runs/{portfolio_run_id}/final/
  final_manifest.json
  portfolio_final_selection.md
  fusion_manifest.json
  fusion_policy.py
  fusion_policy_spec.md
  param_space.json
  fusion_policy_meta.json
  daily_portfolio_agent_prompt.md     如果最终候选版已生成
  daily_decision_contract.json        如果最终候选版已生成
  daily_override_scenarios.md         如果最终候选版已生成
  budget_policy/
    budget_policy_config.json
    budget_policy_spec.md      如果存在
    param_space.json           如果存在
    budget_policy_meta.json    如果存在
  signal_strategies/
    {symbol}/
      strategy.py
      strategy_spec.md
      param_space.json
      strategy_meta.json
      strategy_params.json
      metrics.json             如果存在
  evaluation/
    evaluation_manifest.json
    evaluation_summary.md
    daily_budget_weights.parquet
    daily_signal_targets.parquet
    daily_final_weights.parquet
    fusion_diagnostics.json
    fusion_diagnostics.md
    fusion_diagnostics.parquet
    fusion_asset_diagnostics.parquet
    backtest/
      metrics.json
      equity_curve.parquet
      orders.parquet
      holdings.parquet
      budget_vs_benchmark.png  如果生成了图
```

后续模块优先读取：

```text
artifacts/portfolio_runs/{portfolio_run_id}/final/final_manifest.json
```

## final_manifest.json

`final_manifest.json` 是最终版本统一入口，至少包含：

```text
portfolio_run_id
selected_version_id
selected_at
reason
source_version_dir
final_dir
fusion_manifest_path
fusion_policy_path
fusion_policy_spec_path
param_space_path
fusion_policy_meta_path
daily_portfolio_agent_prompt_path
daily_decision_contract_path
daily_override_scenarios_path
budget_policy_config_path
signal_strategies_dir
evaluation_manifest_path
metrics_path
fusion_diagnostics_json_path
fusion_diagnostics_report_path
fusion_diagnostics_path
fusion_asset_diagnostics_path
daily_budget_weights_path
daily_signal_targets_path
daily_final_weights_path
selection_report_path
```

## 重新选择最终版本

可以再次调用本 skill 更换最终版本。服务会自动：

```text
1. 将旧 final/ 移动到 final_history/。
2. 用新选择的版本重新生成 final/。
3. 更新 final_selection 和 artifacts.final。
```

因此 `final/` 永远指向当前最终方案，旧最终方案不会直接丢失。

## 常见失败原因

```text
version_id 不存在
版本尚未完成 portfolio-evaluation
版本目录缺少 fusion_manifest.json
版本目录缺少 fusion_policy.py
版本目录缺少 budget_policy/budget_policy_config.json
版本目录缺少 signal_strategies/
版本目录缺少 evaluation/backtest/metrics.json
版本目录缺少 evaluation/daily_final_weights.parquet
版本目录缺少 evaluation/fusion_diagnostics.json
```

失败时不要手工改 `portfolio_run_state.json`，应先补齐缺失文件或重新执行 `portfolio-evaluation`。

## 检查清单

执行后必须检查：

```text
1. CLI 返回 status 为 success。
2. final/final_manifest.json 存在且 JSON 可解析。
3. final/fusion_policy.py 存在。
4. final/fusion_manifest.json 存在。
5. final/budget_policy/budget_policy_config.json 存在。
6. final/signal_strategies/ 存在。
7. final/evaluation/fusion_diagnostics.json 存在。
8. final/evaluation/backtest/metrics.json 存在。
9. 如果所选版本已经生成 DailyPortfolioAgent 三件套，final/ 下也必须存在 daily_portfolio_agent_prompt.md、daily_decision_contract.json、daily_override_scenarios.md。
10. 如果 DailyPortfolioAgent 三件套存在，final_manifest.json 中对应路径字段不能为 null。
11. portfolio_run_state.json 的 final_selection.status 为 selected。
12. portfolio_run_state.json 的 final_selection.version_id 等于 VERSION_ID。
13. artifacts.final.portfolio_version 已登记。
14. events 中有 portfolio_final_selection_completed。
```
