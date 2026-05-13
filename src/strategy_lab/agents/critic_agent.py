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


class CriticAgentResult(BaseModel):
    request: str
    workspace_dir: Path
    messages: list[Any] = Field(default_factory=list)
    raw_result: dict[str, Any] = Field(default_factory=dict)
    trace_path: Path | None = None


class CriticAgent:
    """股票策略复盘智能体。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self._ensure_windows_utf8_env()

    def run(
        self,
        request: str,
        trace_path: str | Path | None = None,
        max_iterations: int | None = None,
    ) -> CriticAgentResult:
        workspace = self._ensure_workspace_dirs()
        agent = self.create_agent(workspace=workspace)
        messages = [{"role": "user", "content": request}]
        config = self._build_run_config(max_iterations)
        result = agent.invoke({"messages": messages}, config=config)
        result_dict = self._coerce_result(result)
        actual_trace_path = self._write_trace(
            trace_path=trace_path,
            request=request,
            workspace=workspace,
            result=result_dict,
        )
        return CriticAgentResult(
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
            raise RuntimeError("缺少 DeepAgents 运行依赖。请先安装：python -m pip install -e .[agents]") from exc

        agent_cfg = load_config_file("agent")
        llm_cfg = agent_cfg.get("agents", {}).get("llm", {})
        critic_cfg = agent_cfg.get("agents", {}).get("critic_agent", {})
        model = self._create_model(
            llm_cfg=llm_cfg,
            critic_cfg=critic_cfg,
            context_windows=agent_cfg.get("agents", {}).get("context_windows", {}),
        )
        backend = WindowsSafeLocalShellBackend(
            root_dir=str(self.config.root_dir),
            virtual_mode=True,
            timeout=int(critic_cfg.get("tool_timeout_seconds") or 604800),
            max_output_bytes=30000,
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
                "/src/strategy_lab/skills/critic_agent",
            ],
        )

    def _create_model(
        self,
        *,
        llm_cfg: dict[str, Any],
        critic_cfg: dict[str, Any],
        context_windows: dict[str, Any] | None = None,
    ):
        provider = str(critic_cfg.get("provider") or llm_cfg.get("provider") or "deepseek").lower()
        cfg_base_url = critic_cfg.get("base_url")
        cfg_model = critic_cfg.get("model")
        cfg_matches_provider = self._config_matches_provider(
            provider=provider,
            base_url=cfg_base_url,
            model_name=cfg_model,
        )
        cfg_api_key = critic_cfg.get("api_key") if cfg_matches_provider else None
        cfg_base_url = cfg_base_url if cfg_matches_provider else None
        cfg_model = cfg_model if cfg_matches_provider else None
        if provider in {"moonshot", "kimi"}:
            api_key = (
                cfg_api_key
                or (os.getenv("CRITIC_AGENT_API_KEY") if cfg_matches_provider else None)
                or os.getenv("MOONSHOT_API_KEY")
                or llm_cfg.get("api_key")
                or os.getenv("DEEPSEEK_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
            base_url = (
                cfg_base_url
                or (os.getenv("CRITIC_AGENT_BASE_URL") if cfg_matches_provider else None)
                or os.getenv("MOONSHOT_BASE_URL")
                or llm_cfg.get("base_url")
                or os.getenv("DEEPSEEK_BASE_URL")
                or os.getenv("OPENAI_API_BASE")
            )
            model_name = (
                cfg_model
                or (os.getenv("CRITIC_AGENT_MODEL") if cfg_matches_provider else None)
                or os.getenv("MOONSHOT_MODEL")
                or llm_cfg.get("model")
                or os.getenv("DEEPSEEK_MODEL")
                or "kimi-k2.6"
            )
        else:
            api_key = (
                cfg_api_key
                or (os.getenv("CRITIC_AGENT_API_KEY") if cfg_matches_provider else None)
                or llm_cfg.get("api_key")
                or os.getenv("DEEPSEEK_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
            base_url = (
                cfg_base_url
                or (os.getenv("CRITIC_AGENT_BASE_URL") if cfg_matches_provider else None)
                or llm_cfg.get("base_url")
                or os.getenv("DEEPSEEK_BASE_URL")
                or os.getenv("OPENAI_API_BASE")
            )
            model_name = (
                cfg_model
                or (os.getenv("CRITIC_AGENT_MODEL") if cfg_matches_provider else None)
                or llm_cfg.get("model")
                or os.getenv("DEEPSEEK_MODEL")
                or "deepseek-v4-pro"
            )
        thinking = str(critic_cfg.get("thinking") or llm_cfg.get("thinking") or "enabled")
        reasoning_effort = critic_cfg.get("reasoning_effort") or llm_cfg.get("reasoning_effort")
        max_completion_tokens = self._optional_int(critic_cfg.get("max_completion_tokens"))
        if not api_key or not base_url:
            raise RuntimeError("缺少大模型 API Key 或 Base URL。")

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

        kwargs = {
            "model": model_name,
            "api_key": api_key,
            "base_url": base_url,
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

    def _build_system_prompt(self, workspace: dict[str, Path]) -> str:
        return f"""你是 stock_strategy_lab 项目的 CriticAgent，职责是基于已有文件和数据证据，对 SignalAgent 生成的股票量化策略进行复盘。

你是独立 Agent，会被 SignalAgent、总控 Agent 或用户用自然语言调用。调用方通常只会告诉你 run_state.json 路径、一个或多个 attempt_id，以及复盘重点。你必须自行判断任务属于：
1. 单 attempt 复盘：使用 single-attempt-review skill。例如，用户说“请复盘 run_state_path=... 下的 attempt_001。”
2. 多 attempt 横向比较：使用 multi-attempt-comparison skill。例如，用户说“请比较 run_state_path=... 下的 attempt_001、attempt_002、attempt_003。”

你可以使用 DeepAgents 内置的 ls、glob、grep、read_file、write_file、edit_file、execute、write_todos、task 等工具读取项目文件、运行 CLI 服务、生成复盘产物和检查结果。需要理解图片时，如果你是多模态模型，可以使用read_file tool或 image-review skill ；如果你不是多模态模型，只使用image-review skill 。

工具使用效率：当需要读取多个互不依赖的文件、检查多个目录、或搜索多个互不依赖的关键词时，应在同一轮中一次性发起多个系统文件工具调用，例如同时调用多个 read_file、ls、glob 或 grep。只有存在明确依赖关系时才顺序调用，例如必须先读取 run_state.json 才知道 attempt 路径、必须先生成复盘文件才能检查该文件。

SignalAgent 当前统一策略框架是：
SignalStrategy = RegimeDetector 市场状态识别 + RegimeAlphaPolicy 分状态多周期 Alpha + Filters 辅助过滤器 + ExitPolicy 出场与风控 + PositionMapper 仓位映射 + StateRules 状态与交易纪律。默认优先使用 RegimeSwitchingAlpha + MultiTimeframeAlpha：先判断 uptrend/range/downtrend/high_vol 等市场状态，再为不同状态使用长/中/短周期组合 Alpha，最后统一输出 target_S ∈ [0,1]，不是加减仓动作。

你提出下一轮建议时，必须按这个框架说明“保留什么、修改什么、避免什么”，并明确影响的模块。重点判断：
- RegimeDetector 是否与 market_profile 和 stage_attribution 的真实阶段一致。
- RegimeAlphaPolicy / RegimeAlphaMap 是否在不同状态下选择了合适 Alpha，是否存在所有 regime 共用同一 Alpha 的问题。
- MultiTimeframeAlpha 的 long/mid/short 窗口是否互相确认，还是互相冲突或过拟合。
- StateRules 是否有效抑制 regime 切换抖动和仓位剧烈变化。
多 attempt 横向比较时，不要要求固定四类候选齐全；应判断候选方向是否来自 market_profile。若候选只是机械套用趋势/突破/回归/防御四类，而没有结合资产画像调整，应指出该问题。若多个候选同属一类但 RegimeAlphaMap、窗口、入场/退出逻辑或风控结构有实质差异，也可以接受。
如果证据显示某个 Alpha 不适合，应直接建议替换对应 regime 的 Alpha；任何阶段都允许建议调整 Alpha，不要只建议调参数。
可参考 `/src/strategy_lab/skills/signal_agent/strategy-authoring/references/signal_strategy_library.md` 中的 RegimeSwitchingAlpha、MultiTimeframeAlpha、Filters、ExitPolicy、PositionMapper、StateRules 模板，但不要替 SignalAgent 直接生成完整策略脚本。

项目根目录：{self.config.root_dir}
CriticAgent 临时工作区：{workspace["workspace_dir"]}
临时日志目录：{workspace["logs_dir"]}
临时文件目录：{workspace["files_dir"]}

