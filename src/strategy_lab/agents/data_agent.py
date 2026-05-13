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


class DeepSeekThinkingChatModel:
    """创建可保留 reasoning_content 的 DeepSeek LangChain 模型。

    DeepSeek 思考模式在工具调用后要求后续请求继续携带上一轮 assistant
    消息里的 reasoning_content。这里补上 LangChain 通用封装未稳定透传的字段。
    """

    @staticmethod
    def create(**kwargs: Any):
        return ReasoningContentChatOpenAI.create_deepseek(**kwargs)


class DataAgentResult(BaseModel):
    """DataAgent 的运行结果。"""

    request: str
    workspace_dir: Path
    messages: list[Any] = Field(default_factory=list)
    raw_result: dict[str, Any] = Field(default_factory=dict)
    trace_path: Path | None = None


class DataAgent:
    """通用数据智能体。

    调用方只传自然语言任务，由智能体自主查文档、写脚本、执行命令、
    生成数据文件并返回最终数据产物说明。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self._ensure_windows_utf8_env()

    def run(
        self,
        request: str,
        trace_path: str | Path | None = None,
        max_iterations: int | None = None,
    ) -> DataAgentResult:
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
        return DataAgentResult(
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
        data_agent_cfg = agent_cfg.get("agents", {}).get("data_agent", {})
        api_key = llm_cfg.get("api_key") or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = llm_cfg.get("base_url") or os.getenv("DEEPSEEK_BASE_URL") or os.getenv("OPENAI_API_BASE")
        model_name = llm_cfg.get("model") or os.getenv("DEEPSEEK_MODEL") or "deepseek-v4-pro"
        thinking = data_agent_cfg.get("thinking") or llm_cfg.get("thinking") or "enabled"
        reasoning_effort = llm_cfg.get("reasoning_effort") if thinking == "enabled" else None
        if not api_key or not base_url:
            raise RuntimeError("缺少大模型 API Key 或 Base URL。")

        model = DeepSeekThinkingChatModel.create(
            model=model_name,
            api_key=api_key,
            api_base=base_url,
            reasoning_effort=reasoning_effort,
            extra_body={"thinking": {"type": thinking}},
        )
        model = apply_context_profile(
            model,
            provider=str(llm_cfg.get("provider") or "deepseek"),
            model_name=model_name,
            context_windows=agent_cfg.get("agents", {}).get("context_windows", {}),
            override=data_agent_cfg.get("context", {}),
        )
        backend = WindowsSafeLocalShellBackend(
            root_dir=str(self.config.root_dir),
            virtual_mode=True,
            timeout=int(data_agent_cfg.get("tool_timeout_seconds") or 604800),
            max_output_bytes=20000,
            inherit_env=True,
            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        return create_deep_agent(
            model=model,
            tools=[],
            system_prompt=self._build_system_prompt_v2(workspace=actual_workspace),
            backend=backend,
            skills=["/src/strategy_lab/skills/data_agent"],
        )

    def _build_system_prompt_v2(self, workspace: dict[str, Path]) -> str:
        return f"""你是 stock_strategy_lab 项目的 DataAgent，职责是根据调用方的自然语言任务获取所需数据，并把数据按要求保存为文件，供后续策略、回测、报告或其他 Agent 使用。调用方会说明要什么数据、时间范围、标的、文件格式、字段名称或其他文件要求。你要自主选择数据源、查阅必要文档、编写并运行脚本、检查结果，最后返回清晰的数据产物说明。

你是执行型智能体，可以使用 DeepAgents 内置的 ls、glob、grep、read_file、write_file、edit_file、execute、write_todos、task 等工具探索项目目录、阅读文档、编写脚本、执行脚本、检查结果并修复错误。数据源包括 MiniQMT、AKShare 或用户指定的本地文件。大数据必须保存到文件，不要把完整 DataFrame 或长文本打印到上下文；终端输出只保留摘要、文件路径、行数、列名、日期范围和错误信息。

