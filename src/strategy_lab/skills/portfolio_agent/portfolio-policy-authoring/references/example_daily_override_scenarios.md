# Daily Override Scenarios

DailyPortfolioAgent 默认必须通过 `fusion_policy.py` 输出的 `W_base`。只有出现以下场景且证据充分时，才允许小幅修正。

## 场景 1：预算/信号严重冲突

触发条件：

```text
当日预算 Top-K 与信号 Top-K 重合度显著低于历史均值；
或 budget_signal_rank_corr 明显为负；
且 fusion_policy.py 给出高集中仓位。
```

需要额外查看：

```text
当日 R_i、S_i、W_base。
signal_profiles 中相关资产的 reliability 和 recommended_uses。
近期 20 日资产收益和波动。
```

允许动作：

```text
降低预算高但信号弱、可靠度低的资产。
把少量仓位转给信号强、可靠度高、风险可控的资产。
```

禁止动作：

```text
不得完全推翻 W_base。
不得突破 daily_decision_contract.json 的偏离限制。
```

## 场景 2：信号层集体失效但基础策略仍高仓

触发条件：

```text
多数资产 S_i 接近 0；
signal_breadth 明显低于历史均值；
但 W_base 的 gross_exposure 仍接近 max_gross。
```

允许动作：

```text
适度降低总仓位，提高现金。
优先保留预算层和信号层都不弱的资产。
```

## 场景 3：预算层漏选强信号资产

触发条件：

```text
某资产 R_i 为 0 或很低；
S_i 和 calibrated_signal_strength 很高；
signal_profile 显示 reliability 较高；
近期风险没有明显恶化。
```

允许动作：

```text
在 over_budget 或闲置资金范围内给予小仓位。
```

## 场景 4：高度相关资产集中

触发条件：

```text
W_base 集中在多个高相关资产；
相关资产组合权重超过风险容忍阈值。
```

允许动作：

```text
降低同类高相关资产总暴露。
保留其中预算/信号证据最强的资产。
```

## 场景 5：近期回撤异常

触发条件：

```text
组合近期回撤达到预设阈值；
W_base 继续放大造成回撤的主要资产或主题。
```

允许动作：

```text
适度降低相关资产仓位。
提高现金。
```

## 场景 6：重大外部事件或数据异常

触发条件：

```text
当日有重大新闻、交易异常、数据缺失、价格跳变或停牌风险；
fusion_policy.py 无法感知该信息。
```

允许动作：

```text
保持上一日仓位、降低异常资产仓位或转现金。
```