核心原则：
1. **重要**：先阅读对应场景的 SKILL.md 文件，严格按照其中的要求执行。
2. 所有结论必须基于文件、指标、表格、图像分析或你运行的确定性服务结果，不得编造不存在的数据。
3. 你不直接生成策略脚本，不直接做参数搜索，不替 SignalAgent 做最终策略选择；你输出复盘结论和下一步建议。
4. 单 attempt 复盘前，若缺少阶段归因，请运行：python -m strategy_lab.cli signal stage-attribution RUN_STATE_PATH ATTEMPT_ID。
5. 多 attempt 横向比较前，请运行：python -m strategy_lab.cli signal compare-attempts RUN_STATE_PATH --attempt-ids attempt_001,attempt_002。
6. 如果图像能提供数据文件无法直接提供的信息，或调用方明确要求查看图像时，可使用 image-review skill。该 skill 会调用其他多模态大模型完成图片识别任务，如果你本身就是多模态大模型的话也可以使用 read_file tool 自己识别图片，但如果你不是多模态大模型的话只能使用image-review  skill。
7. 输出文件必须写入对应 SKILL.md 文件要求的目录，并在完成后执行最终检查：确认必需产物存在、非空、JSON 可解析、Markdown 可读；确认 run_state.json 已按任务要求通过 CLI 服务更新；确认最终回复中的文件路径和真实路径一致。
8. 如果执行命令、读取文件或最终检查中出现失败，必须说明失败原因、缺少的文件、已完成的部分和下一步建议。

run_state.json 更新规则：
- 不要手动用 read_file/edit_file 修改 run_state.json。
- 单 attempt 复盘完成后，必须调用：
  `python -m strategy_lab.cli signal update-critic-review RUN_STATE_PATH ATTEMPT_ID --critic-review-path CRITIC_REVIEW_JSON --critic-review-md-path CRITIC_REVIEW_MD --next-action-path NEXT_ACTION_JSON --summary "复盘摘要"`
- 多 attempt 横向比较完成后，如需登记 run 级比较报告，调用：
  `python -m strategy_lab.cli signal register-run-report RUN_STATE_PATH REPORT_KEY REPORT_PATH --report-type critic_comparison --summary "比较摘要" --extra-json "{{}}"`
- 如果需要登记 selection_advice_path、comparison_json_path 等额外路径，优先把这些元数据写成 JSON 文件，再使用 `--extra-json-file EXTRA_JSON_PATH`，避免 PowerShell 引号转义问题。
- 服务会自动更新 attempt 状态、复盘路径、artifacts.run_reports、events、updated_at。

路径规则：
- ls、read_file、write_file、edit_file、grep、glob 使用以 / 开头的路径，其中 / 代表项目根目录 `{self.config.root_dir}`。
  例：`/artifacts/signal_runs/x.parquet`
- execute 运行命令时，工作目录已自动设为项目根目录，使用不带 / 前缀的相对路径即可。
  转换规则：文件工具路径去掉开头的 / 就是 execute 的相对路径。
  例：`/artifacts/signal_runs/x.parquet` → `artifacts/signal_runs/x.parquet`
- execute 中也可以直接使用 Windows 绝对路径。
- 如果调用方给的是 Windows 绝对路径，要用文件工具读取时，需去掉 `{self.config.root_dir}` 前缀并加上 / 转换为以 / 开头的路径。
- 当前运行环境是 Windows PowerShell。不要使用 Linux/macOS shell 命令，例如 `mkdir -p`、`touch`、`cat`、`rm -rf`、`cp -r`、`grep`、`sed`、`awk`、`chmod`。
- 文件和目录操作优先使用 DeepAgents 文件工具（ls、read_file、write_file、edit_file、glob、grep）。如果必须使用 execute 做文件操作，优先写一个短 Python 脚本或使用 PowerShell 原生命令。
- 使用 execute 时，尽量避免依赖 PowerShell 中文错误输出；命令要简短、路径加引号、参数少换行。复杂命令优先写成 `.py` 脚本再执行，减少 Windows 编码和引号问题。

最终回复给调用方时，用中文简要说明：
- 任务状态：success / partial / failed
- 生成的文件路径
- 核心复盘结论
- 给 SignalAgent 的下一步建议文件路径
- 最终检查结果：已通过 / 部分通过 / 未通过

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

    def _resolve_recursion_limit(self, max_iterations: int | None) -> int:
        if max_iterations is None:
            raise ValueError("未显式提供 max_iterations 时不应解析 recursion_limit。")
        return int(max_iterations)

    def _build_run_config(self, max_iterations: int | None) -> dict[str, Any]:
        if max_iterations is None:
            return {}
        return {"recursion_limit": self._resolve_recursion_limit(max_iterations)}

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
        workspace_dir = self.config.root_dir / "artifacts" / "critic_agent_workspace"
        paths = {
            "workspace_dir": workspace_dir,
            "logs_dir": workspace_dir / "logs",
            "files_dir": workspace_dir / "files",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths
