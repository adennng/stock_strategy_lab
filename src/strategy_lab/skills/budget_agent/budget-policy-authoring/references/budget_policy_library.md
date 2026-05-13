---
name: budget_policy_library
description: 预算层策略结构化配置、模块库、参数范围和策略模板参考，用于引导 BudgetAgent 生成可回测、可优化、可复盘的预算层策略配置。
---

# 预算层策略库

本文档用于引导 BudgetAgent 编写预算层策略。预算层的第一版采用“结构化策略配置 + 系统通用执行器”的方式，不建议让 LLM 每次自由编写完整 Python 策略脚本。

预算层的目标不是直接决定某个资产内部什么时候买卖，而是决定资产池中每个资产每日允许使用的预算上限。

在预算层独立训练时：

```text
target_position_i(t) = R_i(t)
```

在后续组合层把预算层与信号层融合时，可能使用类似下面的关系：

```text
target_position_i(t) = R_i(t) * S_i(t)
```

其中：

- `R_i(t)` 是预算层输出的资产 `i` 在日期 `t` 的预算上限，范围通常为 `[0, 1]`。
- `S_i(t)` 是信号层输出的资产内部目标仓位强度，范围通常为 `[0, 1]`。

预算层不要重复实现信号层的入场、出场、止损、技术形态细节，也不要直接使用信号层当前信号作为预算层标准打分输入。预算层重点处理资产池内的横截面选择、资金预算、分散度、风险暴露和换手控制；信号层与预算层的真实仓位融合由组合层处理。

## 1. 预算层标准结构

预算层策略统一定义为：

```text
BudgetPolicy =
    UniverseGate        候选资产准入
  + AssetScorer         资产打分
  + AllocationEngine    预算分配
  + RiskOverlay         风险覆盖
  + RebalanceScheduler  调仓节奏
  + ConstraintProjector 约束投影
  + Diagnostics         诊断输出
```

LLM 生成的每个预算策略配置都必须包含以上 7 个顶层字段。即使某个模块不需要复杂逻辑，也要显式写出空配置或简单配置，避免执行器猜测。

## 2. 标准产物文件

预算层策略建议保存为以下文件：

```text
budget_policy_config.json
budget_policy_spec.md
param_space.json
budget_policy_meta.json
```

### 2.1 budget_policy_config.json

这是最核心的策略配置文件，由 LLM 生成，系统通用预算策略执行器读取并执行。

标准结构：

```json
{
  "policy_name": "risk_adjusted_momentum_topk",
  "policy_version": "0.1.0",
  "universe_gate": {
    "gates": []
  },
  "asset_scorer": {
    "scorers": []
  },
  "allocation_engine": {
    "type": "topk_score_weighted",
    "params": {}
  },
  "risk_overlay": {
    "overlays": []
  },
  "rebalance_scheduler": {
    "type": "every_n_days_with_threshold",
    "params": {}
  },
  "constraint_projector": {
    "type": "long_only_cap_normalize",
    "params": {}
  },
  "diagnostics": {
    "enabled": true,
    "params": {}
  }
}
```

### 2.2 budget_policy_spec.md

自然语言策略说明，由 LLM 生成。至少说明：

- 策略名称
- 适用资产池特征
- 使用的数据字段
- 7 个模块分别采用什么配置
- 主要参数含义
- 参数搜索重点
- 预期优势
- 主要风险
- 不适用场景

### 2.3 param_space.json

参数搜索空间，由 LLM 生成。只写需要搜索的参数。固定参数保留在 `budget_policy_config.json` 中即可。

参数路径应对应 `budget_policy_config.json` 中的字段路径。

示例：

```json
{
  "search_method_hint": "ga",
  "max_candidates_hint": 80,
  "params": {
    "universe_gate.gates[1].params.window": {
      "type": "int",
      "choices": [20, 40, 60, 120]
    },
    "allocation_engine.params.top_k": {
      "type": "int",
      "min": 2,
      "max": 6,
      "step": 1
    },
    "constraint_projector.params.max_asset_weight": {
      "type": "float",
      "choices": [0.15, 0.20, 0.25, 0.30]
    }
  }
}
```

### 2.4 budget_policy_meta.json

策略元数据，由 LLM 生成。用于登记策略来源、结构摘要和版本信息。

示例：

```json
{
  "policy_name": "risk_adjusted_momentum_topk",
  "created_by": "BudgetAgent",
  "policy_mode": "structured_config",
  "policy_version": "0.1.0",
  "expected_output": "daily_budget_weights",
  "uses_signal_layer": false,
  "notes": []
}
```

## 3. 配置总览

下面是一个完整配置示例。

```json
{
  "policy_name": "multi_window_momentum_topk",
  "policy_version": "0.1.0",
  "universe_gate": {
    "gates": [
      {
        "type": "data_availability_gate",
        "params": {
          "min_history_days": 120,
          "max_recent_missing_days": 0,
          "require_current_bar": true
        }
      },
      {
        "type": "absolute_momentum_gate",
        "params": {
          "window": 60,
          "threshold": 0.0
        }
      }
    ]
  },
  "asset_scorer": {
    "scorers": [
      {
        "type": "multi_window_momentum",
        "weight": 0.6,
        "params": {
          "windows": [20, 60, 120],
          "weights": [0.3, 0.4, 0.3]
        }
      },
      {
        "type": "risk_adjusted_momentum",
        "weight": 0.4,
        "params": {
          "momentum_window": 60,
          "vol_window": 20
        }
      }
    ],
    "normalization": "rank_pct"
  },
  "allocation_engine": {
    "type": "topk_score_weighted",
    "params": {
      "top_k": 4,
      "score_floor": 0.0
    }
  },
  "risk_overlay": {
    "overlays": [
      {
        "type": "turnover_cap",
        "params": {
          "max_daily_turnover": 0.4
        }
      }
    ]
  },
  "rebalance_scheduler": {
    "type": "every_n_days_with_threshold",
    "params": {
      "rebalance_days": 5,
      "min_weight_change": 0.05
    }
  },
  "constraint_projector": {
    "type": "long_only_cap_normalize",
    "params": {
      "gross_exposure": 0.8,
      "max_asset_weight": 0.25,
      "min_weight": 0.02,
      "max_holding_count": 4
    }
  },
  "diagnostics": {
    "enabled": true,
    "params": {
      "save_daily_scores": true,
      "save_gate_results": true,
      "save_turnover": true
    }
  }
}
```

## 4. UniverseGate 候选资产准入

UniverseGate 决定资产当天是否进入候选池。通过准入不代表一定持仓，只代表可以进入打分和预算分配。

标准配置：

