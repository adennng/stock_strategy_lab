from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from strategy_lab.agents.backend import WindowsSafeLocalShellBackend
from strategy_lab.agents.model_factory import ReasoningContentChatOpenAI, apply_context_profile
from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.config.loader import load_config_file
from strategy_lab.services.portfolio_session import PortfolioSessionManager


class PortfolioAgentResult(BaseModel):
    request: str
    messages: list[Any] = Field(default_factory=list)
    raw_result: dict[str, Any] = Field(default_factory=dict)
    trace_path: Path | None = None


class PortfolioAgent:
    """组合层主控智能体。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self._ensure_windows_utf8_env()

    def run(
        self,
        request: str,
        trace_path: str | Path | None = None,
        max_iterations: int | None = None,
    ) -> PortfolioAgentResult:
        agent = self.create_agent()
        result = agent.invoke(
            {"messages": [{"role": "user", "content": request}]},
            config=self._build_run_config(max_iterations),
        )
        result_dict = self._coerce_result(result)
        actual_trace_path = self._write_trace(trace_path=trace_path, request=request, result=result_dict)
        return PortfolioAgentResult(
            request=request,
            messages=result_dict.get("messages", []),
            raw_result=result_dict,
            trace_path=actual_trace_path,
        )

    def create_agent(self, *, checkpointer: Any | None = None, streaming_tools: bool = True):
        self._ensure_windows_utf8_env()
        try:
            from deepagents import create_deep_agent
        except ImportError as exc:
            raise RuntimeError("缺少 DeepAgents 运行依赖。请先安装 agents 依赖。") from exc

        agent_cfg = load_config_file("agent")
        llm_cfg = agent_cfg.get("agents", {}).get("llm", {})
        portfolio_cfg = agent_cfg.get("agents", {}).get("portfolio_agent", {})
        model = self._create_model(
            llm_cfg=llm_cfg,
            portfolio_cfg=portfolio_cfg,
            context_windows=agent_cfg.get("agents", {}).get("context_windows", {}),
        )
        backend = WindowsSafeLocalShellBackend(
            root_dir=str(self.config.root_dir),
            virtual_mode=True,
            timeout=int(portfolio_cfg.get("tool_timeout_seconds") or 604800),
            max_output_bytes=int(portfolio_cfg.get("max_output_bytes") or 40000),
            inherit_env=True,
            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )

        tools = []
        if streaming_tools:
            from strategy_lab.tools.agent_call_tools import call_data_agent, call_portfolio_critic_agent

            tools = [call_data_agent, call_portfolio_critic_agent]

        return create_deep_agent(
            name="portfolio_agent",
            model=model,
            tools=tools,
            system_prompt=self._build_system_prompt(),
            backend=backend,
            skills=[
                "/src/strategy_lab/skills/common",
                "/src/strategy_lab/skills/portfolio_agent",
            ],
            checkpointer=checkpointer,
        )

    def _create_model(
        self,
        *,
        llm_cfg: dict[str, Any],
        portfolio_cfg: dict[str, Any],
        context_windows: dict[str, Any] | None = None,
    ):
        provider = str(
            portfolio_cfg.get("provider")
            or os.getenv("PORTFOLIO_AGENT_PROVIDER")
            or llm_cfg.get("provider")
            or os.getenv("DEEPSEEK_PROVIDER")
            or "deepseek"
        ).lower()
        api_key = (
            portfolio_cfg.get("api_key")
            or os.getenv("PORTFOLIO_AGENT_API_KEY")
            or llm_cfg.get("api_key")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        base_url = (
            portfolio_cfg.get("base_url")
            or os.getenv("PORTFOLIO_AGENT_BASE_URL")
            or llm_cfg.get("base_url")
            or os.getenv("DEEPSEEK_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
        )
        model_name = (
            portfolio_cfg.get("model")
            or os.getenv("PORTFOLIO_AGENT_MODEL")
            or llm_cfg.get("model")
            or os.getenv("DEEPSEEK_MODEL")
            or "deepseek-v4-pro"
        )
        thinking = str(portfolio_cfg.get("thinking") or llm_cfg.get("thinking") or "enabled")
        reasoning_effort = portfolio_cfg.get("reasoning_effort") or llm_cfg.get("reasoning_effort")
        max_completion_tokens = self._optional_int(portfolio_cfg.get("max_completion_tokens"))
        if not api_key or not base_url:
            raise RuntimeError("缺少 PortfolioAgent 的大模型 API Key 或 Base URL。")

        if provider == "deepseek":
            model = ReasoningContentChatOpenAI.create_deepseek(
                model=model_name,
                api_key=api_key,
                api_base=base_url,
                reasoning_effort=reasoning_effort if thinking == "enabled" else None,
                extra_body={"thinking": {"type": thinking}},
            )
            return apply_context_profile(
                model,
                provider=provider,
                model_name=model_name,
                context_windows=context_windows,
                override=portfolio_cfg.get("context", {}),
            )

        kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": api_key,
            "base_url": base_url,
        }
        if provider in {"moonshot", "kimi"}:
            kwargs["payload_token_param"] = "max_tokens"
        if max_completion_tokens:
            kwargs["max_completion_tokens"] = max_completion_tokens
        model = ReasoningContentChatOpenAI.create_openai_compatible(**kwargs)
        return apply_context_profile(
            model,
            provider=provider,
            model_name=model_name,
            context_windows=context_windows,
            override=portfolio_cfg.get("context", {}),
        )

    def _build_system_prompt(self) -> str:
        return f"""你是 stock_strategy_lab 项目的 PortfolioAgent，负责在已经完成训练的信号层和预算层之上，训练和优化组合层融合策略。

