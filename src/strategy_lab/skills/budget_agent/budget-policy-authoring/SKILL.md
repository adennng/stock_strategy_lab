---
name: budget-policy-authoring
description: "指导 BudgetAgent 按结构化预算策略规范生成 budget_policy_config.json、budget_policy_spec.md、param_space.json 和 budget_policy_meta.json。"
license: Proprietary project skill
---

# Budget Policy Authoring Skill

## 适用场景

当 BudgetAgent 已经完成 `budget-run`、`budget-data-panel`、`budget-data-split` 和 `budget-profile` 后，需要生成一版或多版预算层策略配置时，使用本 skill。

本 skill 只负责预算策略创作与文件生成，不负责参数搜索、回测、复盘或最终选择。

预算层采用：

```text
结构化策略配置 + 系统通用预算策略执行器
```

不要让 LLM 自由编写完整 `budget_policy.py`。LLM 只生成结构化配置和说明文件，后续执行器会读取这些配置完成每日预算权重计算。

## 必须先阅读的文件

生成预算策略前，必须阅读：

```text
src/strategy_lab/skills/budget_agent/budget-policy-authoring/references/budget_policy_library.md
```

该文件定义了预算策略的模块、可选参数、参数范围、策略模板、分组规则和检查清单。

建议同时参考：

```text
src/strategy_lab/skills/budget_agent/budget-policy-authoring/references/example_budget_policy_config.json
src/strategy_lab/skills/budget_agent/budget-policy-authoring/references/example_param_space.json
src/strategy_lab/skills/budget_agent/budget-policy-authoring/references/example_budget_policy_spec.md
src/strategy_lab/skills/budget_agent/budget-policy-authoring/references/example_budget_policy_meta.json
```

参考文件只用于学习结构，不要照抄资产代码、策略名或参数。

## 输入材料

使用本 skill 时，应先读取本次预算任务目录中的材料：

```text
artifacts/budget_runs/{budget_run_id}/budget_run_state.json
artifacts/budget_runs/{budget_run_id}/profile/budget_profile.md
artifacts/budget_runs/{budget_run_id}/profile/budget_profile.json
artifacts/budget_runs/{budget_run_id}/profile/asset_summary.csv
artifacts/budget_runs/{budget_run_id}/profile/correlation_matrix.csv
artifacts/budget_runs/{budget_run_id}/signal_artifacts/
```

重点关注：

```text
资产数量
训练数据范围
数据覆盖质量
元数据完整度
单资产收益、波动、Sharpe-like、最大回撤
等权组合收益和最大回撤
全样本相关性摘要 correlation_reference
资产池阶段 regime_segments
pool_flags
用户是否要求集中持仓、分散持仓、低回撤、低换手或满仓
```

注意：`correlation_reference` 只用于理解资产池和辅助手写 explicit groups，不作为每日执行时的自动分组信号。

注意：预算层默认独立于信号层训练。`signal_artifacts/` 只用于资产来源追溯和后续组合层融合参考，不要把信号层策略质量、当前信号强弱或信号层持仓作为预算层标准输入。信号层和预算层的日常融合由组合层处理。

## 必须生成的四件套

每个预算策略目录必须包含：

```text
budget_policy_config.json
budget_policy_spec.md
param_space.json
budget_policy_meta.json
```

建议写入：

```text
artifacts/budget_runs/{budget_run_id}/policies/generated/{policy_name}_{timestamp}/
```

如果一次生成多版候选策略，建议写入同一个批次目录下：

```text
artifacts/budget_runs/{budget_run_id}/policies/generated/policy_batch_001/
  policy_a/
  policy_b/
  policy_c/
```

每个子目录都必须有完整四件套。

## 预算策略统一结构

每个 `budget_policy_config.json` 必须包含 7 个顶层模块：

```text
universe_gate
asset_scorer
allocation_engine
risk_overlay
rebalance_scheduler
constraint_projector
diagnostics
```

即使某个模块很简单，也必须显式写出。例如：

```json
"risk_overlay": {
  "overlays": []
}
```

不要省略顶层模块。


## 四阶段自主探索规则

BudgetAgent 的目标是自主探索并形成当前证据下最优预算策略，不是生成一轮候选后等待用户选择。预算层策略探索分为四个阶段，每个阶段内部可以反复多轮，阶段之间也允许回退。

每一轮都是完整闭环：

