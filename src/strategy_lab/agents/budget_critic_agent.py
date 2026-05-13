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


class BudgetCriticAgentResult(BaseModel):
    request: str
    workspace_dir: Path
    messages: list[Any] = Field(default_factory=list)
    raw_result: dict[str, Any] = Field(default_factory=dict)
    trace_path: Path | None = None


class BudgetCriticAgent:
    """预算层策略复盘智能体。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self._ensure_windows_utf8_env()

    def run(
        self,
        request: str,
        trace_path: str | Path | None = None,
        max_iterations: int | None = None,
    ) -> BudgetCriticAgentResult:
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
        return BudgetCriticAgentResult(
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
        critic_cfg = agent_cfg.get("agents", {}).get("budget_critic_agent", {}) or agent_cfg.get("agents", {}).get("critic_agent", {})
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
                "/src/strategy_lab/skills/budget_critic_agent",
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
            or os.getenv("BUDGET_CRITIC_AGENT_PROVIDER")
            or os.getenv("CRITIC_AGENT_PROVIDER")
            or llm_cfg.get("provider")
            or os.getenv("DEEPSEEK_PROVIDER")
            or "deepseek"
        ).lower()
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
                or (os.getenv("BUDGET_CRITIC_AGENT_API_KEY") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_API_KEY") if cfg_matches_provider else None)
                or os.getenv("MOONSHOT_API_KEY")
                or llm_cfg.get("api_key")
                or os.getenv("DEEPSEEK_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
            base_url = (
                cfg_base_url
                or (os.getenv("BUDGET_CRITIC_AGENT_BASE_URL") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_BASE_URL") if cfg_matches_provider else None)
                or os.getenv("MOONSHOT_BASE_URL")
                or llm_cfg.get("base_url")
                or os.getenv("DEEPSEEK_BASE_URL")
                or os.getenv("OPENAI_API_BASE")
            )
            model_name = (
                cfg_model
                or (os.getenv("BUDGET_CRITIC_AGENT_MODEL") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_MODEL") if cfg_matches_provider else None)
                or os.getenv("MOONSHOT_MODEL")
                or llm_cfg.get("model")
                or os.getenv("DEEPSEEK_MODEL")
                or "kimi-k2.6"
            )
        else:
            api_key = (
                cfg_api_key
                or (os.getenv("BUDGET_CRITIC_AGENT_API_KEY") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_API_KEY") if cfg_matches_provider else None)
                or llm_cfg.get("api_key")
                or os.getenv("DEEPSEEK_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
            base_url = (
                cfg_base_url
                or (os.getenv("BUDGET_CRITIC_AGENT_BASE_URL") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_BASE_URL") if cfg_matches_provider else None)
                or llm_cfg.get("base_url")
                or os.getenv("DEEPSEEK_BASE_URL")
                or os.getenv("OPENAI_API_BASE")
            )
            model_name = (
                cfg_model
                or (os.getenv("BUDGET_CRITIC_AGENT_MODEL") if cfg_matches_provider else None)
                or (os.getenv("CRITIC_AGENT_MODEL") if cfg_matches_provider else None)
                or llm_cfg.get("model")
                or os.getenv("DEEPSEEK_MODEL")
                or "deepseek-v4-pro"
            )
        thinking = str(critic_cfg.get("thinking") or llm_cfg.get("thinking") or "enabled")
        reasoning_effort = critic_cfg.get("reasoning_effort") or llm_cfg.get("reasoning_effort")
        max_completion_tokens = self._optional_int(critic_cfg.get("max_completion_tokens"))
        if not api_key or not base_url:
            raise RuntimeError("缺少预算层复盘智能体的大模型 API Key 或 Base URL。")

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
        return f"""你是 stock_strategy_lab 项目的 BudgetCriticAgent，职责是复盘预算层策略评估结果，并给 BudgetAgent 提供下一轮预算策略优化建议。

你是独立 Agent，会被 BudgetAgent、总控 Agent 或用户用自然语言调用。调用方通常会给你 budget_run_state.json 路径、search_id、batch_id 或批量评估目录。

你需要自行判断任务属于：
1. 单策略复盘：使用 single-policy-review skill。例如，用户说“请复盘 budget_run_state_path=... 下的 search_id=budget_policy_001。”
2. 多策略横向比较：使用 multi-policy-comparison skill。例如，用户说“请复盘 budget_run_state_path=... 下 batch_id=budget_batch_001 的多策略结果。
请比较 search_a、search_b、search_c，给出下一轮保留哪个预算策略方向。”

项目根目录：{self.config.root_dir}
临时工作区：{workspace["workspace_dir"]}
临时日志目录：{workspace["logs_dir"]}
临时文件目录：{workspace["files_dir"]}

预算层策略统一框架是：
BudgetPolicy = UniverseGate 候选资产准入 + AssetScorer 资产打分 + AllocationEngine 预算分配 + RiskOverlay 风险覆盖 + RebalanceScheduler 调仓节奏 + ConstraintProjector 约束投影 + Diagnostics 诊断输出。

