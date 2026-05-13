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
from strategy_lab.services.budget_session import BudgetSessionManager


class BudgetAgentResult(BaseModel):
    request: str
    messages: list[Any] = Field(default_factory=list)
    raw_result: dict[str, Any] = Field(default_factory=dict)
    trace_path: Path | None = None


class BudgetAgent:
    """预算层主控智能体。

    BudgetAgent 负责从信号层最终产物构建资产池，生成预算层策略，调用预算层评估链路，
    再调用 BudgetCriticAgent 做单策略复盘或多策略横向比较，最终选择预算层配置。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self._ensure_windows_utf8_env()

    def run(
        self,
        request: str,
        trace_path: str | Path | None = None,
        max_iterations: int | None = None,
    ) -> BudgetAgentResult:
        agent = self.create_agent()
        result = agent.invoke(
            {"messages": [{"role": "user", "content": request}]},
            config=self._build_run_config(max_iterations),
        )
        result_dict = self._coerce_result(result)
        actual_trace_path = self._write_trace(trace_path=trace_path, request=request, result=result_dict)
        return BudgetAgentResult(
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
        budget_cfg = agent_cfg.get("agents", {}).get("budget_agent", {})
        model = self._create_model(
            llm_cfg=llm_cfg,
            budget_cfg=budget_cfg,
            context_windows=agent_cfg.get("agents", {}).get("context_windows", {}),
        )
        backend = WindowsSafeLocalShellBackend(
            root_dir=str(self.config.root_dir),
            virtual_mode=True,
            timeout=int(budget_cfg.get("tool_timeout_seconds") or 604800),
            max_output_bytes=int(budget_cfg.get("max_output_bytes") or 40000),
            inherit_env=True,
            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )

        tools = []
        if streaming_tools:
            from strategy_lab.tools.agent_call_tools import call_budget_critic_agent, call_data_agent

            tools = [call_data_agent, call_budget_critic_agent]

        return create_deep_agent(
            name="budget_agent",
            model=model,
            tools=tools,
            system_prompt=self._build_system_prompt(),
            backend=backend,
            skills=[
                "/src/strategy_lab/skills/common",
                "/src/strategy_lab/skills/budget_agent",
            ],
            checkpointer=checkpointer,
        )

    def _create_model(
        self,
        *,
        llm_cfg: dict[str, Any],
        budget_cfg: dict[str, Any],
        context_windows: dict[str, Any] | None = None,
    ):
        provider = str(
            budget_cfg.get("provider")
            or os.getenv("BUDGET_AGENT_PROVIDER")
            or llm_cfg.get("provider")
            or os.getenv("DEEPSEEK_PROVIDER")
            or "deepseek"
        ).lower()
        api_key = (
            budget_cfg.get("api_key")
            or os.getenv("BUDGET_AGENT_API_KEY")
            or llm_cfg.get("api_key")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        base_url = (
            budget_cfg.get("base_url")
            or os.getenv("BUDGET_AGENT_BASE_URL")
            or llm_cfg.get("base_url")
            or os.getenv("DEEPSEEK_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
        )
        model_name = (
            budget_cfg.get("model")
            or os.getenv("BUDGET_AGENT_MODEL")
            or llm_cfg.get("model")
            or os.getenv("DEEPSEEK_MODEL")
            or "deepseek-v4-pro"
        )
        thinking = str(budget_cfg.get("thinking") or llm_cfg.get("thinking") or "enabled")
        reasoning_effort = budget_cfg.get("reasoning_effort") or llm_cfg.get("reasoning_effort")
        max_completion_tokens = self._optional_int(budget_cfg.get("max_completion_tokens"))
        if not api_key or not base_url:
            raise RuntimeError("缺少 BudgetAgent 的大模型 API Key 或 Base URL。")

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
                override=budget_cfg.get("context", {}),
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
            override=budget_cfg.get("context", {}),
        )

    def _build_system_prompt(self) -> str:
        return f"""你是 stock_strategy_lab 项目的 BudgetAgent，负责对一组已经完成信号层训练的资产进行预算层训练，目标是生成可回测、可复盘、可迭代优化的组合预算策略。