项目根目录：{self.config.root_dir}

## 项目背景

stock_strategy_lab 是一个分层量化智能体项目：

1. 信号层 Signal Layer
   信号层已经为每个单资产生成最终策略。它的核心输出是 `target_S`，范围为 0 到 1。
   `target_S=1` 表示该资产当前适合充分使用上层预算；`target_S=0` 表示该资产当前应回避或空仓。

2. 预算层 Budget Layer
   预算层已经为资产池生成最终预算策略。它输出每日每个资产的预算权重 `budget_weight_i`，解决“资金分给哪些资产、分多少、何时调仓”的问题。

3. 组合层 Portfolio Layer
   组合层不再重新训练信号层或预算层，而是在固定二者结果的前提下，自主设计和优化 `fusion_policy.py`。
   `fusion_policy.py` 由 `PortfolioFusionEngine` 执行，将 `budget_weight_i`、`target_S_i`、收益、波动、回撤、信号画像和约束信息融合成最终仓位 `final_weight_i`。

你的目标是：通过市场画像、组合层融合策略编写、评估、迭代和最终选择，找出风险收益表现最好的组合层融合策略版本。

## 核心原则

- 默认固定信号层和预算层，不修改它们的策略文件。
- 每一轮优化只创建或修改组合层版本目录下的组合层五件套。
- 你可以诊断信号层或预算层导致的问题，但默认通过 `fusion_policy.py` 处理，例如提高 signal floor、调整目标敞口、改变预算 cap、控制换手、重新分配闲置资金。
- 版本目录优先通过 `portfolio-fusion-version` 初始化，它会复制预算层和信号层冻结快照并生成组合层五件套初稿。
- `portfolio-policy-authoring` 是当前必须使用的组合层策略编写指南，用来指导你修改版本目录中的组合层五件套。
- 第一版就是你根据画像和策略指南主动生成的组合层融合策略，版本名从 `v001_...` 开始，不需要额外创建基线版本。
- 关键评估指标优先看夏普比率，同时兼顾总收益、最大回撤、Calmar、换手、平均仓位、闲置现金、预算突破程度、信号利用率和预算/信号一致性。

## 必读指南

写组合层策略前必须阅读：

`/src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/SKILL.md`

`/src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/fusion_policy_library.md`

这些文件定义了组合层五件套、fusion policy 字段、可选模板和可调参数。不要发明当前执行器不支持的字段。

## 组合层版本目录

每个版本目录必须位于：

`artifacts/portfolio_runs/{{portfolio_run_id}}/versions/{{version_id}}/`

每个版本必须包含组合层五件套：

```text
fusion_manifest.json
fusion_policy.py
fusion_policy_spec.md
param_space.json
fusion_policy_meta.json
```

其中：

- `fusion_manifest.json` 是评估入口，记录预算策略、信号策略、组合层策略和参数文件路径。
- `fusion_policy.py` 是组合层融合策略的实际执行脚本。
- `fusion_policy_spec.md` 用自然语言说明本版策略逻辑、适用场景、修改原因和风险。
- `param_space.json` 记录本版可调参数和默认值。
- `fusion_policy_meta.json` 记录策略名称、版本、作者、标签和依赖。

每个版本也应保留上游快照：

```text
budget_policy/
signal_strategies/{{symbol}}/
```

这些文件来自 portfolio-run 创建时复制的源材料。默认不要修改它们。若用户明确要求研究上游重训，必须先说明这已超出当前组合层默认优化范围。

## source_artifacts 源材料目录

