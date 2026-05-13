---
name: portfolio-signal-profile
description: "在组合层画像之后，生成每个资产信号层最终策略的结构化画像，供 PortfolioAgent 编写组合层融合策略时使用。"
license: Proprietary project skill
---

# Portfolio Signal Profile Skill

## 适用场景

当组合层已经完成 `portfolio-profile` 后，使用本 skill。

本 skill 不重新训练信号层，也不修改信号层策略。它会把每个资产已有的信号层最终策略整理成组合层可理解的材料：

```text
信号层 target_S 的统计分布
信号层策略说明、记忆文件、参数文件和绩效指标
信号层策略的语义角色和可靠度
该资产信号在组合层中适合做 veto、discount、boost 还是 budget override
```

## 调用方式

```powershell
python -m strategy_lab.cli portfolio signal-profile PORTFOLIO_RUN_STATE_PATH
```

常用示例：

```powershell
python -m strategy_lab.cli portfolio signal-profile artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json
```

快速调试、不调用大模型：

```powershell
python -m strategy_lab.cli portfolio signal-profile artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json --no-llm
```

启用大模型并发提速：

```powershell
python -m strategy_lab.cli portfolio signal-profile artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json --max-workers 4
```

注意：启用 LLM 的全资产画像可能耗时较长。16 个资产、`--max-workers 4` 时常见耗时约 10 分钟。
如果通过 DeepAgents 的 `execute` 工具调用，`timeout` 建议至少设置为 `1200` 秒。

## 参数

`PORTFOLIO_RUN_STATE_PATH`
组合层 `portfolio_run_state.json` 路径。

`--output-dir`
可选。指定输出目录。不传时写入：

```text
artifacts/portfolio_runs/{portfolio_run_id}/signal_profiles/
```

`--llm / --no-llm`
是否调用大模型提炼信号层策略语义。默认调用。调试、节省时间或模型额度不足时使用 `--no-llm`。

`--max-memory-chars`
每个资产读取 `signal_agent_memory.md` 的最大字符数。默认 0，表示不截断。

`--max-workers`
单资产信号画像并发 worker 数。默认 1，表示串行。
启用 LLM 时，为了提速通常可以设置为 4；如果 API 限流、余额紧张或模型报错频繁，降回 1 或 2。
并发只用于逐资产画像构建，最终汇总文件仍由主线程统一写入。

`--symbols`
可选。逗号分隔的资产代码过滤器，主要用于调试真实 LLM 调用。例如：

```powershell
python -m strategy_lab.cli portfolio signal-profile artifacts\portfolio_runs\portfolio_xxx\portfolio_run_state.json --symbols 512800.SH
```

正常组合层流程不需要传该参数，应处理全部资产。

## 输入要求

运行前必须已经完成：

```text
portfolio-run
DataAgent 数据准备
portfolio-data-split
portfolio-profile
```

本 skill 会读取：

```text
portfolio_run_state.json
profile/daily_signal_targets.parquet
source_artifacts/signals/{symbol}/signal_agent_memory.md
source_artifacts/signals/{symbol}/strategy.py
source_artifacts/signals/{symbol}/strategy_spec.md
source_artifacts/signals/{symbol}/param_space.json
source_artifacts/signals/{symbol}/strategy_meta.json
source_artifacts/signals/{symbol}/metrics.json
source_artifacts/signals/{symbol}/run_state.json
```

其中 `daily_signal_targets.parquet` 来自 `portfolio-profile`，是每个资产信号层策略在组合层数据区间上的每日 `target_S`。

## 内部执行流程

```text
1. 读取 portfolio_run_state.json。
2. 找到 profile/daily_signal_targets.parquet。
3. 对每个资产计算 target_S 的分布：均值、空仓比例、活跃比例、强信号比例、仓位档位、变化次数等。
4. 读取该资产复制过来的信号层小文件：memory、strategy_spec、strategy_meta、param_space、metrics、run_state。
5. 调用大模型提炼策略语义；如果使用 --no-llm，则只用规则画像。
6. 合并机器统计和语义画像，估计 reliability score。
7. 输出每个资产的 fusion guidance。
8. 生成全局 signal_profiles.json/md/csv 和 per_asset 文件。
9. 更新 portfolio_run_state.json 的 signal_profile 字段。
```

## 大模型调用位置

大模型调用写在：

```text
src/strategy_lab/services/portfolio_signal_profile.py
```

关键方法：

