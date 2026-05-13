---
name: strategy-authoring
description: "指导 SignalAgent 按模块化信号策略规范生成 strategy.py、strategy_spec.md、param_space.json 和 strategy_meta.json。"
license: Proprietary project skill
---

# Strategy Authoring Skill

## 适用场景

当 SignalAgent 需要生成一版新的信号层策略，或根据 CriticAgent 的复盘建议改写上一版策略时，使用本 skill。

本 skill 只负责策略创作与文件生成，不负责回测、参数搜索、阶段归因或复盘。策略生成完成后，应把生成的四件套交给 `attempt-evaluation` skill。

## 信号层统一定义

信号层策略统一定义为：

```text
SignalStrategy =
    RegimeDetector 市场状态识别
  + RegimeAlphaPolicy 分状态多周期 Alpha
  + Filters 辅助过滤器
  + ExitPolicy 出场与风控
  + PositionMapper 仓位映射
  + StateRules 状态与交易纪律
```

默认优先使用 `RegimeSwitchingAlpha + MultiTimeframeAlpha`：

```text
先用 RegimeDetector 判断当前市场状态；
再根据不同状态选择对应的 MultiTimeframeAlpha；
每个 MultiTimeframeAlpha 使用长 / 中 / 短三个窗口共同打分；
最后统一输出 target_S。
```

输出必须是目标仓位：

```text
target_S ∈ [0, 1]
```

禁止输出加减仓动作，例如 `+0.2` 或 `-0.3`。

信号层含义：

```text
final_target_account_weight = budget_weight * target_S
```

`current_position_in_budget` 表示：

```text
当前该资产实际持仓 / 当前该资产预算上限
```

策略内部可以读取 `current_position_in_budget` 做状态判断，但 `suggest()` 的返回值仍然必须是目标比例 `target_S`。

## 核心优化目标

当前信号层策略探索的最关键评估指标是：

```text
Sharpe
```

策略生成和迭代时，应优先提升 Sharpe，同时兼顾：

```text
max_drawdown
total_return
excess_total_return
walk_forward_mean_score
walk_forward_min_score
walk_forward_std_score
order_count / signal_changes
```

不能为了单纯提高收益而明显牺牲 Sharpe、最大回撤和 walk-forward 稳定性。

## 必须生成的四件套

每次策略创作必须生成：

```text
strategy.py
strategy_spec.md
param_space.json
strategy_meta.json
```

建议先写到临时生成目录，例如：

```text
artifacts/signal_runs/{run_id}/strategies/generated/{strategy_name}_{timestamp}/
```

然后把这些文件路径传给 `attempt-evaluation`。

## 参考文件

本 skill 提供参考模板。生成策略前应先阅读如下文件：

```text
src/strategy_lab/skills/signal_agent/strategy-authoring/references/signal_strategy_library.md
src/strategy_lab/skills/signal_agent/strategy-authoring/references/example_strategy.py
src/strategy_lab/skills/signal_agent/strategy-authoring/references/example_strategy_spec.md
src/strategy_lab/skills/signal_agent/strategy-authoring/references/example_param_space.json
src/strategy_lab/skills/signal_agent/strategy-authoring/references/example_strategy_meta.json
```

**重要**：signal_strategy_library.md是生成策略时的模板库和操作指南，必须全文阅读！！！
**重要**：参考文件只用于学习接口和结构，不要求照抄。

## strategy.py 接口要求

策略脚本必须提供策略类，默认类名为 `Strategy`。

推荐继承：

```python
from strategy_lab.signals.base import BaseSignalStrategy
```

必须实现：

```python
def suggest(self, history, current_position_in_budget=0.0) -> float:
    ...
```

规则：

```text
1. history 只包含截至当前 K 线的历史数据，不得使用未来数据。
2. 只能根据 history 计算信号。
3. 输出必须 clamp 到 [0, 1]。
4. 不得读取外部全量数据文件作弊。
5. 不得直接调用回测服务、DataAgent 或 CriticAgent。
6. 不得在 suggest 内写文件、联网或执行慢速全局扫描。
```

## 策略结构要求

每版策略必须在 `strategy_spec.md` 和 `strategy_meta.json` 中明确写出：

```text
RegimeDetector:
RegimeAlphaPolicy:
RegimeAlphaMap:
Filters:
ExitPolicy:
PositionMapper:
StateRules:
适合的市场阶段:
可能失效的市场阶段:
```

