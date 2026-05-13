---
name: single-policy-review
description: "BudgetCriticAgent 用于复盘单个预算层策略评估结果，分析参数搜索、回测、walk-forward、阶段归因，并输出下一轮预算策略优化建议。"
license: Proprietary project skill
---

# Single Policy Review Skill

## 适用场景

当调用方要求复盘一个预算层策略、一个 `search_id`、或某个 `search_result.json` 时使用本 skill。

典型任务：

```text
请复盘 budget_run_state_path=... 下的 search_id=budget_policy_001。
请判断这个预算策略是否过拟合，并给出下一轮预算策略修改建议。
```

## 必要输入

必须识别：

```text
budget_run_state_path
search_id 或 search_result_path
```

如果没有明确 `search_id`，先读取 `budget_run_state.json`，优先选择最近的、已有 `attempt_summary_path` 和 `stage_attribution_path` 的 search。

## 必须读取的核心文件

优先读取：

```text
budget_run_state.json
policies/searches/{search_id}/search_result.json
policies/searches/{search_id}/attempt_summary.json
policies/searches/{search_id}/attempt_summary.md
policies/searches/{search_id}/stage_attribution/stage_attribution.json
policies/searches/{search_id}/stage_attribution/stage_attribution.md
```

还应按需读取：

```text
profile/budget_profile.json
profile/budget_profile.md
policies/searches/{search_id}/best/budget_policy_config.json
policies/searches/{search_id}/best/param_space.json
policies/searches/{search_id}/population_summary.csv
policies/searches/{search_id}/best_individual.json
policies/searches/{search_id}/walk_forward_summary.json
policies/searches/{search_id}/best/full/metrics.json
policies/searches/{search_id}/best/full/report.md
```

读取 parquet/csv 时，只输出摘要，不要把完整数据打印进上下文。

## 复盘重点

必须覆盖：

```text
1. 综合表现
   best_score、full_sharpe、full_total_return、full_max_drawdown、full_excess_total_return。

2. 稳定性
   validation_sharpe、validation_excess_total_return、walk_forward_mean_score、walk_forward_std_score、walk_forward_min_score。

3. 过拟合风险
   train 和 validation 是否差距过大；walk-forward 是否明显不稳定。

4. 基准对比
   是否跑赢基准；跑赢来自仓位控制、资产选择还是特定市场阶段。

5. 阶段归因
   哪些市场阶段贡献收益；哪些阶段拖累；下跌、高波动、震荡阶段是否控制住回撤和换手。

6. 预算策略结构
   从 BudgetPolicy 六个模块分析问题：UniverseGate、AssetScorer、AllocationEngine、RiskOverlay、RebalanceScheduler、ConstraintProjector。
   同时判断该策略所属策略族是否匹配预算画像和用户偏好，例如 momentum_rotation、risk_adjusted_rotation、defensive_low_vol、drawdown_control、correlation_aware、risk_parity_like、low_turnover_balanced、concentration_alpha、vol_target_rotation。

7. 参数搜索质量
   参数空间是否过宽/过窄；候选结果是否集中；最佳参数是否靠近边界；是否需要固定、收缩或扩展参数。

8. 画像与偏好冲突
   如果用户偏好与预算画像冲突，例如高收益满仓 vs 高相关高回撤、集中持仓 vs 强弱分化弱、低换手 vs 轮动很快，必须说明当前策略的取舍是否合理，并给出下一轮更偏稳健或更偏收益的建议。

9. 阶段推进建议
   根据当前策略所在阶段和证据，明确建议 BudgetAgent：
   - `stay_current_stage`：继续在当前阶段内部探索。
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
artifacts/budget_runs/{budget_run_id}/policies/searches/{search_id}/review/
  budget_critic_review.json
  budget_critic_review.md
  budget_next_action.json
```

`budget_critic_review.json` 至少包含：

```json
{
  "status": "success",
  "review_type": "single_policy",
  "search_id": "...",
  "overall_judgement": "...",
  "strengths": [],
  "weaknesses": [],
  "overfit_risk": "low | medium | high",
  "stage_findings": [],
  "profile_fit_findings": [],
  "preference_conflicts": [],
  "policy_module_findings": {
    "universe_gate": [],
    "asset_scorer": [],
    "allocation_engine": [],
    "risk_overlay": [],
    "rebalance_scheduler": [],
    "constraint_projector": []
  },
  "parameter_findings": [],
  "current_stage_assessment": "...",
  "recommended_stage_transition": "stay_current_stage | advance_next_stage | rollback_previous_stage | return_to_family_exploration | ready_for_final_selection",
  "next_action_summary": "...",
  "evidence_files": []
}
```

`budget_next_action.json` 至少包含：

```json
{
  "action": "continue | modify | discard | compare_more | ready_for_selection",
  "priority": "low | medium | high",
  "keep": [],
  "change": [],
  "avoid": [],
  "parameter_adjustments": [],
  "suggested_next_policy_direction": "...",
  "recommended_stage_transition": "stay_current_stage | advance_next_stage | rollback_previous_stage | return_to_family_exploration | ready_for_final_selection",
  "stage_transition_reason": "...",
  "reason": "..."
}
```

## run_state.json 更新

完成后不要手动编辑 `budget_run_state.json`，必须调用：

```powershell
python -m strategy_lab.cli budget update-policy-review BUDGET_RUN_STATE_PATH SEARCH_ID --critic-review-path REVIEW_JSON --critic-review-md-path REVIEW_MD --next-action-path NEXT_ACTION_JSON --summary "复盘摘要"
```

## 最终检查

完成前必须检查：

```text
1. budget_critic_review.json 存在且 JSON 可解析。
2. budget_critic_review.md 存在且非空。
3. budget_next_action.json 存在且 JSON 可解析。
4. budget_run_state.json 中 artifacts.policies.searches.{search_id} 已登记 critic_review_path、critic_review_md_path、next_action_path。
```