项目根目录：{self.config.root_dir}

## 核心职责
你预算层训练流程的主控智能体。收到用户任务后，你要严格按工作流推进：创建 budget run，汇总信号层产物，生成资产池数据面板，切分数据，生成预算层画像，编写预算策略，按需执行单策略评估或多策略批量评估，调用预算层复盘智能体做单策略复盘或多策略横向比较，迭代优化，最后给出候选或最终预算策略。

预算层策略统一定义为：
BudgetPolicy = UniverseGate 候选资产准入 + AssetScorer 资产打分 + AllocationEngine 预算分配 + RiskOverlay 风险覆盖 + RebalanceScheduler 调仓节奏 + ConstraintProjector 约束投影 + Diagnostics 诊断输出。

预算层默认独立于信号层。信号层产物用于确定资产池来源、追溯最终策略和后续组合层融合参考；不要把信号层当前信号、信号层目标仓位或信号层策略质量作为预算层标准打分因子。信号层和预算层的日常仓位融合由 PortfolioAgent/组合层处理。

## 可用技能
1. budget-run：创建或读取 budget_run_state.json。它会扫描用户提供的信号层 run 目录，复制每个资产的最终策略四件套、signal_agent_memory.md、原始数据引用和必要小文件到预算层 run 目录。任务开始时优先使用。
2. budget-data-panel：把各资产独立行情数据汇总成预算层使用的 panel_ohlcv.parquet、returns_wide.parquet 和 data_panel_manifest.json。
3. budget-data-split：对预算层面板数据生成 train、validation、walk-forward 切分。
4. budget-profile：补充资产元数据，生成资产池画像、相关性摘要、风险收益摘要、走势阶段和图表。
5. budget-policy-authoring：预算策略编写操作指南。写 budget_policy_config.json、budget_policy_spec.md、param_space.json、budget_policy_meta.json 前必须阅读。
6. budget-policy-evaluation：一键评估单个或多个预算策略，内部完成参数搜索、full/train/validation/walk-forward 回测、attempt_summary、stage_attribution、图表和 budget_run_state.json 登记。
7. budget-final-selection：预算层最终选择登记 skill。达到停止条件后，用它登记最终 search_id，生成最终选择报告，并统一更新 budget_run_state.json。
8. image-review：图片理解 skill。当用户要求看图，或数据文件无法表达图像中的视觉信息时使用。本 skill 会调用其他多模态大模型完成图片识别任务，如果你本身就是多模态大模型的话也可以使用 read_file tool 自己识别图片，但如果你不是多模态大模型的话只能使用image-review  skill。

## 工具
### call_data_agent
用途：只在预算层数据缺失、用户要求补充更长时间范围、或需要额外行情/元数据时调用。正常情况下预算层数据来自 budget-run 收集到的信号层数据文件，然后由 budget-data-panel 汇总，不需要先调用 DataAgent。
调用参数：
- request：完整自然语言数据任务。必须写清资产代码列表、日期范围、频率、字段、保存位置和用途。
- run_state_path：可传 budget_run_state.json 路径。虽然参数名叫 run_state_path，但在预算层场景下传 budget_run_state.json 即可。
- trace_path：可选 JSONL 日志路径。
调用示例：
call_data_agent(
  request="请为预算层任务补齐 512880.SH、512800.SH、159995.SZ 在 2014-01-01 至 2024-12-31 的日线 OHLCV 数据，字段至少包含 date、symbol、open、high、low、close、volume、pctchange。请保存到 budget_run_state.json 的 directories.data 对应目录，文件格式为 parquet，并说明每个资产的数据起止日期、行数和缺失情况。",
  run_state_path="artifacts/budget_runs/budget_xxx/budget_run_state.json",
  trace_path="artifacts/budget_agent_workspace/logs/dataagent_budget_xxx.jsonl"
)
返回 JSON 字符串，字段含义：
- status：子代理执行状态。success 表示拿到了最终回答；partial 表示没有拿到清晰最终回答，需要查看 trace。
- agent：固定为 DataAgent。
- trace_path：DataAgent 的 JSONL 执行日志路径，用于排查它读了什么、调用了什么工具、输出了什么。
- final_message：DataAgent 的最终中文总结，通常包含生成的数据文件路径、manifest 路径、数据来源、日期范围、行数、字段、质量检查和失败原因。
调用后必须重新读取 budget_run_state.json 或目标目录，不要只相信 final_message。
这是同步长耗时工具，可能运行数分钟以上。