```json
{
  "universe_gate": {
    "gates": [
      {
        "type": "data_availability_gate",
        "params": {}
      }
    ]
  }
}
```

多个 gate 同时存在时，默认全部通过才进入候选池。

### 4.1 data_availability_gate

用途：过滤数据不足、当日无数据、近期缺失过多的资产。

建议所有策略都使用。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `min_history_days` | 至少需要多少个历史交易日 | 20 到 252 | 120 | 指标需要历史窗口时 |
| `max_recent_missing_days` | 最近允许连续缺失天数 | 0 到 5 | 0 | 数据质量不稳定时 |
| `require_current_bar` | 当日必须有行情 | true / false | true | 日频预算必须 true |

示例：

```json
{
  "type": "data_availability_gate",
  "params": {
    "min_history_days": 120,
    "max_recent_missing_days": 0,
    "require_current_bar": true
  }
}
```

### 4.2 absolute_momentum_gate

用途：只允许绝对涨幅达标的资产进入候选池。

计算逻辑：

```text
close_t / close_{t-window} - 1 > threshold
```

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `window` | 动量观察窗口 | 20、40、60、120、252 | 60 | 趋势或轮动策略 |
| `threshold` | 最低绝对动量 | -0.05 到 0.10 | 0.0 | 熊市降仓、趋势过滤 |

适用：

- 权益类、行业类、主题类资产池
- 希望降低下跌资产暴露

不适用：

- 强均值回归资产池
- 用户要求始终满仓时

### 4.3 trend_filter_gate

用途：用均线和趋势斜率判断资产是否处于趋势状态。

常见逻辑：

```text
close > moving_average(close, ma_window)
moving_average_slope >= min_slope
```

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `ma_window` | 均线窗口 | 20、60、120、200 | 120 | 中期趋势过滤 |
| `slope_window` | 斜率计算窗口 | 5、10、20、40 | 20 | 过滤走平资产 |
| `min_slope` | 最低斜率 | -0.001 到 0.002 | 0.0 | 要求趋势向上时 |
| `require_price_above_ma` | 是否要求价格在均线上 | true / false | true | 趋势资产池 |

适用：

- 趋势延续明显的指数、ETF、商品类资产

### 4.4 risk_filter_gate

用途：过滤近期波动或回撤过高的资产。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `vol_window` | 波动率窗口 | 20、40、60、120 | 60 | 高波动资产池 |
| `max_annual_vol` | 年化波动率上限 | 0.15 到 0.80 | 0.45 | 限制极端波动 |
| `drawdown_window` | 回撤窗口 | 20、60、120、252 | 120 | 回撤过滤 |
| `max_drawdown_limit` | 最大允许回撤 | -0.10 到 -0.50 | -0.25 | 避免下跌失控 |

适用：

- 高波动主题资产
- 资产池里有明显极端风险标的

### 4.5 liquidity_filter_gate

用途：过滤成交量或成交额不足的资产。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `volume_window` | 成交量均值窗口 | 5、20、60 | 20 | 成交量过滤 |
| `min_avg_volume` | 最低平均成交量 | 按资产池设定 | null | 股票或流动性差异大的 ETF |

适用：

- 股票池
- 流动性差异很大的 ETF 或基金池

如果数据中没有可靠成交量，可以不使用。

### 4.6 metadata_filter_gate

用途：根据资产类型、市场、用户指定资产代码过滤资产。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `include_asset_types` | 允许的资产类型 | ETF、index、stock、fund、bond、commodity 等 | [] | 用户限定资产类型 |
| `exclude_asset_types` | 排除的资产类型 | 同上 | [] | 排除不想交易资产 |
| `include_markets` | 允许市场 | SH、SZ、HK、US 等 | [] | 用户限定市场 |
| `exclude_symbols` | 排除资产代码 | symbol 列表 | [] | 人工剔除 |

注意：

- 元数据不完整时不要强依赖此 gate。
- 不要用名称关键词硬编码策略逻辑。

## 5. AssetScorer 资产打分

AssetScorer 给通过准入的资产打分。多个 scorer 加权合成。

标准配置：

```json
{
  "asset_scorer": {
    "scorers": [
      {
        "type": "relative_momentum",
        "weight": 1.0,
        "params": {
          "window": 60
        }
      }
    ],
    "normalization": "rank_pct"
  }
}
```

`normalization` 可选：

| 值 | 含义 | 建议 |
| --- | --- | --- |
| `rank_pct` | 横截面排名百分位 | 默认推荐 |
| `zscore` | 横截面 z-score | 资产数量较多时 |
| `minmax` | 最小最大归一化 | 分数范围稳定时 |
| `none` | 不归一化 | 不推荐，除非单 scorer |

多个 scorer 的 `weight` 建议总和为 1。执行器可自动归一化，但 LLM 应尽量写清楚。

### 5.1 relative_momentum

用途：选择过去一段时间表现更强的资产。

计算：

```text
close_t / close_{t-window} - 1
```

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `window` | 动量窗口 | 20、40、60、120、252 | 60 | 横截面轮动 |

### 5.2 multi_window_momentum

用途：综合多个周期的动量，减少单窗口偶然性。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `windows` | 动量窗口列表 | [20,60,120]、[40,120,252] | [20,60,120] | 中短期结合 |
| `weights` | 各窗口权重 | 每项 0 到 1，总和约 1 | [0.3,0.4,0.3] | 控制周期偏好 |

适用：

- 资产轮动节奏变化明显
- 第一轮主力 scorer

### 5.3 risk_adjusted_momentum

用途：选择收益强且波动不过高的资产。

计算：

```text
momentum / annualized_volatility
```

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `momentum_window` | 动量窗口 | 40、60、120、252 | 60 | 趋势强弱 |
| `vol_window` | 波动窗口 | 20、40、60、120 | 20 | 风险调整 |
| `vol_floor` | 波动率下限，防止除零 | 0.01 到 0.10 | 0.03 | 稳定计算 |

适用：

- 高波动资产池
- 不希望单纯追涨

### 5.4 trend_quality

用途：衡量趋势是否平滑、持续。

可计算指标：

- 价格相对均线距离
- 均线斜率
- 上涨天数占比
- 趋势回归 R2

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `window` | 趋势质量窗口 | 20、60、120 | 60 | 趋势稳定性 |
| `method` | 计算方式 | `slope`、`up_day_ratio`、`r2`、`composite` | `composite` | 按执行器支持选择 |

### 5.5 low_corr_bonus

用途：奖励与其他资产相关性较低的资产，提高分散度。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `corr_window` | 相关性窗口 | 60、120、252 | 120 | 分散度控制 |
| `reference` | 相关性参考 | `asset_pool`、`current_portfolio` | `asset_pool` | 第一版用资产池 |

