---
name: portfolio-run
description: "指导 PortfolioAgent 创建组合层任务目录，复制预算层最终策略、预算层记忆文件和信号层最终策略源产物，并生成 portfolio_run_state.json。"
license: Proprietary project skill
---

# Portfolio Run Skill

## 何时使用

当用户提供一个已经训练好的预算层 run 目录，并希望开始组合层训练时，第一步使用本 skill。

本 skill 只负责：

```text
创建组合层 run 目录
复制预算层最终策略源产物
复制预算层 budget_run_state.json 和 reports/budget_agent_memory.md
复制信号层最终策略源产物
生成 portfolio_run_state.json
```

本 skill 不负责获取数据、切分数据、生成组合层画像、编写组合层策略、回测或最终选择。

## 核心命令

```powershell
python -m strategy_lab.cli portfolio new-run SOURCE_BUDGET_RUN_PATH [OPTIONS]
```

示例：

```powershell
python -m strategy_lab.cli portfolio new-run artifacts\budget_runs\budget_sector_etf_pool_20260510_174103 --task "基于预算层最终策略和信号层最终策略启动组合层仓位融合训练。"
```

## 参数说明

```text
SOURCE_BUDGET_RUN_PATH
  已训练完成且已登记 final_selection 的预算层 run 目录，或 budget_run_state.json 路径。

--portfolio-run-id
  可选。手动指定 portfolio_run_id。不传时系统自动生成。

--task
  可选。自然语言任务描述，会写入 portfolio_run_state.json。
  兼容写法：--task-description，与 --task 含义相同。推荐使用 --task。
```

## 内部执行流程

命令会自动完成：

```text
1. 读取预算层 budget_run_state.json。
2. 检查 final_selection，并读取 final_selection 指向的最终预算策略。
3. 找到最终预算策略 budget_policy_config.json。
4. 读取预算层 signal_artifacts_manifest.json。
5. 复制预算层最终策略四件套、budget_run_state.json、budget_final_selection.md 和 budget_agent_memory.md 到 source_artifacts/budget。
6. 复制每个资产的信号层策略四件套、signal_agent_memory.md 和 metrics.json 到 source_artifacts/signals/{symbol}。
7. 创建 data、versions、reports、logs 等标准目录。
8. 生成 portfolio_run_state.json。
```

注意：执行完成后 `versions` 为空，`current_version` 为 `null`。这是正常状态，表示组合层策略尚未开始生成。

## 输出目录

成功后生成：

```text
artifacts/portfolio_runs/{portfolio_run_id}/
  portfolio_run_state.json
  source_artifacts/
    budget/
      budget_run_state.json
      budget_agent_memory.md       如果原预算层 reports/budget_agent_memory.md 存在
      final_budget_policy_config.json
      final_budget_policy/
        budget_policy_config.json
        budget_policy_spec.md      如果存在
        param_space.json           如果存在
        budget_policy_meta.json    如果存在
      budget_final_selection.md    如果原预算层有该报告
    signals/
      portfolio_signal_artifacts_manifest.json
      {symbol}/
        run_state.json
        strategy.py
        strategy_spec.md
        param_space.json
        strategy_meta.json
        signal_agent_memory.md
        metrics.json
  data/
  versions/
  reports/
  logs/
```

## portfolio_run_state.json 关键状态

创建后应是：

```text
status = created
source_artifacts.budget.status = success
source_artifacts.budget.budget_agent_memory_path = 复制后的预算层记忆文件路径；如果源文件不存在则为 null
source_artifacts.signals.status = success 或 partial_success
data.status = pending
profile.status = pending
versions = []
current_version = null
best_version = null
final_selection.status = pending
```

其中：

```text
source_artifacts/budget
  是预算层最终策略、预算层状态和预算层记忆文件的快照。

source_artifacts/signals
  是信号层各资产最终策略源文件的快照。

versions/
  初始为空。后续 PortfolioAgent 读取画像后再创建组合层策略版本。
```

## 检查清单

执行后必须检查：

```text
1. CLI 返回 portfolio_run_id 和 state_path。
2. portfolio_run_state.json 存在。
3. source_artifacts.budget.status 为 success。
4. source_artifacts.signals.count 大于 0。
5. source_artifacts/budget/final_budget_policy_config.json 存在。
6. 如果源预算层 reports/budget_agent_memory.md 存在，则 source_artifacts/budget/budget_agent_memory.md 也必须存在。
7. source_artifacts/signals/portfolio_signal_artifacts_manifest.json 存在。
8. versions 目录存在但可以为空。
9. portfolio_run_state.json 的 versions 为空数组。
10. portfolio_run_state.json 的 current_version 为 null。
11. profile.status 为 pending。
```

## 后续步骤

创建组合层 run 后，下一步通常是：

```text
1. 调用 DataAgent 获取用户指定时间范围的组合层数据，保存到 portfolio run 的 data 目录。
2. 调用 portfolio-data-split 对组合层多资产行情面板生成数据切分。
3. 调用 portfolio-signal-profile 汇总信号层策略画像。
4. 调用 portfolio-profile 生成组合层画像。
5. 阅读 portfolio-policy-authoring skill，编写第一版 fusion_policy.py。
6. 中间探索阶段通常只优化 fusion_policy.py；等最终候选版基本确定后，再按 portfolio-policy-authoring 生成 DailyPortfolioAgent 三件套。
```