```text
_create_model()
  读取 configs/agent.yaml 和 .env，创建 DeepSeek/Kimi/OpenAI-compatible 模型。
  默认使用 portfolio_signal_profile 配置；当前默认是 DeepSeek deepseek-v4-pro。
  DeepSeek 会开启 thinking 和 reasoning_effort。
  本服务不设置 max_completion_tokens，也不对输入材料做上下文压缩。

_llm_system_prompt()
  信号画像语义提取的系统提示词。

_build_llm_prompt()
  给单个资产构造用户提示词，包含机器统计、绩效摘要、memory、strategy_spec、strategy_meta、param_space 和 strategy.py 摘要。

_llm_semantic_profile()
  实际调用 llm.invoke(...)，解析 JSON 输出。
```

该 LLM 调用是服务内部调用，不是 DeepAgents 主 Agent 的推理轮次。
默认串行调用；传入 `--max-workers 10` 时会对多个资产并发调用 LLM，由于资产较多，为避免超时建议设置较大超时时间。

## 输出文件

默认输出到：

```text
artifacts/portfolio_runs/{portfolio_run_id}/signal_profiles/
```

主要文件：

```text
signal_profile_manifest.json
signal_profiles.json
signal_profiles.csv
signal_profiles.md
daily_calibrated_signal_strength.parquet
daily_signal_state.parquet
per_asset/{symbol}/signal_profile.json
per_asset/{symbol}/signal_profile.md
```

文件含义：

`signal_profiles.json`
完整结构化信号画像，包含每个资产的信号分布、策略语义、可靠度和组合层使用建议。

`signal_profiles.md`
给 PortfolioAgent 阅读的摘要报告。

`signal_profiles.csv`
便于快速横向比较的表格。

`daily_calibrated_signal_strength.parquet`
按资产自身仓位档位和可靠度校准后的每日信号强度。注意它不是原始 `target_S`。

`daily_signal_state.parquet`
每日信号状态，常见值包括 `zero`、`weak`、`medium`、`strong`、`defensive_participation`。

`per_asset/{symbol}/signal_profile.json/md`
单资产信号画像，方便需要细看某个资产时读取。

## portfolio_run_state.json 更新

服务会自动写入：

```text
signal_profile.status
signal_profile.profile_dir
signal_profile.signal_profiles_path
signal_profile.signal_profiles_md_path
signal_profile.signal_profiles_csv_path
signal_profile.daily_calibrated_signal_strength_path
signal_profile.daily_signal_state_path
signal_profile.summary
signal_profile.warnings
```

并在 `artifacts.profiles.portfolio_signal_profile` 和 `events` 中登记。

## 使用要点

写组合层策略前必须读取：

```text
signal_profiles/signal_profiles.md
signal_profiles/signal_profiles.json
```

如果需要细看某个资产，再读取：

```text
signal_profiles/per_asset/{symbol}/signal_profile.md
```

组合层策略编写时重点使用这些字段：

```text
signal_distribution.mean_target
signal_distribution.zero_ratio
signal_distribution.gt_0_6_ratio
semantic_profile.style_tags
semantic_profile.strategy_role
reliability.score
fusion_guidance.recommended_uses
fusion_guidance.veto_power
fusion_guidance.boost_power
fusion_guidance.budget_override_suitability
fusion_guidance.suggested_constraints
```

不要把不同资产的原始 `target_S` 机械横向比较。每个资产信号层策略的仓位档位可能不同，应结合 `daily_calibrated_signal_strength.parquet` 和 `position_interpretation` 理解。

## 检查清单

执行后必须检查：

```text
1. CLI 返回 status 不是 failed。
2. signal_profiles/signal_profile_manifest.json 存在。
3. signal_profiles/signal_profiles.md 存在。
4. signal_profiles/signal_profiles.json 存在且 JSON 可解析。
5. signal_profiles/signal_profiles.csv 存在。
6. signal_profiles/daily_calibrated_signal_strength.parquet 存在。
7. signal_profiles/daily_signal_state.parquet 存在。
8. 每个资产尽量存在 per_asset/{symbol}/signal_profile.md。
9. portfolio_run_state.json 的 signal_profile.status 为 success。
10. events 中有 portfolio_signal_profile_completed。
```

## 下一步

完成后阅读 `portfolio-policy-authoring`，基于 `portfolio-profile` 和 `portfolio-signal-profile` 共同创建或修改组合层五件套，然后调用 `portfolio-evaluation`。