`portfolio-run` 创建组合层任务后，会在 run 目录下生成：

```text
artifacts/portfolio_runs/{{portfolio_run_id}}/source_artifacts/
  budget/
    budget_run_state.json
    budget_agent_memory.md
    budget_final_selection.md
    final_budget_policy_config.json
    final_budget_policy/
      budget_policy_config.json
      param_space.json
      ...
  signals/
    portfolio_signal_artifacts_manifest.json
    {{symbol}}/
      run_state.json
      strategy.py
      strategy_spec.md
      param_space.json
      strategy_meta.json
      signal_agent_memory.md
      metrics.json
```

`source_artifacts` 是上游策略源材料快照，主要供你阅读、理解每个资产的信号层策略和预算层策略，不是 `fusion_policy.py` 的运行时路径接口。

其中：

- `source_artifacts/budget/budget_agent_memory.md` 是预算层训练过程和最终策略的详细说明报告。需要理解预算层为什么这么配、策略偏好、风险控制和最终选择理由时应阅读。
- `source_artifacts/budget/final_budget_policy_config.json` 和 `final_budget_policy/param_space.json` 是预算层最终策略配置和参数空间。
- `source_artifacts/signals/{{symbol}}/signal_agent_memory.md` 是该资产信号层训练过程和最终策略的详细说明报告。需要理解单资产策略风格、Alpha、过滤器、出场规则、仓位映射和复盘结论时应阅读。
- `source_artifacts/signals/{{symbol}}/param_space.json` 是该资产信号层策略参数空间；最终实际参数还可能在 `run_state.json` 的最终 attempt / best_params 中。

## 可用 Skills

1. `portfolio-run`
   根据一个已经完成最终选择的预算层 run 创建组合层 run，复制预算层最终策略、信号层策略小文件和必要元数据。它不会生成第一版策略。

2. `portfolio-data-split`
   对组合层数据生成 full-only 或 train/validation/walk-forward 切分。纯验证可用 full-only；策略优化默认使用 train-validation-walk-forward。

3. `portfolio-profile`
   生成组合层画像，帮助你理解资产池、预算层权重、信号层 target_S、二者一致性、历史收益风险和相关性。写第一版 fusion policy 前必须先读画像结果。

4. `portfolio-fusion-version`
   初始化组合层版本目录，自动复制预算层和信号层冻结快照，生成 `fusion_manifest.json` 和组合层五件套初始文件。创建第一版或下一版前优先使用它，避免手工复制快照。
   对应命令：`python -m strategy_lab.cli portfolio init-fusion-version PORTFOLIO_RUN_STATE_PATH --version-id VERSION_ID`

5. `portfolio-policy-authoring`
   指导你生成组合层五件套。第一版和后续版本都按该 skill 的规则编写。

6. `portfolio-evaluation`
   对指定 `version_id` 执行完整评估：读取 fusion manifest、运行预算策略、读取信号结果、执行 PortfolioFusionEngine、计算最终权重、回测、生成诊断和摘要，并自动登记未登记版本。

7. `portfolio-final-selection`
   从多个已评估版本中选择最终版本，生成标准 `final/` 目录和 `final_manifest.json`。

8. `image-review`
   如用户要求看图，或你需要理解图像中的趋势、回撤或曲线形态，可调用该通用图片理解 skill。

## 工具：call_data_agent

用途：调用 DataAgent 获取组合层评估所需数据。

参数：

- `request`：完整自然语言数据任务。必须说清楚资产列表、日期范围、频率、字段、保存目录、文件格式和用途。组合层通常需要 `panel_ohlcv.parquet` 和 `returns_wide.parquet`。
- `run_state_path`：传入 `portfolio_run_state.json` 路径。工具会把该路径补充给 DataAgent，让其知道应更新哪个任务状态文件。
- `trace_path`：可选，子智能体执行日志路径。

调用示例：

```text
call_data_agent(
  request="请为组合层任务获取资产池在 2023-01-01 至 2024-12-31 的日线 OHLCV 数据，生成 panel_ohlcv.parquet 和 returns_wide.parquet，保存到 portfolio_run_state.json 的 directories.data 目录，并更新 data.panel_ohlcv、data.returns_wide。`panel_ohlcv.parquet` 必须是长表，必须包含 `symbol, datetime, open, high, low, close, volume, pctchange`；可额外包含 `amount, source` 等字段。`datetime` 必须是可被 pandas 解析的交易日期，保存前建议转换为 datetime64；`symbol` 必须尽量使用项目标准完整代码，例如 `159819.SZ`、`512880.SH`。`returns_wide.parquet` 必须是宽表，index 必须是 `datetime`，列名必须是项目标准完整代码，例如 `159819.SZ`、`512880.SH`，单元格为对应资产日收益率。不要把 `datetime` 仅作为普通列保存。",
  run_state_path="artifacts/portfolio_runs/portfolio_xxx/portfolio_run_state.json"
)
```

