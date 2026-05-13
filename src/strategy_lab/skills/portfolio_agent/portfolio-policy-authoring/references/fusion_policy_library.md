# Portfolio Fusion Policy Library

## 1. 组合层定位

组合层固定上游两层策略：

```text
预算层 BudgetPolicy -> 多资产交易策略，输出每日预算参考权重 R_i
信号层 SignalStrategy -> 每个资产单独的交易策略，每个资产每日目标参与度 S_i
```

组合层负责把这些输入融合成最终组合基础仓位：

```text
W_base_i = PortfolioFusionPolicy(R_i, S_i, profile, risk_state)
```

`fusion_policy.py` 是默认机械执行策略，必须可复现、可回测。  
DailyPortfolioAgent 是复核器，只在最终候选策略确定后，需要人工用来处理机械策略不适合直接执行的特殊场景。

## 2. 标准输入

`fusion_policy.py` 不要写死任何路径。评估服务会准备并传入：

```text
budget_weights:
  DataFrame[index=datetime, columns=symbol]
  每日预算层参考权重 R_i。
  示例含义：512880.SH 当天为 0.10，表示预算层希望组合资金的 10% 分给该资产。

signal_targets:
  DataFrame[index=datetime, columns=symbol]
  每日信号层目标参与度 S_i，范围 0 到 1。
  示例含义：512880.SH 当天为 0.80，表示该资产自己的信号层策略认为当前适合高参与；0.10 表示轻仓参与；0 表示回避。

returns:
  DataFrame[index=datetime, columns=symbol]
  每日资产收益率。
  示例含义：0.012 表示该资产当天上涨 1.2%。

signal_profile:
  dict，可选。
  portfolio-signal-profile 输出，包含每个资产的信号分布、绩效、语义角色、可靠度、推荐用途和仓位解释等。

market_context:
  dict，可选。
  portfolio-profile 输出，包含预算层独立表现、预算/信号关系、相关性摘要、冲突资产、事实提示等。
```

这些输入是评估服务整理好的运行时数据，不是 `source_artifacts` 原始文件全文。  
`source_artifacts` 目录下的 `signal_agent_memory.md`、`budget_agent_memory.md`、`param_space.json`、策略源码等，主要供 PortfolioAgent 在写策略前阅读和理解；不要在 `fusion_policy.py` 里写死路径读取。

### 2.1 `signal_profile` 常用字段

`signal_profile` 来自：

```text
artifacts/portfolio_runs/{portfolio_run_id}/signal_profiles/signal_profiles.json
```

常用结构：

```text
signal_profile["summary"]
  asset_count
  avg_signal_mean_target
  avg_signal_active_ratio
  avg_signal_zero_ratio
  avg_reliability_score
  high_reliability_assets
  cash_heavy_assets
  strong_signal_assets

signal_profile["profiles"][]
  symbol
  input_files
  signal_distribution
  performance
  semantic_profile
  reliability
  fusion_guidance
  position_interpretation
```

每个资产常用字段：

```text
signal_distribution.mean_target
signal_distribution.median_target
signal_distribution.max_target
signal_distribution.zero_ratio
signal_distribution.active_ratio
signal_distribution.positive_levels
signal_distribution.signal_shape

performance.sharpe
performance.max_drawdown
performance.total_return
performance.excess_total_return

semantic_profile.strategy_role
semantic_profile.style_tags
semantic_profile.best_market_conditions
semantic_profile.weak_market_conditions
semantic_profile.strengths
semantic_profile.weaknesses
semantic_profile.fusion_usage

reliability.score
reliability.grade

fusion_guidance.veto_power
fusion_guidance.boost_power
fusion_guidance.budget_override_suitability
fusion_guidance.suggested_constraints

position_interpretation.mapping_type
position_interpretation.positive_levels
```

调用示例：

```python
def _signal_profiles_by_symbol(signal_profile):
    return {
        item.get("symbol"): item
        for item in (signal_profile or {}).get("profiles", [])
        if item.get("symbol")
    }

profiles = _signal_profiles_by_symbol(signal_profile)
reliability = pd.Series(
    {
        symbol: float(profiles.get(symbol, {}).get("reliability", {}).get("score", 0.5) or 0.5)
        for symbol in signal_targets.columns
    }
)
cash_heavy = set((signal_profile or {}).get("summary", {}).get("cash_heavy_assets", []))
strong_signal = set((signal_profile or {}).get("summary", {}).get("strong_signal_assets", []))
```

### 2.2 `market_context` 常用字段

`market_context` 来自：

```text
artifacts/portfolio_runs/{portfolio_run_id}/profile/portfolio_profile.json
```

常用结构：

```text
market_context["summary"]
  symbol_count
  date_count
  budget_gross_mean
  budget_gross_max
  signal_mean
  signal_breadth_gt_03_mean
  budget_signal_stacked_corr
  daily_budget_signal_rank_corr_mean
  daily_top5_overlap_mean
  daily_budget_signal_share_l1_gap_mean
  average_pairwise_correlation

market_context["budget_benchmark"]["metrics"]["budget_only"]
  total_return
  annual_return
  sharpe
  max_drawdown
  average_gross_exposure
  average_holding_count
  average_daily_turnover

market_context["top_budget_assets"]
market_context["top_signal_assets"]
market_context["top_budget_signal_conflicts"]
market_context["top_signal_budget_gaps"]
market_context["daily_alignment_summary"]
market_context["high_correlation_pairs"]
market_context["fact_hints"]
```

调用示例：

```python
summary = (market_context or {}).get("summary", {})
budget_metrics = ((market_context or {}).get("budget_benchmark", {}).get("metrics", {}).get("budget_only", {}))
budget_sharpe = float(budget_metrics.get("sharpe", 0.0) or 0.0)
budget_gross_mean = float(summary.get("budget_gross_mean", 0.0) or 0.0)
rank_corr_mean = float(summary.get("daily_budget_signal_rank_corr_mean", 0.0) or 0.0)
```

### 2.3 `returns` 常用计算示例

```python
recent_ret_20 = returns.rolling(20).mean().fillna(0.0)
recent_vol_20 = returns.rolling(20).std().fillna(0.0)
vol_penalty = (recent_vol_20 / recent_vol_20.median(axis=1).replace(0, pd.NA).values.reshape(-1, 1)).fillna(1.0)
```

写策略时要注意避免未来函数：某天仓位只能使用当天及以前可得的信息。滚动计算默认包含当天收益，若用于当日收盘前决策，应整体 `shift(1)`；如果回测假设收盘后生成下一日仓位，也要在策略说明中写清楚。

## 3. 关于 R_i 和 S_i 的使用

不要默认把 `R_i` 和 `S_i` 直接相乘作为最终仓位。  
但也不要机械认为二者完全不可比较。很多信号层策略本身输出的是“单资产视角下愿意动用的目标仓位”，例如 `S_i=0.10` 可以理解为该资产只适合轻仓参与，剩余资金可用于其他资产或现金。

因此 `S_i` 可以直接作为仓位来源，也可以作为预算层的修正因子。可用表达包括：

```text
原始仓位值：直接使用 R_i 或 S_i。
横截面排名：比较当日资产强弱。
横截面份额：用于预算主导或机会分归一化。
校准信号强度：使用 portfolio-signal-profile 的 daily_calibrated_signal_strength。
条件规则：高预算低信号、高信号低预算、预算为 0 但强信号等。
风险状态：波动、相关性、回撤、市场阶段。
```

归一化不是必须步骤。它只是一种可选工具，尤其适合“预算主导 + 信号微调”或“机会分打分”场景。

## 4. 必须实现的 Python 接口

```python
class PortfolioFusionPolicy:
    def __init__(self, params: dict | None = None):
        self.params = params or {}

    def generate_weights(
        self,
        budget_weights,
        signal_targets,
        returns,
        signal_profile=None,
        market_context=None,
    ):
        return weights, diagnostics
```

`weights` 要求：

```text
DataFrame
index 与输入日期对齐
columns 为资产代码
所有权重 >= 0
每日总权重 <= max_gross
单资产 <= max_weight
不做空，不加杠杆
```

`diagnostics` 要求：

```text
DataFrame
index 为 datetime
至少包含：
  gross_exposure
  cash_weight
  turnover
  budget_gross
  signal_mean
  signal_breadth
  over_budget_total
  budget_signal_rank_corr
```

## 5. 基础融合策略族

以下策略族可以自由组合、拆分、调整和扩展，不是固定模板，仅供参考。

### 5.1 信号层直用基线

适合：

```text
第一轮探索。
需要先确认“只按单资产信号层仓位执行”表现如何。
信号层策略已经经过充分单资产训练。
```

思路：

```text
W_raw_i = S_i
如果 sum(W_raw) > max_gross，则按比例缩放，或按可靠度/风险筛选。
如果 W_raw_i > max_weight，则截断并可选择再分配。
应用换手和平滑。
```

注意：

```text
S_i 可以直接作为仓位使用，不必先做横截面归一化。
如果信号层普遍保守，低总仓位可能是策略真实意图，也可能造成资金利用不足，需要通过回测验证。
```

### 5.2 预算层直用基线

适合：

```text
需要确认预算层自身表现。
需要判断组合层改动是否真的比预算层原策略更好。
```

思路：

```text
W_raw_i = R_i
只应用 max_gross、max_weight、换手和异常保护。
```

### 5.3 预算主导 + 信号修正

适合：

```text
预算层独立表现优于等权基准（具体看 market_context["budget_benchmark"] 和 portfolio_profile.md）。
预算层排名稳定。
信号层更多承担择时、风险过滤、局部增强。
```

思路：

```text
base_i = R_i
signal_modifier_i = f(S_i, signal_profile_i)
W_i = base_i * discount_or_boost_i
```

修正方式可以包括：

```text
低信号折扣
强信号 boost
信号 veto
预算软突破
闲置资金再分配
分段规则
结合 signal_profile 可靠度
结合近期波动或回撤
```

归一化不是唯一选择。直接在预算仓位上做折扣、加成、截断、再分配都可以。

### 5.4 信号主导 + 预算约束

适合：

```text
信号层强信号资产可靠度高。
预算层经常漏选高信号资产。
预算/信号 Top-K 重合低，但信号侧后续表现更好。
```

思路：

```text
W_raw_i = S_i 或 calibrated_signal_strength_i
预算层用于准入、上限、主题偏好或风险约束。
```

### 5.5 双证据打分

适合：

```text
预算层和信号层都有价值，但经常局部冲突。
希望两层证据共同决定仓位。
```

思路：

```text
budget_score_i = R_i、rank(R_i) 或 share(R_i)
signal_score_i = S_i、rank(S_i) 或 calibrated_signal_strength_i
risk_penalty_i = recent_vol_i
score_i = f(budget_score_i, signal_score_i, risk_penalty_i, reliability_i)
```

### 5.6 分市场状态融合

适合：

```text
资产池在趋势市、震荡市、下跌市表现差异明显。
```

思路：

```text
uptrend:
  提高 target_gross、signal boost、over_budget_pool。

range:
  控制 target_gross，提高预算约束和换手控制。

downtrend:
  降低 max_gross，强化 signal veto 和现金保护。
```

### 5.7 闲置资金再分配组件

闲置资金再分配不是一个独立必须模板，更适合作为上述多类策略中的可选组件。

适合：

```text
基础仓位因 signal veto、max_weight、budget cap 或风险约束产生大量现金。
同日存在高可靠、高信号、风险可控的资产。
```

思路：

```text
residual = target_gross - sum(W)
将 residual 的一部分分配给 eligible assets。
eligible assets 需要满足强信号、可靠度、风险约束。
```

## 6. 常用参数

建议按策略需要写入 `param_space.json`：

```text
target_gross: 0.35-0.90
max_gross: 0.60-1.00
max_weight: 0.15-0.35
alpha_budget: 0.3-2.0
beta_signal: 0.3-2.0
gamma_vol: 0.0-1.5
signal_floor: 0.00-0.25
signal_power: 0.5-2.0
cap_multiplier: 1.0-1.8
over_budget_pool: 0.0-0.25
zero_override_enabled: 默认 false，只有预算漏选强信号资产证据充分时才测试 true。
rebalance_speed: 0.2-1.0
max_turnover_per_day: 0.10-0.60
```

## 7. DailyPortfolioAgent 设计原则

中间探索阶段可以不生成 DailyPortfolioAgent 相关文件。  
当 `fusion_policy.py` 已经多轮优化，机械规则继续改进有限时，再生成：

```text
daily_override_scenarios.md
daily_portfolio_agent_prompt.md
daily_decision_contract.json
```

DailyPortfolioAgent 不是替代 `fusion_policy.py` 的每日自由策略。它只做人工或下llm复核：

```text
PASS:
  沿用 W_base。

ADJUST:
  明确触发复核场景时，做有限修正。
```

默认必须 PASS。证据不足必须 PASS。

例如，适合写入 `daily_override_scenarios.md` 的问题：

```text
低频、偶发、难以稳定参数化的场景。
需要结合新闻或外部事件判断的场景。
数据异常、停牌、异常跳空等机械策略不容易识别的场景。
回测中只在少数阶段暴露，但一旦发生影响较大的场景。
fusion_policy.py 只能提示风险但不适合机械改仓的场景。
```

## 8. 自检

写完策略后检查：

```text
fusion_policy.py 是否能导入。
PortfolioFusionPolicy 是否存在。
generate_weights 是否返回两个 DataFrame。
weights 是否非负、无杠杆、无做空。
diagnostics 是否包含关键字段。
param_space.json 的参数是否都能被 fusion_policy.py 使用。
如果是最终候选版，DailyPortfolioAgent 三件套是否基于多轮回测、诊断和实际数据生成。
```