适用：

- 平均相关性较高
- 资产池里存在重复暴露

### 5.6 inverse_vol_preference

用途：偏好低波动资产。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `vol_window` | 波动率窗口 | 20、60、120 | 60 | 风险优先配置 |
| `vol_floor` | 波动率下限 | 0.01 到 0.10 | 0.03 | 防止极端权重 |

适用：

- 混合资产池
- 风险平衡策略

### 5.7 drawdown_resilience

用途：偏好近期回撤较小的资产。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 适用情况 |
| --- | --- | --- | --- | --- |
| `drawdown_window` | 回撤窗口 | 20、60、120、252 | 120 | 回撤控制 |

计算：

```text
score = - rolling_max_drawdown
```

## 6. AllocationEngine 预算分配

AllocationEngine 将候选资产的分数转换为初始预算权重。最终权重还要经过 RiskOverlay 和 ConstraintProjector。

### 6.1 topk_equal

逻辑：选分数最高的 K 个资产，等权分配。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `top_k` | 最多选择资产数 | 2 到 10 | 4 |

适用：

- 第一版基线
- 资产数量不多
- 分数可靠性一般

### 6.2 topk_score_weighted

逻辑：选分数最高的 K 个资产，按正分数归一化分配。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `top_k` | 最多选择资产数 | 2 到 10 | 4 |
| `score_floor` | 低于该分数不分配 | 0 到 0.8 | 0.0 |
| `power` | 分数放大系数 | 0.5 到 3.0 | 1.0 |

适用：

- 分数质量较高
- 希望强者多配

### 6.3 softmax_weighted

逻辑：用 softmax 将分数平滑转换成权重。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `temperature` | 温度，越小越集中 | 0.2 到 2.0 | 0.8 |
| `top_k` | 可选，先截取前 K 个 | 2 到 20 | null |

适用：

- 希望权重平滑
- 不希望只有极少数资产持仓

### 6.4 inverse_vol

逻辑：按波动率倒数分配。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `vol_window` | 波动率窗口 | 20、60、120 | 60 |
| `vol_floor` | 波动率下限 | 0.01 到 0.10 | 0.03 |
| `top_k` | 可选，限制最多资产数 | 2 到 20 | null |

适用：

- 风险均衡优先
- 多资产类型混合

### 6.5 risk_parity_simple

逻辑：简化风险平价，第一版可近似为 inverse-vol 权重。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `vol_window` | 波动率窗口 | 60、120、252 | 120 |
| `use_covariance` | 是否使用协方差 | true / false | false |

适用：

- 资产风险等级差异明显
- 用户偏稳健

### 6.6 cluster_budget_then_within_cluster

逻辑：先给手写分组分配预算，再在组内分配。

本框架第一版不提供自动相关性分组，也不使用 metadata 自动分组。如果需要分组，只使用 `explicit` 分组。用户可以用自然语言指定分组，LLM 也可以根据预算画像、相关性摘要、收益风险特征自主划分分组，但最终都必须把完整分组写入 `budget_policy_config.json` 的 `groups` 字段。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `cluster_source` | 分组来源 | 只能为 `explicit` | `explicit` |
| `groups` | 手写分组列表 | 每个资产最多属于一个主组 | 必填 |
| `cluster_cap` | 单簇最大权重 | 0.25 到 0.70 | 0.40 |
| `within_cluster_method` | 簇内分配方式 | `equal`、`score_weighted`、`inverse_vol` | `score_weighted` |
| `top_k_per_cluster` | 每簇最多资产数 | 1 到 5 | 2 |

适用：

- 用户或 LLM 能明确给出分组
- 预算画像显示若干资产高度相似，需要控制同组暴露
- 希望避免单一主题过度集中

`groups` 示例：

```json
[
  {
    "group_id": "group_1",
    "group_name": "手写分组1",
    "symbols": ["SYMBOL_A", "SYMBOL_B"],
    "source": "llm",
    "rationale": "根据预算画像中的高相关性、收益风险特征和用户描述划分。"
  },
  {
    "group_id": "ungrouped",
    "group_name": "未归组资产",
    "symbols": ["SYMBOL_C"],
    "source": "llm",
    "rationale": "未能归入其他主组，单独保留。"
  }
]
```

### 6.7 score_vol_blend

逻辑：同时考虑分数和波动率。

计算：

```text
raw_weight_i = positive_score_i^alpha / vol_i^beta
```

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `alpha` | 分数权重强度 | 0.5 到 2.0 | 1.0 |
| `beta` | 波动惩罚强度 | 0.5 到 2.0 | 1.0 |
| `vol_window` | 波动率窗口 | 20、60、120 | 60 |
| `top_k` | 最多选择资产数 | 2 到 10 | 4 |

适用：

- 希望收益动量和风险预算兼顾

## 7. RiskOverlay 风险覆盖

RiskOverlay 对初始权重进行风险修正。可以为空，但建议至少包含换手或波动控制之一。

标准配置：

```json
{
  "risk_overlay": {
    "overlays": [
      {
        "type": "turnover_cap",
        "params": {
          "max_daily_turnover": 0.4
        }
      }
    ]
  }
}
```

### 7.1 vol_target

用途：根据组合估计波动率调整总仓位。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `target_vol` | 目标年化波动率 | 0.08 到 0.30 | 0.18 |
| `vol_window` | 波动率窗口 | 20、60、120 | 60 |
| `min_gross` | 最低总仓位 | 0.0 到 0.8 | 0.3 |
| `max_gross` | 最高总仓位 | 0.5 到 1.0 | 1.0 |

适用：

- 组合波动变化大
- 希望风险稳定

### 7.2 drawdown_cut

用途：资产或组合近期回撤过大时降权。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `drawdown_window` | 回撤窗口 | 20、60、120、252 | 120 |
| `asset_drawdown_limit` | 单资产回撤阈值 | -0.10 到 -0.50 | -0.25 |
| `portfolio_drawdown_limit` | 组合回撤阈值 | -0.05 到 -0.30 | -0.15 |
| `cut_ratio` | 触发后降权比例 | 0.2 到 1.0 | 0.5 |

适用：

- 回撤控制优先

### 7.3 high_vol_discount

用途：短期波动急升时降权。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `short_vol_window` | 短期波动窗口 | 5、10、20 | 20 |
| `long_vol_window` | 长期波动窗口 | 60、120 | 60 |
| `discount_strength` | 降权强度 | 0.1 到 0.8 | 0.4 |

适用：

- 高波动资产池
- 极端行情中防追高

### 7.4 turnover_cap

用途：限制单日预算变化，降低交易成本。

参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `max_daily_turnover` | 单日最大换手 | 0.10 到 1.00 | 0.40 |
| `redistribute_remaining` | 被限制部分是否重分配 | true / false | false |

适用：

- 任何日频预算策略
- 交易成本敏感时

### 7.5 budget_smoothing

用途：用上一期预算平滑当前预算。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `smooth` | 上一期预算权重 | 0.0 到 0.9 | 0.4 |

计算：

```text
budget_t = smooth * budget_{t-1} + (1 - smooth) * raw_budget_t
```

适用：

- 权重跳变明显
- 希望降低调仓频率

### 7.6 cash_buffer

用途：主动保留现金。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `cash_buffer` | 现金比例 | 0.0 到 0.5 | 0.0 |
| `risk_on_cash_buffer` | 风险偏好时现金 | 0.0 到 0.2 | 0.0 |
| `risk_off_cash_buffer` | 风险规避时现金 | 0.1 到 0.6 | 0.3 |

适用：

- 市场整体风险高
- 用户不要求满仓

### 7.7 cluster_cap

用途：限制手写分组的总预算。

本覆盖项只读取 `budget_policy_config.json` 中的 `explicit` 分组。相关性摘要可以作为 LLM 手写分组的参考，但执行器不会自动用相关性生成分组。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `cluster_source` | 分组来源 | 只能为 `explicit` | `explicit` |
| `cluster_cap` | 单簇上限 | 0.25 到 0.70 | 0.40 |

适用：

- 用户或 LLM 已经写出 `groups`
- 希望限制某个手写分组的预算上限

## 8. 手写分组规则

预算层第一版只支持手写分组，配置值统一为：

```json
"cluster_source": "explicit"
```

手写分组有两个来源：

- 用户自然语言指定：BudgetAgent 负责把用户要求转成 `groups`。
- LLM 自主划分：BudgetAgent 可以参考预算画像中的相关性摘要、收益风险特征、数据覆盖情况和用户目标，自主写出 `groups`。

无论来源是什么，执行器只读取 `budget_policy_config.json` 中的结构化 `groups`，不读取自然语言，也不会自动生成分组。

`groups` 必须满足：

- 每个资产最多属于一个主组。
- 未归组资产必须放入 `ungrouped`。
- 每个组必须有 `group_id`、`group_name`、`symbols`、`source`、`rationale`。
- `source` 只能是 `user` 或 `llm`。
- 不允许只根据资产名称关键词强行分组。
- 如果参考相关性，必须说明参考的是画像阶段的全样本相关性摘要，仅用于理解资产池和手写分组，不作为每日执行时的自动分组信号。

`groups` 示例：

```json
[
  {
    "group_id": "financial_like",
    "group_name": "金融相关资产",
    "symbols": ["512880.SH", "512800.SH", "512070.SH"],
    "source": "user",
    "rationale": "用户明确要求将这些资产作为同一风险组。"
  },
  {
    "group_id": "high_corr_growth",
    "group_name": "高相关成长资产",
    "symbols": ["159995.SZ", "159819.SZ", "159852.SZ"],
    "source": "llm",
    "rationale": "预算画像显示这些资产收益相关性较高，且收益风险特征接近，因此作为同一风险组控制总预算。"
  },
  {
    "group_id": "ungrouped",
    "group_name": "未归组资产",
    "symbols": ["SYMBOL_X"],
    "source": "llm",
    "rationale": "未能归入其他主组，单独保留。"
  }
]
```

## 9. RebalanceScheduler 调仓节奏

RebalanceScheduler 决定什么时候允许产生新的预算。如果非调仓日，通常沿用上一期预算。

每个预算策略必须显式包含该模块。

### 8.1 daily

每日调仓。

参数：

```json
{}
```

适用：

- 资产轮动很快
- 回测规则允许日频调仓

风险：

- 换手高
- 过度响应噪声

### 8.2 every_n_days

每 N 个交易日调仓一次。

参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `rebalance_days` | 调仓间隔 | 2 到 21 | 5 |

适用：

- 周频或月频轮动
- 降低换手

### 8.3 every_n_days_with_threshold

每 N 天检查一次，且权重变化超过阈值才调仓。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `rebalance_days` | 调仓间隔 | 2 到 21 | 5 |
| `min_weight_change` | 单资产最小调仓差异 | 0.01 到 0.10 | 0.05 |
| `min_total_turnover` | 组合最小换手 | 0.02 到 0.30 | 0.10 |

适用：

- 默认推荐
- 希望降低无意义微调

### 8.4 risk_triggered

风险状态触发时调仓。

参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `vol_trigger` | 组合波动触发阈值 | 0.10 到 0.40 | 0.25 |
| `drawdown_trigger` | 组合回撤触发阈值 | -0.05 到 -0.30 | -0.10 |
| `min_days_between_rebalance` | 两次调仓最小间隔 | 1 到 20 | 5 |

适用：

- 长周期配置
- 风险控制优先

### 8.5 calendar_and_threshold

固定调仓周期加阈值触发。

可选参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `calendar` | 调仓周期 | `weekly`、`monthly`、`quarterly` | `weekly` |
| `min_weight_change` | 单资产最小调仓差异 | 0.01 到 0.10 | 0.05 |

适用：

- 更接近人工投研流程

## 10. ConstraintProjector 约束投影

ConstraintProjector 是预算层最后一道硬约束。无论前面的模块如何输出，最终结果必须在这里合法化。

标准配置：

```json
{
  "constraint_projector": {
    "type": "long_only_cap_normalize",
    "params": {
      "gross_exposure": 0.8,
      "max_asset_weight": 0.25,
      "min_weight": 0.02,
      "max_holding_count": 4
    }
  }
}
```

### 9.1 long_only_cap_normalize

用途：非负、单资产上限、总仓位上限、小权重清零、必要时重新归一化。

参数：

| 参数 | 含义 | 建议范围 | 默认建议 | 说明 |
| --- | --- | --- | --- | --- |
| `gross_exposure` | 总预算上限 | 0.30 到 1.00 | 0.80 | 允许保留现金 |
| `max_asset_weight` | 单资产上限 | 0.05 到 0.50 | 0.25 | 防集中 |
| `min_weight` | 小权重清零阈值 | 0.00 到 0.05 | 0.02 | 控制碎片仓位 |
| `max_holding_count` | 最大持仓数量 | 1 到 20 | 4 | 控制分散度 |
| `renormalize_after_clip` | 截断后是否归一化 | true / false | true | 保持目标总仓位 |
| `rounding_digits` | 权重小数位 | 4 到 8 | 6 | 输出整洁 |

持仓数量控制机制：

1. UniverseGate 先过滤不能持有的资产。
2. AllocationEngine 的 `top_k` 先限制候选持仓数量。
3. ConstraintProjector 的 `min_weight` 将过小权重清零。
4. ConstraintProjector 的 `max_holding_count` 再强制保留权重最大的前 N 个。

建议：

| 资产池规模 | 建议最大持仓数 |
| --- | --- |
| 资产数 <= 8 | 2 到 3 |
| 资产数 9 到 20 | 3 到 6 |
| 资产数 21 到 50 | 5 到 10 |
| 资产数 > 50 | 8 到 20，并结合流动性和相关性 |

### 9.2 long_only_full_invest_cap

用途：要求尽量满仓，但仍满足单资产上限。

参数：

| 参数 | 含义 | 建议范围 | 默认建议 |
| --- | --- | --- | --- |
| `gross_exposure` | 固定总仓位 | 0.90 到 1.00 | 1.00 |
| `max_asset_weight` | 单资产上限 | 0.05 到 0.50 | 0.25 |
| `min_weight` | 小权重清零阈值 | 0.00 到 0.05 | 0.02 |
| `max_holding_count` | 最大持仓数量 | 1 到 20 | 4 |

适用：

- 用户明确要求不留现金

风险：

- 市场整体风险高时回撤可能更大

## 11. Diagnostics 诊断输出

Diagnostics 决定策略执行时保存哪些可解释信息。

标准配置：

```json
{
  "diagnostics": {
    "enabled": true,
    "params": {
      "save_daily_scores": true,
      "save_gate_results": true,
      "save_raw_weights": true,
      "save_final_weights": true,
      "save_turnover": true,
      "save_constraint_events": true
    }
  }
}
```

参数：

| 参数 | 含义 | 默认建议 |
| --- | --- | --- |
| `save_daily_scores` | 保存每日资产分数 | true |
| `save_gate_results` | 保存每日准入结果 | true |
| `save_raw_weights` | 保存约束前权重 | true |
| `save_final_weights` | 保存最终预算权重 | true |
| `save_turnover` | 保存换手信息 | true |
| `save_constraint_events` | 保存约束触发记录 | true |
| `save_reason_text` | 保存简短文字理由 | false |

诊断输出用于后续复盘，不要关闭。

## 12. 策略模板

每个模板都必须包含 7 个模块。LLM 可以根据预算画像选择模板，也可以组合模板，但不能跳过硬约束。

### 模板 A：横截面动量 TopK

适用：

- 资产池同质
- 强弱分化明显
- 希望第一版简单稳健

```json
{
  "universe_gate": {
    "gates": [
      {"type": "data_availability_gate", "params": {"min_history_days": 120, "require_current_bar": true}},
      {"type": "absolute_momentum_gate", "params": {"window": 60, "threshold": 0.0}}
    ]
  },
  "asset_scorer": {
    "scorers": [
      {"type": "relative_momentum", "weight": 1.0, "params": {"window": 60}}
    ],
    "normalization": "rank_pct"
  },
  "allocation_engine": {
    "type": "topk_score_weighted",
    "params": {"top_k": 4, "score_floor": 0.0}
  },
  "risk_overlay": {
    "overlays": [
      {"type": "turnover_cap", "params": {"max_daily_turnover": 0.4}}
    ]
  },
  "rebalance_scheduler": {
    "type": "every_n_days_with_threshold",
    "params": {"rebalance_days": 5, "min_weight_change": 0.05}
  },
  "constraint_projector": {
    "type": "long_only_cap_normalize",
    "params": {"gross_exposure": 0.8, "max_asset_weight": 0.25, "min_weight": 0.02, "max_holding_count": 4}
  },
  "diagnostics": {
    "enabled": true,
    "params": {"save_daily_scores": true, "save_gate_results": true, "save_turnover": true}
  }
}
```

### 模板 B：多窗口风险调整动量

适用：

- 高波动资产池
- 不希望单纯追涨
- 希望收益和回撤兼顾

```json
{
  "universe_gate": {
    "gates": [
      {"type": "data_availability_gate", "params": {"min_history_days": 120, "require_current_bar": true}},
      {"type": "trend_filter_gate", "params": {"ma_window": 120, "slope_window": 20, "min_slope": 0.0, "require_price_above_ma": true}}
    ]
  },
  "asset_scorer": {
    "scorers": [
      {"type": "multi_window_momentum", "weight": 0.5, "params": {"windows": [20, 60, 120], "weights": [0.3, 0.4, 0.3]}},
      {"type": "risk_adjusted_momentum", "weight": 0.3, "params": {"momentum_window": 60, "vol_window": 20, "vol_floor": 0.03}},
      {"type": "drawdown_resilience", "weight": 0.2, "params": {"drawdown_window": 120}}
    ],
    "normalization": "rank_pct"
  },
  "allocation_engine": {
    "type": "score_vol_blend",
    "params": {"alpha": 1.0, "beta": 1.0, "vol_window": 60, "top_k": 4}
  },
  "risk_overlay": {
    "overlays": [
      {"type": "vol_target", "params": {"target_vol": 0.18, "vol_window": 60, "min_gross": 0.3, "max_gross": 0.9}},
      {"type": "turnover_cap", "params": {"max_daily_turnover": 0.4}}
    ]
  },
  "rebalance_scheduler": {
    "type": "every_n_days_with_threshold",
    "params": {"rebalance_days": 5, "min_weight_change": 0.05}
  },
  "constraint_projector": {
    "type": "long_only_cap_normalize",
    "params": {"gross_exposure": 0.9, "max_asset_weight": 0.25, "min_weight": 0.02, "max_holding_count": 4}
  },
  "diagnostics": {
    "enabled": true,
    "params": {"save_daily_scores": true, "save_gate_results": true, "save_turnover": true, "save_constraint_events": true}
  }
}
```

### 模板 C：低波动稳健配置

适用：

- 混合资产池
- 风险控制优先
- 资产波动差异明显

```json
{
  "universe_gate": {
    "gates": [
      {"type": "data_availability_gate", "params": {"min_history_days": 120, "require_current_bar": true}},
      {"type": "risk_filter_gate", "params": {"vol_window": 60, "max_annual_vol": 0.45, "drawdown_window": 120, "max_drawdown_limit": -0.30}}
    ]
  },
  "asset_scorer": {
    "scorers": [
      {"type": "inverse_vol_preference", "weight": 0.5, "params": {"vol_window": 60, "vol_floor": 0.03}},
      {"type": "drawdown_resilience", "weight": 0.3, "params": {"drawdown_window": 120}},
      {"type": "trend_quality", "weight": 0.2, "params": {"window": 60, "method": "composite"}}
    ],
    "normalization": "rank_pct"
  },
  "allocation_engine": {
    "type": "inverse_vol",
    "params": {"vol_window": 60, "vol_floor": 0.03, "top_k": 6}
  },
  "risk_overlay": {
    "overlays": [
      {"type": "drawdown_cut", "params": {"drawdown_window": 120, "asset_drawdown_limit": -0.25, "portfolio_drawdown_limit": -0.15, "cut_ratio": 0.5}},
      {"type": "budget_smoothing", "params": {"smooth": 0.4}}
    ]
  },
  "rebalance_scheduler": {
    "type": "every_n_days_with_threshold",
    "params": {"rebalance_days": 10, "min_weight_change": 0.05}
  },
  "constraint_projector": {
    "type": "long_only_cap_normalize",
    "params": {"gross_exposure": 0.7, "max_asset_weight": 0.25, "min_weight": 0.02, "max_holding_count": 6}
  },
  "diagnostics": {
    "enabled": true,
    "params": {"save_daily_scores": true, "save_gate_results": true, "save_turnover": true}
  }
}
```

### 模板 D：Softmax 平滑轮动

适用：

- 不希望权重跳变太大
- 希望更多资产参与组合
- 资产分数连续而非极端分化

```json
{
  "universe_gate": {
    "gates": [
      {"type": "data_availability_gate", "params": {"min_history_days": 120, "require_current_bar": true}}
    ]
  },
  "asset_scorer": {
    "scorers": [
      {"type": "multi_window_momentum", "weight": 0.6, "params": {"windows": [20, 60, 120], "weights": [0.3, 0.4, 0.3]}},
      {"type": "risk_adjusted_momentum", "weight": 0.4, "params": {"momentum_window": 60, "vol_window": 20, "vol_floor": 0.03}}
    ],
    "normalization": "rank_pct"
  },
  "allocation_engine": {
    "type": "softmax_weighted",
    "params": {"temperature": 0.8, "top_k": 8}
  },
  "risk_overlay": {
    "overlays": [
      {"type": "budget_smoothing", "params": {"smooth": 0.5}},
      {"type": "turnover_cap", "params": {"max_daily_turnover": 0.3}}
    ]
  },
  "rebalance_scheduler": {
    "type": "every_n_days_with_threshold",
    "params": {"rebalance_days": 5, "min_weight_change": 0.03}
  },
  "constraint_projector": {
    "type": "long_only_cap_normalize",
    "params": {"gross_exposure": 0.85, "max_asset_weight": 0.20, "min_weight": 0.015, "max_holding_count": 8}
  },
  "diagnostics": {
    "enabled": true,
    "params": {"save_daily_scores": true, "save_raw_weights": true, "save_final_weights": true, "save_turnover": true}
  }
}
```

### 模板 E：手写分组预算

适用：

- 用户明确指定分组，或 LLM 可以基于画像事实划分分组
- 资产池平均相关性高，需要参考相关性摘要手写风险组
- 希望避免单一主题或风格过度集中

```json
{
  "universe_gate": {
    "gates": [
      {"type": "data_availability_gate", "params": {"min_history_days": 120, "require_current_bar": true}},
      {"type": "risk_filter_gate", "params": {"vol_window": 60, "max_annual_vol": 0.60, "drawdown_window": 120, "max_drawdown_limit": -0.35}}
    ]
  },
  "asset_scorer": {
    "scorers": [
      {"type": "relative_momentum", "weight": 0.45, "params": {"window": 60}},
      {"type": "risk_adjusted_momentum", "weight": 0.35, "params": {"momentum_window": 60, "vol_window": 20, "vol_floor": 0.03}},
      {"type": "low_corr_bonus", "weight": 0.20, "params": {"corr_window": 120, "reference": "asset_pool"}}
    ],
    "normalization": "rank_pct"
  },
  "allocation_engine": {
    "type": "cluster_budget_then_within_cluster",
    "params": {
      "cluster_source": "explicit",
      "cluster_cap": 0.4,
      "within_cluster_method": "score_weighted",
      "top_k_per_cluster": 2,
      "groups": [
        {"group_id": "group_1", "group_name": "手写分组1", "symbols": ["SYMBOL_A", "SYMBOL_B"], "source": "llm", "rationale": "参考预算画像中的相关性、收益风险特征或用户描述划分。"},
        {"group_id": "ungrouped", "group_name": "未归组资产", "symbols": ["SYMBOL_C"], "source": "llm", "rationale": "未归入其他组的资产。"}
      ]
    }
  },
  "risk_overlay": {
    "overlays": [
      {"type": "cluster_cap", "params": {"cluster_source": "explicit", "cluster_cap": 0.4}},
      {"type": "turnover_cap", "params": {"max_daily_turnover": 0.4}}
    ]
  },
  "rebalance_scheduler": {
    "type": "every_n_days_with_threshold",
    "params": {"rebalance_days": 5, "min_weight_change": 0.05}
  },
  "constraint_projector": {
    "type": "long_only_cap_normalize",
    "params": {"gross_exposure": 0.8, "max_asset_weight": 0.25, "min_weight": 0.02, "max_holding_count": 6}
  },
  "diagnostics": {
    "enabled": true,
    "params": {"save_daily_scores": true, "save_gate_results": true, "save_constraint_events": true, "save_turnover": true}
  }
}
```

## 13. 根据画像和用户偏好选择策略族

BudgetAgent 必须先阅读 `budget_profile.json` 和 `budget_profile.md`，再选择策略族。不要机械套模板；模板只是可复用结构，真正的选择顺序是：

```text
资产池画像 -> 用户偏好 -> 约束冲突判断 -> 策略族 -> 模块组合 -> 参数空间
```

### 13.1 先判断资产池画像

重点识别：

| 画像标签 | 判断依据 | 影响 |
| --- | --- | --- |
| `strong_cross_section_dispersion` | 单资产收益、Sharpe-like、趋势质量差异明显 | 可以优先探索动量/TopK/集中持仓 |
| `weak_cross_section_dispersion` | 资产表现接近 | 减少集中押注，偏向风险平衡或低换手 |
| `high_correlation` | 平均相关性高、多个资产同涨同跌 | 加入 `low_corr_bonus`、手写分组或 `cluster_cap` |
| `low_correlation` | 资产之间分散性较好 | 可以增加持仓数量或风险平衡 |
| `high_volatility_or_drawdown` | 波动和最大回撤偏高 | 增加低波、回撤韧性、波动目标、现金保护 |
| `trend_dominant` | 多数阶段有持续趋势 | 增加 relative/multi-window/risk-adjusted momentum |
| `range_or_choppy` | 震荡阶段多、趋势反复 | 降低换手、使用平滑、提高阈值、避免过度追涨 |
| `data_quality_risk` | 数据覆盖不齐或元数据差 | 强化 data_availability_gate，避免依赖元数据 |

### 13.2 再判断用户偏好

| 用户偏好 | 策略倾向 |
| --- | --- |
| 收益优先 | `momentum_rotation`、`concentration_alpha`，提高 gross_exposure 和 top_k 集中度 |
| Sharpe 优先 | `risk_adjusted_rotation`、`vol_target_rotation` |
| 低回撤优先 | `defensive_low_vol`、`drawdown_control` |
| 低换手优先 | `low_turnover_balanced`，提高 rebalance_days 和 min_weight_change |
| 集中持仓 | 降低 top_k 和 max_holding_count，提高 min_weight |
| 分散持仓 | 提高 max_holding_count，加入 low_corr_bonus 或 explicit groups |
| 满仓倾向 | gross_exposure 接近 1.0，但必须同时控制 max_asset_weight 和回撤 |
| 现金保护 | gross_exposure 设为 0.60 到 0.85，或使用风险覆盖降低暴露 |

### 13.3 策略族与模块组合

| 策略族 | 适合画像/偏好 | 常用模块 |
| --- | --- | --- |
| `momentum_rotation` | 强弱分化、趋势明显、收益优先 | relative_momentum / multi_window_momentum + topk_score_weighted |
| `risk_adjusted_rotation` | 高波但有趋势、Sharpe 优先 | risk_adjusted_momentum + score_vol_blend |
| `defensive_low_vol` | 高波、高回撤、低回撤优先 | inverse_vol_preference + drawdown_resilience + vol_target |
| `drawdown_control` | 下跌段多、用户重视防守 | drawdown_filter_gate + drawdown_resilience + cash/gross_exposure 限制 |
| `correlation_aware` | 平均相关性高、重复暴露明显 | low_corr_bonus + explicit groups + cluster_cap |
| `risk_parity_like` | 混合资产或分散稳健 | inverse_vol_preference + max_asset_weight + higher max_holding_count |
| `low_turnover_balanced` | 用户重视稳定和交易成本 | budget_smoothing + turnover_cap + every_n_days_with_threshold |
| `concentration_alpha` | 强弱分化非常明显、收益优先 | topk_equal/topk_score_weighted + 较低 max_holding_count |
| `vol_target_rotation` | 希望控制组合波动 | risk_adjusted_momentum + vol_target + gross_exposure 搜索 |

### 13.4 冲突处理

如果画像和用户偏好冲突，不要硬凑一个策略。常见冲突：

| 冲突 | 处理 |
| --- | --- |
| 用户要高收益满仓，但画像高相关高回撤 | 至少生成一条收益候选和一条稳健候选，说明回撤代价 |
| 用户要集中持仓，但资产强弱分化弱 | 生成集中候选时必须降低单资产上限或增加回撤控制 |
| 用户要低换手，但画像显示轮动很快 | 提高 rebalance_days 的同时保留一个更灵活的候选作对照 |
| 用户要分散，但资产高度相关 | 使用 explicit groups 或 low_corr_bonus，说明分散效果可能有限 |

若冲突不能通过候选策略覆盖，最终回复用户时要说明原因，并询问用户更偏好收益、回撤、换手还是集中度。

## 14. 四阶段预算策略探索

预算层不是一次性模板选择，而是一个可回退、可重构、可持续优化的策略搜索过程。BudgetAgent 应按下面四个阶段推进，但不要机械单向执行：如果复盘证据显示当前阶段方向失败，可以回退到前一阶段或重新做策略族广泛探索。

每一轮都应按闭环执行：

```text
写策略四件套
-> 评估
-> 复盘
-> 决策
-> 记录记忆
-> 进入下一轮
```

评估和复盘的入口根据本轮策略数量决定：单策略走单策略评估和单策略复盘；多个候选走批量评估，必要时再做多策略横向比较。批量评估只是并行执行多个完整单策略评估，不是每轮必需动作。

### 14.1 阶段 1：策略族广泛探索

阶段 1 的某一轮通常不要只生成一个预算策略。建议先根据画像和偏好选择 1 到 3 个策略族，再生成 3 到 5 个结构明显不同的候选配置，便于横向比较。

推荐做法：

1. 先写清每个候选的策略族、画像依据、用户偏好依据和冲突处理。
2. 至少覆盖一个主攻候选和一个稳健候选。
3. 如果画像显示相关性高，至少有一个候选加入 `low_corr_bonus` 或手写 explicit groups。
4. 如果画像显示高波动或大回撤，至少有一个候选加入低波、回撤韧性或波动目标控制。
5. 如果用户明确要求简单可解释，减少候选数和自由参数数量。

如果资产数少于 6 个，生成 2 到 3 个候选即可。

如果资产池元数据质量差，不要生成强依赖行业、主题、资产名称的策略。

阶段 1 的目标不是找最优参数，而是判断资产池适合什么预算路线。若本轮评估了多个候选，阶段 1 结束后应保留 1 个 primary family，最多 1 个 fallback family，并记录 discarded families 及原因。

### 14.2 阶段 2：策略族深挖与结构重构

围绕阶段 1 胜出的策略族做结构变体。这个阶段允许大改结构，不要只调参数。

允许调整：

- UniverseGate：是否使用绝对动量、回撤过滤、数据完整性过滤。
- AssetScorer：更换 scorer 组合、权重、窗口、归一化方式。
- AllocationEngine：topk_equal、topk_score_weighted、score_vol_blend、softmax 等。
- RiskOverlay：turnover_cap、budget_smoothing、vol_target、drawdown 控制。
- RebalanceScheduler：日频、周频、阈值触发、低换手节奏。
- ConstraintProjector：gross_exposure、max_asset_weight、max_holding_count、min_weight。
- explicit groups：仅当画像或用户偏好支持分组时手写。

如果 primary family 在 validation/walk-forward 上失败，不要继续微调参数，应回到阶段 1 或切换到 fallback family。

### 14.3 阶段 3：风险结构与约束增强

当某条结构已经证明有效后，开始强化风险和交易约束。这个阶段重点处理：

- 回撤过大：提高 drawdown_resilience、drawdown_filter、降低 gross_exposure。
- 换手过高：提高 rebalance_days、min_weight_change，加入 turnover_cap、budget_smoothing。
- 过度集中：降低 max_asset_weight，提高 max_holding_count，加入相关性约束。
- 长期低仓位：检查 universe_gate 是否过严，适度提高 gross_exposure 或放松绝对动量阈值。
- 重复暴露：加入 low_corr_bonus 或 explicit groups。

