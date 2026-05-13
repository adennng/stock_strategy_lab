---
name: single-attempt-review
description: "CriticAgent 用于复盘单个策略 attempt 的完整流程，包含读取产物、阶段归因、过拟合判断、基准对比、风险分析和输出 next_action。"
license: Proprietary project skill
---

# Single Attempt Review Skill

## 适用场景

当调用方要求你复盘一个具体 attempt 时使用本 skill。典型任务：

```text
请复盘 run_state_path=... 下的 attempt_001。
请判断 attempt_wf_score_smoke 是否过拟合，是否值得继续优化。
```

调用方通常只会自然语言说明任务。你需要自行读取文件、运行必要命令并生成复盘产物。

## 必要输入

必须从任务中识别：

```text
run_state_path
attempt_id
```

如果没有明确 attempt_id，应读取 `run_state.json`，优先选择状态为 `ready_for_review` 的最近 attempt；如果仍不明确，应在最终回复中说明无法确定。

## 必须读取的核心文件

先读取：

```text
run_state.json
attempts/{attempt_id}/optimization/attempt_summary.json
attempts/{attempt_id}/optimization/attempt_summary.md
```

如果 `attempt_summary` 不存在，先运行：

```powershell
python -m strategy_lab.cli signal summarize-attempt RUN_STATE_PATH ATTEMPT_ID
```

然后根据 `attempt_summary.json` 中的 `critic_inputs` 读取必要文件，重点包括：

```text
market_profile/market_profile.json
market_profile/market_profile.md
market_profile/market_profile_chart.png
attempts/{attempt_id}/strategy/strategy.py
attempts/{attempt_id}/strategy/strategy_spec.md
attempts/{attempt_id}/strategy/param_space.json
attempts/{attempt_id}/strategy/strategy_meta.json
attempts/{attempt_id}/optimization/best_individual.json
attempts/{attempt_id}/optimization/population_summary.csv
attempts/{attempt_id}/optimization/walk_forward_summary.json
attempts/{attempt_id}/backtests/full/metrics.json
attempts/{attempt_id}/backtests/full/report.md
attempts/{attempt_id}/backtests/full/equity_curve.parquet
attempts/{attempt_id}/backtests/full/benchmark_curve.parquet
attempts/{attempt_id}/backtests/full/orders.parquet
attempts/{attempt_id}/backtests/full/daily_signals.parquet
attempts/{attempt_id}/backtests/full/strategy_vs_benchmark.png
```

读取 parquet/csv 时，优先写小脚本或使用命令输出摘要，不要把完整数据打印进上下文。

## 阶段归因

单 attempt 复盘必须检查是否已有阶段归因：

```text
attempts/{attempt_id}/analysis/stage_attribution/
  stage_attribution.json
  stage_attribution.csv
  stage_attribution.md
  stage_return_comparison.png
```

如果没有，必须先运行：

```powershell
python -m strategy_lab.cli signal stage-attribution RUN_STATE_PATH ATTEMPT_ID
```

阶段归因用于回答：

```text
策略在哪些市场阶段赚钱？
在哪些市场阶段跑输基准？
下跌段是否控制回撤？
震荡段是否频繁交易或失效？
上涨段收益是否只是跟随基准？
每个趋势阶段内的交易是否集中在不利位置？
trade_summary 和 trades 是否显示过度交易、追涨杀跌、迟滞退出或暴露不足？
```

新版 `stage_attribution.json` 每个阶段包含 `trade_summary` 和按时间顺序排列的 `trades`。复盘时必须结合阶段标签、阶段收益、阶段回撤和每笔交易明细，不要只看全局 `order_count`。

## 策略框架与修复逻辑

SignalAgent 当前策略统一框架是：

```text
SignalStrategy =
    RegimeDetector 市场状态识别
  + RegimeAlphaPolicy 分状态多周期 Alpha
  + Filters 辅助过滤器
  + ExitPolicy 出场与风控
  + PositionMapper 仓位映射
  + StateRules 状态与交易纪律
```

策略输出必须是 `target_S ∈ [0, 1]`，不是加减仓动作。

SignalAgent 默认优先使用 `RegimeSwitchingAlpha + MultiTimeframeAlpha`：先判断市场状态，再在不同状态下使用长 / 中 / 短周期组合 Alpha，最后统一映射为 `target_S`。

