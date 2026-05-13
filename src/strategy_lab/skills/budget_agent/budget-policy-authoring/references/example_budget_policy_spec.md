# Example Budget Policy Spec

## 策略名称

example_multi_window_momentum_topk

## 策略目标

在资产池中选择中短期表现较强且风险不过高的资产，并通过持仓数量、单资产上限、总仓位上限和换手控制降低组合波动。

## 探索阶段

`stage_1_family_exploration`

## 策略族

`risk_adjusted_rotation`

## 适用资产池画像

- 资产数量约 9 到 20 个。
- 资产之间存在横截面强弱分化。
- 资产波动差异较明显。
- 用户希望持仓相对集中，但不希望单资产过度集中。

## 用户偏好依据

- 兼顾收益和 Sharpe。
- 接受适度集中，但不接受单资产过度集中。
- 希望换手受控。

## 画像与偏好冲突

无明显冲突。若画像显示资产高度相关，应加入 `low_corr_bonus` 或 explicit groups 作为对照候选。

## 策略结构

### UniverseGate

- `data_availability_gate`：要求至少 120 个历史交易日，且当日必须有行情。
- `absolute_momentum_gate`：要求 60 日绝对动量大于 0。

### AssetScorer

- `multi_window_momentum`，权重 0.6，综合 20、60、120 日动量。
- `risk_adjusted_momentum`，权重 0.4，用 60 日动量除以 20 日波动率。
- 标准化方式为 `rank_pct`。

### AllocationEngine

- `topk_score_weighted`。
- 默认选前 4 个资产。
- 按正分数归一化分配初始预算。

### RiskOverlay

- `turnover_cap`：单日最大换手 0.4。
- `budget_smoothing`：使用 0.3 的上一期预算平滑。

### RebalanceScheduler

- `every_n_days_with_threshold`。
- 每 5 个交易日检查一次。
- 单资产权重变化小于 0.05 时不做无意义微调。

### ConstraintProjector

- `long_only_cap_normalize`。
- 总仓位上限 0.8。
- 单资产上限 0.25。
- 小于 0.02 的权重清零。
- 最多持有 4 个资产。

### Diagnostics

保存每日分数、准入结果、约束前权重、最终权重、换手和约束事件。

## 参数搜索重点

- 动量窗口。
- `top_k`。
- `max_holding_count`。
- `max_asset_weight`。
- `gross_exposure`。
- `rebalance_days`。
- `max_daily_turnover`。

## 是否使用 explicit groups

不使用。

## 预期优势

- 结构清晰，容易解释。
- 可以控制持仓数量和集中度。
- 同时考虑动量和波动。
- 换手不会过度放大。

## 主要风险

- 横盘市场中动量信号可能失效。
- 绝对动量过滤可能导致较长时间低仓位。
- 如果资产池高度相关，仍可能出现组合集中回撤。

## 不适用场景

- 用户要求长期满仓。
- 资产池没有明显横截面强弱差异。
- 用户要求完全按基本面或主观分组配置。

## 后续回测重点

- Sharpe。
- 最大回撤。
- walk-forward 稳定性。
- 换手率。
- 持仓数量是否符合预期。
- 是否存在长期空仓或过度集中。
