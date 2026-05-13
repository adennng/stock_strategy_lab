---
name: portfolio-fusion-version
description: "初始化组合层融合策略版本：自动复制预算层和信号层快照，生成 fusion_manifest.json 和组合层五件套初始文件，避免 PortfolioAgent 手工复制大量文件。"
license: Proprietary project skill
---

# Portfolio Fusion Version Skill

## 何时使用

当组合层 run 已经完成以下步骤后，使用本 skill：

```text
portfolio-run
DataAgent 数据准备
portfolio-data-split
portfolio-profile
portfolio-signal-profile
```

本 skill 负责创建一个新的组合层版本目录，并自动复制上游快照。它只解决机械文件准备问题，不负责最终策略创作、不负责评估。

## 命令

```powershell
python -m strategy_lab.cli portfolio init-fusion-version PORTFOLIO_RUN_STATE_PATH --version-id VERSION_ID [OPTIONS]
```

示例：

```powershell
python -m strategy_lab.cli portfolio init-fusion-version artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json --version-id v001_budget_direct --version-role initial_fusion --summary "第一版组合层融合策略初始化。"
```

## 参数

```text
PORTFOLIO_RUN_STATE_PATH
  组合层状态文件路径。

--version-id
  要创建的版本 ID，例如 v001_budget_direct、v002_signal_boost。

--summary
  本版本的初始化说明。建议写清楚本轮准备优化什么。

--version-role
  版本角色。第一版可用 initial_fusion，后续候选版本可用 candidate。

--policy-name
  可选。写入 fusion_policy_meta.json 的策略名称，并作为 fusion_policy.py 默认类的 policy_name。
```

## 内部自动完成

服务会自动执行：

```text
1. 创建 versions/{version_id}/
2. 复制 source_artifacts/budget/final_budget_policy 到 versions/{version_id}/budget_policy/
3. 复制 source_artifacts/signals 下每个资产的信号层小文件到 versions/{version_id}/signal_strategies/{symbol}/
4. 自动补齐每个资产的 strategy_params.json
5. 自动生成 fusion_manifest.json，所有路径都指向当前 version 目录
6. 自动生成初始版组合层五件套：
   fusion_policy.py
   fusion_policy_spec.md
   param_space.json
   fusion_policy_meta.json
7. 预留最终候选版可选的 DailyPortfolioAgent 三件套位置；初始化阶段默认不生成这三份文件
8. 更新 portfolio_run_state.json 的 versions、current_version、events
```

## 输出目录

默认输出到：

```text
artifacts/portfolio_runs/{portfolio_run_id}/versions/{version_id}/
```

目录结构：

```text
fusion_manifest.json
fusion_policy.py
fusion_policy_spec.md
param_space.json
fusion_policy_meta.json
daily_portfolio_agent_prompt.md     最终候选版后续可生成
daily_decision_contract.json        最终候选版后续可生成
daily_override_scenarios.md         最终候选版后续可生成
budget_policy/
  budget_policy_config.json
signal_strategies/
  {symbol}/
    strategy.py
    strategy_spec.md
    param_space.json
    strategy_meta.json
    strategy_params.json
evaluation/
```

## 使用规则

调用本 skill 后，PortfolioAgent 只需要修改组合层五件套：

```text
fusion_policy.py
fusion_policy_spec.md
param_space.json
fusion_policy_meta.json
fusion_manifest.json  仅在路径或版本元数据确需调整时修改
```

如果当前版本已经是多轮优化后的最终候选版，且 `fusion_policy.py` 的机械规则继续改进有限，可以按 `portfolio-policy-authoring` 的要求补充：

```text
daily_portfolio_agent_prompt.md
daily_decision_contract.json
daily_override_scenarios.md
```

这三份文件不是中间探索版本的必填项。后续调用 `portfolio-final-selection` 时，如果它们存在，服务会一起复制到 `final/` 并写入 `final_manifest.json`。

默认不要修改：

```text
budget_policy/
signal_strategies/
```

这些目录是冻结快照，目的是让每个组合层版本可独立复现。

## 检查清单

调用完成后必须检查：

```text
1. 命令返回 status=success。
2. versions/{version_id}/fusion_manifest.json 存在。
3. versions/{version_id}/fusion_policy.py 存在。
4. versions/{version_id}/budget_policy/budget_policy_config.json 存在。
5. versions/{version_id}/signal_strategies/ 下资产数量与资产池一致。
6. portfolio_run_state.json 中 current_version 已更新为本版本。
```

下一步：阅读 `portfolio-policy-authoring`，基于画像修改组合层五件套，然后调用 `portfolio-evaluation`。