初始探索阶段允许简化，但仍必须有：

```text
RegimeDetector
RegimeAlphaPolicy
RegimeAlphaMap
ExitPolicy
PositionMapper
```

`RegimeAlphaMap` 必须显式说明每个核心 regime 绑定哪个 Alpha，例如：

```text
uptrend -> alpha_uptrend_trend_momentum
range -> alpha_range_mean_reversion
downtrend -> alpha_downtrend_flat
high_vol -> alpha_high_vol_defensive
```

不允许所有 regime 共用同一个 alpha_score 后只调整仓位。允许某些 regime 不交易，但必须写成明确的防御或空仓 Alpha。

## 阶段 1：RegimeAlpha 广泛探索

目标：找出该资产在不同市场状态下更适合哪类多周期 Alpha 组合。

阶段 1 采用批量探索，不要“写一个策略、评估一个策略、复盘一个策略”地完全串行推进。SignalAgent 应先一次性生成最多 4 个不同 `RegimeSwitchingAlpha + MultiTimeframeAlpha` 路线的策略目录，然后调用 `attempt-evaluation` 的批量模式统一评估。

要求：

```text
最多生成 4 个策略目录。
每个 attempt 必须包含 1 个 RegimeDetector。
每个 attempt 最多定义 3 个核心 regime，例如 uptrend / range / downtrend。
每个核心 regime 必须在 RegimeAlphaMap 中显式绑定 1 个 Alpha。
不同核心 regime 应使用不同 Alpha 逻辑；不允许所有 regime 共用同一个 Alpha。
每个 Alpha 最多使用 3 个周期窗口，例如 long_window / mid_window / short_window。
所有 Alpha 必须输出 0 到 1 的 alpha_score。
Filters 允许 0 到 1 个。
ExitPolicy 至少 1 个。
PositionMapper 必须 1 个。
StateRules 至少包含一个简单防抖或仓位变化控制规则。
自由参数建议 ≤ 8。
每个策略目录都必须包含 strategy.py、strategy_spec.md、param_space.json、strategy_meta.json。
```

候选方向菜单如下，仅供参考，不是必选项。SignalAgent 必须先阅读 market_profile，再自主决定阶段 1 生成哪些候选策略。不得机械固定生成同一组 4 类策略；如果画像不支持某类方向，不要为了凑数生成。

```text
1. 趋势主导型：uptrend 用趋势/动量，range 降仓，downtrend 空仓或防御。
2. 突破主导型：uptrend 用通道突破，range 用低仓观察，downtrend 空仓。
3. 震荡回归型：range 用 RSI/Bollinger/ZScore，uptrend 只做回调再启动，downtrend 空仓。
4. 防御稳健型：强调高波动降仓、趋势确认和较慢仓位变化。
5. 回调再启动型：uptrend 内等待短期回调修复后再入场。
6. 波动率收缩突破型：低波收敛后突破才提高仓位。
7. 高波动防御型：high_vol 下优先降仓，只保留极少量确认信号。
8. 低频确认型：减少交易次数，用长周期确认主方向。
9. 短反弹捕捉型：下跌或震荡环境中只捕捉短期修复。
10. 空仓优先 / 机会过滤型：默认不交易，只在高置信状态短暂参与。
```

阶段 1 可以少于 4 个候选。也允许多个候选属于同一大类，但它们的 RegimeAlphaMap、窗口、入场逻辑、退出逻辑或风控结构必须有实质差异。

阶段 1 结束后，通过 CriticAgent 多 attempt 横向比较，最多保留：

```text
primary_candidate: 1 个
fallback_candidate: 0 到 1 个
```

最多保留 2 个候选，其他方向应明确废弃或暂存。

推荐执行顺序：

```text
1. 读取 market_profile。
2. 根据 market_profile 自主设计最多 4 个 RegimeAlpha 方向，可以少于 4 个，不要机械套用固定模板。
3. 为每个方向分别生成完整四件套。
4. 把四个策略目录放在同一个父目录下，例如：
   artifacts/signal_runs/{run_id}/strategies/generated/alpha_batch_001/
5. 调用 attempt-evaluation 的批量模式：
   python -m strategy_lab.cli signal evaluate-attempts RUN_STATE_PATH --strategies-dir STRATEGIES_DIR --search-method ga --max-candidates 30 --max-workers 2 --batch-workers 1
6. 批量评估完成后，调用 CriticAgent 多 attempt 比较。
7. 根据 Sharpe、walk-forward 稳定性、最大回撤和复盘建议，最多保留 2 个候选进入阶段 2。
```

