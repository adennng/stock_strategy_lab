---
name: multi-policy-comparison
description: "BudgetCriticAgent 用于横向比较多个预算层策略评估结果或批量评估结果，输出排名解释、保留/放弃建议和下一轮预算策略方向。"
license: Proprietary project skill
---

# Multi Policy Comparison Skill

## 适用场景

当调用方要求比较多个预算层策略、复盘批量评估结果、选择继续优化方向时使用本 skill。

典型任务：

```text
请复盘 budget_run_state_path=... 下 batch_id=budget_batch_001 的多策略结果。
请比较 search_a、search_b、search_c，给出下一轮保留哪个预算策略方向。
```

## 必要输入

识别以下任一组输入：

```text
budget_run_state_path + batch_id
budget_run_state_path + search_ids
budget_run_state_path + batch summary 路径
```

如果调用方没有明确 search_ids，应读取 `budget_run_state.json`，优先使用最近的 `artifacts.policies.batch_evaluations`。

## 必须读取的核心文件

优先读取：

```text
budget_run_state.json
policies/batch_evaluations/{batch_id}/batch_policy_evaluation_summary.json
policies/batch_evaluations/{batch_id}/batch_policy_evaluation_summary.md
policies/batch_evaluations/{batch_id}/batch_policy_evaluation_summary.csv
```

对排名靠前、差异明显或需要解释的策略，再读取：

```text
policies/searches/{search_id}/attempt_summary.json
policies/searches/{search_id}/attempt_summary.md
policies/searches/{search_id}/stage_attribution/stage_attribution.json
policies/searches/{search_id}/stage_attribution/stage_attribution.md
policies/searches/{search_id}/best/budget_policy_config.json
policies/searches/{search_id}/best_individual.json
```

## 比较重点

必须覆盖：

```text
1. 横向排名
   解释 rank、best_score、full_sharpe、validation_sharpe、walk_forward_mean_score。

2. 稳健性
   不只看 full 表现，要优先看 validation 和 walk-forward。

3. 回撤与换手
   比较 max_drawdown、average_turnover、transaction cost、阶段性亏损。

4. 策略路线
   比较各策略在 UniverseGate、AssetScorer、AllocationEngine、RiskOverlay、RebalanceScheduler、ConstraintProjector 上的差异。
   同时判断每个策略所属策略族是否匹配预算画像和用户偏好，例如 momentum_rotation、risk_adjusted_rotation、defensive_low_vol、drawdown_control、correlation_aware、risk_parity_like、low_turnover_balanced、concentration_alpha、vol_target_rotation。

5. 阶段适应性
   比较不同策略在哪些市场阶段表现好或差。

6. 画像与偏好冲突
   如果用户偏好与资产池画像冲突，比较不同策略分别代表的取舍，不要只按收益排序。需要说明哪条路线偏收益、哪条路线偏稳健、哪条路线换手更低或集中度更高。

7. 下一轮方向
   最多保留 1 个 primary_policy 和 1 个 fallback_policy；说明保留原因、修改重点和禁止事项。
   同时必须给出阶段推进建议：
   - `stay_current_stage`：继续当前阶段。
   - `advance_next_stage`：进入下一阶段。
   - `rollback_previous_stage`：回退到上一阶段。
   - `return_to_family_exploration`：回到阶段 1 重新探索策略族。
   - `ready_for_final_selection`：进入最终选择。
```

预算策略框架和模块含义可参考：

```text
/src/strategy_lab/skills/budget_agent/budget-policy-authoring/references/budget_policy_library.md
```

## 输出文件

输出到：

```text
artifacts/budget_runs/{budget_run_id}/reports/budget_critic_comparison_{timestamp}/
  budget_policy_comparison.json
  budget_policy_comparison.md
  budget_selection_advice.json
```

`budget_policy_comparison.json` 至少包含：

```json
{
  "status": "success",
  "review_type": "multi_policy_comparison",
  "batch_id": "...",
  "compared_search_ids": [],
  "ranking": [],
  "primary_policy": "...",
  "fallback_policy": "...",
  "primary_family": "...",
  "fallback_family": null,
  "discarded_families": [],
  "recommended_stage_transition": "stay_current_stage | advance_next_stage | rollback_previous_stage | return_to_family_exploration | ready_for_final_selection",
  "discarded_policies": [],
  "profile_fit_ranking": [],
  "preference_conflicts": [],
  "key_findings": [],
  "evidence_files": []
}
```

`budget_selection_advice.json` 至少包含：

```json
{
  "recommendation": "continue_primary | refine_primary | compare_more | ready_for_final_selection",
  "primary_search_id": "...",
  "fallback_search_id": null,
  "primary_family": "...",
  "fallback_family": null,
  "discarded_families": [],
  "recommended_stage_transition": "stay_current_stage | advance_next_stage | rollback_previous_stage | return_to_family_exploration | ready_for_final_selection",
  "next_round_focus": [],
  "parameter_adjustments": [],
  "risk_warnings": [],
  "reason": "..."
}
```

## run_state.json 更新

完成后不要手动编辑 `budget_run_state.json`，必须调用：

```powershell
python -m strategy_lab.cli budget register-run-report BUDGET_RUN_STATE_PATH REPORT_KEY REPORT_PATH --report-type budget_critic_comparison --summary "比较摘要" --extra-json-file EXTRA_JSON_PATH
```

其中 `EXTRA_JSON_PATH` 可包含：

```json
{
  "comparison_json_path": "...",
  "selection_advice_path": "...",
  "batch_id": "...",
  "primary_search_id": "...",
  "fallback_search_id": null
}
```

## 最终检查

完成前必须检查：

```text
1. budget_policy_comparison.json 存在且 JSON 可解析。
2. budget_policy_comparison.md 存在且非空。
3. budget_selection_advice.json 存在且 JSON 可解析。
4. budget_run_state.json 的 artifacts.run_reports 中已登记本次比较报告。
```