```text
确定当前阶段和本轮目标
-> 编写或改写预算策略四件套
-> 单策略评估或多策略批量评估
-> 单策略复盘或多策略横向比较
-> 根据复盘结果决定推进、停留、回退、切换路线或最终选择
-> 更新 budget_agent_memory.md
```

本 skill 只指导“编写或改写预算策略四件套”这一环节，但写策略时必须清楚本轮处于哪个阶段、为什么要这样改，以及后续应使用单策略评估还是批量评估。

### 阶段 1：策略族广泛探索

目标：判断这个资产池更适合哪类预算路线。

阶段 1 的某一轮通常会生成 3 到 5 个结构明显不同的候选策略，不要只生成一个。生成前必须先完成下面的判断：

```text
1. 从 budget_profile 识别资产池画像：资产数量、相关性、强弱分化、波动/回撤、趋势/震荡阶段、数据质量。
2. 从用户任务识别偏好：收益优先、Sharpe 优先、低回撤、低换手、集中持仓、分散持仓、满仓/现金保护等。
3. 根据画像 + 偏好选择 1 到 3 个预算策略族，再在每个策略族内组合模块和参数。
4. 如果画像与用户偏好冲突，必须生成至少一条稳健候选和一条偏好候选，或在最终回复中说明冲突并询问用户取舍。
```

可选策略族：

```text
1. momentum_rotation：横截面动量/强弱轮动，适合强弱分化明显、趋势阶段较多。
2. risk_adjusted_rotation：风险调整动量，适合高波动但仍有趋势的资产池。
3. defensive_low_vol：低波动/回撤韧性，适合用户低回撤或资产池高波动。
4. drawdown_control：回撤控制优先，适合下跌段多、回撤集中或用户强调防守。
5. correlation_aware：相关性约束/手写分组预算，适合平均相关性高或重复暴露明显。
6. risk_parity_like：近似风险平衡，适合混合资产或用户偏好分散稳健。
7. low_turnover_balanced：平滑低换手，适合低换手、低交易成本或策略稳定性优先。
8. concentration_alpha：集中 TopK，适合用户收益优先且资产强弱分化明显。
9. vol_target_rotation：波动目标轮动，适合希望控制组合整体波动。
```

如果资产数量少于 6 个，可以只生成 2 到 3 个候选策略。

如果用户明确要求简单可解释，优先生成 2 到 3 个低复杂度策略。

如果用户偏好与画像冲突，例如“希望高收益满仓”但画像显示高相关、高回撤、弱趋势，必须在 `budget_policy_spec.md` 中写清冲突、处理方式和代价。最终回复用户时也要说明原因，并可以询问用户更偏好收益、回撤、换手还是集中度。

阶段 1 结束时，若本轮评估了多个候选，应通过 BudgetCriticAgent 横向比较，保留 1 个 primary family，最多 1 个 fallback family，并放弃明显不适合的策略族。

### 阶段 2：策略族深挖与结构重构

目标：围绕阶段 1 胜出的策略族做结构变体，验证“这类路线内部哪种结构更好”。

允许大改：

```text
UniverseGate
AssetScorer
AllocationEngine
RiskOverlay
RebalanceScheduler
ConstraintProjector
持仓数量逻辑
总仓位逻辑
分组/相关性约束逻辑
```

示例：

```text
risk_adjusted_rotation:
  - risk_adjusted_momentum + topk_score_weighted
  - multi_window_momentum + score_vol_blend
  - risk_adjusted_momentum + low_corr_bonus

defensive_low_vol:
  - inverse_vol_preference + drawdown_resilience
  - drawdown_filter_gate + vol_target
  - low_turnover_balanced + cash protection
```

如果阶段 2 证明 primary family 在 validation/walk-forward 上明显失败，应回退到阶段 1，重新选择策略族，而不是只放宽参数。

### 阶段 3：风险结构与约束增强

目标：在已证明有效的结构上提高稳健性和可交易性。

重点调整：

```text
gross_exposure
max_asset_weight
max_holding_count
min_weight
turnover_cap
budget_smoothing
rebalance_days
min_weight_change
explicit groups / cluster_cap
vol_target
drawdown_filter
```

重点解决：

```text
最大回撤过大
换手过高
持仓过度集中
长期低仓位
某些市场阶段明显失效
高相关资产重复暴露
```

### 阶段 4：稳健性精修与最终选择

目标：降低过拟合，选择最终预算策略。

主要动作：