## 阶段 2：RegimeAlpha 深度探索与局部重构

目标：在阶段 1 的候选基础上，继续探索和确认“不同市场状态下到底该用什么 Alpha”。本阶段仍然以 RegimeDetector、RegimeAlphaPolicy 和 RegimeAlphaMap 为核心，不要过早把重点放到 Filter、ExitPolicy 或 PositionMapper 上。

允许动作：

```text
替换某个 regime 下的 Alpha，例如 uptrend 从 momentum 改成 breakout_reentry。
调整 RegimeDetector 的状态边界，例如 trend_threshold、vol_threshold、confirm_days。
拆分 regime，例如 uptrend 拆成 weak_uptrend / strong_uptrend。
合并 regime，例如 range 和 weak_downtrend 都走 defensive。
新增一个小批量对照 RegimeAlphaPolicy。
删除无效 regime 的交易逻辑，例如让 downtrend 直接 flat。
回退到历史最佳 attempt 后继续重构。
```

要求：

```text
每轮优先只改 RegimeDetector 或某一个 regime 的 Alpha，不要一次性重写所有模块。
Filter、ExitPolicy、PositionMapper 本阶段只能保持简单，避免掩盖 Alpha 好坏。
如果某个 regime 明显失败，应优先替换该 regime 的 Alpha，而不是只调入场阈值。
如果整个路线失败，可以重新做一批小规模 RegimeAlpha 探索。
自由参数建议 ≤ 10。
```

阶段 2 结束时，应尽量形成：

```text
primary_regime_alpha_policy: 1 个
fallback_regime_alpha_policy: 0 到 1 个
每个候选都必须说明 RegimeAlphaMap。
```

## 阶段 3：结构增强

目标：在已经相对有效的 RegimeAlphaPolicy 上增加交易结构，使策略更稳、更可交易。注意：即使进入本阶段，如果证据显示 Alpha 不合适，仍然允许调整或替换 Alpha。

允许动作：

```text
增加 1 个 Filter
替换 ExitPolicy
调整 PositionMapper，例如二元映射改成分段、连续映射或波动率打折
增加 cooldown / min_hold_days / max_daily_target_change
缩小或扩展参数空间
加入成交量确认、趋势保护或异常 K 线冷却
```

要求：

```text
每轮最多改 1 到 2 个结构模块。
如果增强后变差，必须回退，不要继续堆复杂度。
不要同时大改 RegimeDetector、多个 Alpha、ExitPolicy 和 PositionMapper。
Filters 总数建议 1 到 2 个，最多 3 个。
ExitPolicy 总数 1 到 2 个。
PositionMapper 仍只能 1 个。
自由参数建议 ≤ 10。
```

## 阶段 4：稳健性精修与最终选择

目标：提升 Sharpe 和 walk-forward 稳定性，降低最大回撤、无效交易和过拟合风险。Alpha / RegimeAlphaPolicy 在本阶段仍然允许调整；如果复盘证据显示问题来自 Alpha 本身，不要只调参数。

优先调整：

```text
入场阈值
退出阈值
止损倍数
仓位上限
波动率打折
cooldown
min_hold_days
参数空间边界
RegimeDetector 边界
某个 regime 的 Alpha 选择
```

**重要原则**：Alpha / RegimeAlphaPolicy 不是阶段 1 后冻结的对象。任何阶段都允许根据证据替换某个 regime 的 Alpha、调整 RegimeDetector、回退到历史最佳 attempt，或重新生成一批对照策略。总之，你的任务是合理尝试、充分探索，尽可能找出最优策略，而不是机械沿着上一版小修小补。

## 停止条件：

```text
- attempt 的数量达到 run_state.json 的 steps.strategy_search.max_iterations（如果没有这个值，就按20算）
- 连续 3 轮 Sharpe 或综合表现没有明显改善。
- walk-forward 稳定性明显恶化。
- CriticAgent 明确建议停止。
- 策略复杂度超过约束条件。
- 用户要求暂停或停止。
```

停止后从全部的结果中选出最优策略作为最终结果。

## 市场画像如何影响策略选择

生成策略前应阅读 market_profile，重点关注：

```text
trend_label
total_return
max_drawdown
volatility_regime
趋势阶段占比
震荡阶段占比
高波动阶段
阶段切换频率
```

RegimeDetector、RegimeAlphaPolicy、Filters、ExitPolicy、PositionMapper、StateRules 的具体选择规则，不在本文件重复展开。必须详细阅读：

```text
src/strategy_lab/skills/signal_agent/strategy-authoring/references/signal_strategy_library.md
```

该文件包含 RegimeSwitchingAlpha + MultiTimeframeAlpha 的写法、不同市场画像下的 Alpha 方向、过滤器、出场策略、仓位映射、状态规则、推荐组合以及指标计算参考。SignalAgent 生成策略前必须先阅读该文件，再结合 market_profile 选择策略结构。

## 参数空间要求

`param_space.json` 应覆盖策略代码中所有可调参数。

推荐使用离散 `values`：

```json
{
  "ma_window": {"type": "int", "values": [10, 20, 60], "default": 20},
  "band": {"type": "float", "values": [0.0, 0.005, 0.01], "default": 0.0}
}
```

也可以使用连续展开：

```json
{
  "ma_window": {"type": "int", "low": 5, "high": 120, "step": 5, "default": 20}
}
```

建议：

```text
初始探索参数数量 ≤ 6。
结构增强和精修参数数量 ≤ 8。
优先给清晰、少量、有意义的候选值。
不要给过宽、过密、无解释的搜索空间。
```

## 复杂度约束

```text
RegimeDetector 数量: 1
核心 regime 数量: 2 到 4，建议 3
每个 regime 的 Alpha 数量: 1
每个 Alpha 的周期窗口数量: 2 到 3
Filters 数量: 0 到 3
ExitPolicy 数量: 1 到 2
PositionMapper 数量: 1
StateRules 数量: 1 到 3
自由参数总数: ≤ 10
```

如果想超过这些限制，必须在 `strategy_spec.md` 中写明原因，并说明如何避免过拟合。

## signal_agent_memory.md

每完成一轮策略生成、评估和复盘后，SignalAgent 必须维护：

```text
artifacts/signal_runs/{run_id}/reports/signal_agent_memory.md
```

每轮追加简要摘要，不复制大段 JSON。

建议格式：

```markdown
## Iteration 003

- stage: alpha_exploration / structure_enhancement / robustness_refinement
- attempt_id:
- strategy_name:
- structure:
  - Alpha:
  - Filters:
  - ExitPolicy:
  - PositionMapper:
  - StateRules:
- evaluation:
  - sharpe:
  - total_return:
  - max_drawdown:
  - walk_forward_mean:
  - walk_forward_min:
- critic_summary:
- decision:
- next_plan:
```

## 生成后自检

生成四件套后，SignalAgent 必须检查：

```text
1. strategy.py 可以被 Python 编译。
2. 策略类名与 strategy_class_name 一致。
3. suggest() 返回 target_S，不是加减仓动作。
4. target_S 已限制在 [0, 1]。
5. 未使用未来数据。
6. param_space.json 是合法 JSON。
7. param_space.json 覆盖策略代码中主要可调参数。
8. strategy_spec.md 写明 Alpha / Filters / ExitPolicy / PositionMapper / StateRules。
9. 自由参数数量没有超过当前阶段限制。
10. 没有把回测结果硬编码进策略。
```

## 完成后调用 attempt-evaluation

生成四件套后，调用：

```powershell
python -m strategy_lab.cli signal evaluate-attempt RUN_STATE_PATH --strategy-path STRATEGY_PATH --strategy-spec-path STRATEGY_SPEC_PATH --param-space-path PARAM_SPACE_PATH --strategy-meta-path STRATEGY_META_PATH --strategy-name STRATEGY_NAME --search-method ga --max-candidates 30 --max-workers 2
```

如果只是快速测试，可降低候选数：

```powershell
python -m strategy_lab.cli signal evaluate-attempt RUN_STATE_PATH --strategy-path STRATEGY_PATH --strategy-spec-path STRATEGY_SPEC_PATH --param-space-path PARAM_SPACE_PATH --strategy-meta-path STRATEGY_META_PATH --strategy-name STRATEGY_NAME --search-method grid --max-candidates 5 --max-workers 1 --no-stage-chart
```

如果 `attempt-evaluation` 返回 `failed`，不要进入 CriticAgent 复盘。应读取 `error_path`，修复策略文件或参数空间后重新评估。