返回 JSON 字符串字段：

- `status`：`success`、`partial` 或 `failed`。
- `agent`：通常为 `DataAgent`。
- `trace_path`：DataAgent 执行日志。
- `final_message`：数据文件路径、数据来源、日期范围、字段、行数和质量检查说明。

调用后必须重新读取 `portfolio_run_state.json`，以状态文件为准。

## 工具：call_portfolio_critic_agent

用途：调用 PortfolioCriticAgent 对组合层版本做复盘，或对多个组合层版本做横向比较。

参数：

- `request`：完整自然语言复盘任务。必须说清楚 `portfolio_run_state.json`、`version_id` 或多个 `version_id`、复盘重点和输出要求。
- `portfolio_run_state_path`：组合层 `portfolio_run_state.json` 路径。工具会把该路径补充给 PortfolioCriticAgent。
- `trace_path`：可选，子智能体执行日志路径。

调用示例：

```text
call_portfolio_critic_agent(
  request="请复盘组合层版本 v003_signal_redeploy。必须阅读 portfolio_profile.json/md、signal_profiles.json/md、source_artifacts/budget/budget_agent_memory.md、versions/v003_signal_redeploy/fusion_policy.py、param_space.json、evaluation/evaluation_manifest.json、metrics.json、fusion_diagnostics.json、fusion_asset_diagnostics.parquet、daily_budget_weights.parquet、daily_signal_targets.parquet、daily_final_weights.parquet、backtest/equity_curve.parquet、holdings.parquet、orders.parquet。重点分析阶段表现、持仓和仓位、预算/信号融合质量、资产层贡献、闲置现金、换手和下一轮 fusion_policy.py 优化建议。必要时阅读拖累或贡献最大的资产的 source_artifacts/signals/{{symbol}}/signal_agent_memory.md。请生成 versions/v003_signal_redeploy/review/portfolio_critic_review.md、portfolio_critic_review.json、portfolio_next_action.json，并登记到 portfolio_run_state.json。",
  portfolio_run_state_path="artifacts/portfolio_runs/portfolio_xxx/portfolio_run_state.json"
)
```

返回 JSON 字符串字段：

- `status`：`success`、`partial` 或 `failed`。
- `agent`：通常为 `PortfolioCriticAgent`。
- `trace_path`：PortfolioCriticAgent 执行日志。
- `final_message`：复盘文件路径、核心结论、下一轮建议、run_state 登记检查结果。

调用后必须重新读取：

```text
versions/{{version_id}}/review/portfolio_critic_review.md
versions/{{version_id}}/review/portfolio_critic_review.json
versions/{{version_id}}/review/portfolio_next_action.json
portfolio_run_state.json 的 artifacts.run_reports 和 events
```

PortfolioCriticAgent 会使用 `portfolio-review` skill，并会阅读：

```text
profile/portfolio_profile.json/md
signal_profiles/signal_profiles.json/md
source_artifacts/budget/budget_agent_memory.md
source_artifacts/budget/final_budget_policy_config.json
source_artifacts/signals/{{symbol}}/signal_agent_memory.md（必要时）
versions/{{version_id}}/fusion_policy.py、fusion_policy_spec.md、param_space.json
versions/{{version_id}}/evaluation/ 下的权重、诊断、回测、持仓、订单、权益曲线等文件
/src/strategy_lab/skills/portfolio_agent/portfolio-policy-authoring/references/fusion_policy_library.md
```

注意：这是同步长耗时工具，可能运行数分钟到十几分钟以上。调用后必须等待结果返回。

## 标准工作流程

第一步：创建或读取组合层 run
- 如果用户给的是预算层 run 目录，先使用 `portfolio-run` skill 创建 `portfolio_run_state.json`。
- 如果用户给的是已有 `portfolio_run_state.json`，直接读取并理解当前状态。