```text
缩小参数空间
固定不敏感参数
降低策略复杂度
比较 primary 和 fallback
确认 validation / walk-forward 稳定
确认阶段归因没有明显单点依赖
确认换手、回撤、集中度可接受
```

如果阶段 4 发现复杂版本没有稳定优于简单版本，应回退到更简单版本，或回到阶段 3 做约束增强。

## 关键约束

每个策略都必须明确：

```text
gross_exposure
max_asset_weight
min_weight
max_holding_count
rebalance_scheduler
diagnostics.enabled
```

建议范围：

```text
gross_exposure: 0.60 到 1.00
max_asset_weight: 0.15 到 0.30
min_weight: 0.01 到 0.03
max_holding_count:
  资产数 <= 8: 2 到 3
  资产数 9 到 20: 3 到 6
  资产数 21 到 50: 5 到 10
rebalance_days: 1、5、10、20
min_weight_change: 0.02 到 0.08
```

## budget_policy_config.json 要求

必须是合法 JSON。

必须符合 `budget_policy_library.md` 中允许的模块类型和参数范围。

禁止：

```text
使用未知模块 type。
把自然语言策略直接写进 config 代替结构化参数。
省略 constraint_projector。
省略 rebalance_scheduler。
省略 diagnostics。
使用 cluster_source=correlation 或 cluster_source=metadata。
输出负权重、做空、杠杆或超出 gross_exposure 的设计。
```

## param_space.json 要求

`param_space.json` 只写需要搜索的参数，不需要搜索的参数保留在 `budget_policy_config.json`。

参数路径必须能对应到 `budget_policy_config.json`。

优先搜索：

```text
allocation_engine.params.top_k
constraint_projector.params.max_holding_count
constraint_projector.params.max_asset_weight
constraint_projector.params.gross_exposure
rebalance_scheduler.params.rebalance_days
rebalance_scheduler.params.min_weight_change
主要 scorer 的窗口参数
risk_overlay 中的换手和平滑参数
```

建议单个策略自由参数数量控制在 4 到 10 个。

## budget_policy_spec.md 要求

必须说明：

```text
策略名称
策略目标
探索阶段 exploration_stage
策略族 strategy_family
适用资产池画像
用户偏好依据
画像与用户偏好的冲突及处理方式，如无冲突也要写“无明显冲突”
7 个模块分别采用什么配置
关键参数及含义
为什么选择这些参数范围
是否使用 explicit groups
预期优势
主要风险
不适用场景
后续回测时重点观察什么
```

## budget_policy_meta.json 要求

必须说明：

```text
policy_name
policy_version
created_by
policy_mode
uses_signal_layer
uses_explicit_groups
exploration_stage
strategy_family
complexity_level
created_at
notes
```

`policy_mode` 固定为：

```json
"structured_config"
```

## 生成后自检

生成四件套后必须检查：

```text
1. 四个文件都存在。
2. budget_policy_config.json 是合法 JSON。
3. param_space.json 是合法 JSON。
4. budget_policy_meta.json 是合法 JSON。
5. budget_policy_config.json 包含 7 个顶层模块。
6. 所有模块 type 都在 budget_policy_library.md 允许范围内。
7. 如果使用分组，cluster_source 必须是 explicit，且 groups 完整。
8. 如果不使用分组，不要出现 cluster_budget_then_within_cluster 或 cluster_cap。
9. constraint_projector 明确 gross_exposure、max_asset_weight、min_weight、max_holding_count。
10. diagnostics.enabled 为 true。
11. param_space.json 的参数路径能在 config 中找到。
12. 没有未来数据、日期硬编码、资产名称硬编码权重。
13. spec 和 meta 写明策略族、画像依据和用户偏好依据。
14. 如果画像与用户偏好冲突，spec 写明了冲突、取舍和代价。
```

如果自检不通过，先修复四件套，不要进入预算策略评估阶段。

## 完成后的下一步

完成预算策略四件套后，回到本轮闭环继续执行评估和复盘。根据本轮生成的策略数量选择评估方式：

```text
单策略：使用 budget-policy-evaluation 的 evaluate-policy。
多策略：使用 budget-policy-evaluation 的 batch-evaluate-policies。
单策略评估后：调用 BudgetCriticAgent 单策略复盘。
多策略批量评估后：如需比较多个候选，调用 BudgetCriticAgent 多策略横向比较。
```

本 skill 不执行评估或复盘，只负责让本轮要评估的策略四件套完整、合法、可解释。
