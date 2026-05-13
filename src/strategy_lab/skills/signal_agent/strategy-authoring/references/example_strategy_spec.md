# Example Regime MultiTimeframe Signal Strategy

## Strategy Structure

- RegimeDetector: 使用长期均线、长期斜率、中期收益和实现波动率识别 `uptrend`、`range`、`downtrend`、`high_vol`。
- RegimeAlphaPolicy:
  - `uptrend`: 调用 `alpha_uptrend_trend_momentum`。
  - `range`: 调用 `alpha_range_mean_reversion`。
  - `downtrend`: 调用 `alpha_downtrend_flat`。
  - `high_vol`: 调用 `alpha_high_vol_defensive`。
- Filters: 高波动状态由 RegimeDetector 直接处理。
- ExitPolicy: regime 转为 `downtrend` 或 `high_vol` 时降仓；震荡回归条件消失时退出。
- PositionMapper: 各 regime 使用不同最大 `target_S`。
- StateRules: 使用 `max_daily_target_change` 限制每日目标仓位变化。

## RegimeAlphaMap

```text
uptrend -> alpha_uptrend_trend_momentum
range -> alpha_range_mean_reversion
downtrend -> alpha_downtrend_flat
high_vol -> alpha_high_vol_defensive
```

本策略不允许所有 regime 共用同一个 alpha_score。每个核心 regime 必须先调用自己的 Alpha 函数，再进入统一 PositionMapper。

## Alpha Structure

```text
type: regime_switching_multitimeframe

regimes:
  uptrend:
    long_horizon: long_window 判断长期趋势方向
    mid_horizon: mid_window 判断趋势强度
    short_horizon: short_window 判断短期确认和追高风险
  range:
    long_horizon: 避免明显下跌趋势
    mid_horizon: mid_window 计算区间均值和标准差
    short_horizon: short_window 确认短期下跌或修复
  downtrend:
    alpha: alpha_downtrend_flat，输出 0 或极低防御分数
  high_vol:
    alpha: alpha_high_vol_defensive，优先防御，仅在短期仍为正时允许小仓位
```

## Intended Regime

适合市场阶段差异明显、趋势和震荡切换较清楚的指数或 ETF。趋势阶段用趋势 Alpha，震荡阶段用回归 Alpha，下跌或高波动阶段主动防御。

## Failure Regime

如果市场频繁假突破、状态切换非常快，或者价格长期窄幅横盘但噪音很大，策略可能因为 regime 判断滞后或频繁切换而表现较差。

## Objective

优先提升 Sharpe，同时控制最大回撤、交易次数和 walk-forward 稳定性。