### call_budget_critic_agent
用途：调用 BudgetCriticAgent 执行预算层复盘。
适用场景：
- 单策略复盘：给定 budget_run_state.json 和 search_id，分析单个预算策略结果，并生成 next_action。
- 多策略横向比较：给定 batch_id 或多个 search_id，比较多条预算策略路线，给出保留、放弃、回退或继续优化建议。
调用参数：
- request：完整自然语言复盘任务，必须说明 budget_run_state.json、search_id/batch_id/search_ids、复盘重点。
- budget_run_state_path：预算层状态文件路径，工作流中必须传入。
- trace_path：可选 JSONL 日志路径。
调用示例一，单策略复盘：
call_budget_critic_agent(
  request="请对 budget_run_state.json 下 search_id=budget_policy_003_defensive_low_vol 做单策略复盘。当前处于预算层阶段 2：策略族深挖与结构重构。重点分析 full/train/validation/walk-forward、阶段归因、换手、最大回撤、资产集中度、策略复杂度、策略族是否匹配画像，并生成 budget_critic_review.md、budget_critic_review.json、budget_next_action.json，最后登记到 budget_run_state.json。next_action 中必须给出 recommended_stage_transition。",
  budget_run_state_path="artifacts/budget_runs/budget_xxx/budget_run_state.json",
  trace_path="artifacts/budget_agent_workspace/logs/budgetcritic_single_xxx.jsonl"
)
调用示例二，多策略横向比较：
call_budget_critic_agent(
  request="请比较 batch_id=budget_batch_001 下所有已完成评估的预算策略。当前处于预算层阶段 1：策略族广泛探索。重点比较 Sharpe、收益、最大回撤、walk-forward 稳定性、阶段归因、换手、策略复杂度、策略族与画像匹配度和用户偏好冲突，输出 selection_advice，并把比较报告登记到 budget_run_state.json 的 artifacts.run_reports。selection_advice 中必须给出 primary_family、fallback_family、discarded_families 和 recommended_stage_transition。",
  budget_run_state_path="artifacts/budget_runs/budget_xxx/budget_run_state.json",
  trace_path="artifacts/budget_agent_workspace/logs/budgetcritic_comparison_xxx.jsonl"
)
返回 JSON 字符串，字段含义：
- status：子代理执行状态。success 表示拿到了最终回答；partial 表示没有拿到清晰最终回答，需要查看 trace。
- agent：固定为 BudgetCriticAgent。
- trace_path：BudgetCriticAgent 的 JSONL 执行日志路径。
- final_message：BudgetCriticAgent 的最终中文总结，通常包含复盘/比较报告路径、核心结论、next_action 或 selection_advice、检查结果和失败原因。
调用后必须重新读取 BudgetCriticAgent 生成的报告和 budget_run_state.json。
这是同步长耗时工具，可能运行数分钟到十几分钟以上。

## 操作流程（严格执行！）
第一步：创建预算层 run
- 用户通常会提供一个或多个信号层结果目录，例如 artifacts/signal_runs 或某些筛选后的目录。
- 使用 budget-run skill 创建 budget_run_state.json。
- 如果用户指定训练数据范围，传入 start_date/end_date；如果没指定，由服务根据信号层数据自动取交集。
- 完成后读取 budget_run_state.json，确认 universe、signal_artifacts、directories、backtest_config 是否完整。

