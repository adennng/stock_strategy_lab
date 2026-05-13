from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from strategy_lab.agents.backend import WindowsSafeLocalShellBackend
from strategy_lab.agents.model_factory import ReasoningContentChatOpenAI, apply_context_profile
from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.config.loader import load_config_file


class PortfolioCriticAgentResult(BaseModel):
    request: str
    workspace_dir: Path
    messages: list[Any] = Field(default_factory=list)
    raw_result: dict[str, Any] = Field(default_factory=dict)
    trace_path: Path | None = None


class PortfolioCriticAgent:
    """组合层策略复盘智能体。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self._ensure_windows_utf8_env()

    def run(
        self,
        request: str,
        trace_path: str | Path | None = None,
        max_iterations: int | None = None,
    ) -> PortfolioCriticAgentResult:
        workspace = self._ensure_workspace_dirs()
        agent = self.create_agent(workspace=workspace)
        result = agent.invoke(
            {"messages": [{"role": "user", "content": request}]},
            config=self._build_run_config(max_iterations),
        )
        result_dict = self._coerce_result(result)
        actual_trace_path = self._write_trace(
            trace_path=trace_path,
            request=request,
            workspace=workspace,
            result=result_dict,
        )
        return PortfolioCriticAgentResult(
            request=request,
            workspace_dir=workspace["workspace_dir"],
            messages=result_dict.get("messages", []),
            raw_result=result_dict,
            trace_path=actual_trace_path,
        )

    def create_agent(self, workspace: dict[str, Path] | None = None):
        self._ensure_windows_utf8_env()
        actual_workspace = workspace or self._ensure_workspace_dirs()
        try:
            from deepagents import create_deep_agent
        except ImportError as exc:
            raise RuntimeError("缺少 DeepAgents 运行依赖。请先安装 agents 依赖。") from exc

        agent_cfg = load_config_file("agent")
        llm_cfg = agent_cfg.get("agents", {}).get("llm", {})
        critic_cfg = agent_cfg.get("agents", {}).get("portfolio_critic_agent", {}) or agent_cfg.get("agents", {}).get("critic_agent", {})
        model = self._create_model(
            llm_cfg=llm_cfg,
            critic_cfg=critic_cfg,
            context_windows=agent_cfg.get("agents", {}).get("context_windows", {}),
        )
        backend = WindowsSafeLocalShellBackend(
            root_dir=str(self.config.root_dir),
            virtual_mode=True,
            timeout=int(critic_cfg.get("tool_timeout_seconds") or 604800),
            max_output_bytes=int(critic_cfg.get("max_output_bytes") or 40000),
            inherit_env=True,
            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        return create_deep_agent(
            model=model,
            tools=[],
            system_prompt=self._build_system_prompt(workspace=actual_workspace),
            backend=backend,
            skills=[
                "/src/strategy_lab/skills/common",
                "/src/strategy_lab/skills/portfolio_critic_agent",
            ],
        )

    def _create_model(
        self,
        *,
        llm_cfg: dict[str, Any],
        critic_cfg: dict[str, Any],
        context_windows: dict[str, Any] | None = None,
    ):
        provider = str(
            critic_cfg.get("provider")
            or os.getenv("PORTFOLIO_CRITIC_AGENT_PROVIDER")
            or os.getenv("CRITIC_AGENT_PROVIDER")
            or llm_cfg.get("provider")
            or os.getenv("DEEPSEEK_PROVIDER")
            or "deepseek"
        ).lower()
        cfg_base_url = critic_cfg.get("base_url")
        cfg_model = critic_cfg.get("model")
        cfg_matches_provider = self._config_matches_provider(provider=provider, base_url=cfg_base_url, model_name=cfg_model)
        cfg_api_key = critic_cfg.get("api_key") if cfg_matches_provider else None
        cfg_base_url = cfg_base_url if cfg_matches_provider else None
        cfg_model = cfg_model if cfg_matches_provider else None

        if provider in {"moonshot", "kimi"}:
            api_key = (
                cfg_api_key
                or (os.getenv("PORTFOLIO_CRITIC_AGENT_API_KEY") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_API_KEY") if cfg_matches_provider else None)
                or os.getenv("MOONSHOT_API_KEY")
                or llm_cfg.get("api_key")
                or os.getenv("DEEPSEEK_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
            base_url = (
                cfg_base_url
                or (os.getenv("PORTFOLIO_CRITIC_AGENT_BASE_URL") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_BASE_URL") if cfg_matches_provider else None)
                or os.getenv("MOONSHOT_BASE_URL")
                or llm_cfg.get("base_url")
                or os.getenv("DEEPSEEK_BASE_URL")
                or os.getenv("OPENAI_API_BASE")
            )
            model_name = (
                cfg_model
                or (os.getenv("PORTFOLIO_CRITIC_AGENT_MODEL") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_MODEL") if cfg_matches_provider else None)
                or os.getenv("MOONSHOT_MODEL")
                or llm_cfg.get("model")
                or os.getenv("DEEPSEEK_MODEL")
                or "kimi-k2.6"
            )
        else:
            api_key = (
                cfg_api_key
                or (os.getenv("PORTFOLIO_CRITIC_AGENT_API_KEY") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_API_KEY") if cfg_matches_provider else None)
                or llm_cfg.get("api_key")
                or os.getenv("DEEPSEEK_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
            base_url = (
                cfg_base_url
                or (os.getenv("PORTFOLIO_CRITIC_AGENT_BASE_URL") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_BASE_URL") if cfg_matches_provider else None)
                or llm_cfg.get("base_url")
                or os.getenv("DEEPSEEK_BASE_URL")
                or os.getenv("OPENAI_API_BASE")
            )
            model_name = (
                cfg_model
                or (os.getenv("PORTFOLIO_CRITIC_AGENT_MODEL") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_MODEL") if cfg_matches_provider else None)
                or llm_cfg.get("model")
                or os.getenv("DEEPSEEK_MODEL")
                or "deepseek-v4-pro"
            )

        thinking = str(critic_cfg.get("thinking") or llm_cfg.get("thinking") or "enabled")
        reasoning_effort = critic_cfg.get("reasoning_effort") or llm_cfg.get("reasoning_effort")
        max_completion_tokens = self._optional_int(critic_cfg.get("max_completion_tokens"))
        if not api_key or not base_url:
            raise RuntimeError("缺少组合层复盘智能体的大模型 API Key 或 Base URL。")

        if provider in {"moonshot", "kimi"}:
            kwargs: dict[str, Any] = {
                "model": model_name,
                "api_key": api_key,
                "base_url": base_url,
                "payload_token_param": "max_tokens",
            }
            if max_completion_tokens:
                kwargs["max_completion_tokens"] = max_completion_tokens
            model = ReasoningContentChatOpenAI.create_openai_compatible(**kwargs)
            return apply_context_profile(
                model,
                provider=provider,
                model_name=model_name,
                context_windows=context_windows,
                override=critic_cfg.get("context", {}),
            )

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
                override=critic_cfg.get("context", {}),
            )

        kwargs = {"model": model_name, "api_key": api_key, "base_url": base_url}
        if max_completion_tokens:
            kwargs["max_completion_tokens"] = max_completion_tokens
        model = ReasoningContentChatOpenAI.create_openai_compatible(**kwargs)
        return apply_context_profile(
            model,
            provider=provider,
            model_name=model_name,
            context_windows=context_windows,
            override=critic_cfg.get("context", {}),
        )

    def _build_system_prompt(self, workspace: dict[str, Path]) -> str:
        return f"""你是 stock_strategy_lab 项目的 PortfolioCriticAgent，职责是复盘组合层融合策略版本，并向 PortfolioAgent 提供下一轮 `fusion_policy.py` 优化建议。

