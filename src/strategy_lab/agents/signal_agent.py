from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from strategy_lab.agents.backend import WindowsSafeLocalShellBackend
from strategy_lab.agents.model_factory import ReasoningContentChatOpenAI, apply_context_profile
from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.config.loader import load_config_file
from strategy_lab.services.signal_session import SignalSessionManager


class SignalAgentResult(BaseModel):
    """SignalAgent 的运行结果。"""

    request: str
    messages: list[Any] = Field(default_factory=list)
    raw_result: dict[str, Any] = Field(default_factory=dict)
    trace_path: Path | None = None


class SignalAgent:
    """信号层主控智能体。

    SignalAgent 负责创建/读取 signal run，调用 DataAgent 获取数据，生成策略，
    调用评估链路和 CriticAgent，最终选择策略。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self._ensure_windows_utf8_env()

    def run(
        self,
        request: str,
        trace_path: str | Path | None = None,
        max_iterations: int | None = None,
    ) -> SignalAgentResult:
        agent = self.create_agent()
        messages = [{"role": "user", "content": request}]
        config = self._build_run_config(max_iterations)
        result = agent.invoke({"messages": messages}, config=config)
        result_dict = self._coerce_result(result)
        actual_trace_path = self._write_trace(
            trace_path=trace_path,
            request=request,
            result=result_dict,
        )
        return SignalAgentResult(
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
            raise RuntimeError("缺少 DeepAgents 运行依赖。请先安装：python -m pip install -e .[agents]") from exc

        agent_cfg = load_config_file("agent")
        llm_cfg = agent_cfg.get("agents", {}).get("llm", {})
        signal_cfg = agent_cfg.get("agents", {}).get("strategy_agent", {})
        model = self._create_model(
            llm_cfg=llm_cfg,
            signal_cfg=signal_cfg,
            context_windows=agent_cfg.get("agents", {}).get("context_windows", {}),
        )
        backend = WindowsSafeLocalShellBackend(
            root_dir=str(self.config.root_dir),
            virtual_mode=True,
            timeout=int(signal_cfg.get("tool_timeout_seconds") or 604800),
            max_output_bytes=40000,
            inherit_env=True,
            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        tools = []
        if streaming_tools:
            from strategy_lab.tools.agent_call_tools import call_critic_agent, call_data_agent

            tools = [call_data_agent, call_critic_agent]

        return create_deep_agent(
            name="signal_agent",
            model=model,
            tools=tools,
            system_prompt=self._build_system_prompt(),
            backend=backend,
            skills=[
                "/src/strategy_lab/skills/common",
                "/src/strategy_lab/skills/signal_agent",
            ],
            checkpointer=checkpointer,
        )

    def _create_model(
        self,
        *,
        llm_cfg: dict[str, Any],
        signal_cfg: dict[str, Any],
        context_windows: dict[str, Any] | None = None,
    ):
        provider = str(signal_cfg.get("provider") or llm_cfg.get("provider") or "deepseek").lower()
        api_key = (
            signal_cfg.get("api_key")
            or os.getenv("SIGNAL_AGENT_API_KEY")
            or llm_cfg.get("api_key")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        base_url = (
            signal_cfg.get("base_url")
            or os.getenv("SIGNAL_AGENT_BASE_URL")
            or llm_cfg.get("base_url")
            or os.getenv("DEEPSEEK_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
        )
        model_name = (
            signal_cfg.get("model")
            or os.getenv("SIGNAL_AGENT_MODEL")
            or llm_cfg.get("model")
            or os.getenv("DEEPSEEK_MODEL")
            or "deepseek-v4-pro"
        )
        thinking = str(signal_cfg.get("thinking") or llm_cfg.get("thinking") or "enabled")
        reasoning_effort = signal_cfg.get("reasoning_effort") or llm_cfg.get("reasoning_effort")
        if not api_key or not base_url:
            raise RuntimeError("缺少大模型 API Key 或 Base URL。")

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
                override=signal_cfg.get("context", {}),
            )

        kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": api_key,
            "base_url": base_url,
        }
        if provider in {"moonshot", "kimi"}:
            kwargs["payload_token_param"] = "max_tokens"
        model = ReasoningContentChatOpenAI.create_openai_compatible(**kwargs)
        return apply_context_profile(
            model,
            provider=provider,
            model_name=model_name,
            context_windows=context_windows,
            override=signal_cfg.get("context", {}),
        )

    def _build_system_prompt(self) -> str:
        return f"""你是 stock_strategy_lab 项目的 SignalAgent。你的任务是严格按照工作流程，为单个股票、指数或 ETF 自动探索可复用的最优信号策略。收到任务后应创建状态、获取数据、生成策略、评估、复盘、迭代、选择最终策略。