第二步：生成预算层数据面板
- 使用 budget-data-panel skill。
- 目标是生成统一的 panel_ohlcv.parquet、returns_wide.parquet 和 data_panel_manifest.json。
- 如果发现某些资产数据缺失，先根据 budget-run 返回的 missing_data 信息判断是否需要调用 call_data_agent 补齐。

第三步：数据切分
- 使用 budget-data-split skill，生成 train、validation 和 walk-forward 数据切分。
- 后续评估必须基于这些切分做样本内、样本外和滚动验证。

第四步：预算层画像
- 使用 budget-profile skill。
- 重点阅读 budget_profile.md、budget_profile.json、correlation_summary.csv、图表和 metadata_quality。
- 画像用于判断资产池特征、用户偏好冲突、预算策略族、分组约束、风险覆盖和调仓节奏。

第五步：进入预算策略探索循环
- 阅读 budget-policy-authoring skill 和 budget_policy_library.md。
- 你的任务不是走完一轮就停止，而是像信号层一样按“轮次”自主探索、评估、复盘、重构、回退、继续优化，直到达到停止条件或选出当前证据下最优预算策略。
- 每一轮都是一个完整闭环，必须包含：
  1. 确定当前探索阶段和本轮目标。
  2. 编写或改写预算策略四件套。
  3. 调用 budget-policy-evaluation 执行单策略评估或多策略批量评估。
  4. 调用 BudgetCriticAgent 执行单策略复盘或多策略横向比较。
  5. 决定推进、停留、回退、放弃、切换路线或最终选择。
  6. 更新 budget_agent_memory.md。
- 四个阶段如下：
  阶段 1：策略族广泛探索。根据预算画像和用户偏好选择 1 到 3 个策略族，生成 3 到 5 个真正不同的预算策略目录，判断资产池更适合哪类预算路线。
  阶段 2：策略族深挖与结构重构。围绕阶段 1 胜出的 primary family 和最多 1 个 fallback family 做结构变体，允许替换 UniverseGate、AssetScorer、AllocationEngine、RiskOverlay、RebalanceScheduler、ConstraintProjector。
  阶段 3：风险结构与约束增强。在已证明有效的结构上强化 gross_exposure、max_asset_weight、max_holding_count、turnover_cap、budget_smoothing、rebalance_days、explicit groups、vol_target、drawdown_filter 等风险和交易约束。
  阶段 4：稳健性精修与最终选择。缩小参数空间、固定不敏感参数、降低复杂度，重点看 validation、walk-forward、阶段归因、回撤、换手和参数是否卡边界。
- 阶段不是单向流程。BudgetCriticAgent 如果建议 rollback_previous_stage 或 return_to_family_exploration，你必须回退；如果某个策略族样本外失败，要放弃该路线，不要硬调参数。
- 每个策略目录必须包含 budget_policy_config.json、budget_policy_spec.md、param_space.json、budget_policy_meta.json。
- 可选策略族包括 momentum_rotation、risk_adjusted_rotation、defensive_low_vol、drawdown_control、correlation_aware、risk_parity_like、low_turnover_balanced、concentration_alpha、vol_target_rotation。
- 如果画像与用户偏好冲突，例如用户要高收益满仓但资产池高相关高回撤，必须生成不同取舍的候选策略，或在最终回复中说明冲突并询问用户偏好。不要假装一个策略能同时满足所有矛盾目标。

## 每轮内部执行规则
- 写策略：使用 budget-policy-authoring。新策略或改写策略都必须写完整四件套，并写清 current stage、本轮修改理由、预期解决的问题。
- 评估策略：使用 budget-policy-evaluation。它支持两个入口：单策略评估 `budget evaluate-policy`，以及多策略批量评估 `budget batch-evaluate-policies`。
- 选择评估入口：
  1. 当本轮生成多个候选策略，尤其是阶段 1 策略族广泛探索，或阶段 2 生成多个结构变体时，使用多策略批量评估。批量评估只是同时执行多个完整单策略评估，用于提高效率和横向筛选，不代表每轮都必须批量。
  2. 当本轮只改写一个保留策略，尤其是阶段 2/3/4 的结构重构、风险约束增强、稳健性精修时，使用单策略评估。
  3. 如果本轮又生成多个明显不同的变体，可以再次使用批量评估。
- 参数搜索建议：
  1. 阶段 1 多策略粗筛建议使用 random，max-candidates 取 8 到 16，且提高 batch-workers（大于等于4）和 max-workers（大于等于5）。不要第一轮就用 GA 做大量搜索。
  2. 阶段 2 可以对多个结构变体用 random/小规模 GA 批量评估，也可以对单个保留策略用单策略评估。
  3. 阶段 3/4 通常对少数保留路线用单策略 GA 精调。
- 复盘策略：call_budget_critic_agent 支持两种复盘场景：单策略复盘和多策略横向比较。
- 选择复盘入口：
  1. 如果刚完成的是单策略评估，调用单策略复盘：传 budget_run_state.json + search_id，要求输出 budget_next_action.json，并给出 recommended_stage_transition。
  2. 如果刚完成的是批量评估，或者需要比较多个历史 search_id，才调用多策略横向比较：传 batch_id 或 search_ids，要求输出 selection_advice，并给出 primary_family、fallback_family、discarded_families 和 recommended_stage_transition。
  3. 不要把“复盘”默认理解成横向比较。只有存在多个策略需要比较时才横向复盘；单策略优化链路中应使用单策略复盘。
- 决策：基于 BudgetCriticAgent 的 next_action 或 selection_advice，决定下一轮动作。可以继续当前策略、改写其他历史版本、切换 fallback、回退到更早版本、回到阶段 1，或进入最终选择。
- 运行时间：评估阶段很慢，可能持续十几分钟到几十分钟。调用后等待结果，不要重复启动相同 search 或 batch。
- 不要因为完成一次 batch evaluation、一次单策略评估或一次复盘就停止。除非满足停止条件，否则要继续下一轮。

第六步：最终候选选择
- 达到停止条件后，可以考虑对比较好的候选策略做一次横向比较（如需）。
- 最终选择由你完成，BudgetCriticAgent 只提供证据和建议。
- 使用 budget-final-selection skill 调用 `python -m strategy_lab.cli budget final-select`，把最终选择的 search_id 登记为预算层最终候选。
- final-select 会自动生成最终选择报告，并统一更新 budget_run_state.json 的 final_selection、strategy_search.best_attempt_id、strategy_search.best_search_id、strategy_search.best_score、artifacts.policies.final、artifacts.run_reports 和 events。
- 常用命令示例：
  python -m strategy_lab.cli budget final-select artifacts/budget_runs/budget_xxx/budget_run_state.json budget_policy_003_defensive_low_vol --reason "该策略在 validation 和 walk-forward 中更稳定，最大回撤可控，阶段归因显示弱势阶段保护更好，因此选为最终预算层候选。"

第七步：最终回复用户
完成任务后，用中文简明说明：budget_run_state.json 路径、已生成的策略目录、评估 batch/search_id、关键指标、复盘/比较报告路径、当前推荐策略和下一步建议。
如果执行中发现资产池画像与用户偏好存在冲突，最终回复必须说明冲突原因、当前采用的处理方式、代价，并在需要时询问用户更偏好收益、回撤、换手还是集中度。

## 记忆文件
每完成一轮，都要维护预算层记忆文件：`artifacts/budget_runs/{{run_id}}/reports/budget_agent_memory.md`。
该文件用于记录你的长期执行过程，避免多轮训练时重复走弯路。每次更新只写简要要点，至少包括：
- 当前轮次或 batch/search_id。
- 当前探索阶段：stage_1_family_exploration / stage_2_family_deepening / stage_3_risk_constraint_enhancement / stage_4_robustness_finalization。
- 本轮生成或评估的预算策略名称、策略结构摘要和关键参数。
- 核心指标摘要：Sharpe、收益、最大回撤、validation/walk-forward 表现、换手和阶段归因要点。
- BudgetCriticAgent 的关键结论。
- 保留、放弃、阶段推进、阶段回退或继续优化的理由。
- 下一步计划。

## 停止条件（满足之一即可）
- 达到 budget_run_state.json 中定义的策略搜索轮数，或用户指定的轮数。（如果没有找到这个数字，就用20）
- 连续多轮 validation/walk-forward 没有改善。
- 多策略比较显示策略复杂度增加但收益风险没有改善。
- BudgetCriticAgent 明确建议 ready_for_final_selection 或停止。
- 用户要求暂停或停止。

## 路径规则
- ls、read_file、write_file、edit_file、grep、glob 工具使用以 / 开头的路径，其中 / 代表项目根目录 `{self.config.root_dir}`。
  例：`/artifacts/budget_runs/budget_xxx/budget_run_state.json`
  例：`/artifacts/budget_runs/budget_xxx/policies/generated/balanced_score/budget_policy_config.json`
  例：`/artifacts/budget_runs/budget_xxx/reports/budget_agent_memory.md`
- execute 运行命令时，工作目录已自动设为项目根目录，使用不带 / 前缀的相对路径即可。
  转换规则：文件工具路径去掉开头的 / 就是 execute 的相对路径。
  例：`/artifacts/budget_runs/budget_xxx/budget_run_state.json` → `artifacts/budget_runs/budget_xxx/budget_run_state.json`
  例：`/artifacts/budget_runs/budget_xxx/policies/generated/balanced_score/budget_policy_config.json` → `artifacts/budget_runs/budget_xxx/policies/generated/balanced_score/budget_policy_config.json`
- execute 中也可以直接使用 Windows 绝对路径。
- 当前运行环境是 Windows PowerShell。不要使用 Linux/macOS shell 命令，例如 `mkdir -p`、`touch`、`cat`、`rm -rf`、`cp -r`、`grep`、`sed`、`awk`、`chmod`。
- 文件和目录操作优先使用 DeepAgents 文件工具（ls、read_file、write_file、edit_file、glob）。不要使用内置 grep 工具；当前 Windows 环境下 DeepAgents 的 grep 偶发兼容问题，可能导致任务在最终检查阶段中断。需要搜索文件内容时，优先用 glob/ls 定位文件后 read_file 阅读；如果必须批量搜索，使用 execute 运行一个简短 Python 脚本读取文本并输出匹配摘要。
- 使用 execute 时，尽量避免依赖 PowerShell 中文错误输出；命令要简短、路径加引号、参数少换行。复杂命令优先写成 `.py` 脚本再执行，减少 Windows 编码和引号问题。

## 工具使用效率
当需要读取多个互不依赖的文件、检查多个目录、或搜索多个互不依赖的关键词时，应在同一轮中一次性发起多个系统文件工具调用，例如同时调用多个 read_file、ls 或 glob。不要使用内置 grep。

"""

    def _build_run_config(self, max_iterations: int | None) -> dict[str, Any]:
        if max_iterations is None:
            return {}
        return {"recursion_limit": int(max_iterations)}

    def _write_trace(self, trace_path: str | Path | None, request: str, result: dict[str, Any]) -> Path | None:
        if not trace_path:
            return None
        path = Path(trace_path)
        if not path.is_absolute():
            path = self.config.root_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": datetime.now().isoformat(),
            "request": request,
            "result": result,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def _coerce_result(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json")
        return {"value": result}

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


def run_budget_chat_loop(
    max_iterations: int | None = None,
    *,
    thread_id: str | None = None,
    resume_latest: bool = False,
    persist: bool = True,
) -> None:
    """启动 BudgetAgent 交互式终端。"""

    from langgraph.checkpoint.memory import InMemorySaver
    from rich.console import Console
    from rich.panel import Panel

    from strategy_lab.agents.signal_agent import stream_agent_turn

    console = Console()
    agent_runner = BudgetAgent()
    session_manager = BudgetSessionManager(agent_runner.config)
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
        thread_id = thread_id or f"budget-chat-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        current_run_state_path = None

    agent = agent_runner.create_agent(checkpointer=checkpointer)
    console.print(
        Panel(
            "BudgetAgent 交互模式\n"
            f"thread_id: {thread_id}\n"
            "/help 查看命令，/exit 退出，/new 开新会话，/sessions 查看会话，"
            "/resume THREAD_ID 恢复会话，/runs 查看预算层 runs，/load RUN_ID 加载任务。",
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
                    "/reset 开新持久化会话（/new 的别名）\n"
                    "/sessions 列出历史对话会话\n"
                    "/resume THREAD_ID 恢复某个历史会话\n"
                    "/clear 清屏\n"
                    "/runs 列出最近 budget runs\n"
                    "/load RUN_ID 加载某个 budget_run_state.json\n"
                    "/status 查看当前 budget_run_state 摘要\n"
                    "/memory 查看当前 budget_agent_memory.md\n"
                    "/pause 当前实现为轮次边界暂停；正在执行的工具不会被强行中断\n"
                    "/help 查看命令"
                )
                continue
            if user_input == "/clear":
                os.system("cls" if os.name == "nt" else "clear")
                continue
            if user_input in {"/reset", "/new"}:
                session = session_manager.create_session() if persist else None
                thread_id = session.thread_id if session else f"budget-chat-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                current_run_state_path = None
                console.print(f"[dim]new thread: {thread_id}[/dim]")
                continue
            if user_input == "/sessions":
                _print_budget_chat_sessions(console, session_manager)
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
                    console.print(f"[dim]当前 budget_run_state：{current_run_state_path}[/dim]")
                continue
            if user_input == "/runs":
                _print_recent_budget_runs(console, agent_runner.config.root_dir)
                continue
            if user_input.startswith("/load "):
                run_id = user_input.split(maxsplit=1)[1].strip()
                current_run_state_path = _find_budget_run_state(agent_runner.config.root_dir, run_id)
                if current_run_state_path:
                    if persist:
                        session_manager.update_session(thread_id, current_run_state_path=current_run_state_path)
                    console.print(f"[green]已加载 budget_run_state：{current_run_state_path}[/green]")
                else:
                    console.print(f"[red]未找到 budget_run_id：{run_id}[/red]")
                continue
            if user_input == "/status":
                _print_budget_run_status(console, current_run_state_path)
                continue
            if user_input == "/memory":
                _print_budget_run_memory(console, current_run_state_path)
                continue
            if user_input == "/pause":
                console.print("[dim]当前没有正在执行的轮次。长任务运行时可按 Ctrl+C 尝试中断；更稳妥的是等当前工具返回后暂停。[/dim]")
                continue

            try:
                actual_input = user_input
                if current_run_state_path is not None:
                    actual_input = f"{user_input}\n\n当前已加载的 budget_run_state.json：{current_run_state_path}"
                final_state = stream_agent_turn(
                    agent,
                    actual_input,
                    label="BudgetAgent",
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
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]BudgetAgent error: {type(exc).__name__}: {exc}[/red]")
                print("", file=sys.stderr)
    finally:
        if checkpoint_conn is not None:
            checkpoint_conn.close()


def _create_sqlite_checkpointer(session_manager: BudgetSessionManager) -> tuple[Any, sqlite3.Connection]:
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:
        raise RuntimeError("缺少持久化会话依赖。请安装：python -m pip install langgraph-checkpoint-sqlite") from exc
    session_manager.ensure_dirs()
    conn = sqlite3.connect(session_manager.checkpoint_path, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver, conn


def _derive_session_title(user_input: str, limit: int = 50) -> str:
    title = " ".join(user_input.strip().split())
    if not title:
        return "空会话"
    return title if len(title) <= limit else title[: limit - 3] + "..."


def _print_budget_chat_sessions(console: Any, session_manager: BudgetSessionManager, limit: int = 20) -> None:
    from rich.table import Table

    table = Table(title="BudgetAgent chat sessions", expand=True)
    table.add_column("thread_id", no_wrap=True, overflow="ignore", min_width=32)
    table.add_column("updated")
    table.add_column("messages", justify="right")
    table.add_column("budget_run_state")
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


def _print_recent_budget_runs(console: Any, root_dir: Path, limit: int = 12) -> None:
    from rich.table import Table

    runs_dir = root_dir / "artifacts" / "budget_runs"
    table = Table(title="recent budget runs")
    table.add_column("budget_run_id")
    table.add_column("updated")
    table.add_column("status")
    table.add_column("assets", justify="right")
    table.add_column("searches", justify="right")
    if not runs_dir.exists():
        console.print("[dim]暂无 artifacts/budget_runs 目录。[/dim]")
        return
    items = []
    for state_path in runs_dir.glob("*/budget_run_state.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        items.append((state_path.stat().st_mtime, state_path, state))
    for _, state_path, state in sorted(items, reverse=True)[:limit]:
        table.add_row(
            str(state.get("budget_run_id") or state_path.parent.name),
            str(state.get("updated_at") or ""),
            str(state.get("status") or ""),
            str(len(state.get("asset_pool", {}).get("symbols") or [])),
            str(len(state.get("attempts") or [])),
        )
    console.print(table)


def _find_budget_run_state(root_dir: Path, run_id: str) -> Path | None:
    direct = root_dir / "artifacts" / "budget_runs" / run_id / "budget_run_state.json"
    if direct.exists():
        return direct
    matches = list((root_dir / "artifacts" / "budget_runs").glob(f"*{run_id}*/budget_run_state.json"))
    return matches[0] if matches else None


def _print_budget_run_status(console: Any, run_state_path: Path | None) -> None:
    if run_state_path is None:
        console.print("[yellow]尚未 /load 任何 budget run。[/yellow]")
        return
    if not run_state_path.exists():
        console.print(f"[red]budget_run_state 不存在：{run_state_path}[/red]")
        return
    state = json.loads(run_state_path.read_text(encoding="utf-8"))
    summary = {
        "budget_run_id": state.get("budget_run_id"),
        "status": state.get("status"),
        "updated_at": state.get("updated_at"),
        "asset_pool": state.get("asset_pool", {}).get("status"),
        "asset_count": len(state.get("asset_pool", {}).get("symbols") or []),
        "signal_artifacts": state.get("signal_artifacts", {}).get("status"),
        "data_panel": state.get("data_panel", {}).get("status"),
        "data_split": state.get("data_split", {}).get("status"),
        "budget_profile": state.get("budget_profile", {}).get("status"),
        "strategy_search": {
            "status": state.get("strategy_search", {}).get("status"),
            "current_iteration": state.get("strategy_search", {}).get("current_iteration"),
            "max_iterations": state.get("strategy_search", {}).get("max_iterations"),
            "attempt_count": len(state.get("attempts") or []),
            "best_attempt_id": state.get("strategy_search", {}).get("best_attempt_id"),
        },
        "final_selection": state.get("final_selection", {}).get("status"),
        "run_reports": list((state.get("artifacts", {}).get("run_reports") or {}).keys()),
    }
    console.print_json(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def _print_budget_run_memory(console: Any, run_state_path: Path | None) -> None:
    from rich.markdown import Markdown
    from rich.panel import Panel

    if run_state_path is None:
        console.print("[yellow]尚未 /load 任何 budget run。[/yellow]")
        return
    memory_path = run_state_path.parent / "reports" / "budget_agent_memory.md"
    if not memory_path.exists():
        console.print(f"[yellow]尚未生成 memory 文件：{memory_path}[/yellow]")
        return
    text = memory_path.read_text(encoding="utf-8")
    console.print(Panel(Markdown(text), title=str(memory_path), border_style="cyan"))
