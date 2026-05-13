---
name: multi-attempt-comparison
description: "CriticAgent 用于横向比较多个策略 attempts，输出排名、保留/回退/废弃建议和 selection_advice。"
license: Proprietary project skill
---

# Multi Attempt Comparison Skill

## 适用场景

当调用方要求比较多个策略版本、选择继续优化对象、判断是否回退到旧版本时使用本 skill。典型任务：

```text
请比较 run_state_path=... 下的 attempt_001、attempt_002、attempt_003。
请横向复盘所有 ready_for_review 的 attempts，并给出下一轮应继续哪个策略方向。
```

## 必要输入

从自然语言任务中识别：

```text
run_state_path
attempt_ids
```

如果没有明确 attempt_ids，应读取 `run_state.json`，选择所有状态为 `ready_for_review`、`reviewed` 或 `optimized`，且有 attempt_summary 或可生成 attempt_summary 的 attempts。

## 必须先生成横向比较数据

多 attempt 比较不应只靠 LLM 手动读文件。必须先运行确定性比较服务：

```powershell
python -m strategy_lab.cli signal compare-attempts RUN_STATE_PATH --attempt-ids attempt_001,attempt_002,attempt_003
```

如果调用方没有给 attempt_ids，可以运行：

```powershell
python -m strategy_lab.cli signal compare-attempts RUN_STATE_PATH
```

该命令会生成：

```text
reports/attempt_comparison_{timestamp}/
  comparison_summary.json
  comparison_summary.csv
  comparison_report.md
  score_ranking.png
  return_drawdown_scatter.png
  walk_forward_stability.png
```

你应优先读取 `comparison_summary.json`、`comparison_summary.csv`、`comparison_report.md`，再按需要读取各 attempt 的详细文件。

## 每个 attempt 需要读取的材料

```text
attempts/{attempt_id}/optimization/attempt_summary.json
attempts/{attempt_id}/optimization/attempt_summary.md
attempts/{attempt_id}/strategy/strategy_spec.md
attempts/{attempt_id}/strategy/strategy_meta.json
attempts/{attempt_id}/optimization/best_individual.json
attempts/{attempt_id}/optimization/population_summary.csv
attempts/{attempt_id}/optimization/walk_forward_summary.json
attempts/{attempt_id}/backtests/full/metrics.json
```

如要深入解释某个 attempt 的市场阶段失效问题，可再读取它的：

```text
attempts/{attempt_id}/analysis/stage_attribution/stage_attribution.json
attempts/{attempt_id}/analysis/stage_attribution/stage_attribution.md
```

如果缺少阶段归因且它是候选前几名，应运行：

```powershell
python -m strategy_lab.cli signal stage-attribution RUN_STATE_PATH ATTEMPT_ID
```

## 策略框架比较逻辑

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

横向比较时，不能只比较指标，还要比较策略路线：

```text
1. RegimeDetector：哪种市场状态划分更贴近 market_profile 和 stage_attribution。
2. RegimeAlphaPolicy / RegimeAlphaMap：不同状态下的 Alpha 组合是否合理，例如趋势、突破、均值回归、防御；是否存在所有 regime 共用同一 Alpha 的问题。
3. MultiTimeframeAlpha：长 / 中 / 短周期窗口是否分工清楚、互相确认，还是冲突或过拟合。
4. Filters：是否有效降低震荡/高波/下跌阶段损失。
5. ExitPolicy：是否改善最差阶段、最大回撤和退出迟滞。
6. PositionMapper：是否带来更好的 Sharpe、暴露控制和状态差异化仓位。
7. StateRules：是否减少 regime 抖动、无效交易、频繁切换和冷却不足。
```

可参考：

```text
/src/strategy_lab/skills/signal_agent/strategy-authoring/references/signal_strategy_library.md
```

阶段 1 RegimeAlpha 批量探索后，横向比较的重点是“选择更适合该资产的状态划分和多周期 Alpha 路线”，最多保留 2 个候选进入下一阶段：一个 primary_candidate，一个可选 fallback_candidate。

阶段 1 不要求候选覆盖固定四类策略。必须判断候选集合是否符合 market_profile：

```text
如果资产画像显示长期下行、震荡多，就不应要求趋势/突破/回归/防御四类齐全。
如果资产画像显示强趋势，可以接受多个候选都围绕趋势或突破，但它们的 RegimeAlphaMap、窗口、入场/退出逻辑或风控结构必须有实质差异。
如果候选只是机械套用趋势/突破/回归/防御四类，而没有根据资产画像调整，应在比较报告中指出。
如果候选少于 4 个，但方向与画像匹配、差异清楚，可以接受。
```

阶段 2 RegimeAlpha 深度探索后，横向比较的重点是“哪个 RegimeAlphaMap 更合理”。如果某个候选只是用同一个 Alpha 加不同仓位映射，应降低评价，并建议拆成明确的 regime-specific Alpha。

## 必须分析的维度

```text
1. 综合排名
   score、full_sharpe、full_total_return、full_max_drawdown、full_excess_total_return。Sharpe 是当前最关键指标。

2. 稳定性
   walk_forward_mean_score、walk_forward_std_score、walk_forward_min_score。

3. 过拟合风险
   train_score、validation_score、train_validation_gap、overfit_penalty。

4. 基准对比
   benchmark_total_return、excess_total_return、information_ratio。

5. 风险收益质量
   sharpe、calmar、max_drawdown、最差 fold。

6. 交易行为
   order_count、signal_changes，判断是否过度交易或过于迟钝。

7. 策略路线建议
   哪个 attempt 继续优化，哪个作为回退候选，哪个废弃，是否需要新增策略方向；建议必须映射到 RegimeDetector / RegimeAlphaPolicy / RegimeAlphaMap / Filters / ExitPolicy / PositionMapper / StateRules。

8. Regime 与多周期结构比较
   比较不同 attempt 的 regime 数量、状态切换频率、RegimeAlphaMap、长中短周期窗口选择、各状态仓位暴露，以及它们在不同市场阶段的收益和回撤差异。
```

## 图片分析

如果需要理解图像（调用方明确要求看图，或图像能提供数据文件没有直接表达的视觉信息时），例如：

```text
score_ranking.png
return_drawdown_scatter.png
walk_forward_stability.png
```

如果你是多模态模型，可以使用read_file tool或 image-review skill ；如果你不是多模态模型，只使用image-review skill 

使用 `image-review` skill 调用独立多模态服务：

```powershell
python -m strategy_lab.cli image review IMAGE_PATH --question "请描述这张横向比较图中与策略选择有关的关键信息"
```

## 输出目录

必须写入一个新目录：

```text
reports/critic_comparison_{timestamp}/
  critic_comparison.json
  critic_comparison.md
  selection_advice.json
```

## critic_comparison.json 格式

```json
{
  "run_id": "...",
  "comparison_status": "completed",
  "attempt_ids": [],
  "evidence_files": [],
  "ranked_attempts": [
    {
      "rank": 1,
      "attempt_id": "...",
      "judgement": "best_candidate | fallback_candidate | discard | needs_more_tests",
      "reason": "...",
      "strengths": [],
      "weaknesses": [],
      "strategy_framework": {
        "alpha": "...",
        "filters": [],
        "exit_policy": [],
        "position_mapper": "...",
        "state_rules": []
      }
    }
  ],
  "continue_candidate": "...",
  "rollback_candidate": "...",
  "discard_attempts": [],
  "comparison_findings": [],
  "selection_advice": {
    "action": "continue_from_attempt | rollback_to_attempt | generate_new_direction | needs_more_tests",
    "attempt_id": "...",
    "reason": "...",
    "next_instructions": [],
    "module_instructions": {
      "alpha": "keep | modify | replace | unclear",
      "filters": "keep | add | remove | modify | unclear",
      "exit_policy": "keep | add | remove | modify | unclear",
      "position_mapper": "keep | modify | simplify | unclear",
      "state_rules": "keep | add | remove | modify | unclear"
    }
  }
}
```

## selection_advice.json 格式

`selection_advice.json` 是给 SignalAgent 读取的，必须短、明确、可执行：

```json
{
  "action": "continue_from_attempt",
  "attempt_id": "attempt_002",
  "fallback_attempt_id": "attempt_001",
  "discard_attempts": [],
  "reason": "...",
  "next_instructions": [],
  "module_instructions": {
    "alpha": "keep | modify | replace | unclear",
    "filters": "keep | add | remove | modify | unclear",
    "exit_policy": "keep | add | remove | modify | unclear",
    "position_mapper": "keep | modify | simplify | unclear",
    "state_rules": "keep | add | remove | modify | unclear"
  }
}
```

## run_state.json 更新

如果需要在 `artifacts.run_reports` 中登记比较结果路径，必须调用系统服务。不要手动编辑 `run_state.json`，不要覆盖已有 attempts。

命令格式：

```powershell
python -m strategy_lab.cli signal register-run-report RUN_STATE_PATH REPORT_KEY REPORT_PATH --report-type critic_comparison --summary "比较摘要" --extra-json "{}"
```

示例：

```powershell
python -m strategy_lab.cli signal register-run-report artifacts\signal_runs\signal_000300_xxx\run_state.json critic_comparison_20260508 artifacts\signal_runs\signal_000300_xxx\reports\critic_comparison_20260508\critic_comparison.md --report-type critic_comparison --summary "已完成 attempt_001、attempt_002、attempt_003 横向比较。"
```

如果需要登记更多路径，建议先把额外元数据写入 JSON 文件，再用 `--extra-json-file`，避免 PowerShell 引号转义问题：

```powershell
python -m strategy_lab.cli signal register-run-report RUN_STATE_PATH critic_comparison_20260508 REPORT_PATH --report-type critic_comparison --summary "比较摘要" --extra-json-file artifacts\signal_runs\signal_000300_xxx\reports\critic_comparison_20260508\run_report_extra.json
```

服务会自动更新 `artifacts.run_reports`、`events` 和 `updated_at`。

## 最终检查

最终回复前必须执行检查。检查内容：

```text
1. reports/critic_comparison_{timestamp}/ 下存在 critic_comparison.json、critic_comparison.md、selection_advice.json。
2. critic_comparison.json 和 selection_advice.json 可以被 json.loads 正常解析。
3. critic_comparison.md 非空，且包含综合排名、稳定性比较、过拟合风险、基准对比、最终建议。
4. critic_comparison.md 必须说明各候选的 Alpha / Filters / ExitPolicy / PositionMapper / StateRules 差异。
5. 如果更新 run_state.json，确认 artifacts.run_reports 或 events 中已登记本次比较结果，且没有覆盖已有 attempts。
6. 最终回复给调用方的路径与真实生成路径一致。
```