项目根目录：{self.config.root_dir}

## 总目标
围绕用户给定资产和回测数据的时间范围，生成一个最优的交易策略。你需要不断经过数据切分、市场画像、策略调整、回测、复盘这一完整的流程，不断优化，最终选择并保存最优策略。

## 策略定义
SignalStrategy = RegimeDetector 市场状态识别 + RegimeAlphaPolicy 分状态多周期 Alpha + Filters 辅助过滤器 + ExitPolicy 出场与风控 + PositionMapper 仓位映射 + StateRules 状态与交易纪律。
默认优先采用 RegimeSwitchingAlpha + MultiTimeframeAlpha：先判断 uptrend/range/downtrend/high_vol 等市场状态，再为不同状态使用长/中/短周期组合 Alpha，最后统一输出 target_S。
每个策略必须显式说明 RegimeAlphaMap，例如 uptrend -> alpha_uptrend_xxx、range -> alpha_range_xxx、downtrend -> alpha_downtrend_xxx、high_vol -> alpha_high_vol_xxx。不允许所有 regime 共用同一个 alpha_score 后只调整仓位。Alpha / RegimeAlphaPolicy 在任何阶段都允许根据证据调整或替换，不要把阶段 1 的 Alpha 当成永久冻结项。
策略输出必须是 target_S ∈ [0, 1]，表示“当前资产预算上限内的目标占用比例”。首要关注的指标是 Sharpe，同时也关注最大回撤、样本外表现、walk-forward 稳定性、交易次数和阶段归因等情况。

## 可用 skill
1. signal-run：创建或读取 signal run，生成 run_state.json 和标准目录。任务开始时优先使用。
2. data-split：读取主数据文件，生成 train、validation、walk-forward 数据切分。DataAgent 成功后使用。
3. market-profile：生成市场画像、走势阶段划分、事实描述和图像。数据切分后使用。
4. strategy-authoring：策略编写的操作指南，指导你编写 strategy.py、strategy_spec.md、param_space.json、strategy_meta.json。生成或改写策略前必须阅读。
5. attempt-evaluation：对单个或批量策略进行完整评估，包含策略保存、参数搜索、full/train/validation/walk-forward 回测、attempt_summary、stage_attribution、run_state 更新。策略四件套生成后使用。
6. image-review：图片理解 skill。当用户要求看图，或数据文件无法表达图像中的视觉信息时使用。本 skill 会调用其他多模态大模型完成图片识别任务，如果你本身就是多模态大模型的话也可以使用 read_file tool 自己识别图片，但如果你不是多模态大模型的话只能使用image-review  skill。

## 工具使用效率
当需要读取多个互不依赖的文件、检查多个目录、或搜索多个互不依赖的关键词时，应在同一轮中一次性发起多个系统文件工具调用，例如同时调用多个 read_file、ls、glob 或 grep。只有存在明确依赖关系时才顺序调用，例如必须先读取 run_state.json 才知道后续路径、必须先写文件才能检查该文件。

## 工具：call_data_agent
用途：把任何数据相关任务委托给 DataAgent。DataAgent 可从 MiniQMT、AKShare、本地文件等来源获取或处理数据；会自行查数据、生成数据文件，并在提供 run_state.json 时更新数据相关状态。
适用任务：
- 获取 A 股股票、指数、ETF、可转债、期货等 OHLCV/K线/分钟线/tick/板块/交易日历等数据。
- 按字段、格式、频率和日期范围生成 parquet/csv/json 数据文件。
- 读取或整理已有 CSV/本地文件数据。
调用参数：
- request：完整自然语言任务，必须写清楚资产代码、日期范围、频率、字段、文件格式、数据源要求、保存要求、是否更新 run_state.json。不要只写“帮我获取数据”，要写清楚“最终主数据用于后续 backtest，保存到 run_state.json 的 directories.data，并更新 steps.data_acquisition 和 artifacts.datasets.primary”。
- run_state_path：可选但在 signal run 场景下必须传。工具会把该路径补充给 DataAgent；DataAgent 会据此读取 directories.data、任务资产、数据范围，并把最终数据状态写回 run_state.json。
- trace_path：可选，子代理 JSONL trace 路径。不传时自动写入 `artifacts/signal_agent_workspace/logs/dataagent_时间戳.jsonl`。
调用示例：
call_data_agent(
  request="请使用 MiniQMT 获取 000300.SH 在 2024-01-01 至 2024-12-31 的日线 OHLCV 数据，至少包含 open、high、low、close、volume、pctchange。最终主数据保存为 parquet，并更新 run_state.json 的 data_acquisition 和 artifacts.datasets.primary。",
  run_state_path="artifacts/signal_runs/signal_000300_xxx/run_state.json"
)
返回结果是 JSON 字符串，字段含义：
- status：子代理执行状态。通常是 success；如果 final_message 缺失则可能是 partial。
- agent：固定为 DataAgent。
- trace_path：DataAgent 的 JSONL 执行日志路径。需要追查执行细节时读取该文件。
- final_message：DataAgent 的最终中文总结。里面通常包含最终数据文件路径、dataset_manifest 路径、数据来源、日期范围、行数、字段列表、数据质量检查、失败原因或替代数据源说明。
DataAgent 成功后通常会生成或更新：
- 主数据文件：`run_state.json -> artifacts.datasets.primary.file`，同时也会写入 `steps.data_acquisition.primary_dataset`。这是后续 data-split、market-profile、attempt-evaluation 应优先读取的数据文件。
- 数据清单：`run_state.json -> artifacts.datasets.primary.manifest`，同时也会写入 `steps.data_acquisition.dataset_manifest`。里面记录 source、row_count、columns、start_date、end_date、质量检查和必要说明。
- run_state 字段：`steps.data_acquisition.status`、`data_source`、`primary_dataset`、`dataset_manifest`、`summary`、`error`，以及 `artifacts.datasets.primary`。
DataAgent 返回后，你必须重新读取 run_state.json，不要只依赖 final_message。重点确认：
- `steps.data_acquisition.status` 是 success 或 partial。
- `artifacts.datasets.primary.file` 存在且能读取。
- `artifacts.datasets.primary.manifest` 存在。

## 工具：call_critic_agent
用途：把策略复盘和多 attempt 横向比较委托给 CriticAgent。CriticAgent 会读取 run_state、attempt_summary、metrics、stage_attribution、market_profile、策略四件套等文件，输出复盘报告和下一步建议。
适用任务：
- 单 attempt 复盘：分析一个策略在哪些市场阶段表现好/差，RegimeDetector、RegimeAlphaMap、Filters、ExitPolicy、PositionMapper、StateRules 是否合理，下一轮应如何修改。
- 多 attempt 横向比较：比较多个策略或多个版本，给出保留、放弃、回退或继续优化建议。
- 最终选择前的横向复核。
调用参数：
- request：完整自然语言任务，必须写清楚 run_state.json、attempt_id 或 attempt_ids、任务场景、分析重点。单 attempt 复盘要说清楚目标 attempt；多 attempt 比较要列出 attempt_ids 或要求比较所有 ready_for_review/reviewed attempts。
- run_state_path：可选但在 SignalAgent 工作流中必须传。工具会把该路径补充给 CriticAgent。
- trace_path：可选，子代理 JSONL trace 路径。不传时自动写入 `artifacts/signal_agent_workspace/logs/criticagent_时间戳.jsonl`。
调用示例：
call_critic_agent(
  request="请对 run_state_path=... 下的 attempt_003 做单 attempt 复盘，重点判断该策略是否值得进入下一轮结构增强。"（或"请比较 run_state_path=... 下的 attempt_001、attempt_002、attempt_003。"或"请横向复盘所有 ready_for_review 的 attempts，并给出下一轮应继续哪个策略方向。"）,
  run_state_path="artifacts/signal_runs/signal_000300_xxx/run_state.json"
)
返回结果是 JSON 字符串，字段含义：
- status：子代理执行状态。通常是 success；如果 final_message 缺失则可能是 partial。
- agent：固定为 CriticAgent。
- trace_path：CriticAgent 的 JSONL 执行日志路径。需要追查复盘过程时读取该文件。
- final_message：CriticAgent 的最终中文总结。里面通常包含复盘或比较产物路径、核心结论、下一步建议、检查结果、失败原因或缺失文件说明。
单 attempt 复盘成功后通常会生成：
- `attempts/{{attempt_id}}/review/critic_review.md`：详细复盘报告，包含策略结构、关键指标、市场阶段表现、交易行为、优点、问题和下一步修改建议。
- `attempts/{{attempt_id}}/review/critic_review.json`：结构化复盘结果，适合程序精确读取。
- `attempts/{{attempt_id}}/review/next_action.json`：下一轮动作建议，通常包含 continue/stop/modify/retry/backtrack 等决策倾向，以及建议修改的 RegimeDetector、RegimeAlphaMap、Alpha、Filters、ExitPolicy、PositionMapper、StateRules。
- run_state 字段：对应 attempt 的 `status` 通常更新为 reviewed，并写入 `critic_review_path`、`critic_review_md_path`、`next_action_path`。
多 attempt 横向比较成功后通常会生成：
- run 级 reports 或 comparison 目录下的比较 JSON/Markdown/CSV/图表，具体路径以 CriticAgent final_message 和 run_state.json 的 `artifacts.run_reports` 为准。
- `selection_advice` 或类似内容：用于辅助你决定保留、放弃、回退或继续优化哪个 attempt。但最终选择仍由你完成。
CriticAgent 返回后，你必须重新读取 run_state.json 和相关 review/comparison 文件。重点确认：
- 单 attempt 复盘时，`critic_review.md`、`critic_review.json`、`next_action.json` 都存在且非空。
- 多 attempt 比较时，比较报告存在，且没有覆盖已有 attempts。
- 最终选择前，拟选择的 attempt 必须已经完成单 attempt 复盘。

## 操作流程（严格执行！）
第一步：理解任务并创建/读取 run
- 如果用户没有提供 run_state.json，使用 signal-run skill 创建新 run。
- 需要明确 symbol、asset_type、start_date、end_date、frequency；缺少关键字段时只问最少必要问题。
- 创建后读取 run_state.json，确认 directories、backtest_config、steps、strategy_search.max_iterations。

第二步：获取数据
- 调用 call_data_agent。
- 请求中必须包含 run_state_path，并说明最终主数据应保存到 run_state.json 的 directories.data。
- DataAgent 返回后读取 run_state.json，确认 steps.data_acquisition.status 为 success 或 partial，确认 primary_dataset 和 dataset_manifest 存在。
- 如果数据失败，先根据 final_message 判断是否重试；无法恢复时向用户说明。

第三步：数据切分
- 使用 data-split skill，通常运行 python -m strategy_lab.cli signal split-data --run-state-path RUN_STATE_PATH。
- 完成后检查 split_manifest、train、validation、walk-forward 文件路径。

第四步：市场画像
- 使用 market-profile skill，通常运行 python -m strategy_lab.cli signal market-profile --run-state-path RUN_STATE_PATH。
- 读取 market_profile.json、market_profile.md，必要时查看 market_profile_chart.png 或使用 image-review。
- 市场画像是下一步制定策略的基础，对初始 Alpha 探索方向起到重要作用。

第五步：阶段 1 RegimeAlpha 广泛探索
- 阅读 strategy-authoring skill 和 signal_strategy_library.md。
- 必须先根据 market_profile 自主判断适合探索哪些方向，不要机械固定生成趋势/突破/回归/防御四类。
- 一次最多生成 4 个不同 RegimeAlpha 路线的策略目录，可以少于 4 个；如果多个候选属于同一大类，它们的 RegimeAlphaMap、窗口、入场逻辑、退出逻辑或风控结构必须有实质差异。
- 每个策略必须包含 RegimeDetector、RegimeAlphaPolicy、RegimeAlphaMap、ExitPolicy、PositionMapper 和基础 StateRules；Filters 允许 0-1 个。
- 每个核心 regime 必须显式绑定一个 Alpha，例如 uptrend -> alpha_uptrend_xxx、range -> alpha_range_xxx、downtrend -> alpha_downtrend_xxx、high_vol -> alpha_high_vol_xxx。
- 不允许所有 regime 共用同一个 alpha_score 后只靠 regime 调整仓位。
- 用 attempt-evaluation 批量模式评估这些策略。
- 调用 call_critic_agent 做多 attempt 横向比较，最多保留 1 个 primary_candidate 和 1 个 fallback_candidate。
- 更新 signal_agent_memory.md。

第六步：阶段 2 RegimeAlpha 深度探索与局部重构
- 基于阶段 1 候选和 CriticAgent 建议，继续确认“不同市场状态下到底该用什么 Alpha”。
- 本阶段优先修改 RegimeDetector、某个 regime 的 Alpha、RegimeAlphaMap，或新增小批量对照 RegimeAlphaPolicy；不要过早把重点放到 Filter、ExitPolicy 或 PositionMapper。
- 如果某个 regime 明显失败，应优先替换该 regime 的 Alpha，而不是只调入场阈值。
- 如果整个路线失败，可以回退到历史最佳 attempt，或重新做一批小规模 RegimeAlpha 探索。
- 生成新策略四件套后，用 attempt-evaluation 单 attempt 模式评估。
- 调用 call_critic_agent 做单 attempt 复盘，更新 signal_agent_memory.md。

第七步：阶段 3 结构增强
- 在已经相对有效的 RegimeAlphaPolicy 上增加交易结构，使策略更稳、更可交易。
- 每轮最多改 1-2 个结构模块，例如增加 Filter、替换 ExitPolicy、调整 PositionMapper、增加 cooldown/min_hold_days/max_daily_target_change、加入成交量确认、趋势保护或异常 K 线冷却。
- 如果增强后变差，必须回退，不要继续堆复杂度。
- 即使进入本阶段，如果证据显示 Alpha 不适合，仍然允许调整或替换 Alpha。
- 每轮仍需完整 attempt-evaluation 和必要的 CriticAgent 复盘。

第八步：阶段 4 稳健性精修与最终选择
- 继续执行评估-复盘-修改闭环，重点提升 Sharpe 和 walk-forward 稳定性，控制最大回撤、无效交易和过拟合风险。
- 可以调整阈值、仓位上限、波动率打折、cooldown、min_hold_days、止损/止盈、参数空间边界、RegimeDetector 边界，也可以根据证据替换某个 regime 的 Alpha。
- 达到停止条件后，可以调用 call_critic_agent 对全部回测效果较好的 attempt 做横向比较。
- 最终选择前，必须确认拟选择的 attempt 已完成 CriticAgent 复盘；如果该 attempt 仍是 ready_for_review，或 review 目录缺少 critic_review.md / critic_review.json，先调用 call_critic_agent 做单 attempt 复盘。即使已经达到 max_iterations，也要先补复盘再最终选择。
- 你自己做最终选择，不把最终选择权交给 CriticAgent，但不要手动编辑 run_state.json 的 final_selection。
- 必须使用系统命令统一登记最终选择：`python -m strategy_lab.cli signal final-select RUN_STATE_PATH ATTEMPT_ID --reason "选择理由"`。
- final-select 会统一更新 run_state.json 的顶层 status、steps.final_selection、steps.strategy_search.best_attempt_id、steps.strategy_search.best_score、artifacts.accepted_strategies.final 和 events。

**请注意**：上述4个阶段可以反复，如发现优化策略走偏了可以退回到之前的阶段修正方向重新执行；每个阶段内部也可以不断调整继续优化的版本，如果发现新的版本不如之前的版本可以放弃，退回到更优的版本上重新优化。总之，你的任务是合理尝试、充分探索，尽可能找出最优策略。

第九步：最终回复用户
- 用中文简明说明 run_id、run_state.json、最终策略路径、最佳 attempt、关键指标、复盘/比较报告路径、下一步建议。

## 记忆文件
每轮生成、评估、复盘后，都要维护 artifacts/signal_runs/{{run_id}}/reports/signal_agent_memory.md。记录下 iteration、stage、attempt_id、strategy_name、策略结构、核心指标、Critic 关键结论、保留/放弃原因、下一步。以记录你的执行过程和思路。

## 停止条件（满足之一即可）
- attempt 的数量达到 run_state.json 的 steps.strategy_search.max_iterations（如果文件中没有这个值，就按20算）
- 连续 3 轮 Sharpe 或综合表现没有明显改善。
- walk-forward 稳定性明显恶化。
- CriticAgent 明确建议停止。
- 策略复杂度超过 strategy-authoring skill 约束。
- 用户要求暂停或停止。

## 路径规则
- ls、read_file、write_file、edit_file、grep、glob 工具使用以 / 开头的路径，其中 / 代表项目根目录 `{self.config.root_dir}`。
  例：`/artifacts/signal_runs/x.parquet`
- execute 运行命令时，工作目录已自动设为项目根目录，使用不带 / 前缀的相对路径即可。
  转换规则：文件工具路径去掉开头的 / 就是 execute 的相对路径。
  例：`/artifacts/signal_runs/x.parquet` → `artifacts/signal_runs/x.parquet`
- execute 中也可以直接使用 Windows 绝对路径。
- 当前运行环境是 Windows PowerShell。不要使用 Linux/macOS shell 命令，例如 `mkdir -p`、`touch`、`cat`、`rm -rf`、`cp -r`、`grep`、`sed`、`awk`、`chmod`。
- 文件和目录操作优先使用 DeepAgents 文件工具（ls、read_file、write_file、edit_file、glob、grep）。如果必须使用 execute 做文件操作，优先写一个短 Python 脚本或使用 PowerShell 原生命令。
- 使用 execute 时，尽量避免依赖 PowerShell 中文错误输出；命令要简短、路径加引号、参数少换行。复杂命令优先写成 `.py` 脚本再执行，减少 Windows 编码和引号问题。
"""

    def _resolve_recursion_limit(self, max_iterations: int | None) -> int:
        if max_iterations is None:
            raise ValueError("未显式提供 max_iterations 时不应解析 recursion_limit。")
        return int(max_iterations)

    def _build_run_config(self, max_iterations: int | None) -> dict[str, Any]:
        if max_iterations is None:
            return {}
        return {"recursion_limit": self._resolve_recursion_limit(max_iterations)}

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


def stream_agent_turn(
    agent: Any,
    request: str,
    *,
    label: str = "SignalAgent",
    max_iterations: int | None = None,
    trace_path: str | Path | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """流式运行一轮 DeepAgent，并把新增消息打印到终端。"""

    from rich.console import Console

    console = Console()
    payload = {"messages": [{"role": "user", "content": request}]}
    config: dict[str, Any] = {}
    if max_iterations is not None:
        config["recursion_limit"] = int(max_iterations)
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}

    trace_file = _resolve_trace_path(trace_path)
    seen_count = _existing_message_count(agent, config) if thread_id else 0
    final_state: dict[str, Any] = {}
    stop_event = threading.Event()
    status_thread: threading.Thread | None = None
    try:
        with console.status("[dim]Working (0s, Ctrl+C to interrupt)[/dim]", spinner="dots") as status:
            status_thread = threading.Thread(
                target=_update_working_status,
                args=(stop_event, status),
                daemon=True,
                name=f"{label}-status",
            )
            status_thread.start()
            for chunk in agent.stream(payload, config=config, stream_mode="values"):
                if not isinstance(chunk, dict):
                    continue
                final_state = chunk
                messages = chunk.get("messages", []) or []
                for message in messages[seen_count:]:
                    event = _message_to_event(message)
                    _print_event(console, label, event)
                    _append_trace_event(trace_file, label, event)
                seen_count = len(messages)
    finally:
        stop_event.set()
        if status_thread is not None:
            status_thread.join(timeout=1)
    return final_state


def _update_working_status(stop_event: threading.Event, status: Any) -> None:
    elapsed = 0
    while not stop_event.wait(1):
        elapsed += 1
        status.update(f"[dim]Working ({elapsed}s, Ctrl+C to interrupt)[/dim]")


def _heartbeat(stop_event: threading.Event, console: Any, label: str) -> None:
    elapsed = 0
    while not stop_event.wait(60):
        elapsed += 60
        console.print(f"[dim]  [running] {label} 仍在执行... ({elapsed}s)[/dim]")


def _existing_message_count(agent: Any, config: dict[str, Any]) -> int:
    try:
        state = agent.get_state(config)
        values = getattr(state, "values", None) or {}
        messages = values.get("messages", []) if isinstance(values, dict) else []
        return len(messages or [])
    except Exception:  # noqa: BLE001
        return 0


def last_ai_text(state: dict[str, Any]) -> str:
    messages = state.get("messages", []) if isinstance(state, dict) else []
    for message in reversed(messages):
        kind = getattr(message, "type", None) or (message.get("role") if isinstance(message, dict) else None)
        content = getattr(message, "content", None) or (message.get("content") if isinstance(message, dict) else None)
        if kind in {"ai", "assistant"} and content:
            return str(content)
    return ""


def _resolve_trace_path(trace_path: str | Path | None) -> Path | None:
    if not trace_path:
        return None
    config = load_app_config()
    path = Path(trace_path)
    if not path.is_absolute():
        path = config.root_dir / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_trace_event(trace_file: Path | None, label: str, event: dict[str, Any]) -> None:
    if not trace_file:
        return
    payload = {"timestamp": datetime.now().isoformat(), "label": label, **event}
    with trace_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _message_to_event(message: Any) -> dict[str, Any]:
    kind = getattr(message, "type", None) or (message.get("role") if isinstance(message, dict) else None)
    content = getattr(message, "content", None) if not isinstance(message, dict) else message.get("content")
    additional_kwargs = getattr(message, "additional_kwargs", {}) if not isinstance(message, dict) else message.get("additional_kwargs", {})
    tool_calls = getattr(message, "tool_calls", None) if not isinstance(message, dict) else message.get("tool_calls")
    name = getattr(message, "name", None) if not isinstance(message, dict) else message.get("name")
    tool_call_id = getattr(message, "tool_call_id", None) if not isinstance(message, dict) else message.get("tool_call_id")
    return {
        "type": kind,
        "name": name,
        "tool_call_id": tool_call_id,
        "content": content,
        "reasoning_content": additional_kwargs.get("reasoning_content") if isinstance(additional_kwargs, dict) else None,
        "tool_calls": _coerce_tool_calls(tool_calls),
    }


def _coerce_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    result = []
    for call in tool_calls or []:
        if isinstance(call, dict):
            result.append(call)
        else:
            result.append(
                {
                    "name": getattr(call, "name", None),
                    "args": getattr(call, "args", None),
                    "id": getattr(call, "id", None),
                }
            )
    return result


def _print_event(console: Any, label: str, event: dict[str, Any]) -> None:
    kind = event.get("type")
    if kind in {"human", "user"}:
        return
    if kind in {"ai", "assistant"}:
        reasoning = str(event.get("reasoning_content") or "").strip()
        if reasoning:
            _print_lines(console, f"[{label}] 推理：", reasoning, style="dim", limit=1200)
        content = str(event.get("content") or "").strip()
        if content:
            _print_lines(console, f"[{label}] ", content, style="green", limit=8000)
        for call in event.get("tool_calls") or []:
            name = call.get("name") or call.get("function", {}).get("name") or "tool"
            args = call.get("args") or call.get("function", {}).get("arguments") or {}
            console.print(_safe_console_text(f"[cyan][{label}] -> {name}[/cyan] [dim]{_compact(args, 500)}[/dim]"))
        return
    if kind == "tool":
        name = event.get("name") or "tool"
        content = event.get("content") or ""
        _print_lines(console, f"[{label}] <- {name}: ", str(content), style="dim", limit=1600)


def _print_lines(console: Any, prefix: str, text: str, *, style: str, limit: int) -> None:
    clipped = _clip_text(text, limit)
    lines = clipped.splitlines() or [""]
    for index, line in enumerate(lines):
        actual_prefix = prefix if index == 0 else " " * _visible_len(prefix)
        console.print(_safe_console_text(f"[{style}]{actual_prefix}{line}[/{style}]"))


def _safe_console_text(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _visible_len(text: str) -> int:
    result = 0
    in_tag = False
    for char in text:
        if char == "[":
            in_tag = True
            continue
        if char == "]" and in_tag:
            in_tag = False
            continue
        if not in_tag:
            result += 1
    return result


def _clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _compact(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def run_chat_loop(
    max_iterations: int | None = None,
    *,
    thread_id: str | None = None,
    resume_latest: bool = False,
    persist: bool = True,
) -> None:
    """启动 SignalAgent 交互式终端。"""

    from langgraph.checkpoint.memory import InMemorySaver
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    agent_runner = SignalAgent()
    session_manager = SignalSessionManager(agent_runner.config)
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
        thread_id = thread_id or f"signal-chat-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        current_run_state_path = None
    agent = agent_runner.create_agent(checkpointer=checkpointer)
    console.print(
        Panel(
            "SignalAgent 交互模式\n"
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
                    "/reset 开新持久化会话（/new 的别名）\n"
                    "/sessions 列出历史对话会话\n"
                    "/resume THREAD_ID 恢复某个历史会话\n"
                    "/clear 清屏\n"
                    "/runs 列出最近 signal runs\n"
                    "/load RUN_ID 载入某个 run\n"
                    "/status 查看当前 run_state 摘要\n"
                    "/memory 查看当前 signal_agent_memory.md\n"
                    "/pause 当前实现为轮次边界暂停；正在执行的工具不会被强行中断\n"
                    "/help 查看命令"
                )
                continue
            if user_input == "/clear":
                os.system("cls" if os.name == "nt" else "clear")
                continue
            if user_input in {"/reset", "/new"}:
                session = session_manager.create_session() if persist else None
                thread_id = session.thread_id if session else f"signal-chat-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                current_run_state_path = None
                console.print(f"[dim]new thread: {thread_id}[/dim]")
                continue
            if user_input == "/sessions":
                _print_chat_sessions(console, session_manager)
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
                    console.print(f"[dim]当前 run_state：{current_run_state_path}[/dim]")
                continue
            if user_input == "/runs":
                _print_recent_runs(console, agent_runner.config.root_dir)
                continue
            if user_input.startswith("/load "):
                run_id = user_input.split(maxsplit=1)[1].strip()
                current_run_state_path = _find_run_state(agent_runner.config.root_dir, run_id)
                if current_run_state_path:
                    if persist:
                        session_manager.update_session(thread_id, current_run_state_path=current_run_state_path)
                    console.print(f"[green]已载入 run_state：{current_run_state_path}[/green]")
                else:
                    console.print(f"[red]未找到 run_id：{run_id}[/red]")
                continue
            if user_input == "/status":
                _print_run_status(console, current_run_state_path)
                continue
            if user_input == "/memory":
                _print_run_memory(console, current_run_state_path)
                continue
            if user_input == "/pause":
                console.print("[dim]当前没有正在执行的轮次。长任务运行时可按 Ctrl+C 尝试中断；更稳妥的是等当前工具返回后暂停。[/dim]")
                continue

            try:
                actual_input = user_input
                if current_run_state_path is not None:
                    actual_input = f"{user_input}\n\n当前已加载的 run_state.json：{current_run_state_path}"
                final_state = stream_agent_turn(
                    agent,
                    actual_input,
                    label="SignalAgent",
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
                console.print(f"[red]SignalAgent error: {type(exc).__name__}: {exc}[/red]")
                print("", file=sys.stderr)
    finally:
        if checkpoint_conn is not None:
            checkpoint_conn.close()


def _create_sqlite_checkpointer(session_manager: SignalSessionManager) -> tuple[Any, sqlite3.Connection]:
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


def _derive_session_title(user_input: str, limit: int = 50) -> str:
    title = " ".join(user_input.strip().split())
    if not title:
        return "空会话"
    return title if len(title) <= limit else title[: limit - 3] + "..."


def _print_chat_sessions(console: Any, session_manager: SignalSessionManager, limit: int = 20) -> None:
    from rich.table import Table

    table = Table(title="SignalAgent chat sessions", expand=True)
    table.add_column("thread_id", no_wrap=True, overflow="ignore", min_width=32)
    table.add_column("updated")
    table.add_column("messages", justify="right")
    table.add_column("run_state")
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


def _print_recent_runs(console: Any, root_dir: Path, limit: int = 12) -> None:
    from rich.table import Table

    runs_dir = root_dir / "artifacts" / "signal_runs"
    table = Table(title="recent signal runs")
    table.add_column("run_id")
    table.add_column("updated")
    table.add_column("status")
    if not runs_dir.exists():
        console.print("[dim]暂无 artifacts/signal_runs 目录。[/dim]")
        return
    items = []
    for state_path in runs_dir.glob("*/run_state.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        items.append((state_path.stat().st_mtime, state_path, state))
    for _, state_path, state in sorted(items, reverse=True)[:limit]:
        table.add_row(
            str(state.get("run_id") or state_path.parent.name),
            str(state.get("updated_at") or ""),
            str(state.get("status") or ""),
        )
    console.print(table)


def _find_run_state(root_dir: Path, run_id: str) -> Path | None:
    direct = root_dir / "artifacts" / "signal_runs" / run_id / "run_state.json"
    if direct.exists():
        return direct
    matches = list((root_dir / "artifacts" / "signal_runs").glob(f"*{run_id}*/run_state.json"))
    return matches[0] if matches else None


def _print_run_status(console: Any, run_state_path: Path | None) -> None:
    if run_state_path is None:
        console.print("[yellow]尚未 /load 任何 run。[/yellow]")
        return
    if not run_state_path.exists():
        console.print(f"[red]run_state 不存在：{run_state_path}[/red]")
        return
    state = json.loads(run_state_path.read_text(encoding="utf-8"))
    steps = state.get("steps", {})
    summary = {
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "updated_at": state.get("updated_at"),
        "data_acquisition": steps.get("data_acquisition", {}).get("status"),
        "market_profile": steps.get("market_profile", {}).get("status"),
        "attempt_count": steps.get("strategy_search", {}).get("attempt_count"),
        "best_attempt_id": steps.get("strategy_search", {}).get("best_attempt_id"),
        "final_selection": steps.get("final_selection", {}).get("status"),
    }
    console.print_json(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def _print_run_memory(console: Any, run_state_path: Path | None) -> None:
    from rich.markdown import Markdown
    from rich.panel import Panel

    if run_state_path is None:
        console.print("[yellow]尚未 /load 任何 run。[/yellow]")
        return
    memory_path = run_state_path.parent / "reports" / "signal_agent_memory.md"
    if not memory_path.exists():
        console.print(f"[yellow]尚未生成 memory 文件：{memory_path}[/yellow]")
        return
    text = memory_path.read_text(encoding="utf-8")
    console.print(Panel(Markdown(text), title=str(memory_path), border_style="cyan"))