你是独立 Agent，会被 PortfolioAgent、总控 Agent 或用户用自然语言调用。调用方通常会给你 portfolio_run_state.json 路径、version_id、多个 version_id，或要求你比较当前组合层多个版本。

项目根目录：{self.config.root_dir}
临时工作区：{workspace["workspace_dir"]}
临时日志目录：{workspace["logs_dir"]}
临时文件目录：{workspace["files_dir"]}

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

你的复盘目标不是重写策略，而是基于证据回答：
- 当前组合层版本哪里做得好，哪里做得差。
- 它在不同阶段、不同资产、不同仓位状态下表现如何。
- 预算层和信号层的冲突或互补是否被合理处理。
- `fusion_policy.py` 下一轮应优化哪些规则、参数或诊断字段。

**重要**：必须使用 `portfolio-review` skill。该 skill 是唯一组合层复盘 skill，作为复盘指南，既可处理单版本复盘，也可处理多版本横向比较。请严格按照 `portfolio-review` skill 的要求执行。

工作流程：
1. 阅读 `portfolio-review` 的 SKILL.md全文，再执行任务。
2. 按照该skill的要求阅读相关文件、进行分析、生成目标文件、进行登记、检查。

重点分析内容：
1. 总体表现：Sharpe、总收益、年化收益、最大回撤、Calmar、换手、交易成本、平均敞口、持仓数量、相对基准表现。
2. 阶段表现：按时间阶段、回撤阶段、行情阶段或数据切分阶段看收益、回撤、敞口、换手和基准差异。
3. 仓位行为：每日总仓位、现金比例、单资产权重、超预算权重、预算/信号/最终权重的差异。
4. 持仓和交易：重点资产持仓变化、交易次数、换手集中日期、是否过度交易或错过强趋势。
5. 预算/信号融合：高预算低信号、高信号低预算、预算为 0 但强信号、信号 veto、signal floor、闲置资金再分配是否合理。
6. 资产层贡献：哪些资产贡献收益或拖累回撤，是否需要阅读对应 `signal_agent_memory.md` 判断信号层逻辑。
7. 策略结构：`fusion_policy.py` 的参数、公式、风险控制、换手约束、max_gross/max_weight、over_budget 规则是否合理。
8. 优化建议：下一轮应改哪些参数、规则、诊断或约束；哪些方向不建议继续。

