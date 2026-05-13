---
name: portfolio-review
description: "指导 PortfolioCriticAgent 对组合层 fusion policy 版本做单版本复盘或多版本横向比较，生成复盘报告、结构化结论和下一轮优化建议。"
license: Proprietary project skill
---

# Portfolio Review Skill

## 适用场景

当 PortfolioAgent 已完成至少一个组合层版本评估后，使用本 skill。

本 skill 只做复盘和建议，不直接修改 `fusion_policy.py`，不修改信号层或预算层策略。

可以处理两类任务：

```text
单版本复盘
  复盘一个 version_id 的组合层评估结果，找出优势、问题和下一轮优化建议。

多版本横向比较
  比较多个 version_id，判断哪个版本更值得保留、回退或继续派生。
```

## 必须阅读的策略指南

复盘前必须阅读：

```text
/src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/fusion_policy_library.md
```

用途：

```text
理解组合层策略框架。
判断当前 fusion_policy.py 属于哪类融合策略族。
检查当前策略是否正确使用 budget_weights、signal_targets、returns、signal_profile、market_context。
基于该指南给出下一轮可落地的 fusion_policy.py 优化方向。
```

## 输入信息

调用方通常会用自然语言提供：

```text
portfolio_run_state.json 路径
version_id 或多个 version_id
复盘重点，例如重点看仓位闲置、预算突破、信号利用、阶段表现、最大回撤或换手
```

如果没有明确 version_id，应读取 `portfolio_run_state.json`，优先选择：

```text
current_version
best_version
versions 中最近一个 status=evaluated 的版本
```

如果仍无法确定，最终回复中说明无法确定。

## 必读文件

### Run 级文件

```text
portfolio_run_state.json
  当前组合层任务状态。用于确认 portfolio_run_id、版本列表、画像路径、最终选择、run_reports 和 events。

profile/portfolio_profile.json
profile/portfolio_profile.md
  组合层市场画像和预算/信号关系画像。重点看：
  - budget_benchmark
  - top_budget_assets
  - top_signal_assets
  - top_budget_signal_conflicts
  - top_signal_budget_gaps
  - daily_alignment_summary
  - high_correlation_pairs
  - fact_hints

signal_profiles/signal_profiles.json
signal_profiles/signal_profiles.md
  信号层策略画像。重点看：
  - 每个资产的 strategy_role
  - reliability.score / grade
  - signal_distribution.mean_target / zero_ratio / active_ratio / positive_levels
  - fusion_guidance.veto_power / boost_power / budget_override_suitability
  - position_interpretation

source_artifacts/budget/budget_agent_memory.md
  预算层训练过程和最终策略说明报告。用于理解预算层策略为什么这样配置。

source_artifacts/budget/final_budget_policy_config.json
source_artifacts/budget/final_budget_policy/param_space.json
  预算层最终策略结构和参数。
```

必要时读取：

```text
source_artifacts/signals/{symbol}/signal_agent_memory.md
source_artifacts/signals/{symbol}/strategy_spec.md
source_artifacts/signals/{symbol}/param_space.json
source_artifacts/signals/{symbol}/metrics.json
```

触发条件：

```text
某资产贡献特别大或拖累特别大。
某资产预算高但信号低。
某资产信号强但预算长期为 0。
某资产最终仓位长期被放大或被压制。
某资产交易频繁、回撤明显或错过主要行情。
```

### 版本级策略文件

对每个复盘 version_id 阅读：

```text
versions/{version_id}/fusion_manifest.json
  组合层版本清单，包含预算层策略路径、信号层策略路径、fusion_policy.py、param_space.json。

versions/{version_id}/fusion_policy.py
  本版组合层实际执行脚本。必须检查公式、参数、仓位上限、换手约束、信号/预算使用方式。

versions/{version_id}/fusion_policy_spec.md
  本版策略说明。检查它是否与代码和回测表现一致。

versions/{version_id}/param_space.json
  本版参数默认值和可调范围。

versions/{version_id}/fusion_policy_meta.json
  本版元数据和策略标签。
```

### 版本级评估（回测结果）文件

对每个复盘 version_id 阅读或统计：

```text
versions/{version_id}/evaluation/evaluation_summary.md
  本版评估摘要。

versions/{version_id}/evaluation/evaluation_manifest.json
  评估总清单，包含输入输出路径、融合摘要、指标和 warnings。

versions/{version_id}/evaluation/daily_budget_weights.parquet
  每日预算层权重 R_i。用于比较预算层原始意图。

versions/{version_id}/evaluation/daily_signal_targets.parquet
  每日信号层 target_S。用于判断信号层是否看好或回避。

versions/{version_id}/evaluation/daily_final_weights.parquet
  每日最终组合仓位 W_i。用于分析最终持仓、现金、集中度和是否过度保守。

versions/{version_id}/evaluation/fusion_diagnostics.json
  融合诊断摘要。重点看 gross、cash、turnover、over_budget、warning。

versions/{version_id}/evaluation/fusion_diagnostics.md
  融合诊断文字报告。

versions/{version_id}/evaluation/fusion_diagnostics.parquet
  日频组合诊断。用于看每一阶段总仓位、现金、换手、信号广度、预算/信号关系。

versions/{version_id}/evaluation/fusion_asset_diagnostics.parquet
  日频资产级融合诊断。用于看每个资产的预算权重、信号目标、最终权重、超预算等。

versions/{version_id}/evaluation/aligned_returns_wide.parquet
  与最终权重对齐的收益率数据。用于计算阶段收益、资产贡献和回撤阶段。

versions/{version_id}/evaluation/backtest/metrics.json
  组合层回测指标。重点看 Sharpe、total_return、annual_return、max_drawdown、benchmark、excess_return、turnover、gross_exposure、order_count。

versions/{version_id}/evaluation/backtest/report.md
  回测报告。

versions/{version_id}/evaluation/backtest/equity_curve.parquet
  权益曲线。用于阶段表现、回撤和收益节奏分析。

versions/{version_id}/evaluation/backtest/holdings.parquet
  持仓记录。用于看每个阶段持仓集中度和资产暴露。

versions/{version_id}/evaluation/backtest/orders.parquet
  交易记录。用于看交易频率、换手、成本和异常调仓日期。

versions/{version_id}/evaluation/backtest/budget_vs_benchmark.png
  策略与基准曲线图。如果数据文件不足以表达曲线形态，可调用 image-review。
```

## 必须分析什么

### 1. 总体表现

至少分析：

```text
Sharpe
total_return
annual_return
max_drawdown
benchmark_total_return
benchmark_sharpe
excess_total_return
average_turnover
average_gross_exposure
average_holding_count
total_transaction_cost
```

判断：

```text
收益来自更高胜率、更高敞口、更低回撤，还是更少交易。
是否只是因为仓位很低才回撤小。
是否比预算层独立表现、等权基准和上一版本更好。
```

### 2. 阶段表现

必须分阶段或分时期看：

```text
权益曲线阶段收益
阶段最大回撤
阶段平均总仓位
阶段现金比例
阶段换手
阶段对基准超额收益
```

如果已有 train/validation/walk-forward 或 full 区间信息，应分别讨论。
如果没有现成阶段文件，可用权益曲线和日期区间按年度、季度、明显回撤段或画像阶段自行聚合。

### 3. 持仓和仓位

必须检查：

```text
daily_final_weights：最终仓位是否长期过低或过于集中。
daily_budget_weights：预算层原本想持有什么。
daily_signal_targets：信号层是否支持预算层持仓。
fusion_asset_diagnostics：哪些资产被压低、放大、超预算、长期闲置。
holdings：真实回测持仓是否符合 fusion_policy.py 设计。
orders：是否存在频繁交易、集中换手或异常交易日期。
```

### 4. 预算/信号融合质量

必须回答：

```text
高预算低信号时，策略处理是否合理。
高信号低预算时，策略是否错失机会。
预算为 0 但信号很强时，是否应该允许小幅突破。
signal floor 是否导致不该持有的资产被持有。
veto 是否导致资金大量闲置。
闲置资金再分配是否真正提高收益，还是增加回撤。
```

### 5. 资产层优劣势

必须找出：

```text
贡献最大的资产。
拖累最大的资产。
仓位最高的资产。
换手最高的资产。
预算/信号/最终仓位偏差最大的资产。
```

必要时阅读这些资产的：

```text
source_artifacts/signals/{symbol}/signal_agent_memory.md
```

判断问题来自：

```text
信号层自身过于保守或误判。
预算层给错方向。
组合层融合公式压制/放大不当。
换手或风险约束过强。
```

