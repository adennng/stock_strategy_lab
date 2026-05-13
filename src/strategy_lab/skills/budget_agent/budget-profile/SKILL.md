---
name: budget-profile
description: "为 BudgetAgent 生成预算层资产池画像；第一步自动补全资产简称、类型、市场、行业/主题等元数据。"
license: Proprietary project skill
---

# Budget Profile Skill

## 适用场景

在 `budget-run`、`budget-data-panel`、`budget-data-split` 完成后使用本 skill。它为预算策略生成提供事实材料，不输出策略建议。

本 skill 第一环节会补全资产元数据：

```text
1. 读取资产代码列表。
2. 先尝试 MiniQMT:
   - xtdata.get_instrument_detail(symbol, True)
   - xtdata.get_instrument_type(symbol)
3. MiniQMT 不可用或缺字段时，再尝试 AKShare:
   - fund_etf_spot_em
   - fund_name_em
   - stock_info_a_code_name
   - stock_zh_index_spot_em
4. 如果仍无法获得完整信息，则用规则兜底：
   - symbol 推断市场 SH/SZ
   - 51/15/56/58 开头大致推断为 ETF
   - 000/399 开头大致推断为指数
5. 写出 asset_metadata.json 和 asset_metadata.csv。
6. 在 budget_run_state.json 的 asset_pool.asset_metadata_path 登记元数据路径。
```

接口失败不会中止画像流程；服务会记录 `metadata_quality` 和 warning，然后继续生成后续画像。

## 命令

```powershell
python -m strategy_lab.cli budget profile BUDGET_RUN_STATE_PATH [OPTIONS]
```

必填参数：

```text
BUDGET_RUN_STATE_PATH
  预算层任务状态文件路径，例如：
  artifacts\budget_runs\budget_xxx\budget_run_state.json
```

可选参数：

```text
--output-dir
  可选。画像输出目录。不传则写入当前 budget run 的 profile 目录。

--rolling-window
  滚动波动率、滚动相关性的窗口交易日数。
  默认 60。

--min-segment-days
  资产池阶段划分的最短自然日数量。
  默认 40。

--top-n
  图表中展示 Top/Bottom 资产数量。
  默认 5。

--charts / --no-charts
  是否生成图表。
  默认生成。
```

## 输出文件

默认输出：

```text
artifacts/budget_runs/{budget_run_id}/profile/
  budget_profile.json
  budget_profile.md
  asset_metadata.json
  asset_metadata.csv
  asset_summary.csv
  correlation_matrix.csv
  cumulative_returns.png
  correlation_heatmap.png
  return_risk_scatter.png
  availability_timeline.png
```

文件说明：

```text
budget_profile.json
  机器可读画像主文件。包含输入路径、元数据质量、数据覆盖、收益风险、相关性、correlation_reference、等权组合、资产池阶段、信号层策略概览和 pool_flags。

budget_profile.md
  人类可读摘要。BudgetAgent 可先读它快速理解资产池，再按需读 JSON/CSV。

asset_metadata.json / asset_metadata.csv
  资产简称、类型、市场、行业/主题、基金类型、元数据来源和完整度。

asset_summary.csv
  每个资产的累计收益、年化收益、年化波动、Sharpe-like、最大回撤、可用天数、最好/最差单日收益等。

correlation_matrix.csv
  多资产收益率全样本相关性矩阵。仅用于理解资产池和辅助手写 explicit groups，不作为每日执行时的自动分组信号。

cumulative_returns.png
  等权组合和代表性资产累计收益图。

correlation_heatmap.png
  相关性热力图。

return_risk_scatter.png
  收益-风险散点图。

availability_timeline.png
  每个资产数据可用时间轴。
```

## 画像内容

本 skill 会生成：

```text
1. 资产池基础信息：资产数量、日期范围、交易日数量。
2. 元数据质量：MiniQMT/AKShare/规则兜底的数量和完整度。
3. 数据覆盖画像：每个资产实际可用开始/结束日期、早期不可用资产。
4. 单资产收益风险画像：收益、波动、Sharpe-like、回撤、胜率等。
5. 相关性画像：平均相关性、高相关资产对、低相关资产对。
6. correlation_reference：说明全样本相关性摘要的用途和边界；它只用于 BudgetAgent 理解资产池，并在需要时辅助 LLM 手写 explicit groups。
7. 等权组合画像：等权收益、波动、回撤。
8. 资产池阶段画像：按等权组合、滚动相关性、波动和横截面分散度划分阶段。
9. 信号层策略画像：读取复制来的 strategy_meta/strategy_spec 路径，统计最终信号策略概况。
10. pool_flags：只输出事实标签，不提供策略建议。
```

## 调用示例

默认画像：

```powershell
python -m strategy_lab.cli budget profile artifacts\budget_runs\budget_xxx\budget_run_state.json
```

不生成图片：

```powershell
python -m strategy_lab.cli budget profile artifacts\budget_runs\budget_xxx\budget_run_state.json --no-charts
```

调整滚动窗口和阶段长度：

```powershell
python -m strategy_lab.cli budget profile artifacts\budget_runs\budget_xxx\budget_run_state.json --rolling-window 90 --min-segment-days 60
```

## BudgetAgent 使用要点

- 本环节是事实画像，不是策略建议。
- 运行后先读 `budget_profile.md`，再读 `budget_profile.json`。
- 如果 `metadata_quality.mean_completeness` 较低，不要停止流程，但后续描述资产池时应提示元数据不完整。
- 如果 `pool_flags.large_availability_gap=true`，说明很多资产中途才有数据，后续预算回测必须处理不可交易资产。
- 如果后续预算策略需要分组，只能由用户指定或 LLM 自主写入 `budget_policy_config.json` 的 explicit groups；本画像的全样本相关性摘要只能作为手写分组参考。
- 后续预算策略生成应结合本画像和预算策略编写指南，而不是只看单一收益排名。