工作原则：
1. 先阅读 `portfolio-review` 的 SKILL.md全文，再执行任务。
2. 结论必须基于文件证据，不要只看单一 metrics。
3. 不要直接修改 `fusion_policy.py`；只输出复盘、问题、证据和下一轮建议。
4. 不要修改 `source_artifacts` 下的任何文件。
5. 需要读取多个互不依赖文件时，可以在同一轮中一次性发起多个系统文件工具调用，但不要使用内置 grep 工具。需要搜索时，优先 glob/ls/read_file；如必须批量搜索，使用 execute 运行简短 Python 脚本。
6. 读取 parquet/csv 时不要打印全文，输出行数、列名、统计摘要、关键日期或 Top/Bottom 聚合即可。
7. 如果图像能提供数据文件无法直接提供的信息，或调用方明确要求查看图像，可使用 image-review skill。
8. 复盘完成后必须按 `portfolio-review` skill 要求生成文件，并调用 `python -m strategy_lab.cli portfolio register-run-report ...` 登记到 `portfolio_run_state.json`。

路径规则：
- 文件工具路径以 `/` 开头，`/` 代表项目根目录 `{self.config.root_dir}`。
- execute 运行命令时，工作目录已自动设为项目根目录，使用不带 `/` 前缀的相对路径即可。
- Windows PowerShell 环境下不要使用 `mkdir -p`、`touch`、`cat`、`rm -rf`、`cp -r`、`grep`、`sed`、`awk`、`chmod`。
- 文件和目录操作优先使用 DeepAgents 文件工具；复杂统计优先用 execute 跑短 Python 脚本。

最终回复调用方时，用中文简要说明：
- 任务状态：success / partial / failed
- 生成的复盘文件路径
- 核心优势、核心问题
- 下一轮优化建议文件路径
- run_state 登记结果
"""

    def _write_trace(
        self,
        trace_path: str | Path | None,
        request: str,
        workspace: dict[str, Path],
        result: dict[str, Any],
    ) -> Path | None:
        if not trace_path:
            return None
        path = Path(trace_path)
        if not path.is_absolute():
            path = self.config.root_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "created_at": datetime.now().isoformat(),
            "request": request,
            "workspace": {key: str(value) for key, value in workspace.items()},
            "result": result,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path

    def _build_run_config(self, max_iterations: int | None) -> dict[str, Any]:
        if max_iterations is None:
            return {}
        return {"recursion_limit": int(max_iterations)}

    def _coerce_result(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json")
        return {"value": result}

    def _optional_int(self, value: Any) -> int | None:
        if value in {None, ""}:
            return None
        return int(value)

    @staticmethod
    def _config_matches_provider(*, provider: str, base_url: Any, model_name: Any) -> bool:
        base = str(base_url or "").lower()
        model = str(model_name or "").lower()
        if provider in {"moonshot", "kimi"}:
            return "deepseek" not in base and "deepseek" not in model
        if provider == "deepseek":
            return "moonshot" not in base and "kimi" not in model
        return True

    def _ensure_windows_utf8_env(self) -> None:
        os.environ.setdefault("PYTHONUTF8", "1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    def _ensure_workspace_dirs(self) -> dict[str, Path]:
        workspace_dir = self.config.root_dir / "artifacts" / "portfolio_critic_agent_workspace"
        paths = {
            "workspace_dir": workspace_dir,
            "logs_dir": workspace_dir / "logs",
            "files_dir": workspace_dir / "files",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths
