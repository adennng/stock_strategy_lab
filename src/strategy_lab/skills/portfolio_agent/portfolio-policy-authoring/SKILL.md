---
name: portfolio-policy-authoring
description: "指导 PortfolioAgent 编写组合层基础融合策略 fusion_policy.py，并在最终候选版确定后生成 DailyPortfolioAgent 或人工每日复核提示词、决策契约和复核场景。"
license: Proprietary project skill
---

# Portfolio Policy Authoring Skill

## 适用场景

当组合层 run 已经完成以下步骤后，使用本 skill：

```text
portfolio-run
DataAgent 数据准备
portfolio-data-split
portfolio-profile
portfolio-signal-profile
portfolio-fusion-version
```

本 skill 只负责组合层策略创作，不负责回测、不负责最终选择。

## 核心定位

组合层先反复优化一个可复现、可回测的基础融合策略：

```text
fusion_policy.py
```

当 `fusion_policy.py` 已经多轮优化，机械规则继续改动收益有限时，再根据回测、诊断、阶段归因和实际数据生成 DailyPortfolioAgent 三件套：

```text
daily_override_scenarios.md
daily_portfolio_agent_prompt.md
daily_decision_contract.json
```

DailyPortfolioAgent 是每日复核器，不是每日自由组合优化器。默认必须 `PASS`，只有明确触发复核场景且证据充分时，才允许有限 `ADJUST`。

## 先决条件

写策略前先初始化版本目录：

```powershell
python -m strategy_lab.cli portfolio init-fusion-version PORTFOLIO_RUN_STATE_PATH --version-id VERSION_ID
```

版本目录由系统创建。新方案只使用 `fusion_policy.py` 作为组合层基础策略。

版本目录最终可包含：

```text
versions/{version_id}/
  fusion_manifest.json
  fusion_policy.py
  fusion_policy_spec.md
  param_space.json
  fusion_policy_meta.json
  daily_portfolio_agent_prompt.md          # 最终候选版再生成
  daily_decision_contract.json             # 最终候选版再生成
  daily_override_scenarios.md              # 最终候选版再生成
  budget_policy/
  signal_strategies/
  evaluation/
```

不要手工修改 `source_artifacts/` 下的源文件；`budget_policy/` 和 `signal_strategies/` 是当前版本的冻结快照。

## 必读文件

生成或修改组合层策略前，必须阅读：

```text
src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/fusion_policy_library.md
  组合层策略框架说明。包含 fusion_policy.py 标准接口、可探索的融合策略族、参数建议和 DailyPortfolioAgent 设计原则。

artifacts/portfolio_runs/{portfolio_run_id}/portfolio_run_state.json
  当前组合层任务状态。用于确认 run_id、数据路径、版本目录、画像路径、源预算层和源信号层快照。

artifacts/portfolio_runs/{portfolio_run_id}/profile/portfolio_profile.md
  组合层画像的人类可读摘要。用于快速理解预算层、信号层、资产池、相关性和关键事实提示。

artifacts/portfolio_runs/{portfolio_run_id}/profile/portfolio_profile.json
  组合层画像的结构化数据。用于读取 summary、fact_hints、预算/信号关系摘要、相关性摘要等。

artifacts/portfolio_runs/{portfolio_run_id}/profile/budget_signal_alignment.csv
  资产级预算/信号关系。用于识别高预算低信号、高信号低预算、预算/信号份额差异和 alignment_type。

artifacts/portfolio_runs/{portfolio_run_id}/profile/daily_budget_signal_alignment.parquet
  日频预算/信号关系。用于查看每天的排名相关性、Top-K 重合度、份额差异和冲突数量。

artifacts/portfolio_runs/{portfolio_run_id}/profile/budget_benchmark_metrics.json
  预算层独立表现与等权基准对比。用于判断预算层本身是否值得信任，以及是否应作为主导层。

artifacts/portfolio_runs/{portfolio_run_id}/signal_profiles/signal_profiles.md
  每个资产信号层策略画像的人类可读摘要。用于理解每个信号策略的角色、可靠度和适合用途。

artifacts/portfolio_runs/{portfolio_run_id}/signal_profiles/signal_profiles.json
  每个资产信号层策略画像的结构化数据。用于读取 reliability、recommended_uses、semantic_profile 等。

artifacts/portfolio_runs/{portfolio_run_id}/source_artifacts/budget/budget_agent_memory.md
  预算层训练过程、最终策略逻辑、最终选择理由和风险控制说明。需要理解预算层策略为什么这样配置时阅读。

artifacts/portfolio_runs/{portfolio_run_id}/source_artifacts/budget/final_budget_policy_config.json
  预算层最终策略配置。包含预算层 UniverseGate、AssetScorer、AllocationEngine、RiskOverlay、RebalanceScheduler、ConstraintProjector 等结构化配置。

artifacts/portfolio_runs/{portfolio_run_id}/source_artifacts/budget/final_budget_policy/param_space.json
  预算层最终策略参数空间。需要理解预算层参数默认值、可调范围和搜索空间时阅读。

artifacts/portfolio_runs/{portfolio_run_id}/source_artifacts/signals/{symbol}/signal_agent_memory.md
  单资产信号层训练过程、最终策略逻辑、策略复盘和最终选择说明。需要理解某个资产的 Alpha、Filter、ExitPolicy、PositionMapper 或状态规则时阅读。

artifacts/portfolio_runs/{portfolio_run_id}/source_artifacts/signals/{symbol}/param_space.json
  单资产信号层策略参数空间。最终实际参数可能还会被同目录 run_state.json 里的 best_params 覆盖。
```

参考模板：

```text
src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_budget_direct_fusion_policy.py
  预算层直用基线示例。用于建立并观察预算层自身组合表现基线情况。

src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_signal_direct_fusion_policy.py
  信号层直用基线示例。用于观察完全尊重单资产信号层时的组合表现。

src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_simple_mixed_fusion_policy.py
  简单混合示例。用于学习如何把预算层和信号层做基础修正。

src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_param_space.json
  参数空间示例。用于学习参数字段写法。

src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_fusion_policy_spec.md
  策略说明示例。用于学习如何解释策略目标、画像依据、风险和后续改法。

src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_fusion_policy_meta.json
  策略元数据示例。用于记录版本、策略名、策略类名和来源画像路径。

src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_daily_portfolio_agent_prompt.md
  DailyPortfolioAgent 提示词示例。通常在最终候选版确定后参考。

src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_daily_decision_contract.json
  DailyPortfolioAgent 每日输出契约示例。通常在最终候选版确定后参考。

src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/example_daily_override_scenarios.md
  DailyPortfolioAgent 复核场景示例。通常在最终候选版确定后参考。
```

参考文件只用于学习结构，不要照抄版本名、策略名或参数。

## 写策略前重点判断

至少判断以下事实：

```text
预算层独立表现是否优于等权基准。
预算层平均总敞口、持仓数量和换手情况。
信号层平均 target_S、信号广度和强信号比例。
预算/信号日频排名相关性。
预算 Top-K 与信号 Top-K 的重合度。
预算/信号份额 L1 差异。
是否存在高预算低信号资产。
是否存在高信号低预算资产。
预算层给 0 权重时，是否经常出现强信号资产。
每个资产信号层策略的语义角色、可靠度和推荐用途。
资产相关性、波动和回撤。
画像 fact_hints。
```

不要把预算权重 `R_i` 和信号目标 `S_i` 直接相乘作为最终策略。

## fusion_policy.py 数据来源和接口要求

`fusion_policy.py` 不要写死任何数据路径，也不要自己读取 `portfolio_run_state.json`。  
数据由 `portfolio-evaluation` 服务统一准备，并作为 DataFrame 或 dict 传入：

```text
budget_weights
  预算层策略执行后的每日仓位。

signal_targets
  信号层每个资产策略执行后的每日目标仓位。

returns
  与日期、资产对齐后的收益率数据。

signal_profile
  portfolio-signal-profile 的结构化结果，包含每个资产的信号语义、可靠度、推荐用途、校准信号强度文件路径等。

market_context
  portfolio-profile 的结构化画像，包含预算层独立表现、预算/信号关系、相关性摘要、冲突资产和事实提示等。
```

因此，LLM 写脚本时只需要实现统一接口，不需要关心数据文件在哪里。

### 运行时数据字段说明

```text
budget_weights
  类型：pandas.DataFrame
  index：交易日 datetime
  columns：资产代码，例如 512880.SH、159995.SZ
  value：预算层当天给该资产的目标权重 R_i
  典型用途：预算直用、预算主导修正、预算 cap、识别高预算低信号资产。

signal_targets
  类型：pandas.DataFrame
  index：交易日 datetime
  columns：资产代码
  value：信号层当天给该资产的单资产目标仓位 S_i，通常在 0 到 1
  典型用途：信号直用、信号 veto、信号 boost、预算漏选强信号资产补位。

returns
  类型：pandas.DataFrame
  index：交易日 datetime
  columns：资产代码
  value：当日收益率
  典型用途：计算近期波动、近期收益、回撤、风险降仓和相关性保护。

signal_profile
  类型：dict
  来源：signal_profiles/signal_profiles.json
  典型字段：
    summary.avg_signal_mean_target
    summary.avg_signal_active_ratio
    summary.cash_heavy_assets
    summary.strong_signal_assets
    profiles[].symbol
    profiles[].signal_distribution.mean_target
    profiles[].signal_distribution.zero_ratio
    profiles[].signal_distribution.positive_levels
    profiles[].performance.sharpe
    profiles[].performance.max_drawdown
    profiles[].semantic_profile.strategy_role
    profiles[].semantic_profile.style_tags
    profiles[].reliability.score
    profiles[].fusion_guidance.veto_power
    profiles[].fusion_guidance.boost_power
    profiles[].position_interpretation.mapping_type
  典型用途：按资产可靠度、信号风格、仓位映射形态决定折扣、放大、veto 或补位。

market_context
  类型：dict
  来源：profile/portfolio_profile.json
  典型字段：
    summary.budget_gross_mean
    summary.signal_mean
    summary.budget_signal_stacked_corr
    summary.daily_top5_overlap_mean
    budget_benchmark.metrics.budget_only
    budget_benchmark.metrics.equal_weight_rebalance
    top_budget_assets
    top_signal_assets
    top_budget_signal_conflicts
    top_signal_budget_gaps
    daily_alignment_summary
    high_correlation_pairs
    fact_hints
  典型用途：判断预算层是否可靠、两层是否冲突、是否需要提高信号主导权、是否应控制相关性和敞口。
```

### 常用读取示例

```python
def _profile_by_symbol(signal_profile):
    return {
        item.get("symbol"): item
        for item in (signal_profile or {}).get("profiles", [])
        if item.get("symbol")
    }

profiles = _profile_by_symbol(signal_profile)
reliability = {
    symbol: float(profiles.get(symbol, {}).get("reliability", {}).get("score", 0.5))
    for symbol in signal_targets.columns
}

cash_heavy_assets = set((signal_profile or {}).get("summary", {}).get("cash_heavy_assets", []))
strong_signal_assets = set((signal_profile or {}).get("summary", {}).get("strong_signal_assets", []))
budget_only_metrics = ((market_context or {}).get("budget_benchmark", {}).get("metrics", {}).get("budget_only", {}))
budget_sharpe = float(budget_only_metrics.get("sharpe", 0.0) or 0.0)
```

如果确实需要理解 `source_artifacts` 里的原始策略说明，应由 PortfolioAgent 在写策略前阅读这些文件，或先通过画像服务整理为结构化数据；不要在 `fusion_policy.py` 里写死 `source_artifacts` 路径。

必须实现：

```python
class PortfolioFusionPolicy:
    def __init__(self, params: dict | None = None):
        ...

    def generate_weights(
        self,
        budget_weights,
        signal_targets,
        returns,
        signal_profile=None,
        market_context=None,
    ):
        ...
        return weights, diagnostics
```

输出要求：

```text
weights:
  pandas.DataFrame
  index 为 datetime
  columns 为 symbol
  每行非负
  每行 sum <= max_gross
  单资产 <= max_weight

diagnostics:
  pandas.DataFrame
  index 为 datetime
  至少包含 gross_exposure、cash_weight、turnover、budget_gross、signal_mean、signal_breadth
```

如果策略需要参数，写进 `param_space.json`，并在 `fusion_policy.py` 中提供合理默认值。

## 第一轮探索建议

第一轮建议先写一个“预算层直用基线”版本：

```text
budget_direct_baseline
  直接根据 budget_weights 形成组合仓位，只做 max_gross、max_weight、换手约束等基础保护。
```

这样可以先回答一个关键问题：预算层自身作为组合策略的表现如何。  
之后再考虑写“信号层直用基线”和“简单混合基线”，用于判断信号层自身价值，以及两层融合是否真的优于任一单层。
之后再考虑如何实现最优策略。

建议第一轮形成三个对照版本：

```text
1. budget_direct_baseline
2. signal_direct_baseline
3. simple_mixed_baseline
```

## 融合策略族建议

基于 src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/fusion_policy_library.md 组合层策略框架说明的建议并结合数据情况，不断调整策略。

## DailyPortfolioAgent 文件要求

中间探索阶段可以不生成以下三份文件。  
当 `fusion_policy.py` 已经多轮优化、再继续机械改动收益有限时，再生成：

```text
daily_override_scenarios.md
daily_portfolio_agent_prompt.md
daily_decision_contract.json
```

生成这三份文件时，要基于最终版或候选最优版 `fusion_policy.py` 的多轮执行结果、回测诊断、阶段归因和实际数据，回答：

```text
fusion_policy.py 哪些场景表现稳定，可以机械执行。
哪些场景表现不稳定，但又不适合继续写死到脚本里。
哪些场景需要结合新闻、异常数据、外部事件或人工判断。
哪些场景只需要 DailyPortfolioAgent 提醒，而不一定允许改仓。
```

## 检查清单

写完后必须检查：

```text
1. fusion_policy.py 存在，并定义 PortfolioFusionPolicy。
2. fusion_policy.py 的 generate_weights 返回 weights 和 diagnostics。
3. fusion_policy_spec.md 说明策略依据了哪些画像事实。
4. param_space.json 是合法 JSON，且参数名能被 fusion_policy.py 使用。
5. fusion_policy_meta.json 是合法 JSON。
6. 如果当前已经是最终候选版，daily_override_scenarios.md 存在，且每个场景有触发条件和允许动作。
7. 如果当前已经是最终候选版，daily_portfolio_agent_prompt.md 存在，且明确默认 PASS、有限 ADJUST。
8. 如果当前已经是最终候选版，daily_decision_contract.json 是合法 JSON，且包含偏离约束。
9. 不要修改 source_artifacts/ 下的源文件。
10. 不要让 DailyPortfolioAgent 获得无限自由裁量权。
```

下一步调用：

```powershell
python -m strategy_lab.cli portfolio evaluate PORTFOLIO_RUN_STATE_PATH --version-id VERSION_ID
```