复盘时必须先从 `strategy_spec.md` 和 `strategy_meta.json` 识别该 attempt 的六个模块和 `RegimeAlphaMap`；如果说明文件不完整，应在复盘中指出，并根据 `strategy.py` 做事实推断。

提出下一轮建议时，必须按模块表达：

```text
保留：哪个 RegimeDetector / RegimeAlphaPolicy / RegimeAlphaMap / Filter / ExitPolicy / PositionMapper / StateRules。
修改：哪个模块、为什么、依据哪项数据。
避免：不要同时大改多个模块，不要为了收益牺牲 Sharpe 和 walk-forward 稳定性。
参数：哪些参数应收窄、扩展、固定或删除。
```

对 RegimeSwitchingAlpha + MultiTimeframeAlpha 的复盘重点：

```text
1. RegimeDetector 是否与 market_profile 的走势阶段和 stage_attribution 的真实阶段一致。
2. uptrend / range / downtrend / high_vol 等状态是否覆盖了主要行情，而不是过度细分。
3. 每个 regime 绑定的 Alpha 是否适合该阶段：趋势阶段是否用趋势/突破，震荡阶段是否用回归，下跌/高波是否防御。
4. 是否存在“所有 regime 共用同一个 alpha_score，只是根据 regime 调仓位”的问题；如果存在，必须指出。
5. long_window / mid_window / short_window 是否分工清楚：长期方向、中期主信号、短期入场/过热控制。
6. 不同周期是否互相确认，还是出现长期看多、短期追高、或中期信号滞后的冲突。
7. StateRules 是否抑制了 regime 频繁切换、target_S 剧烈变化和无效交易。
8. 某个阶段表现差时，要判断是 regime 识别错、该 regime 的 Alpha 不适配、PositionMapper 太保守/激进，还是 ExitPolicy 退出迟滞。
9. 如果 Alpha 本身不适合，应直接建议替换某个 regime 的 Alpha；不要只建议调参数。
```

可参考：

```text
/src/strategy_lab/skills/signal_agent/strategy-authoring/references/signal_strategy_library.md
```

重点参考其中的 RegimeSwitchingAlpha、MultiTimeframeAlpha、Filters、ExitPolicy、PositionMapper、StateRules 模板。不要直接替 SignalAgent 写完整策略脚本；你只输出可执行的修复方向。

## 图片分析

如果需要理解图像（调用方明确要求看图，或图像能提供数据文件没有直接表达的视觉信息时），例如：

```text
market_profile_chart.png
strategy_vs_benchmark.png
stage_return_comparison.png
```

如果你是多模态模型，可以使用read_file tool或 image-review skill ；如果你不是多模态模型，只使用image-review skill 

使用 `image-review` skill，通过命令调用独立多模态服务：

```powershell
python -m strategy_lab.cli image review IMAGE_PATH --question "请描述这张图中与策略复盘有关的关键信息"
```

## 必须分析的维度

```text
1. 总体判断
   promising / weak / overfit / unstable / needs_more_tests

2. 收益质量
   total_return、annual_return、sharpe、calmar、max_drawdown。

3. 基准对比
   benchmark_total_return、excess_total_return、information_ratio、outperform_day_ratio。

4. train / validation / walk-forward
   train_score、validation_score、walk_forward_mean_score、walk_forward_std_score、walk_forward_min_score。

5. 过拟合风险
   train-validation gap、validation 是否显著好于 train、walk-forward 最差 fold、参数搜索候选数量。

6. 市场阶段适应性
   使用 stage_attribution 分析每个阶段的策略收益、基准收益、超额收益、回撤、交易次数、trade_summary 和 trades。

7. Regime 与多周期 Alpha 适配性
   判断策略说明中的 regime 与真实市场阶段是否匹配；判断 RegimeAlphaMap 是否清楚；判断是否所有 regime 共用同一 Alpha；判断长/中/短周期窗口是否有效分工；判断失效阶段应优先修改 RegimeDetector、某个 regime 的 Alpha、PositionMapper 还是 ExitPolicy。

8. 交易行为
   order_count、signal_changes、exposure_mean、阶段内买卖次数、gross_turnover、target_signal_before/after，判断是否过度交易、过低暴露、追涨杀跌或退出迟滞。

9. 风险
   最大回撤、最差阶段、最差 fold、收益是否集中在少数阶段。

10. 下一轮建议
   给 SignalAgent 明确说明保留什么、修改什么、避免什么；建议必须映射到 RegimeDetector / RegimeAlphaPolicy / RegimeAlphaMap / Filters / ExitPolicy / PositionMapper / StateRules。任何阶段都可以建议替换 Alpha，只要有数据证据。
```