预算层默认独立于信号层。复盘预算策略时，应重点判断预算策略族是否匹配预算画像和用户偏好，而不是建议用信号层策略质量或当前信号强弱来替代预算层打分。信号层与预算层的仓位融合属于组合层任务。

工作原则：
1. **重要**：先阅读对应场景的 SKILL.md，再执行任务。
2. 结论必须基于 budget_run_state.json、search_result.json、attempt_summary、stage_attribution、batch summary、metrics、CSV/Parquet 摘要等证据。
3. 不要替 BudgetAgent 生成新预算策略配置；你只输出复盘结论、保留/修改/避免事项和下一轮建议。
4. 不要手动编辑 budget_run_state.json；复盘完成后必须调用 CLI 服务登记结果。
5. 如果预算画像与用户偏好冲突，必须明确说明冲突、当前策略如何取舍，以及下一轮应保留稳健路线还是偏好路线。
6. 复盘输出必须包含阶段推进建议：stay_current_stage、advance_next_stage、rollback_previous_stage、return_to_family_exploration 或 ready_for_final_selection。不要只说“继续优化”，必须说明继续在哪个阶段、为什么。
7. 不要建议 BudgetAgent 用信号层策略质量或当前信号强弱作为标准预算层打分方向；这类融合应放到组合层。
8. 需要读取多个互不依赖文件时，可以在同一轮中一次性发起多个系统文件工具调用，但不要使用内置 grep 工具。当前 Windows 环境下 DeepAgents 的 grep 偶发兼容问题，可能导致任务在最终检查阶段中断。需要搜索文件内容时，优先用 glob/ls 定位文件后 read_file 阅读；如果必须批量搜索，使用 execute 运行一个简短 Python 脚本读取文本并输出匹配摘要。
9. 如果图像能提供数据文件无法直接提供的信息，或调用方明确要求查看图像时，可使用 image-review skill。该 skill 会调用其他多模态大模型完成图片识别任务，如果你本身就是多模态大模型的话也可以使用 read_file tool 自己识别图片，但如果你不是多模态大模型的话只能使用image-review  skill。
10. 运行命令时尽量避免打印大文件全文。读取 parquet/csv 应输出头部、行数、列名、统计摘要或必要聚合。
11. 输出文件必须写入对应 SKILL.md 文件要求的目录，并在完成后按照 SKILL.md 的要求执行最终检查。

run_state.json 更新规则：
- 单策略复盘完成后调用：
  python -m strategy_lab.cli budget update-policy-review BUDGET_RUN_STATE_PATH SEARCH_ID --critic-review-path REVIEW_JSON --critic-review-md-path REVIEW_MD --next-action-path NEXT_ACTION_JSON --summary "复盘摘要"
- 多策略横向比较完成后调用：
  python -m strategy_lab.cli budget register-run-report BUDGET_RUN_STATE_PATH REPORT_KEY REPORT_PATH --report-type budget_critic_comparison --summary "比较摘要" --extra-json-file EXTRA_JSON_PATH

路径规则：
- ls、read_file、write_file、edit_file、glob 使用以 / 开头的路径，其中 / 代表项目根目录 `{self.config.root_dir}`。不要使用内置 grep。
  例：`/artifacts/budget_runs/budget_xxx/policies/searches/search_id/attempt_summary.json`
- execute 运行命令时，工作目录已自动设为项目根目录，使用不带 / 前缀的相对路径即可。
  转换规则：文件工具路径去掉开头的 / 就是 execute 的相对路径。
  例：`/artifacts/signal_runs/x.parquet` → `artifacts/signal_runs/x.parquet`
- execute 中也可以直接使用 Windows 绝对路径。
- 如果调用方给的是 Windows 绝对路径，要用文件工具读取时，需去掉 `{self.config.root_dir}` 前缀并加上 / 转换为以 / 开头的路径。
- 当前运行环境是 Windows PowerShell。不要使用 Linux/macOS shell 命令，例如 `mkdir -p`、`touch`、`cat`、`rm -rf`、`cp -r`、`grep`、`sed`、`awk`、`chmod`。
- 文件和目录操作优先使用 DeepAgents 文件工具（ls、read_file、write_file、edit_file、glob）。如果必须使用 execute 做文件操作，优先写一个短 Python 脚本或使用 PowerShell 原生命令。
- 使用 execute 时，尽量避免依赖 PowerShell 中文错误输出；命令要简短、路径加引号、参数少换行。复杂命令优先写成 `.py` 脚本再执行，减少 Windows 编码和引号问题。

最终回复调用方时，用中文简要说明：
- 任务状态：success / partial / failed
- 生成的文件路径
- 核心复盘结论
- 下一轮建议文件路径
- 最终检查结果
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
        workspace_dir = self.config.root_dir / "artifacts" / "budget_critic_agent_workspace"
        paths = {
            "workspace_dir": workspace_dir,
            "logs_dir": workspace_dir / "logs",
            "files_dir": workspace_dir / "files",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths
