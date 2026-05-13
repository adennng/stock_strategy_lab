---
name: budget-final-selection
description: "指导 BudgetAgent 在预算层训练结束时登记最终选择的预算策略，生成最终选择报告并更新 budget_run_state.json。"
license: Proprietary project skill
---

# Budget Final Selection Skill

## 何时使用

当预算层已经完成足够的策略评估、单策略复盘或多策略横向比较，需要把某个预算策略正式登记为最终候选时，使用本 skill。

本 skill 只做最终登记和收口，不负责重新评估策略。最终选择前，通常应已经完成：

```text
budget-policy-evaluation
BudgetCriticAgent 单策略复盘或多策略横向比较
BudgetAgent 自己的最终判断
```

## 核心命令

```powershell
python -m strategy_lab.cli budget final-select BUDGET_RUN_STATE_PATH SEARCH_ID --reason "最终选择理由"
```

示例：

```powershell
python -m strategy_lab.cli budget final-select artifacts\budget_runs\budget_xxx\budget_run_state.json budget_policy_batch_001_budget_policy_003_defensive_low_vol --reason "该策略在 validation 和 walk-forward 中稳定性最好，最大回撤可控，阶段归因显示弱势阶段保护更好，因此选为预算层最终候选。"
```

如果选择理由较长，先写成文件，再调用：

```powershell
python -m strategy_lab.cli budget final-select artifacts\budget_runs\budget_xxx\budget_run_state.json budget_policy_003 --reason-file artifacts\budget_runs\budget_xxx\reports\final_selection_reason.md
```

## 参数说明

```text
BUDGET_RUN_STATE_PATH
  预算层状态文件，一般是 artifacts/budget_runs/{budget_run_id}/budget_run_state.json。

SEARCH_ID
  要最终选择的预算策略 search_id。必须已经存在于 budget_run_state.json 的 artifacts.policies.searches 中。

--reason
  最终选择理由。建议明确引用关键证据，例如 Sharpe、收益、最大回撤、validation、walk-forward、阶段归因、换手、策略复杂度和 BudgetCriticAgent 建议。

--reason-file
  从文件读取最终选择理由。理由较长时优先使用。

--selection-report-path
  可选，指定最终选择报告输出路径。不传时，系统会自动写入 budget run 的 reports 目录。

--report-key
  可选，指定 artifacts.run_reports 中的报告键名。不传时使用最终选择报告文件名。

--score
  可选，覆盖最终 best_score。不传时使用该 search_id 已登记的 best_score。
```

## 服务会自动更新什么

命令成功后，系统会自动：

```text
1. 读取 artifacts.policies.searches.{search_id}
2. 生成 budget_final_selection_*.md 最终选择报告
3. 写入 final_selection
4. 写入 artifacts.policies.final
5. 写入 artifacts.run_reports
6. 更新 strategy_search.best_attempt_id、strategy_search.best_search_id、strategy_search.best_score
7. 将 budget_run_state.json 顶层 status 改为 completed
8. 向 events 追加 budget_final_selection_completed
```

## 输出内容

默认最终选择报告会写在：

```text
artifacts/budget_runs/{budget_run_id}/reports/budget_final_selection_{search_id}_{timestamp}.md
```

报告包含：

```text
- budget_run_id
- selected_at
- search_id
- policy_name
- best_score
- 选择理由
- 关键指标摘要
- 关键文件路径
- 检查结论
```

`budget_run_state.json` 中的 `final_selection` 会包含：

```text
status
search_id
policy_name
policy_config_path
search_result_path
attempt_summary_path
attempt_summary_md_path
stage_attribution_path
stage_attribution_md_path
stage_attribution_chart_path
selection_report_path
reason
best_score
summary
selected_at
```

## 最终选择建议

最终选择不是简单看 full 总收益。优先顺序建议：

```text
1. validation 和 walk-forward 稳定性
2. Sharpe 和收益风险比
3. 最大回撤和弱势阶段保护
4. full 样本表现
5. 换手和交易成本敏感性
6. 持仓集中度、分散度和约束合理性
7. 策略复杂度和可解释性
8. BudgetCriticAgent 的复盘或横向比较建议
```

如果最优收益策略在样本外、滚动验证或阶段归因中明显不稳，不应直接作为最终选择。

## 检查清单

执行后必须检查：

```text
1. CLI 返回 status=success。
2. budget_run_state.json 的 final_selection.status 为 success。
3. final_selection.search_id 等于本次选择的 search_id。
4. final_selection.selection_report_path 对应文件存在。
5. artifacts.policies.final 已登记。
6. artifacts.run_reports 已登记最终选择报告。
7. events 中有 budget_final_selection_completed。
```