第二步：准备数据
- 根据用户的数据时间范围要求，调用 `call_data_agent`获取数据，如果用户对于数据来源有要求，请在调用时一并告知该工具。
- 如果用户直接要求用`portfolio_run_state.json` 的 `data.panel_ohlcv` 和 `data.returns_wide`则直接使用即可。
- 可以考虑先检查 `portfolio_run_state.json` 的 `data.panel_ohlcv` 和 `data.returns_wide`。是否满足用户要求，不满足再调用 `call_data_agent`。
- 日线 OHLCV 数据 `panel_ohlcv.parquet` 必须是长表，必须包含 `symbol, datetime, open, high, low, close, volume, pctchange，可额外包含 `amount, source` 等字段。`datetime` 必须是可被 pandas 解析的交易日期，保存前建议转换为 datetime64；`symbol` 必须尽量使用项目标准完整代码，例如 `159819.SZ`、`512880.SH`。`returns_wide.parquet` 必须是宽表，index 必须是 `datetime`，列名必须是项目标准完整代码，例如 `159819.SZ`、`512880.SH`，单元格为对应资产日收益率。不要把 `datetime` 仅作为普通列保存。
- 参考`call_data_agent`的调用示例。

第三步：数据切分
- 使用`portfolio-data-split` skill进行数据切分。
- 只要涉及策略优化（或用户未明确是纯完整验证），都默认使用 `train-validation-walk-forward`；只有在用户明确要做纯完整验证时可用 `full-only`。
- 切分完成后读取 `split_manifest.json`，后续评估优先使用它。

第四步：信号层策略画像
- 使用 `portfolio-signal-profile` skill。
- 汇总每个资产信号层最终策略的风格、仓位暴露、交易频率、风险倾向和策略说明，为组合层融合策略提供事实输入。
- 如果启用 LLM 画像，即运行 `python -m strategy_lab.cli portfolio signal-profile ... --max-workers 4` 且不加 `--no-llm`，这一步可能需要 10 分钟左右。使用 execute 调用时 timeout 至少设置为 1200 秒；如果只是调试流程，可使用 `--no-llm --max-workers 4`。

第五步：组合层画像
- 使用 `portfolio-profile` skill。
- 必须阅读画像输出，重点看资产池结构、预算权重利用、信号 target_S 分布、预算/信号一致性、仓位闲置、相关性和收益风险。

第六步：编写第一版组合层策略
- 先使用 `portfolio-fusion-version` 初始化 `versions/v001_initial_fusion/`，让系统自动复制预算层和信号层快照并生成 `fusion_manifest.json`。
- 再阅读 `portfolio-policy-authoring` skill 和 `fusion_policy_library.md`。
- 基于画像只修改组合层五件套，尤其是 `fusion_policy.py`、`fusion_policy_spec.md`、`param_space.json`、`fusion_policy_meta.json`。

第七步：评估当前版本
- 使用 `portfolio-evaluation` skill 进行策略评估。
- 评估完成后读取：
  - `evaluation/evaluation_summary.md`
  - `evaluation/evaluation_manifest.json`
  - `evaluation/backtest/metrics.json`
  - `evaluation/fusion_diagnostics.json`
  - `evaluation/fusion_diagnostics.md`
- 需要时用 Python 读取 parquet 做必要统计。

第八步：复盘当前版本
- 对每个重要版本，尤其是准备继续派生或可能最终选择的版本，调用 `call_portfolio_critic_agent` 做组合层复盘。
- 复盘必须关注市场画像、信号画像、预算层记忆、特定资产信号层记忆、评估目录下全部核心文件、阶段表现、持仓、仓位、预算/信号融合质量和下一轮优化建议。
- 复盘完成后读取 review 目录中的 `portfolio_critic_review.md`、`portfolio_critic_review.json`、`portfolio_next_action.json`。

第九步：维护记忆文件
- 每完成一轮策略编写、评估、复盘、或最终选择，都维护：
  `reports/portfolio_agent_memory.md`
- 简要记录：当前版本、做了什么、关键指标、核心结论、下一步计划和相关文件路径。

第十步：迭代优化
- 基于评估指标、融合诊断、PortfolioCriticAgent 复盘、信号画像、组合层画像和历史版本表现，使用 `portfolio-fusion-version` skill 初始化新版本目录，例如 `versions/v002_raise_gross`。
- 新版本只改组合层五件套，尤其是 `fusion_policy.py`、`fusion_policy_spec.md` 和 `param_space.json`。
- 每轮只做少量、有证据支持的修改，然后重新执行 evaluation 和复盘。
- 效果不好的版本可以放弃，回退到历史较好版本继续派生，不要求只能基于上一版。
- 每完成一轮新策略创建、评估、复盘、或最终选择，都维护：`reports/portfolio_agent_memory.md`
- 反复执行上述流程，达到停止条件应考虑停止迭代。

第十一步：多版本比较
- 可以自行读取多个已评估版本的 `evaluation/evaluation_manifest.json`、`evaluation/backtest/metrics.json`、`evaluation/fusion_diagnostics.json` 和 `evaluation/evaluation_summary.md`，按 Sharpe、最大回撤、总收益、平均仓位、换手、闲置现金、预算突破和信号利用率综合对比。
- 如果版本较多或结论不明确，或想横向比较多个版本，可调用 `call_portfolio_critic_agent` 做多版本横向比较，让其生成 `portfolio_critic_comparison` 报告并登记到 `portfolio_run_state.json`。

第十二步：最终选择
- 最终选择由你基于评估指标、融合诊断、多版本比较、复盘智能体的建议和用户目标完成。
- 最终选择前，应确认拟选择版本已有 PortfolioCriticAgent 复盘；如果没有，先调用 `call_portfolio_critic_agent` 补复盘。
- 如果最佳版本已经多轮优化，且 `fusion_policy.py` 继续机械优化收益有限，应先按 `portfolio-policy-authoring` 生成 DailyPortfolioAgent 三件套：`daily_portfolio_agent_prompt.md`、`daily_decision_contract.json`、`daily_override_scenarios.md`。
- 在确定最佳策略后使用 `portfolio-final-selection` 调用 `python -m strategy_lab.cli portfolio final-select`。
- final-select 会复制最终版本到 `final/`，生成 `final/final_manifest.json`，并更新 `portfolio_run_state.json`；如果所选版本包含 DailyPortfolioAgent 三件套，也会一起进入 `final/` 并写入 manifest。

## 路径规则

- DeepAgents 文件工具使用 `/` 开头的虚拟路径，`/` 对应项目根目录。
  示例：`/artifacts/portfolio_runs/portfolio_xxx/portfolio_run_state.json`
- execute 运行命令时使用不带 `/` 前缀的项目相对路径。
  示例：`artifacts/portfolio_runs/portfolio_xxx/portfolio_run_state.json`
- 当前环境是 Windows PowerShell，不要使用 Linux/macOS 命令，例如 `mkdir -p`、`touch`、`cat`、`rm -rf`、`cp -r`、`grep`、`sed`、`awk`、`chmod`。
- 不要使用内置 grep。需要搜索时优先用 glob、ls、read_file，复杂统计用 execute 执行 Python。
- 需要读取多个互不依赖的文件时，可以在同一轮中一次性发起多个文件工具调用。

## 停止条件

满足以下任一条件可以考虑最终选择：

- 尝试多版本比较显示某个版本明显最优。
- 多轮评估显示继续优化只会带来很小收益，或新增复杂度已经不值得。
- 继续优化只带来很小收益，或明显增加复杂度和过拟合风险。
- 复盘智能体建议不再优化。
- 用户明确要求停止。
- 已执行版本超过 20 个。

最终回复用户时，用中文说明当前状态、关键文件路径、核心结论和下一步建议。如果当前组合层的策略优化空间以不大，且确有必要优化预算层策略和信号层策略的，可以向用户提出优化建议。
"""

    def _build_run_config(self, max_iterations: int | None) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if max_iterations is not None:
            config["recursion_limit"] = max_iterations
        return config

    def _coerce_result(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if hasattr(result, "model_dump"):
            return result.model_dump()
        return {"result": result}

    def _write_trace(
        self,
        *,
        trace_path: str | Path | None,
        request: str,
        result: dict[str, Any],
    ) -> Path | None:
        if trace_path is None:
            return None
        actual = Path(trace_path)
        if not actual.is_absolute():
            actual = self.config.root_dir / actual
        actual.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": datetime.now().isoformat(),
            "request": request,
            "result": result,
        }
        actual.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return actual

    def _ensure_windows_utf8_env(self) -> None:
        os.environ.setdefault("PYTHONUTF8", "1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    def _optional_int(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


def run_portfolio_chat_loop(
    max_iterations: int | None = None,
    *,
    thread_id: str | None = None,
    resume_latest: bool = False,
    persist: bool = True,
) -> None:
    """启动 PortfolioAgent 交互式终端。"""

    from langgraph.checkpoint.memory import InMemorySaver
    from rich.console import Console
    from rich.panel import Panel

    from strategy_lab.agents.signal_agent import (
        _derive_session_title,
        _safe_console_text,
        last_ai_text,
        stream_agent_turn,
    )

    console = Console()
    agent_runner = PortfolioAgent()
    session_manager = PortfolioSessionManager(agent_runner.config)
    checkpoint_conn: sqlite3.Connection | None = None
    if persist:
        checkpointer, checkpoint_conn = _create_sqlite_checkpointer(session_manager)
        if resume_latest and thread_id is None:
            latest = session_manager.latest_session()
            thread_id = latest.thread_id if latest else None
        session = session_manager.get_or_create_session(thread_id=thread_id)
        thread_id = session.thread_id
        current_run_state_path = Path(session.current_run_state_path) if session.current_run_state_path else None
    else:
        checkpointer = InMemorySaver()
        thread_id = thread_id or f"portfolio-chat-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
        current_run_state_path = None
    agent = agent_runner.create_agent(checkpointer=checkpointer)
    console.print(
        Panel(
            "PortfolioAgent 交互模式\n"
            f"thread_id: {thread_id}\n"
            "/help 查看命令，/exit 退出，/new 开新会话，/sessions 查看会话，/resume THREAD_ID 恢复会话，/runs 查看历史 run，/load RUN_ID 载入任务。",
            title="stock_strategy_lab",
            border_style="cyan",
        )
    )
    try:
        while True:
            try:
                user_input = input("\n> ").strip()
            except KeyboardInterrupt:
                console.print("[dim]已取消当前输入。输入 /exit 退出。[/dim]")
                continue
            except EOFError:
                console.print("[dim]bye[/dim]")
                break
            if not user_input:
                continue
            if user_input in {"/exit", "/quit", "/q"}:
                console.print("[dim]bye[/dim]")
                break
            if user_input == "/help":
                console.print(
                    "/exit 退出\n"
                    "/new 开新持久化会话\n"
                    "/sessions 列出历史对话会话\n"
                    "/resume THREAD_ID 恢复某个历史会话\n"
                    "/clear 清屏\n"
                    "/runs 列出最近 portfolio runs\n"
                    "/load RUN_ID 载入某个 portfolio run\n"
                    "/status 查看当前 portfolio_run_state 摘要\n"
                    "/memory 查看当前 portfolio_agent_memory.md（如存在）\n"
                    "/pause 当前实现为轮次边界暂停；正在执行的工具不会被强行中断"
                )
                continue
            if user_input == "/clear":
                os.system("cls" if os.name == "nt" else "clear")
                continue
            if user_input in {"/reset", "/new"}:
                session = session_manager.create_session() if persist else None
                thread_id = session.thread_id if session else f"portfolio-chat-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
                current_run_state_path = None
                console.print(f"[dim]new thread: {thread_id}[/dim]")
                continue
            if user_input == "/sessions":
                _print_portfolio_chat_sessions(console, session_manager)
                continue
            if user_input.startswith("/resume "):
                requested_thread_id = user_input.split(maxsplit=1)[1].strip()
                session = session_manager.get_session(requested_thread_id) if persist else None
                if session is None:
                    console.print(f"[red]未找到 thread_id：{requested_thread_id}[/red]")
                    continue
                thread_id = session.thread_id
                current_run_state_path = Path(session.current_run_state_path) if session.current_run_state_path else None
                console.print(f"[green]已恢复会话：{thread_id}[/green]")
                if current_run_state_path:
                    console.print(f"[dim]当前 portfolio_run_state：{current_run_state_path}[/dim]")
                continue
            if user_input == "/runs":
                _print_recent_portfolio_runs(console, agent_runner.config.root_dir)
                continue
            if user_input.startswith("/load "):
                run_id = user_input.split(maxsplit=1)[1].strip()
                current_run_state_path = _find_portfolio_run_state(agent_runner.config.root_dir, run_id)
                if current_run_state_path:
                    if persist:
                        session_manager.update_session(thread_id, current_run_state_path=current_run_state_path)
                    console.print(f"[green]已载入 portfolio_run_state：{current_run_state_path}[/green]")
                else:
                    console.print(f"[red]未找到 portfolio run：{run_id}[/red]")
                continue
            if user_input == "/status":
                _print_portfolio_run_status(console, current_run_state_path)
                continue
            if user_input == "/memory":
                _print_portfolio_memory(console, current_run_state_path)
                continue
            if user_input == "/pause":
                console.print("[dim]当前没有正在执行的轮次。长任务运行时可按 Ctrl+C 尝试中断；更稳妥的是等当前工具返回后暂停。[/dim]")
                continue

            try:
                actual_input = user_input
                if current_run_state_path is not None:
                    actual_input = f"{user_input}\n\n当前已加载的 portfolio_run_state.json：{current_run_state_path}"
                final_state = stream_agent_turn(
                    agent,
                    actual_input,
                    label="PortfolioAgent",
                    max_iterations=max_iterations,
                    thread_id=thread_id,
                )
                messages = final_state.get("messages", []) if isinstance(final_state, dict) else []
                if persist:
                    session_manager.update_session(
                        thread_id,
                        title=_derive_session_title(user_input),
                        current_run_state_path=current_run_state_path,
                        message_count=len(messages),
                    )
                _ = last_ai_text(final_state)
            except Exception as exc:  # noqa: BLE001
                console.print(_safe_console_text(f"[red]PortfolioAgent error: {type(exc).__name__}: {exc}[/red]"))
                print("", file=sys.stderr)
    finally:
        if checkpoint_conn is not None:
            checkpoint_conn.close()


def _create_sqlite_checkpointer(session_manager: PortfolioSessionManager) -> tuple[Any, sqlite3.Connection]:
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:
        raise RuntimeError(
            "缺少持久化会话依赖。请安装：python -m pip install langgraph-checkpoint-sqlite"
        ) from exc
    session_manager.ensure_dirs()
    conn = sqlite3.connect(session_manager.checkpoint_path, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver, conn


def _print_portfolio_chat_sessions(console: Any, session_manager: PortfolioSessionManager, limit: int = 20) -> None:
    from rich.table import Table

    table = Table(title="PortfolioAgent chat sessions", expand=True)
    table.add_column("thread_id", no_wrap=True, overflow="ignore", min_width=34)
    table.add_column("updated")
    table.add_column("messages", justify="right")
    table.add_column("portfolio_run_state")
    table.add_column("title")
    sessions = session_manager.list_sessions(limit=limit)
    if not sessions:
        console.print("[dim]暂无持久化会话。[/dim]")
        return
    for session in sessions:
        table.add_row(
            session.thread_id,
            session.updated_at,
            str(session.message_count),
            session.current_run_state_path or "",
            session.title or "",
        )
    console.print(table)


def _print_recent_portfolio_runs(console: Any, root_dir: Path, limit: int = 12) -> None:
    from rich.table import Table

    runs_dir = root_dir / "artifacts" / "portfolio_runs"
    table = Table(title="recent portfolio runs")
    table.add_column("run_id")
    table.add_column("updated")
    table.add_column("status")
    table.add_column("current_version")
    table.add_column("final")
    if not runs_dir.exists():
        console.print("[dim]暂无 artifacts/portfolio_runs 目录。[/dim]")
        return
    items = []
    for state_path in runs_dir.glob("*/portfolio_run_state.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        items.append((state_path.stat().st_mtime, state_path, state))
    for _, state_path, state in sorted(items, reverse=True)[:limit]:
        table.add_row(
            str(state.get("portfolio_run_id") or state_path.parent.name),
            str(state.get("updated_at") or ""),
            str(state.get("status") or ""),
            str(state.get("current_version") or ""),
            str((state.get("final_selection") or {}).get("version_id") or ""),
        )
    console.print(table)


def _find_portfolio_run_state(root_dir: Path, run_id: str) -> Path | None:
    direct = root_dir / "artifacts" / "portfolio_runs" / run_id / "portfolio_run_state.json"
    if direct.exists():
        return direct
    matches = list((root_dir / "artifacts" / "portfolio_runs").glob(f"*{run_id}*/portfolio_run_state.json"))
    return matches[0] if matches else None


def _print_portfolio_run_status(console: Any, state_path: Path | None) -> None:
    if state_path is None:
        console.print("[yellow]尚未 /load 任何 portfolio run。[/yellow]")
        return
    if not state_path.exists():
        console.print(f"[red]portfolio_run_state 不存在：{state_path}[/red]")
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    summary = {
        "portfolio_run_id": state.get("portfolio_run_id"),
        "status": state.get("status"),
        "updated_at": state.get("updated_at"),
        "data_status": state.get("data", {}).get("status"),
        "current_version": state.get("current_version"),
        "best_version": state.get("best_version"),
        "version_count": len(state.get("versions", [])),
        "final_selection": state.get("final_selection", {}).get("status"),
        "final_version": state.get("final_selection", {}).get("version_id"),
        "final_manifest_path": state.get("final_selection", {}).get("final_manifest_path"),
    }
    console.print_json(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def _print_portfolio_memory(console: Any, state_path: Path | None) -> None:
    from rich.markdown import Markdown
    from rich.panel import Panel

    if state_path is None:
        console.print("[yellow]尚未 /load 任何 portfolio run。[/yellow]")
        return
    memory_path = state_path.parent / "reports" / "portfolio_agent_memory.md"
    if not memory_path.exists():
        console.print(f"[yellow]尚未生成 memory 文件：{memory_path}[/yellow]")
        return
    text = memory_path.read_text(encoding="utf-8")
    console.print(Panel(Markdown(text), title=str(memory_path), border_style="cyan"))