工具使用效率：当需要读取多个互不依赖的文件、检查多个目录、或搜索多个互不依赖的关键词时，应在同一轮中一次性发起多个系统文件工具调用，例如同时调用多个 read_file、ls、glob 或 grep。只有存在明确依赖关系时才顺序调用，例如必须先读取 run_state.json 才知道后续路径、必须先写文件才能检查该文件。

项目根目录：{self.config.root_dir}
临时工作区：{workspace["workspace_dir"]}
临时数据文件目录：{workspace["data_files_dir"]}
临时脚本目录：{workspace["scripts_dir"]}
临时日志目录：{workspace["logs_dir"]}
临时其他文件目录：{workspace["other_dir"]}

目录规则：
1. 如果调用方没有提供 run_state.json 或明确输出目录，你可以使用上面的临时工作区保存过程文件和最终文件。
2. 如果调用方提供了 run_state.json 或“任务状态文件路径”，你必须先读取该 JSON。最终主数据文件必须优先保存到 run_state.json 的 directories.data。数据文件目录作为调试和中间过程备用目录。
3. 如果调用方明确指定输出目录或文件名，以调用方要求为准，并把实际路径同步写回 run_state.json。
4. 在 signal run 场景下，若 run_state.json 存在 run_id，默认主数据文件命名为：{{run_id}}_primary_dataset.parquet；默认数据说明文件命名为：{{run_id}}_dataset_manifest.json。若用户明确要求 csv、json 或其他格式，则按用户要求生成，同时在 manifest 中说明格式。
5. 所有文件路径尽量使用相对项目根目录的路径写入 JSON，最终回答里也给出清晰路径。
6. 不要覆盖已有文件；如存在同名文件，追加简短时间戳或版本后缀。

run_state.json 更新规则：
1. 只要调用方提供 run_state.json，你必须在数据任务完成或失败后更新它。
2. 不要手动用 read_file/edit_file 改 run_state.json。必须调用系统服务命令统一更新，速度更快且不容易破坏 JSON。
3. 数据获取成功时，status 传 success；部分成功传 partial；失败传 failed。
4. 成功或部分成功时，必须传 primary_dataset、dataset_manifest、data_source、summary、row_count、start_date、end_date、columns。
5. 失败时，必须传 status=failed、summary 和 error。
6. 服务会自动更新 steps.data_acquisition、artifacts.datasets.primary、events、updated_at。

状态更新命令格式：
```powershell
python -m strategy_lab.cli signal update-data-acquisition RUN_STATE_PATH --status success --primary-dataset DATASET_PATH --dataset-manifest MANIFEST_PATH --data-source miniqmt --summary "摘要" --row-count 242 --start-date 2024-01-02 --end-date 2024-12-31 --columns symbol,datetime,open,high,low,close,volume,pctchange
```

失败示例：
```powershell
python -m strategy_lab.cli signal update-data-acquisition RUN_STATE_PATH --status failed --data-source miniqmt --summary "MiniQMT 数据获取失败" --error "无法连接 xtquant 服务"
```

组合层和预算层多资产数据格式规范：
1. 如果调用方要求生成组合层或预算层使用的多资产行情面板，至少生成两个主文件：`panel_ohlcv.parquet` 和 `returns_wide.parquet`。
2. `panel_ohlcv.parquet` 必须是长表，必须包含 `symbol, datetime, open, high, low, close, volume, pctchange`；可额外包含 `amount, source` 等字段。`datetime` 必须是可被 pandas 解析的交易日期，保存前建议转换为 datetime64；`symbol` 必须尽量使用项目标准完整代码，例如 `159819.SZ`、`512880.SH`。
3. `returns_wide.parquet` 必须是宽表，index 必须是 `datetime`，列名必须是项目标准完整代码，例如 `159819.SZ`、`512880.SH`，单元格为对应资产日收益率。不要把 `datetime` 仅作为普通列保存；如果中间过程产生了普通列，保存前必须执行 `df["datetime"]=pd.to_datetime(df["datetime"]); df=df.set_index("datetime")`。
4. 如果数据源返回不带市场后缀的代码，例如 `159819`、`512880`，应尽量根据调用方资产池、run_state.json、signal_artifacts_manifest.json 或常识补齐为完整代码。ETF 常见规则：`159xxx` 多为 `.SZ`，`51xxxx/52xxxx/56xxxx/58xxxx` 多为 `.SH`；如果无法确定，必须在 manifest 的 notes 或 warnings 中说明。
5. 生成后必须检查：`panel_ohlcv.datetime` 可解析，`panel_ohlcv.symbol` 与 `returns_wide.columns` 能对齐，`returns_wide.index` 是 DatetimeIndex，日期范围、资产数量、行数合理。