### 6. 策略结构和下一轮建议

结合 `fusion_policy_library.md` 判断：

```text
当前策略属于预算直用、信号直用、预算主导+信号修正、信号主导+预算约束、双证据打分、分市场状态融合，还是混合方案。
是否应提高/降低 target_gross 或 max_gross。
是否应调整 max_weight。
是否应引入或关闭 zero_override_enabled。
是否应调整 signal_floor、signal_power、cap_multiplier、over_budget_pool。
是否应加强换手限制或放松换手限制。
是否应加入阶段/市场状态切换规则。
优化的可行方案。
```

## 输出目录和文件

单版本复盘默认写入：

```text
versions/{version_id}/review/
  portfolio_critic_review.md
  portfolio_critic_review.json
  portfolio_next_action.json
```

多版本横向比较可写入：

```text
reports/portfolio_critic_comparison_{timestamp}.md
reports/portfolio_critic_comparison_{timestamp}.json
```

也可以对每个版本分别写入对应 `versions/{version_id}/review/`。

## `portfolio_critic_review.json` 格式

单版本复盘至少包含：

```json
{
  "schema_version": "0.1.0",
  "review_type": "single_version",
  "portfolio_run_id": "portfolio_xxx",
  "version_id": "v001_xxx",
  "review_status": "completed",
  "overall_judgement": "keep | modify | abandon | compare_more | ready_for_final_selection",
  "metrics_summary": {},
  "stage_findings": [],
  "position_findings": [],
  "budget_signal_findings": [],
  "asset_findings": [],
  "strengths": [],
  "weaknesses": [],
  "optimization_targets": [],
  "evidence_files": [],
  "created_at": "ISO timestamp"
}
```

## `portfolio_next_action.json` 格式

至少包含：

```json
{
  "schema_version": "0.1.0",
  "version_id": "v001_xxx",
  "recommended_action": "modify_current | derive_new_version | backtrack | stop | final_select",
  "suggested_next_version_id": "v002_xxx",
  "fusion_policy_changes": [
    {
      "target": "fusion_policy.py | param_space.json | fusion_policy_spec.md",
      "change": "建议改什么",
      "reason": "为什么",
      "expected_effect": "预期影响"
    }
  ],
  "parameter_suggestions": {},
  "do_not_change": [],
  "risk_controls": [],
  "notes": []
}
```

多版本比较 JSON 至少包含：

```json
{
  "schema_version": "0.1.0",
  "review_type": "multi_version_comparison",
  "portfolio_run_id": "portfolio_xxx",
  "compared_versions": ["v001", "v002"],
  "preferred_version_id": "v002",
  "ranking": [],
  "selection_reason": "选择理由",
  "next_action": {}
}
```

## 登记到 portfolio_run_state.json

复盘完成后必须调用：

```powershell
python -m strategy_lab.cli portfolio register-run-report PORTFOLIO_RUN_STATE_PATH REPORT_KEY REPORT_PATH --report-type portfolio_critic_review --summary "复盘摘要" --extra-json-file EXTRA_JSON_PATH
```

示例：

```powershell
python -m strategy_lab.cli portfolio register-run-report artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json review_v001 versions\v001\review\portfolio_critic_review.md --report-type portfolio_critic_review --summary "v001 已完成组合层复盘" --extra-json-file versions\v001\review\portfolio_critic_review.json
```

多版本比较时：

```powershell
python -m strategy_lab.cli portfolio register-run-report artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json comparison_v001_v002 reports\portfolio_critic_comparison_20260513_120000.md --report-type portfolio_critic_comparison --summary "已完成 v001/v002 横向比较" --extra-json-file reports\portfolio_critic_comparison_20260513_120000.json
```

## 最终检查清单

完成后必须检查：

```text
1. 复盘 md 文件存在且非空。
2. 复盘 json 文件存在且 json.loads 可解析。
3. 单版本复盘时 portfolio_next_action.json 存在且 json.loads 可解析。
4. 报告中明确列出读取过的关键文件。
5. 报告包含总体表现、阶段表现、持仓/仓位、预算/信号融合、资产层贡献、下一轮建议。
6. 已调用 portfolio register-run-report。
7. portfolio_run_state.json 的 artifacts.run_reports 中出现对应 report_key。
8. events 中出现 portfolio_critic_review_registered 或 portfolio_critic_comparison_registered。
```