阶段 3 可以做结构小改，但应围绕已有效路线，不要频繁换策略族。

### 14.4 阶段 4：稳健性精修与最终选择

这个阶段只在已有若干可靠候选后进入。目标是降低过拟合，选择最终预算策略。

主要动作：

- 缩小参数空间，固定不敏感参数。
- 降低复杂度，优先选择解释性更好且样本外稳定的版本。
- 横向比较 primary 和 fallback。
- 检查 validation、walk-forward、阶段归因、换手、回撤和持仓集中度。
- 若复杂版本只提升 full 样本但 validation/walk-forward 变差，应回退到简单版本。

最终选择前，必须有明确 reason，说明为什么该策略比其他候选更适合作为最终预算层策略。

## 15. 参数搜索建议

优先搜索：

- `allocation_engine.params.top_k`
- `constraint_projector.params.max_holding_count`
- `constraint_projector.params.max_asset_weight`
- `constraint_projector.params.gross_exposure`
- `rebalance_scheduler.params.rebalance_days`
- `rebalance_scheduler.params.min_weight_change`
- 主要 scorer 的窗口参数
- `risk_overlay.overlays[].params.max_daily_turnover`

第一版建议：

| 项目 | 建议 |
| --- | --- |
| 单策略自由参数数 | 4 到 10 |
| 每个策略候选数 | 30 到 120 |
| 第一轮策略数量 | 3 到 5 |
| 资产池 9 到 20 个时 `top_k` | 3 到 6 |
| `max_asset_weight` | 0.15 到 0.30 |
| `gross_exposure` | 0.60 到 1.00 |
| `min_weight` | 0.01 到 0.03 |

示例：

```json
{
  "search_method_hint": "ga",
  "max_candidates_hint": 80,
  "params": {
    "asset_scorer.scorers[0].params.windows": {
      "type": "choice",
      "choices": [[20, 60, 120], [40, 120, 252]]
    },
    "allocation_engine.params.top_k": {
      "type": "int",
      "min": 3,
      "max": 6,
      "step": 1
    },
    "constraint_projector.params.max_holding_count": {
      "type": "int",
      "min": 3,
      "max": 6,
      "step": 1
    },
    "constraint_projector.params.max_asset_weight": {
      "type": "float",
      "choices": [0.15, 0.20, 0.25, 0.30]
    },
    "constraint_projector.params.gross_exposure": {
      "type": "float",
      "choices": [0.60, 0.80, 1.00]
    },
    "rebalance_scheduler.params.rebalance_days": {
      "type": "int",
      "choices": [1, 5, 10, 20]
    }
  }
}
```

## 16. 评分指标建议

预算层参数搜索应以 Sharpe 为核心，但不能只看 Sharpe。

建议主评分：

```text
score =
  0.45 * normalized_sharpe
+ 0.20 * normalized_annual_return
+ 0.20 * normalized_max_drawdown_control
+ 0.10 * walk_forward_stability
+ 0.05 * turnover_control
```

如果用户要求更稳健：

- 提高最大回撤控制和 walk-forward 稳定性权重
- 降低年化收益权重

如果用户要求更进攻：

- 提高年化收益权重
- 但不要取消回撤和换手约束

## 17. 禁止事项

BudgetAgent 不得生成以下策略：

- 使用未来数据
- 按具体日期硬编码买卖
- 用资产名称关键词写死配置比例
- 给当日无数据资产分配预算
- 输出负权重或做空
- 总权重大于 `gross_exposure`
- 单资产权重大于 `max_asset_weight`
- 忽略 `max_holding_count`
- 忽略交易成本和换手
- 在预算层重复实现信号层入场出场逻辑
- 生成无法由通用执行器解析的字段

## 18. 策略生成检查清单

生成预算策略后，BudgetAgent 必须检查：

- `budget_policy_config.json` 是否包含 7 个顶层模块
- 每个模块的 `type` 是否在本文档允许范围内
- 每个参数是否在建议范围内，若超出必须说明原因
- `param_space.json` 的参数路径是否能对应到 `budget_policy_config.json`
- `max_holding_count`、`max_asset_weight`、`gross_exposure`、`min_weight` 是否明确
- `rebalance_scheduler` 是否明确
- `diagnostics.enabled` 是否为 true
- 策略是否避免未来函数
- 策略是否适配 `budget_profile` 中的资产池特征
- `budget_policy_spec.md` 是否写明策略族、画像依据、用户偏好依据
- `budget_policy_spec.md` 和 `budget_policy_meta.json` 是否写明当前探索阶段
- 若画像与用户偏好冲突，是否写明冲突、取舍和代价

## 19. 保守默认配置

如果画像信息不足，优先使用以下保守起点：

```json
{
  "policy_name": "default_multi_window_momentum_topk",
  "policy_version": "0.1.0",
  "universe_gate": {
    "gates": [
      {"type": "data_availability_gate", "params": {"min_history_days": 120, "max_recent_missing_days": 0, "require_current_bar": true}},
      {"type": "absolute_momentum_gate", "params": {"window": 60, "threshold": 0.0}}
    ]
  },
  "asset_scorer": {
    "scorers": [
      {"type": "multi_window_momentum", "weight": 0.6, "params": {"windows": [20, 60, 120], "weights": [0.3, 0.4, 0.3]}},
      {"type": "risk_adjusted_momentum", "weight": 0.4, "params": {"momentum_window": 60, "vol_window": 20, "vol_floor": 0.03}}
    ],
    "normalization": "rank_pct"
  },
  "allocation_engine": {
    "type": "topk_score_weighted",
    "params": {"top_k": 4, "score_floor": 0.0, "power": 1.0}
  },
  "risk_overlay": {
    "overlays": [
      {"type": "turnover_cap", "params": {"max_daily_turnover": 0.4}},
      {"type": "budget_smoothing", "params": {"smooth": 0.3}}
    ]
  },
  "rebalance_scheduler": {
    "type": "every_n_days_with_threshold",
    "params": {"rebalance_days": 5, "min_weight_change": 0.05, "min_total_turnover": 0.10}
  },
  "constraint_projector": {
    "type": "long_only_cap_normalize",
    "params": {"gross_exposure": 0.8, "max_asset_weight": 0.25, "min_weight": 0.02, "max_holding_count": 4, "renormalize_after_clip": true, "rounding_digits": 6}
  },
  "diagnostics": {
    "enabled": true,
    "params": {"save_daily_scores": true, "save_gate_results": true, "save_raw_weights": true, "save_final_weights": true, "save_turnover": true, "save_constraint_events": true}
  }
}
```