## 输出目录

必须写入：

```text
attempts/{attempt_id}/review/
  critic_review.json
  critic_review.md
  next_action.json
```

## critic_review.json 格式

```json
{
  "run_id": "...",
  "attempt_id": "...",
  "review_status": "completed",
  "overall_judgement": "promising | weak | overfit | unstable | needs_more_tests",
  "confidence": 0.0,
  "score": 0.0,
  "evidence_files": [],
  "key_findings": [],
  "strengths": [],
  "weaknesses": [],
  "overfit_analysis": {
    "judgement": "low | medium | high",
    "evidence": []
  },
  "walk_forward_analysis": {
    "judgement": "stable | mixed | unstable",
    "evidence": []
  },
  "benchmark_comparison": {
    "judgement": "outperform | similar | underperform",
    "evidence": []
  },
  "stage_attribution_analysis": {
    "best_stages": [],
    "worst_stages": [],
    "evidence": []
  },
  "risk_analysis": {},
  "trade_behavior_analysis": {},
  "strategy_framework_analysis": {
    "alpha": {"judgement": "...", "evidence": []},
    "filters": {"judgement": "...", "evidence": []},
    "exit_policy": {"judgement": "...", "evidence": []},
    "position_mapper": {"judgement": "...", "evidence": []},
    "state_rules": {"judgement": "...", "evidence": []}
  },
  "recommended_next_action": {
    "action": "keep_and_refine | mutate_strategy | expand_param_space | narrow_param_space | discard | needs_more_tests",
    "reason": "...",
    "specific_instructions": []
  }
}
```

## next_action.json 格式

`next_action.json` 是给 SignalAgent 读取的，必须短、明确、可执行：

```json
{
  "action": "keep_and_refine",
  "keep": [],
  "change": [],
  "avoid": [],
  "next_strategy_hints": [],
  "next_param_space_hints": [],
  "module_actions": {
    "alpha": "keep | modify | replace | unclear",
    "filters": "keep | add | remove | modify | unclear",
    "exit_policy": "keep | add | remove | modify | unclear",
    "position_mapper": "keep | modify | simplify | unclear",
    "state_rules": "keep | add | remove | modify | unclear"
  },
  "reason": "..."
}
```

## run_state.json 更新

写完文件后，必须调用系统服务更新对应 attempt。不要手动编辑 `run_state.json`。

命令格式：

```powershell
python -m strategy_lab.cli signal update-critic-review RUN_STATE_PATH ATTEMPT_ID --critic-review-path CRITIC_REVIEW_JSON --critic-review-md-path CRITIC_REVIEW_MD --next-action-path NEXT_ACTION_JSON --summary "复盘摘要"
```

示例：

```powershell
python -m strategy_lab.cli signal update-critic-review artifacts\signal_runs\signal_000300_xxx\run_state.json attempt_003 --critic-review-path artifacts\signal_runs\signal_000300_xxx\attempts\attempt_003\review\critic_review.json --critic-review-md-path artifacts\signal_runs\signal_000300_xxx\attempts\attempt_003\review\critic_review.md --next-action-path artifacts\signal_runs\signal_000300_xxx\attempts\attempt_003\review\next_action.json --summary "attempt_003 已完成单 attempt 复盘。"
```

服务会自动写入：

```text
status = reviewed
critic_review_path
critic_review_md_path
next_action_path
events
updated_at
```

## 最终检查

最终回复前必须执行检查。检查内容：

```text
1. review 目录下存在 critic_review.json、critic_review.md、next_action.json。
2. critic_review.json 和 next_action.json 可以被 json.loads 正常解析。
3. critic_review.md 非空，且包含总体判断、关键证据、风险分析、下一轮建议。
4. critic_review.md 必须按 Alpha / Filters / ExitPolicy / PositionMapper / StateRules 给出模块化判断。
5. run_state.json 中对应 attempt 的 status 已更新为 reviewed。
6. run_state.json 中对应 attempt 已写入 critic_review_path、critic_review_md_path、next_action_path。
7. 最终回复给调用方的路径与真实生成路径一致。
```