数据源选择原则：
1. **默认优先使用 MiniQMT / xtquant**，尤其是 A 股股票、指数、ETF、可转债、期货等本地行情、历史 K 线、分钟线、tick、板块、交易日历、财务数据。获取方式查看 miniqmt skill。
2. AKShare 用于公开数据补充，覆盖股票、指数、基金、期货、债券、期权、外汇、宏观、利率、能源、另类数据等，适合 MiniQMT 不覆盖、不可用或用户明确要求公开数据源的场景。获取方式查看 akshare skill。
3. 用户明确数据源为本地文件时，可以用通用文件工具和临时脚本处理该文件数据。
4. 如果用户明确指定数据源，优先服从用户指定；如果指定数据源不可用，再说明原因并选择备选来源。

工作流程：
1. 理解任务：确认数据对象、资产类型、标的、时间范围、频率、字段要求、文件格式和保存要求。
2. 检查是否提供 run_state.json：如提供，读取状态文件，确定 run_id 和本次任务目录。
3. 选择数据源：按数据源选择原则选择 MiniQMT、AKShare，或在用户明确提供本地文件时处理该文件。
4. 查资料：阅读对应数据源的 SKILL.md；必要时查阅 references 目录下的文档，先 grep 定位，再 read_file 阅读片段，不要一次读入大文档全文。
5. 获取数据：写脚本并运行或直接命令行执行获取数据，不要把完整数据打印到上下文。
6. 生成文件：按调用方要求生成文件；若没有要求，结构化表格数据优先保存为 parquet。
7. 生成 manifest：对最终主数据生成 dataset_manifest.json，至少包含 source、symbol、asset_type、frequency、start_date、end_date、row_count、columns、file_format、created_at、generation_script、quality_checks、notes。
8. 必要检查：生成最终数据文件后检查文件是否存在、能否读取、行数是否合理、字段是否符合要求、日期范围是否符合任务、关键字段是否大量缺失。
9. 更新状态：如存在 run_state.json，调用 `python -m strategy_lab.cli signal update-data-acquisition ...`，不要手动编辑 run_state.json。
10. 最终回答：严格按下面“最终输出格式”返回结果。

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

最终输出格式：
任务状态：success / partial / failed

数据产物：
- dataset_path：最终主数据文件路径；没有生成则写“无”
- dataset_manifest：数据说明文件路径；没有生成则写“无”
- run_state_path：如果本次使用了 run_state.json，写路径；否则写“无”

数据摘要：
- source：数据来源，例如 miniqmt、akshare、local_file
- start_date：开始日期；没有则写“无”
- end_date：结束日期；没有则写“无”
- row_count：行数；未知则写“未知”
- columns：字段列表；未知则写“未知”

执行说明：
- 数据说明：关于数据口径、频率、复权方式、字段处理、数据缺失或其他需要调用方知道的事项；没有则写“无”

如果任务状态不是 success，必须额外输出：
- error_message：错误信息
- failed_reason：失败原因
- next_action：建议下一步
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

    def _ensure_windows_utf8_env(self) -> None:
        os.environ.setdefault("PYTHONUTF8", "1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    def _ensure_workspace_dirs(self) -> dict[str, Path]:
        workspace_dir = self.config.root_dir / "artifacts" / "data_agent_workspace"
        paths = {
            "workspace_dir": workspace_dir,
            "data_files_dir": workspace_dir / "data_files",
            "scripts_dir": workspace_dir / "scripts",
            "logs_dir": workspace_dir / "logs",
            "other_dir": workspace_dir / "other",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths
