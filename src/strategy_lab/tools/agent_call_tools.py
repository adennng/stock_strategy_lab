from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from strategy_lab.agents.budget_critic_agent import BudgetCriticAgent
from strategy_lab.agents.critic_agent import CriticAgent
from strategy_lab.agents.data_agent import DataAgent
from strategy_lab.agents.portfolio_critic_agent import PortfolioCriticAgent
from strategy_lab.agents.signal_agent import last_ai_text, stream_agent_turn
from strategy_lab.config import load_app_config


@tool
def call_data_agent(
    request: str,
    run_state_path: str | None = None,
    trace_path: str | None = None,
) -> str:
    """同步调用 DataAgent 子代理处理数据任务，并在终端实时展示子代理执行过程。

    用途：
    - 获取或整理股票、指数、ETF、期货、可转债等行情数据。
    - 从 MiniQMT、AKShare 或本地文件生成后续策略/回测可用的数据文件。
    - 在 signal run 场景下更新 run_state.json 的 data_acquisition 和 artifacts.datasets。

    参数：
    - request：必填，完整自然语言任务。必须说明资产、日期范围、频率、字段、格式、数据源和保存要求。
      在 signal run 中应明确要求最终主数据用于 backtest，并保存到 run_state.json 的 directories.data。
    - run_state_path：可选但在 SignalAgent 工作流中必须传入，工具会把该路径补充到 request。
    - trace_path：可选，子代理 JSONL 执行日志路径。不传则自动写入 artifacts/signal_agent_workspace/logs。

    返回：
    JSON 字符串，包含：
    - status：success 或 partial。
    - agent：DataAgent。
    - trace_path：DataAgent JSONL 执行日志路径。
    - final_message：最终中文总结，通常包含 dataset_path、dataset_manifest、数据来源、日期范围、
      行数、字段列表、数据质量、run_state 更新说明或失败原因。

    成功后应检查 run_state.json：
    - artifacts.datasets.primary.file：后续 data-split/market-profile/回测优先读取的主数据文件。
    - artifacts.datasets.primary.manifest：数据清单。
    - steps.data_acquisition.status/data_source/primary_dataset/dataset_manifest/summary/error。

    注意：
    这是同步长耗时工具，可能运行数分钟到十几分钟以上。调用后必须等待结果返回，
    不要假设它在后台继续执行。
    """

    return _run_child_agent(
        agent_name="DataAgent",
        request=request,
        run_state_path=run_state_path,
        trace_path=trace_path,
    )


@tool
def call_critic_agent(
    request: str,
    run_state_path: str | None = None,
    trace_path: str | None = None,
) -> str:
    """同步调用 CriticAgent 子代理处理策略复盘或多 attempt 横向比较。

    用途：
    - 单 attempt 复盘：读取市场画像、策略四件套、metrics、attempt_summary、stage_attribution，
      分析策略在哪些阶段表现好或差，并给出下一轮策略修改建议。
    - 多 attempt 比较：比较多个策略或多个版本，给出保留、放弃、回退或继续优化建议。
    - 最终选择前复核：为 SignalAgent 最终选择策略提供证据，但最终选择仍由 SignalAgent 完成。

    参数：
    - request：必填，完整自然语言任务。必须说明 run_state.json、attempt_id/attempt_ids、复盘重点、输出文件要求。
    - run_state_path：可选但在 SignalAgent 工作流中必须传入，工具会把该路径补充到 request。
    - trace_path：可选，子代理 JSONL 执行日志路径。不传则自动写入 artifacts/signal_agent_workspace/logs。

    返回：
    JSON 字符串，包含：
    - status：success 或 partial。
    - agent：CriticAgent。
    - trace_path：CriticAgent JSONL 执行日志路径。
    - final_message：最终中文总结，通常包含复盘/比较报告路径、核心结论、
      next_action 或 selection_advice、检查结果或失败原因。

    单 attempt 复盘成功后应检查：
    - attempts/{attempt_id}/review/critic_review.md：详细复盘报告。
    - attempts/{attempt_id}/review/critic_review.json：结构化复盘结果。
    - attempts/{attempt_id}/review/next_action.json：下一轮策略动作建议。
    - run_state.json 中该 attempt 的 status、critic_review_path、critic_review_md_path、next_action_path。

    多 attempt 比较成功后应检查：
    - final_message 中列出的比较报告、selection_advice 或 run_state.json 的 artifacts.run_reports。

    注意：
    这是同步长耗时工具，可能运行数分钟以上；如果触发 image-review 会更慢。
    调用后必须等待结果返回，不要假设它在后台继续执行。
    """

    return _run_child_agent(
        agent_name="CriticAgent",
        request=request,
        run_state_path=run_state_path,
        trace_path=trace_path,
    )


@tool
def call_budget_critic_agent(
    request: str,
    budget_run_state_path: str | None = None,
    trace_path: str | None = None,
) -> str:
    """同步调用 BudgetCriticAgent 子代理处理预算层单策略复盘或多策略横向比较。

    用途：
    - 单个预算策略复盘：读取 budget_run_state.json、search_result、attempt_summary、stage_attribution 等文件，
      分析预算策略在不同市场阶段、资产分组、调仓行为和风险暴露上的表现。
    - 多个预算策略横向比较：比较多个 search_id 或一个 batch_id 下的多策略结果，
      给出保留、放弃、回退或继续优化建议。

    参数：
    - request：必填，完整自然语言任务。必须说明 budget_run_state.json、search_id/batch_id/search_ids、
      复盘或比较重点、输出文件要求。
    - budget_run_state_path：可选但在 BudgetAgent 工作流中必须传入。工具会把该路径补充到 request。
    - trace_path：可选，子代理 JSONL 执行日志路径。不传则自动写入 artifacts/budget_agent_workspace/logs。

    返回：
    JSON 字符串，包含 status、agent、trace_path、final_message。

    注意：
    这是同步长耗时工具，可能运行数分钟到十几分钟以上。调用后必须等待结果返回，
    不要假设它在后台继续执行。
    """

    return _run_child_agent(
        agent_name="BudgetCriticAgent",
        request=request,
        run_state_path=budget_run_state_path,
        trace_path=trace_path,
    )


@tool
def call_portfolio_critic_agent(
    request: str,
    portfolio_run_state_path: str | None = None,
    trace_path: str | None = None,
) -> str:
    """同步调用 PortfolioCriticAgent 子代理处理组合层版本复盘或多版本横向比较。

    用途：
    - 单版本复盘：读取 portfolio_run_state.json、portfolio_profile、signal_profiles、source_artifacts、
      fusion policy 五件套和 evaluation 目录，分析组合层融合策略的优劣势、阶段表现、仓位行为和下一轮优化方向。
    - 多版本比较：比较多个 version_id 的回测指标、融合诊断、持仓、换手、预算/信号利用情况，给出保留、回退或最终选择建议。

    参数：
    - request：必填，完整自然语言任务。必须说明 portfolio_run_state.json、version_id/version_ids、复盘重点、输出文件要求。
    - portfolio_run_state_path：可选但在 PortfolioAgent 工作流中建议传入。工具会把该路径补充到 request。
    - trace_path：可选，子代理 JSONL 执行日志路径。不传则自动写入 artifacts/portfolio_critic_agent_workspace/logs。

    返回：
    JSON 字符串，包含 status、agent、trace_path、final_message。

    成功后应检查：
    - 单版本：versions/{version_id}/review/portfolio_critic_review.md、portfolio_critic_review.json、portfolio_next_action.json。
    - 多版本：reports/ 下的 portfolio_critic_comparison_*.md/json。
    - portfolio_run_state.json 的 artifacts.run_reports 和 events。

    注意：
    这是同步长耗时工具，可能运行数分钟到十几分钟以上；如果触发 image-review 会更慢。
    调用后必须等待结果返回，不要假设它在后台继续执行。
    """

    return _run_child_agent(
        agent_name="PortfolioCriticAgent",
        request=request,
        run_state_path=portfolio_run_state_path,
        trace_path=trace_path,
    )


def _run_child_agent(
    *,
    agent_name: str,
    request: str,
    run_state_path: str | None,
    trace_path: str | None,
) -> str:
    config = load_app_config()
    actual_request = _augment_request(request=request, run_state_path=run_state_path)
    actual_trace_path = trace_path or _default_trace_path(config.root_dir, agent_name)
    child = _create_child(agent_name)
    state = stream_agent_turn(
        child,
        actual_request,
        label=agent_name,
        trace_path=actual_trace_path,
    )
    final_text = last_ai_text(state)
    summary = {
        "status": "success" if final_text else "partial",
        "agent": agent_name,
        "trace_path": actual_trace_path,
        "final_message": final_text or "子代理没有返回可读最终消息，请查看 trace。",
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


def _create_child(agent_name: str) -> Any:
    if agent_name == "DataAgent":
        return DataAgent().create_agent()
    if agent_name == "CriticAgent":
        return CriticAgent().create_agent()
    if agent_name == "BudgetCriticAgent":
        return BudgetCriticAgent().create_agent()
    if agent_name == "PortfolioCriticAgent":
        return PortfolioCriticAgent().create_agent()
    raise ValueError(f"未知子代理：{agent_name}")


def _augment_request(*, request: str, run_state_path: str | None) -> str:
    if not run_state_path:
        return request
    if run_state_path in request:
        return request
    return f"{request}\n\n本次任务的 run_state.json 路径：{run_state_path}"


def _default_trace_path(root_dir: Path, agent_name: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{agent_name.lower()}_{stamp}.jsonl"
    if agent_name == "BudgetCriticAgent":
        workspace = "budget_agent_workspace"
    elif agent_name == "PortfolioCriticAgent":
        workspace = "portfolio_critic_agent_workspace"
    else:
        workspace = "signal_agent_workspace"
    return str(root_dir / "artifacts" / workspace / "logs" / filename)
