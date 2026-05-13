from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from strategy_lab.artifacts import ArtifactManager
from strategy_lab.agents.data_agent import DataAgent
from strategy_lab.agents.budget_agent import BudgetAgent, run_budget_chat_loop
from strategy_lab.agents.budget_critic_agent import BudgetCriticAgent
from strategy_lab.agents.critic_agent import CriticAgent
from strategy_lab.agents.portfolio_agent import PortfolioAgent, run_portfolio_chat_loop
from strategy_lab.agents.portfolio_critic_agent import PortfolioCriticAgent
from strategy_lab.agents.signal_agent import SignalAgent, last_ai_text, run_chat_loop, stream_agent_turn
from strategy_lab.config import load_app_config
from strategy_lab.services.attempt_summary import AttemptSummaryRequest, AttemptSummaryService
from strategy_lab.services.attempt_evaluation import AttemptEvaluationError, AttemptEvaluationRequest, AttemptEvaluationService
from strategy_lab.services.attempt_comparison import AttemptComparisonRequest, AttemptComparisonService
from strategy_lab.services.batch_attempt_evaluation import BatchAttemptEvaluationRequest, BatchAttemptEvaluationService
from strategy_lab.services.budget_attempt_summary import BudgetAttemptSummaryRequest, BudgetAttemptSummaryService
from strategy_lab.services.budget_batch_policy_evaluation import BudgetBatchPolicyEvaluationRequest, BudgetBatchPolicyEvaluationService
from strategy_lab.services.budget_backtest import BudgetBacktestRequest, BudgetBacktestService
from strategy_lab.services.budget_data_panel import BudgetDataPanelRequest, BudgetDataPanelService
from strategy_lab.services.budget_data_split import BudgetDataSplitRequest, BudgetDataSplitService
from strategy_lab.services.budget_parameter_search import BudgetParameterSearchRequest, BudgetParameterSearchService
from strategy_lab.services.budget_policy_evaluation import BudgetPolicyEvaluationRequest, BudgetPolicyEvaluationService
from strategy_lab.services.budget_policy_engine import BudgetPolicyEngine, BudgetPolicyEngineRequest
from strategy_lab.services.budget_profile import BudgetProfileRequest, BudgetProfileService
from strategy_lab.services.budget_run import BudgetRunManager
from strategy_lab.services.budget_session import BudgetSessionManager
from strategy_lab.services.budget_stage_attribution import BudgetStageAttributionRequest, BudgetStageAttributionService
from strategy_lab.services.data_split import DataSplitRequest, DataSplitService
from strategy_lab.services.data_service import DataService
from strategy_lab.services.image_review import ImageReviewRequest, ImageReviewService
from strategy_lab.services.market_profile import MarketProfileRequest, MarketProfileService
from strategy_lab.services.parameter_search import ParameterSearchRequest, ParameterSearchService
from strategy_lab.services.portfolio_data_split import PortfolioDataSplitRequest, PortfolioDataSplitService
from strategy_lab.services.portfolio_evaluation import PortfolioEvaluationRequest, PortfolioEvaluationService
from strategy_lab.services.portfolio_profile import PortfolioProfileRequest, PortfolioProfileService
from strategy_lab.services.portfolio_run import PortfolioRunManager
from strategy_lab.services.portfolio_signal_profile import PortfolioSignalProfileRequest, PortfolioSignalProfileService
from strategy_lab.services.signal_backtest import DEFAULT_STRATEGY, SignalBacktestEvaluator, SignalBacktestRequest
from strategy_lab.services.signal_run import SignalRunManager
from strategy_lab.services.signal_session import SignalSessionManager
from strategy_lab.services.stage_attribution import StageAttributionRequest, StageAttributionService
from strategy_lab.services.strategy_artifact import StrategyArtifactRequest, StrategyArtifactService


app = typer.Typer(help="Stock Strategy Lab 命令行工具。")
data_app = typer.Typer(help="数据获取与标准化工具。")
signal_app = typer.Typer(help="信号层策略与回测工具。")
budget_app = typer.Typer(help="预算层策略与组合配置工具。")
portfolio_app = typer.Typer(help="组合层融合策略工具。")
image_app = typer.Typer(help="图片理解工具。")
app.add_typer(data_app, name="data")
app.add_typer(signal_app, name="signal")
app.add_typer(budget_app, name="budget")
app.add_typer(portfolio_app, name="portfolio")
app.add_typer(image_app, name="image")
console = Console()


def _safe_console_text(text: str) -> str:
    encoding = sys.stdout.encoding or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    return text


def _parse_strategy_params(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    result: dict[str, Any] = {}
    if value.strip() in {"", "{}"}:
        return result
    for item in value.split(","):
        if "=" not in item:
            raise typer.BadParameter('params 必须是 JSON object，或 key=value 形式，例如：ma_window=20,band=0.01')
        key, raw = item.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        if not key:
            raise typer.BadParameter("params 中存在空参数名。")
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


@app.command("config")
def show_config() -> None:
    """显示当前项目配置。"""
    config = load_app_config()
    table = Table(title="Strategy Lab Config")
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("root_dir", str(config.root_dir))
    table.add_row("artifacts_dir", str(config.resolve_project_path(config.project.artifacts_dir)))
    table.add_row("experiments_dir", str(config.resolve_project_path(config.project.experiments_dir)))
    table.add_row(
        "strategy_registry_dir",
        str(config.resolve_project_path(config.project.strategy_registry_dir)),
    )
    table.add_row("timezone", config.project.default_timezone)
    console.print(table)


@app.command("init")
def init_project() -> None:
    """创建基础 artifacts 目录。"""
    config = load_app_config()
    manager = ArtifactManager(config)
    manager.ensure_base_dirs()
    console.print(f"Initialized artifacts at [bold]{manager.artifacts_dir}[/bold]")


@app.command("new-run")
def new_run(
    task: str = typer.Argument(..., help="自然语言任务描述。"),
    run_id: str | None = typer.Option(None, help="可选的显式 run id。"),
) -> None:
    """创建一个新的实验 run 目录。"""
    config = load_app_config()
    manager = ArtifactManager(config)
    paths = manager.create_experiment(task_text=task, run_id=run_id)
    result = {
        "run_id": paths.run_id,
        "experiment_dir": str(paths.experiment_dir),
        "data_dir": str(paths.data_dir),
        "profile_dir": str(paths.profile_dir),
        "attempts_dir": str(paths.attempts_dir),
        "task_json": str(paths.task_json),
    }
    console.print_json(json.dumps(result, ensure_ascii=False))


@app.command("new-attempt")
def new_attempt(
    run_id: str = typer.Argument(..., help="已有实验 run id。"),
    iteration: int = typer.Argument(..., help="尝试轮次编号。"),
) -> None:
    """在指定 run 下创建 attempt 目录。"""
    config = load_app_config()
    manager = ArtifactManager(config)
    attempt_dir = manager.create_attempt_dir(run_id=run_id, iteration=iteration)
    console.print(str(attempt_dir))


@data_app.command("csv")
def ingest_csv(
    csv_path: Path = typer.Argument(..., help="输入 CSV 文件。"),
    output_dir: Path = typer.Argument(..., help="输出的数据 artifact 目录。"),
    symbol: str | None = typer.Option(None, help="CSV 中没有 symbol 字段时指定证券代码。"),
    encoding: str | None = typer.Option(None, help="CSV 编码。不填则由 pandas 默认处理。"),
) -> None:
    """把 CSV OHLCV 文件标准化为 strategy-lab 数据产物。"""
    artifact = DataService().load_csv(
        csv_path=csv_path,
        output_dir=output_dir,
        symbol=symbol,
        encoding=encoding,
    )
    console.print_json(artifact.model_dump_json(indent=2))


@data_app.command("miniqmt")
def ingest_miniqmt(
    symbol: str = typer.Argument(..., help="证券代码，例如 600519.SH。"),
    start_date: str = typer.Argument(..., help="开始日期，YYYY-MM-DD。"),
    end_date: str = typer.Argument(..., help="结束日期，YYYY-MM-DD。"),
    output_dir: Path = typer.Argument(..., help="输出的数据 artifact 目录。"),
    period: str = typer.Option("1d", help="K 线周期，例如 1d、5m、1m。"),
    adjust: str = typer.Option("qfq", help="复权方式：qfq、hfq、none。"),
    no_download: bool = typer.Option(False, help="不先下载历史行情，直接读取本地缓存。"),
) -> None:
    """从 MiniQMT/xtdata 获取 OHLCV 并写出标准数据产物。"""
    artifact = DataService().load_miniqmt_ohlcv(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        output_dir=output_dir,
        period=period,
        adjust=adjust,
        download=not no_download,
    )
    console.print_json(artifact.model_dump_json(indent=2))


@budget_app.command("new-run")
def budget_new_run(
    pool_name: str = typer.Argument(..., help="资产池名称，例如 sector_etf_pool、macro_pool 或 custom。"),
    mode: str = typer.Option("standalone", help="预算层运行模式：standalone、signal_joint 或 final_verify。"),
    source_paths: str = typer.Option(..., help="逗号分隔的信号层结果目录或 signal run_state.json 路径。"),
    start_date: str | None = typer.Option(None, help="数据开始日期，YYYY-MM-DD。后续服务会在该范围内自行切分训练/验证/walk-forward。"),
    end_date: str | None = typer.Option(None, help="数据结束日期，YYYY-MM-DD。后续服务会在该范围内自行切分训练/验证/walk-forward。"),
    frequency: str = typer.Option("1d", help="数据频率，默认 1d。"),
    task: str | None = typer.Option(None, help="自然语言任务描述。"),
    task_name: str | None = typer.Option(None, help="可读任务名；不填则使用 budget_run_id。"),
    run_id: str | None = typer.Option(None, help="可选 budget_run_id；不填则系统按资产池和时间戳生成。"),
    benchmark: str | None = typer.Option(
        None,
        help="本次预算层评估主基准：equal_weight_rebalance、equal_weight_buy_hold、simple_momentum_topk 或 cash。",
    ),
    strategy_max_iterations: int | None = typer.Option(None, help="预算策略探索最大 attempt 数；不填读取 configs/budget.yaml。"),
    initial_cash: float | None = typer.Option(None, help="覆盖本次预算层回测初始资金。"),
    commission: float | None = typer.Option(None, help="覆盖本次预算层回测佣金比例。"),
    slippage_perc: float | None = typer.Option(None, help="覆盖本次预算层回测滑点比例。"),
    execution_price: str | None = typer.Option(None, help="覆盖撮合价格口径，例如 close。"),
    allow_short: bool | None = typer.Option(None, "--allow-short/--no-allow-short", help="覆盖是否允许做空。"),
    same_day_sell_cash_available_for_buy: bool | None = typer.Option(
        None,
        "--same-day-sell-cash/--no-same-day-sell-cash",
        help="覆盖当日卖出资金是否可用于当日买入。",
    ),
) -> None:
    """创建预算层任务目录和标准 budget_run_state.json。"""
    parsed_source_paths = [item.strip() for item in source_paths.split(",") if item.strip()]
    paths = BudgetRunManager().create_run(
        mode=mode,
        pool_name=pool_name,
        task_description=task,
        task_name=task_name,
        source_paths=parsed_source_paths,
        start_date=start_date,
        end_date=end_date,
        frequency=frequency,
        run_id=run_id,
        benchmark=benchmark,
        strategy_max_iterations=strategy_max_iterations,
        backtest_overrides={
            "initial_cash": initial_cash,
            "commission": commission,
            "slippage_perc": slippage_perc,
            "execution_price": execution_price,
            "allow_short": allow_short,
            "same_day_sell_cash_available_for_buy": same_day_sell_cash_available_for_buy,
        },
    )
    console.print_json(paths.model_dump_json(indent=2))


@portfolio_app.command("new-run")
def portfolio_new_run(
    source_budget_run_path: Path = typer.Argument(..., help="已完成预算层训练的 budget run 目录或 budget_run_state.json 路径。"),
    portfolio_run_id: str | None = typer.Option(None, help="可选 portfolio_run_id；不传则系统自动生成。"),
    task: str | None = typer.Option(None, "--task", "--task-description", help="自然语言任务描述；--task-description 是兼容别名。"),
) -> None:
    """创建组合层任务目录，并复制预算层和信号层最终源产物。"""
    try:
        paths = PortfolioRunManager().create_run(
            source_budget_run_path=source_budget_run_path,
            portfolio_run_id=portfolio_run_id,
            task_description=task,
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "source_budget_run_path": str(source_budget_run_path),
                    "suggestion": "请确认预算层 run 已经完成 final_selection，并且 final_selection.policy_config_path 指向有效预算策略文件。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(paths.model_dump_json(indent=2))


@portfolio_app.command("init-fusion-version")
def portfolio_init_fusion_version(
    portfolio_run_state_path: Path = typer.Argument(..., help="组合层 portfolio_run_state.json 路径。"),
    version_id: str = typer.Option(..., help="要初始化的组合层版本 ID，例如 v001_initial_fusion。"),
    summary: str | None = typer.Option(None, help="本版本初始化说明。"),
    version_role: str = typer.Option("candidate", help="版本角色，例如 initial_fusion 或 candidate。"),
    policy_name: str | None = typer.Option(None, help="可选：fusion_policy.py / fusion_policy_meta.json 中的 policy_name。"),
) -> None:
    """初始化组合层融合版本，自动复制预算层和信号层快照并生成 fusion_manifest。"""
    try:
        result = PortfolioRunManager().init_fusion_version(
            portfolio_run_state_path,
            version_id=version_id,
            summary=summary,
            version_role=version_role,
            policy_name=policy_name,
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "portfolio_run_state_path": str(portfolio_run_state_path),
                    "version_id": version_id,
                    "suggestion": "请确认已完成 portfolio new-run，且 source_artifacts 中存在预算层最终策略和信号层策略小文件。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@portfolio_app.command("split-data")
def portfolio_split_data(
    portfolio_run_state_path: Path = typer.Argument(..., help="组合层 portfolio_run_state.json 路径。"),
    panel_ohlcv_path: Path | None = typer.Option(None, help="可选：覆盖 panel_ohlcv.parquet 路径。不传则从 portfolio_run_state.json 读取。"),
    returns_wide_path: Path | None = typer.Option(None, help="可选：覆盖 returns_wide.parquet 路径。不传则从 portfolio_run_state.json 读取。"),
    output_dir: Path | None = typer.Option(None, help="可选：切分输出目录。不传则写入当前 portfolio run 的 data/splits。"),
    split_mode: str = typer.Option(
        "train-validation-walk-forward",
        help="切分模式：full-only 或 train-validation-walk-forward。",
    ),
    train_ratio: float = typer.Option(0.70, help="全样本 train/validation 切分中的训练日期比例。"),
    fold_count: int = typer.Option(3, help="walk-forward fold 数量。"),
    fold_train_ratio: float = typer.Option(0.60, help="walk-forward 中每个 fold 的最小训练窗口日期比例。"),
    fold_validation_ratio: float = typer.Option(0.20, help="walk-forward 中每个 fold 的验证窗口日期比例。"),
    min_train_dates: int = typer.Option(120, help="每个训练窗口最少交易日数量。"),
    min_validation_dates: int = typer.Option(40, help="每个验证窗口最少交易日数量。"),
) -> None:
    """基于组合层统一行情面板生成 full-only 或 train/validation/walk-forward 切分。"""
    try:
        result = PortfolioDataSplitService().run(
            PortfolioDataSplitRequest(
                portfolio_run_state_path=portfolio_run_state_path,
                panel_ohlcv_path=panel_ohlcv_path,
                returns_wide_path=returns_wide_path,
                output_dir=output_dir,
                split_mode=split_mode,
                train_ratio=train_ratio,
                fold_count=fold_count,
                fold_train_ratio=fold_train_ratio,
                fold_validation_ratio=fold_validation_ratio,
                min_train_dates=min_train_dates,
                min_validation_dates=min_validation_dates,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "portfolio_run_state_path": str(portfolio_run_state_path),
                    "suggestion": "请先用 DataAgent 获取或整理组合层 panel_ohlcv.parquet 和 returns_wide.parquet，并显式传入路径，或写入 portfolio_run_state.json 的 data.panel_ohlcv / data.returns_wide。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(result.model_dump_json(indent=2))


@portfolio_app.command("profile")
def portfolio_profile(
    portfolio_run_state_path: Path = typer.Argument(..., help="组合层 portfolio_run_state.json 路径。"),
    split_manifest_path: Path | None = typer.Option(None, help="可选：指定 split_manifest.json；不传则读取 portfolio_run_state.json。"),
    output_dir: Path | None = typer.Option(None, help="可选：画像输出目录；不传则写入当前 portfolio run 的 profile。"),
    generate_charts: bool = typer.Option(True, "--chart/--no-chart", help="是否生成画像图表。"),
) -> None:
    """生成组合层画像，供 PortfolioAgent 编写第一版 fusion policy 前阅读。"""
    try:
        result = PortfolioProfileService().run(
            PortfolioProfileRequest(
                portfolio_run_state_path=portfolio_run_state_path,
                split_manifest_path=split_manifest_path,
                output_dir=output_dir,
                generate_charts=generate_charts,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "portfolio_run_state_path": str(portfolio_run_state_path),
                    "suggestion": "请确认已完成 portfolio new-run、DataAgent 数据准备和 portfolio split-data，并且 source_artifacts 中存在预算层和信号层最终策略文件。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(result.model_dump_json(indent=2))


@portfolio_app.command("signal-profile")
def portfolio_signal_profile(
    portfolio_run_state_path: Path = typer.Argument(..., help="组合层 portfolio_run_state.json 路径。"),
    output_dir: Path | None = typer.Option(None, help="可选：信号画像输出目录；不传则写入当前 portfolio run 的 signal_profiles。"),
    use_llm: bool = typer.Option(True, "--llm/--no-llm", help="是否调用大模型提炼信号层策略语义。"),
    max_memory_chars: int = typer.Option(0, help="每个资产读取 signal_agent_memory.md 的最大字符数；0 表示不截断。"),
    max_workers: int = typer.Option(1, help="单资产信号画像并发 worker 数；默认 1，建议真实 LLM 调用时可用 4。"),
    symbols: str | None = typer.Option(None, help="可选：逗号分隔的资产代码过滤器，主要用于调试真实 LLM 调用，例如 512800.SH。"),
) -> None:
    """生成组合层信号画像，供 PortfolioAgent 编写 fusion policy 前阅读。"""
    try:
        result = PortfolioSignalProfileService().run(
            PortfolioSignalProfileRequest(
                portfolio_run_state_path=portfolio_run_state_path,
                output_dir=output_dir,
                use_llm=use_llm,
                max_memory_chars=max_memory_chars,
                max_workers=max_workers,
                symbols=[item.strip() for item in symbols.split(",") if item.strip()] if symbols else None,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "portfolio_run_state_path": str(portfolio_run_state_path),
                    "suggestion": "请先完成 portfolio profile，确保 profile/daily_signal_targets.parquet 已生成，且 source_artifacts.signals.items 中存在信号层小文件。若只是调试，可加 --no-llm 跳过大模型语义提取。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(result.model_dump_json(indent=2))


@portfolio_app.command("evaluate")
def portfolio_evaluate(
    portfolio_run_state_path: Path = typer.Argument(..., help="组合层 portfolio_run_state.json 路径。"),
    version_id_arg: str | None = typer.Argument(None, help="可选兼容写法：要评估的组合层版本 ID，例如 v001_initial_fusion。推荐使用 --version-id。"),
    version_id: str | None = typer.Option(None, help="要评估的组合层版本 ID，例如 v001_initial_fusion。"),
    split_manifest_path: Path | None = typer.Option(None, help="可选：指定 split_manifest.json；不传则读取 portfolio_run_state.json。"),
    output_dir: Path | None = typer.Option(None, help="可选：评估输出目录；不传则写入版本目录 evaluation。"),
    benchmark: str | None = typer.Option(None, help="基准：equal_weight_rebalance、equal_weight_buy_hold、simple_momentum_topk 或 cash。"),
    initial_cash: float | None = typer.Option(None, help="可选：覆盖初始资金。"),
    commission: float | None = typer.Option(None, help="可选：覆盖佣金比例。"),
    slippage_perc: float | None = typer.Option(None, help="可选：覆盖滑点比例。"),
    generate_chart: bool = typer.Option(True, "--chart/--no-chart", help="是否生成策略与基准对比图。"),
) -> None:
    """执行组合层融合评估，并做组合回测。"""
    resolved_version_id = version_id or version_id_arg
    if not resolved_version_id:
        raise typer.BadParameter("必须通过 --version-id VERSION_ID 或位置参数传入要评估的组合层版本 ID。")
    try:
        result = PortfolioEvaluationService().run(
            PortfolioEvaluationRequest(
                portfolio_run_state_path=portfolio_run_state_path,
                version_id=resolved_version_id,
                split_manifest_path=split_manifest_path,
                output_dir=output_dir,
                benchmark=benchmark,
                initial_cash=initial_cash,
                commission=commission,
                slippage_perc=slippage_perc,
                generate_chart=generate_chart,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "portfolio_run_state_path": str(portfolio_run_state_path),
                    "version_id": resolved_version_id,
                    "suggestion": "请确认已完成 portfolio new-run、portfolio split-data，并且已创建指定 version 的 fusion_manifest.json、fusion_policy.py、预算策略和信号策略文件。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(result.model_dump_json(indent=2))


@portfolio_app.command("final-select")
def portfolio_final_select(
    portfolio_run_state_path: Path = typer.Argument(..., help="组合层 portfolio_run_state.json 路径。"),
    version_id: str = typer.Argument(..., help="选择为最终版的组合层 version_id。"),
    reason: str = typer.Option(..., help="最终选择理由。"),
    selection_report_path: Path | None = typer.Option(None, help="可选：最终选择报告输出路径；不传则自动写入 reports 目录。"),
) -> None:
    """登记组合层最终版本，并复制标准化最终文件到 final/ 目录。"""
    try:
        result = PortfolioRunManager().update_final_selection(
            portfolio_run_state_path,
            version_id=version_id,
            reason=reason,
            selection_report_path=selection_report_path,
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "portfolio_run_state_path": str(portfolio_run_state_path),
                    "version_id": version_id,
                    "suggestion": "请确认 version_id 存在，且该版本的 fusion_manifest、budget_policy、signal_strategies 等文件完整。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@portfolio_app.command("register-run-report")
def portfolio_register_run_report(
    portfolio_run_state_path: Path = typer.Argument(..., help="组合层 portfolio_run_state.json 路径。"),
    report_key: str = typer.Argument(..., help="报告登记键名。"),
    report_path: str = typer.Argument(..., help="报告主文件路径。"),
    report_type: str = typer.Option(..., help="报告类型，例如 portfolio_version_comparison。"),
    summary: str | None = typer.Option(None, help="报告摘要。"),
    extra_json: str | None = typer.Option(None, help="可选：JSON 字符串，登记额外元数据。"),
    extra_json_file: Path | None = typer.Option(None, help="可选：从 JSON 文件读取额外元数据。"),
) -> None:
    """登记组合层 run 级报告，例如多版本横向比较报告。"""
    extra: dict[str, Any] = {}
    if extra_json_file is not None:
        extra = json.loads(extra_json_file.read_text(encoding="utf-8-sig"))
    elif extra_json:
        extra = json.loads(extra_json)
    result = PortfolioRunManager().register_run_report(
        portfolio_run_state_path,
        report_key=report_key,
        report_path=report_path,
        report_type=report_type,
        summary=summary,
        extra=extra,
    )
    console.print_json(
        json.dumps(
            {
                "portfolio_run_state_path": str(portfolio_run_state_path),
                "report_key": report_key,
                "registered": result.get("artifacts", {}).get("run_reports", {}).get(report_key),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@portfolio_app.command("agent")
def portfolio_agent(
    request: str = typer.Argument(..., help="自然语言组合层主控任务。"),
    trace_path: Path | None = typer.Option(None, help="可选：将 PortfolioAgent 最终返回写入 JSON trace。"),
    max_iterations: int | None = typer.Option(None, help="可选：覆盖 DeepAgent 最大递归/迭代限制。"),
) -> None:
    """启动 DeepAgents 版 PortfolioAgent 处理一次性组合层主控任务。"""
    result = PortfolioAgent().run(
        request=request,
        trace_path=trace_path,
        max_iterations=max_iterations,
    )
    messages = result.raw_result.get("messages", []) if isinstance(result.raw_result, dict) else []
    final_message = ""
    for message in reversed(messages):
        content = getattr(message, "content", None) if not isinstance(message, dict) else message.get("content")
        if content:
            final_message = str(content)
            break
    payload = {
        "status": "success" if final_message else "partial",
        "trace_path": str(result.trace_path) if result.trace_path else None,
        "final_message": final_message,
    }
    console.print_json(_safe_console_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str)))


@portfolio_app.command("critic")
def portfolio_critic_agent(
    request: str = typer.Argument(..., help="自然语言组合层复盘任务。"),
    trace_path: Path | None = typer.Option(None, help="可选：将 PortfolioCriticAgent 最终返回写入 JSON trace。"),
    max_iterations: int | None = typer.Option(None, help="可选：覆盖 DeepAgent 最大递归/迭代限制。"),
) -> None:
    """启动 PortfolioCriticAgent 处理一次组合层复盘任务。"""
    result = PortfolioCriticAgent().run(
        request=request,
        trace_path=trace_path,
        max_iterations=max_iterations,
    )
    messages = result.raw_result.get("messages", []) if isinstance(result.raw_result, dict) else []
    final_message = ""
    for message in reversed(messages):
        content = getattr(message, "content", None) if not isinstance(message, dict) else message.get("content")
        if content:
            final_message = str(content)
            break
    payload = {
        "status": "success" if final_message else "partial",
        "trace_path": str(result.trace_path) if result.trace_path else None,
        "workspace_dir": str(result.workspace_dir),
        "final_message": final_message,
    }
    console.print_json(_safe_console_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str)))


@portfolio_app.command("chat")
def portfolio_chat(
    max_iterations: int | None = typer.Option(None, help="可选：覆盖 DeepAgent 最大递归/迭代限制。"),
    thread_id: str | None = typer.Option(None, help="可选：恢复指定 thread_id。"),
    resume_latest: bool = typer.Option(False, "--resume-latest", help="恢复最近一次 PortfolioAgent 会话。"),
    no_persist: bool = typer.Option(False, "--no-persist", help="不使用 SQLite 持久化会话。"),
) -> None:
    """启动 PortfolioAgent 交互式终端。"""
    run_portfolio_chat_loop(
        max_iterations=max_iterations,
        thread_id=thread_id,
        resume_latest=resume_latest,
        persist=not no_persist,
    )


@budget_app.command("data-panel")
def budget_data_panel(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    output_dir: Path | None = typer.Option(None, help="可选：输出目录。不传则写入本次 budget run 的 data 目录。"),
    allow_pending_missing_data: bool = typer.Option(
        False,
        "--allow-pending-missing-data",
        help="允许在 data_panel.missing_data 尚未补齐时生成临时面板。",
    ),
    include_supplemental: bool = typer.Option(
        True,
        "--include-supplemental/--no-include-supplemental",
        help="是否读取 data/supplemental/{symbol} 下的补充数据。",
    ),
    min_rows_per_asset: int = typer.Option(20, help="每个资产最少有效行数；低于该值会记录警告。"),
) -> None:
    """汇总信号层 primary_dataset 和补充数据，生成预算层多资产行情面板。"""
    try:
        result = BudgetDataPanelService().run(
            BudgetDataPanelRequest(
                budget_run_state_path=budget_run_state_path,
                output_dir=output_dir,
                allow_pending_missing_data=allow_pending_missing_data,
                include_supplemental=include_supplemental,
                min_rows_per_asset=min_rows_per_asset,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "budget_run_state_path": str(budget_run_state_path),
                    "suggestion": "若 data_panel.missing_data 未补齐，请先调用 DataAgent 补数据；临时分析可加 --allow-pending-missing-data。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(result.model_dump_json(indent=2))


@budget_app.command("split-data")
def budget_split_data(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    panel_ohlcv_path: Path | None = typer.Option(None, help="可选：覆盖 panel_ohlcv.parquet 路径。不传则从 budget_run_state.json 读取。"),
    returns_wide_path: Path | None = typer.Option(None, help="可选：覆盖 returns_wide.parquet 路径。不传则从 budget_run_state.json 读取。"),
    output_dir: Path | None = typer.Option(None, help="可选：切分输出目录。不传则写入当前 budget run 的 data/splits。"),
    train_ratio: float = typer.Option(0.70, help="全样本 train/validation 切分中的训练日期比例。"),
    fold_count: int = typer.Option(3, help="walk-forward fold 数量。"),
    fold_train_ratio: float = typer.Option(0.60, help="walk-forward 中每个 fold 的最小训练窗口日期比例。"),
    fold_validation_ratio: float = typer.Option(0.20, help="walk-forward 中每个 fold 的验证窗口日期比例。"),
    min_train_dates: int = typer.Option(120, help="每个训练窗口最少交易日数量。"),
    min_validation_dates: int = typer.Option(40, help="每个验证窗口最少交易日数量。"),
) -> None:
    """基于预算层统一行情面板生成 train、validation 和 walk-forward 切分。"""
    try:
        result = BudgetDataSplitService().run(
            BudgetDataSplitRequest(
                budget_run_state_path=budget_run_state_path,
                panel_ohlcv_path=panel_ohlcv_path,
                returns_wide_path=returns_wide_path,
                output_dir=output_dir,
                train_ratio=train_ratio,
                fold_count=fold_count,
                fold_train_ratio=fold_train_ratio,
                fold_validation_ratio=fold_validation_ratio,
                min_train_dates=min_train_dates,
                min_validation_dates=min_validation_dates,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "budget_run_state_path": str(budget_run_state_path),
                    "suggestion": "请先确认 budget-data-panel 已成功生成 panel_ohlcv.parquet 和 returns_wide.parquet。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(result.model_dump_json(indent=2))


@budget_app.command("profile")
def budget_profile(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    output_dir: Path | None = typer.Option(None, help="可选：画像输出目录。不传则写入当前 budget run 的 profile 目录。"),
    rolling_window: int = typer.Option(60, help="滚动波动、滚动相关性窗口交易日数。"),
    min_segment_days: int = typer.Option(40, help="资产池阶段划分的最短自然日数量。"),
    top_n: int = typer.Option(5, help="图表中展示的 Top/Bottom 资产数量。"),
    generate_charts: bool = typer.Option(True, "--charts/--no-charts", help="是否生成画像图表。"),
) -> None:
    """生成预算层资产池画像，并在第一步补全资产简称、类型、市场等元数据。"""
    try:
        result = BudgetProfileService().run(
            BudgetProfileRequest(
                budget_run_state_path=budget_run_state_path,
                output_dir=output_dir,
                rolling_window=rolling_window,
                min_segment_days=min_segment_days,
                top_n=top_n,
                generate_charts=generate_charts,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "budget_run_state_path": str(budget_run_state_path),
                    "suggestion": "请先确认 budget-data-panel 和 budget-data-split 已完成，并且 panel/returns 文件存在。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(result.model_dump_json(indent=2))


@budget_app.command("run-policy")
def budget_run_policy(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    policy_config_path: Path = typer.Argument(..., help="budget_policy_config.json 路径。"),
    output_dir: Path | None = typer.Option(None, help="可选：策略执行输出目录。不传则写入 policies/executions/{policy_id}。"),
    panel_ohlcv_path: Path | None = typer.Option(None, help="可选：覆盖 panel_ohlcv.parquet 路径。"),
    returns_wide_path: Path | None = typer.Option(None, help="可选：覆盖 returns_wide.parquet 路径。"),
    policy_id: str | None = typer.Option(None, help="可选：本次策略执行 ID。不传则按策略名和时间戳生成。"),
    update_run_state: bool = typer.Option(True, "--update-run-state/--no-update-run-state", help="是否把执行产物登记到 budget_run_state.json。"),
) -> None:
    """执行结构化预算策略配置，生成每日预算权重和诊断产物。"""
    try:
        result = BudgetPolicyEngine().run(
            BudgetPolicyEngineRequest(
                budget_run_state_path=budget_run_state_path,
                policy_config_path=policy_config_path,
                output_dir=output_dir,
                panel_ohlcv_path=panel_ohlcv_path,
                returns_wide_path=returns_wide_path,
                policy_id=policy_id,
                update_run_state=update_run_state,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "budget_run_state_path": str(budget_run_state_path),
                    "policy_config_path": str(policy_config_path),
                    "suggestion": "请确认 budget-data-panel 已完成，且 budget_policy_config.json 符合 budget-policy-authoring 规范。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(result.model_dump_json(indent=2))


@budget_app.command("backtest-policy")
def budget_backtest_policy(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    weights_path: Path | None = typer.Option(None, help="daily_budget_weights.parquet 路径。"),
    policy_execution_manifest_path: Path | None = typer.Option(None, help="policy_execution_manifest.json 路径；传入后可自动读取权重文件。"),
    returns_wide_path: Path | None = typer.Option(None, help="可选：覆盖 returns_wide.parquet 路径。"),
    output_dir: Path | None = typer.Option(None, help="可选：回测输出目录。不传则写入 policies/backtests/{backtest_id}。"),
    backtest_id: str | None = typer.Option(None, help="可选：本次回测 ID。"),
    benchmark: str | None = typer.Option(None, help="可选：基准，默认读取 budget_run_state.json 的 task.benchmark。"),
    initial_cash: float | None = typer.Option(None, help="可选：覆盖初始资金。"),
    commission: float | None = typer.Option(None, help="可选：覆盖佣金比例。"),
    slippage_perc: float | None = typer.Option(None, help="可选：覆盖滑点比例。"),
    update_run_state: bool = typer.Option(True, "--update-run-state/--no-update-run-state", help="是否把回测产物登记到 budget_run_state.json。"),
    generate_chart: bool = typer.Option(True, "--chart/--no-chart", help="是否生成策略与基准对比图。"),
) -> None:
    """对每日预算权重做组合回测，生成净值、订单、指标和基准对比。"""
    try:
        result = BudgetBacktestService().run(
            BudgetBacktestRequest(
                budget_run_state_path=budget_run_state_path,
                weights_path=weights_path,
                policy_execution_manifest_path=policy_execution_manifest_path,
                returns_wide_path=returns_wide_path,
                output_dir=output_dir,
                backtest_id=backtest_id,
                benchmark=benchmark,
                initial_cash=initial_cash,
                commission=commission,
                slippage_perc=slippage_perc,
                update_run_state=update_run_state,
                generate_chart=generate_chart,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "budget_run_state_path": str(budget_run_state_path),
                    "weights_path": str(weights_path) if weights_path else None,
                    "policy_execution_manifest_path": str(policy_execution_manifest_path) if policy_execution_manifest_path else None,
                    "suggestion": "请先运行 budget run-policy 生成 daily_budget_weights.parquet，再执行预算层组合回测。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(result.model_dump_json(indent=2))


@budget_app.command("search-policy")
def budget_search_policy(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    policy_config_path: Path = typer.Argument(..., help="基础 budget_policy_config.json 路径。"),
    param_space_path: Path = typer.Argument(..., help="param_space.json 路径。"),
    output_dir: Path | None = typer.Option(None, help="可选：参数搜索输出目录。"),
    search_id: str | None = typer.Option(None, help="可选：本次搜索 ID。"),
    data_split_manifest_path: Path | None = typer.Option(None, help="可选：预算层 split_manifest.json 路径。"),
    search_method: str = typer.Option("ga", help="搜索方法：grid、random 或 ga。"),
    max_candidates: int = typer.Option(30, help="最大候选参数数量。"),
    population_size: int = typer.Option(10, help="GA 初始种群数量。"),
    generations: int = typer.Option(5, help="GA 迭代代数。"),
    mutation_rate: float = typer.Option(0.20, help="GA 变异概率。"),
    ga_patience: int = typer.Option(3, help="GA 连续多少代没有提升后提前停止。"),
    min_improvement: float = typer.Option(1e-6, help="判断有效提升的最小分数变化。"),
    max_workers: int = typer.Option(1, help="候选参数并行 worker 数。"),
    cache_enabled: bool = typer.Option(True, "--cache/--no-cache", help="是否启用候选参数缓存。"),
    random_seed: int = typer.Option(42, help="随机种子。"),
    benchmark: str | None = typer.Option(None, help="可选：覆盖基准。"),
    update_run_state: bool = typer.Option(True, "--update-run-state/--no-update-run-state", help="是否把搜索结果登记到 budget_run_state.json。"),
    generate_chart: bool = typer.Option(False, "--chart/--no-chart", help="是否为候选回测生成图表；默认关闭以提升速度。"),
) -> None:
    """对预算层结构化策略配置进行参数搜索。"""
    try:
        result = BudgetParameterSearchService().run(
            BudgetParameterSearchRequest(
                budget_run_state_path=budget_run_state_path,
                policy_config_path=policy_config_path,
                param_space_path=param_space_path,
                output_dir=output_dir,
                search_id=search_id,
                data_split_manifest_path=data_split_manifest_path,
                search_method=search_method,
                max_candidates=max_candidates,
                population_size=population_size,
                generations=generations,
                mutation_rate=mutation_rate,
                ga_patience=ga_patience,
                min_improvement=min_improvement,
                max_workers=max_workers,
                cache_enabled=cache_enabled,
                random_seed=random_seed,
                benchmark=benchmark,
                update_run_state=update_run_state,
                generate_chart=generate_chart,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "budget_run_state_path": str(budget_run_state_path),
                    "policy_config_path": str(policy_config_path),
                    "param_space_path": str(param_space_path),
                    "suggestion": "请确认 budget-data-split 已完成，且 param_space.json 的参数路径能对应到 budget_policy_config.json。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(result.model_dump_json(indent=2))


@budget_app.command("summarize-search")
def budget_summarize_search(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    search_id: str | None = typer.Option(None, help="可选：参数搜索 ID。"),
    search_result_path: Path | None = typer.Option(None, help="可选：search_result.json 路径；传入后可不传 search_id。"),
    output_json_path: Path | None = typer.Option(None, help="可选：attempt_summary.json 输出路径。"),
    output_md_path: Path | None = typer.Option(None, help="可选：attempt_summary.md 输出路径。"),
    update_run_state: bool = typer.Option(True, "--update-run-state/--no-update-run-state", help="是否登记到 budget_run_state.json。"),
) -> None:
    """汇总预算层参数搜索、最佳回测和 walk-forward 结果，生成复盘输入摘要。"""
    try:
        result = BudgetAttemptSummaryService().run(
            BudgetAttemptSummaryRequest(
                budget_run_state_path=budget_run_state_path,
                search_id=search_id,
                search_result_path=search_result_path,
                output_json_path=output_json_path,
                output_md_path=output_md_path,
                update_run_state=update_run_state,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "budget_run_state_path": str(budget_run_state_path),
                    "search_id": search_id,
                    "search_result_path": str(search_result_path) if search_result_path else None,
                    "suggestion": "请先确认 budget search-policy 已成功生成 search_result.json 和 best 目录。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    summary = result.summary or {}
    console.print_json(
        json.dumps(
            {
                "status": "success",
                "budget_run_state_path": str(result.budget_run_state_path),
                "search_id": result.search_id,
                "summary_json_path": str(result.summary_json_path),
                "summary_md_path": str(result.summary_md_path),
                "best_score": summary.get("optimization", {}).get("best_score"),
                "full_metrics": {
                    key: summary.get("backtests", {}).get("full", {}).get("metrics", {}).get(key)
                    for key in ["total_return", "annual_return", "sharpe", "max_drawdown", "excess_total_return"]
                },
                "validation_metrics": {
                    key: summary.get("backtests", {}).get("validation", {}).get("metrics", {}).get(key)
                    for key in ["total_return", "annual_return", "sharpe", "max_drawdown", "excess_total_return"]
                },
                "walk_forward": {
                    key: summary.get("optimization", {}).get("walk_forward", {}).get(key)
                    for key in ["fold_count", "mean_score", "std_score", "min_score", "max_score"]
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@budget_app.command("stage-attribution")
def budget_stage_attribution(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    search_id: str | None = typer.Option(None, help="可选：参数搜索 ID。"),
    search_result_path: Path | None = typer.Option(None, help="可选：search_result.json 路径；传入后可不传 search_id。"),
    profile_path: Path | None = typer.Option(None, help="可选：覆盖预算层 budget_profile.json 路径。"),
    output_dir: Path | None = typer.Option(None, help="可选：阶段归因输出目录。"),
    generate_chart: bool = typer.Option(True, "--chart/--no-chart", help="是否生成阶段归因图。"),
    update_run_state: bool = typer.Option(True, "--update-run-state/--no-update-run-state", help="是否登记到 budget_run_state.json。"),
) -> None:
    """按预算层市场阶段拆解最佳策略表现、交易、持仓和基准差异。"""
    try:
        result = BudgetStageAttributionService().run(
            BudgetStageAttributionRequest(
                budget_run_state_path=budget_run_state_path,
                search_id=search_id,
                search_result_path=search_result_path,
                profile_path=profile_path,
                output_dir=output_dir,
                generate_chart=generate_chart,
                update_run_state=update_run_state,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "budget_run_state_path": str(budget_run_state_path),
                    "search_id": search_id,
                    "search_result_path": str(search_result_path) if search_result_path else None,
                    "suggestion": "请先确认 budget profile 和 budget search-policy 已成功完成，且 best/full/backtest 目录存在。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    summary = result.summary or {}
    console.print_json(
        json.dumps(
            {
                "status": "success",
                "budget_run_state_path": str(result.budget_run_state_path),
                "search_id": result.search_id,
                "output_dir": str(result.output_dir),
                "json_path": str(result.json_path),
                "csv_path": str(result.csv_path),
                "markdown_path": str(result.markdown_path),
                "chart_path": str(result.chart_path) if result.chart_path else None,
                "stage_count": summary.get("stage_count"),
                "best_excess_stage": {
                    key: (summary.get("best_excess_stage") or {}).get(key)
                    for key in ["stage_id", "start", "end", "market_label", "strategy_return", "benchmark_return", "excess_return"]
                },
                "worst_excess_stage": {
                    key: (summary.get("worst_excess_stage") or {}).get(key)
                    for key in ["stage_id", "start", "end", "market_label", "strategy_return", "benchmark_return", "excess_return"]
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@budget_app.command("evaluate-policy")
def budget_evaluate_policy(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    policy_dir: Path | None = typer.Option(None, help="单个预算策略目录；目录内需包含 budget_policy_config.json 和 param_space.json。"),
    policy_config_path: Path | None = typer.Option(None, help="可选：budget_policy_config.json 路径。"),
    param_space_path: Path | None = typer.Option(None, help="可选：param_space.json 路径。"),
    policy_spec_path: Path | None = typer.Option(None, help="可选：budget_policy_spec.md 路径。"),
    policy_meta_path: Path | None = typer.Option(None, help="可选：budget_policy_meta.json 路径。"),
    policy_name: str | None = typer.Option(None, help="可选：策略名称。"),
    search_id: str | None = typer.Option(None, help="可选：本次评估 search_id。"),
    output_dir: Path | None = typer.Option(None, help="可选：参数搜索及评估产物输出目录。"),
    data_split_manifest_path: Path | None = typer.Option(None, help="可选：预算层 split_manifest.json 路径。"),
    search_method: str = typer.Option("ga", help="搜索方法：grid、random 或 ga。"),
    max_candidates: int = typer.Option(30, help="最大候选参数数量。"),
    population_size: int = typer.Option(10, help="GA 初始种群数量。"),
    generations: int = typer.Option(5, help="GA 迭代代数。"),
    mutation_rate: float = typer.Option(0.20, help="GA 变异概率。"),
    ga_patience: int = typer.Option(3, help="GA 连续多少代没有提升后提前停止。"),
    min_improvement: float = typer.Option(1e-6, help="判断有效提升的最小分数变化。"),
    max_workers: int = typer.Option(1, help="候选参数并行 worker 数。"),
    cache_enabled: bool = typer.Option(True, "--cache/--no-cache", help="是否启用候选参数缓存。"),
    random_seed: int = typer.Option(42, help="随机种子。"),
    benchmark: str | None = typer.Option(None, help="可选：覆盖预算回测基准。"),
    generate_chart: bool = typer.Option(False, "--chart/--no-chart", help="是否为候选回测生成图表；默认关闭以提速。"),
    stage_chart: bool = typer.Option(True, "--stage-chart/--no-stage-chart", help="是否生成阶段归因图。"),
    update_run_state: bool = typer.Option(True, "--update-run-state/--no-update-run-state", help="是否登记到 budget_run_state.json。"),
) -> None:
    """一键评估单个预算策略：参数搜索、摘要、阶段归因。"""
    try:
        result = BudgetPolicyEvaluationService().run(
            BudgetPolicyEvaluationRequest(
                budget_run_state_path=budget_run_state_path,
                policy_dir=policy_dir,
                policy_config_path=policy_config_path,
                param_space_path=param_space_path,
                policy_spec_path=policy_spec_path,
                policy_meta_path=policy_meta_path,
                policy_name=policy_name,
                search_id=search_id,
                output_dir=output_dir,
                data_split_manifest_path=data_split_manifest_path,
                search_method=search_method,
                max_candidates=max_candidates,
                population_size=population_size,
                generations=generations,
                mutation_rate=mutation_rate,
                ga_patience=ga_patience,
                min_improvement=min_improvement,
                max_workers=max_workers,
                cache_enabled=cache_enabled,
                random_seed=random_seed,
                benchmark=benchmark,
                generate_chart=generate_chart,
                stage_chart=stage_chart,
                update_run_state=update_run_state,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "budget_run_state_path": str(budget_run_state_path),
                    "policy_dir": str(policy_dir) if policy_dir else None,
                    "policy_config_path": str(policy_config_path) if policy_config_path else None,
                    "param_space_path": str(param_space_path) if param_space_path else None,
                    "suggestion": "请确认策略目录或参数包含 budget_policy_config.json 和 param_space.json，并且 budget-data-split 已完成。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    row = result.summary_row or {}
    console.print_json(
        json.dumps(
            {
                "status": "success",
                "budget_run_state_path": str(result.budget_run_state_path),
                "search_id": result.search_id,
                "policy_name": result.policy_name,
                "best_score": result.best_score,
                "best_policy_config_path": str(result.best_policy_config_path),
                "search_result_path": str(result.search_result_path),
                "attempt_summary_path": str(result.attempt_summary_path),
                "stage_attribution_path": str(result.stage_attribution_path),
                "stage_attribution_chart_path": str(result.stage_attribution_chart_path) if result.stage_attribution_chart_path else None,
                "full_metrics": {
                    key: row.get(key)
                    for key in ["full_total_return", "full_annual_return", "full_sharpe", "full_max_drawdown", "full_excess_total_return"]
                },
                "validation_metrics": {
                    key: row.get(key)
                    for key in ["validation_total_return", "validation_sharpe", "validation_max_drawdown", "validation_excess_total_return"]
                },
                "walk_forward": {
                    key: row.get(key)
                    for key in ["walk_forward_mean_score", "walk_forward_std_score", "walk_forward_min_score", "walk_forward_max_score"]
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@budget_app.command("batch-evaluate-policies")
def budget_batch_evaluate_policies(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    policies_dir: Path | None = typer.Option(None, help="包含多个预算策略子目录的目录；每个子目录需包含 budget_policy_config.json 和 param_space.json。"),
    policy_manifest_path: Path | None = typer.Option(None, help="可选：预算策略 manifest JSON。"),
    batch_id: str | None = typer.Option(None, help="可选：本次批量评估 ID。"),
    search_prefix: str = typer.Option("budget_policy", help="自动生成 search_id 时使用的前缀。"),
    data_split_manifest_path: Path | None = typer.Option(None, help="可选：预算层 split_manifest.json 路径。"),
    search_method: str = typer.Option("ga", help="搜索方法：grid、random 或 ga。"),
    max_candidates: int = typer.Option(30, help="每个策略最大候选参数数量。"),
    population_size: int = typer.Option(10, help="GA 初始种群数量。"),
    generations: int = typer.Option(5, help="GA 迭代代数。"),
    mutation_rate: float = typer.Option(0.20, help="GA 变异概率。"),
    ga_patience: int = typer.Option(3, help="GA 连续多少代没有提升后提前停止。"),
    min_improvement: float = typer.Option(1e-6, help="判断有效提升的最小分数变化。"),
    max_workers: int = typer.Option(1, help="单个策略内部候选参数并行 worker 数。"),
    batch_workers: int = typer.Option(1, help="多个预算策略并行评估 worker 数；首次建议 1。"),
    cache_enabled: bool = typer.Option(True, "--cache/--no-cache", help="是否启用候选参数缓存。"),
    random_seed: int = typer.Option(42, help="随机种子；批量中每个策略会自动加偏移。"),
    benchmark: str | None = typer.Option(None, help="可选：覆盖预算回测基准。"),
    generate_chart: bool = typer.Option(False, "--chart/--no-chart", help="是否为参数候选回测生成图表；默认关闭以提速。"),
    stage_chart: bool = typer.Option(True, "--stage-chart/--no-stage-chart", help="是否生成阶段归因图。"),
    output_dir: Path | None = typer.Option(None, help="批量汇总报告输出目录。"),
    update_run_state: bool = typer.Option(True, "--update-run-state/--no-update-run-state", help="是否登记到 budget_run_state.json。"),
) -> None:
    """批量评估多个预算策略配置，适用于首次多策略探索。"""
    try:
        result = BudgetBatchPolicyEvaluationService().run(
            BudgetBatchPolicyEvaluationRequest(
                budget_run_state_path=budget_run_state_path,
                policies_dir=policies_dir,
                policy_manifest_path=policy_manifest_path,
                batch_id=batch_id,
                search_prefix=search_prefix,
                data_split_manifest_path=data_split_manifest_path,
                search_method=search_method,
                max_candidates=max_candidates,
                population_size=population_size,
                generations=generations,
                mutation_rate=mutation_rate,
                ga_patience=ga_patience,
                min_improvement=min_improvement,
                max_workers=max_workers,
                batch_workers=batch_workers,
                cache_enabled=cache_enabled,
                random_seed=random_seed,
                benchmark=benchmark,
                generate_chart=generate_chart,
                stage_chart=stage_chart,
                output_dir=output_dir,
                update_run_state=update_run_state,
            )
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "budget_run_state_path": str(budget_run_state_path),
                    "policies_dir": str(policies_dir) if policies_dir else None,
                    "policy_manifest_path": str(policy_manifest_path) if policy_manifest_path else None,
                    "suggestion": "请确认每个策略目录包含 budget_policy_config.json 和 param_space.json，并且 budget-data-split 已完成。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(
        json.dumps(
            {
                "status": result.status,
                "budget_run_state_path": str(result.budget_run_state_path),
                "batch_id": result.batch_id,
                "output_dir": str(result.output_dir),
                "summary_json_path": str(result.summary_json_path),
                "summary_md_path": str(result.summary_md_path),
                "summary_csv_path": str(result.summary_csv_path),
                "attempted_count": result.attempted_count,
                "success_count": result.success_count,
                "failed_count": result.failed_count,
                "best": next((item for item in result.results if item.get("rank") == 1), None),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@budget_app.command("update-policy-review")
def budget_update_policy_review(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    search_id: str = typer.Argument(..., help="被复盘的预算策略 search_id。"),
    critic_review_path: str = typer.Option(..., help="budget_critic_review.json 路径。"),
    critic_review_md_path: str = typer.Option(..., help="budget_critic_review.md 路径。"),
    next_action_path: str | None = typer.Option(None, help="budget_next_action.json 路径。"),
    summary: str | None = typer.Option(None, help="复盘摘要。"),
    status: str = typer.Option("reviewed", help="复盘状态，默认 reviewed。"),
) -> None:
    """登记预算层单策略复盘结果。"""
    result = BudgetRunManager().update_policy_review(
        budget_run_state_path,
        search_id=search_id,
        critic_review_path=critic_review_path,
        critic_review_md_path=critic_review_md_path,
        next_action_path=next_action_path,
        summary=summary,
        status=status,
    )
    search = result.get("artifacts", {}).get("policies", {}).get("searches", {}).get(search_id, {})
    print(
        json.dumps(
            {
                "budget_run_state_path": str(budget_run_state_path),
                "search_id": search_id,
                "status": search.get("status"),
                "critic_review_path": search.get("critic_review_path"),
                "critic_review_md_path": search.get("critic_review_md_path"),
                "next_action_path": search.get("next_action_path"),
            },
            ensure_ascii=True,
            indent=2,
            default=str,
        )
    )


@budget_app.command("register-run-report")
def budget_register_run_report(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    report_key: str = typer.Argument(..., help="报告登记键名。"),
    report_path: str = typer.Argument(..., help="报告主文件路径。"),
    report_type: str = typer.Option(..., help="报告类型，例如 budget_critic_comparison。"),
    summary: str | None = typer.Option(None, help="报告摘要。"),
    extra_json: str | None = typer.Option(None, help="可选：JSON 字符串，登记额外元数据。"),
    extra_json_file: Path | None = typer.Option(None, help="可选：从 JSON 文件读取额外元数据。"),
) -> None:
    """登记预算层 run 级报告。"""
    extra: dict[str, Any] = {}
    if extra_json_file is not None:
        extra = json.loads(extra_json_file.read_text(encoding="utf-8-sig"))
    elif extra_json:
        extra = json.loads(extra_json)
    result = BudgetRunManager().register_run_report(
        budget_run_state_path,
        report_key=report_key,
        report_path=report_path,
        report_type=report_type,
        summary=summary,
        extra=extra,
    )
    print(
        json.dumps(
            {
                "budget_run_state_path": str(budget_run_state_path),
                "report": result.get("artifacts", {}).get("run_reports", {}).get(report_key, {}),
            },
            ensure_ascii=True,
            indent=2,
            default=str,
        )
    )


@budget_app.command("final-select")
def budget_final_select(
    budget_run_state_path: Path = typer.Argument(..., help="预算层 budget_run_state.json 路径。"),
    search_id: str = typer.Argument(..., help="最终选择的预算策略 search_id。"),
    reason: str | None = typer.Option(None, help="可选：最终选择理由。"),
    reason_file: Path | None = typer.Option(None, help="可选：从文件读取最终选择理由。"),
    selection_report_path: Path | None = typer.Option(None, help="可选：最终选择报告输出路径；不传则自动写入 reports 目录。"),
    report_key: str | None = typer.Option(None, help="可选：artifacts.run_reports 中使用的报告键名。"),
    score: float | None = typer.Option(None, help="可选：覆盖最终 best_score；不传则使用 search 记录里的 best_score。"),
) -> None:
    """登记预算层最终策略选择，并更新 final_selection、strategy_search 和 run 顶层状态。"""
    if reason_file is not None:
        actual_reason = reason_file.read_text(encoding="utf-8-sig").strip()
    else:
        actual_reason = reason or "BudgetAgent 根据预算层回测、复盘和横向比较结果选择该 search。"
    try:
        result = BudgetRunManager().update_final_selection(
            budget_run_state_path,
            search_id=search_id,
            reason=actual_reason,
            selection_report_path=selection_report_path,
            report_key=report_key,
            best_score=score,
            status="success",
        )
    except Exception as exc:  # noqa: BLE001
        console.print_json(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "budget_run_state_path": str(budget_run_state_path),
                    "search_id": search_id,
                    "suggestion": "请确认 search_id 已由 budget evaluate-policy 或 budget batch-evaluate-policies 登记到 artifacts.policies.searches。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    console.print_json(
        json.dumps(
            {
                "status": "success",
                "budget_run_state_path": str(budget_run_state_path),
                "search_id": search_id,
                "strategy_search": result.get("strategy_search", {}),
                "final_selection": result.get("final_selection", {}),
                "final_policy": result.get("artifacts", {}).get("policies", {}).get("final", {}),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@budget_app.command("agent")
def budget_agent(
    request: str = typer.Argument(..., help="自然语言预算层任务。"),
    trace_path: Path | None = typer.Option(None, help="可选：将 BudgetAgent 主代理执行 trace 写入 JSONL。"),
    max_iterations: int | None = typer.Option(None, help="可选：覆盖 DeepAgent 最大递归/迭代限制。"),
) -> None:
    """启动 DeepAgents 版 BudgetAgent 处理单轮预算层任务。"""
    agent_runner = BudgetAgent()
    agent = agent_runner.create_agent()
    final_state = stream_agent_turn(
        agent,
        request,
        label="BudgetAgent",
        max_iterations=max_iterations,
        trace_path=trace_path,
    )
    answer = last_ai_text(final_state)
    print(
        json.dumps(
            {
                "status": "success" if answer else "partial",
                "final_message": answer,
                "trace_path": str(trace_path) if trace_path else None,
            },
            ensure_ascii=True,
            indent=2,
            default=str,
        )
    )


@budget_app.command("chat")
def budget_agent_chat(
    max_iterations: int | None = typer.Option(None, help="可选：覆盖 DeepAgent 最大递归/迭代限制。"),
    thread_id: str | None = typer.Option(None, help="可选：恢复或创建指定 thread_id 的持久化会话。"),
    resume_latest: bool = typer.Option(False, "--resume-latest", help="恢复最近一次 BudgetAgent 持久化会话。"),
    persist: bool = typer.Option(True, "--persist/--no-persist", help="是否使用 SQLite checkpoint 持久化会话。"),
) -> None:
    """启动 BudgetAgent 交互式终端。"""
    run_budget_chat_loop(
        max_iterations=max_iterations,
        thread_id=thread_id,
        resume_latest=resume_latest,
        persist=persist,
    )


@budget_app.command("sessions")
def budget_chat_sessions(
    limit: int = typer.Option(20, help="最多显示多少个最近会话。"),
) -> None:
    """列出 BudgetAgent 持久化聊天会话。"""
    from rich.table import Table

    manager = BudgetSessionManager()
    table = Table(title="BudgetAgent chat sessions", expand=True)
    table.add_column("thread_id", no_wrap=True, overflow="ignore", min_width=32)
    table.add_column("updated")
    table.add_column("messages", justify="right")
    table.add_column("budget_run_state")
    table.add_column("title")
    sessions = manager.list_sessions(limit=limit)
    for session in sessions:
        table.add_row(
            session.thread_id,
            session.updated_at,
            str(session.message_count),
            session.current_run_state_path or "",
            session.title or "",
        )
    console.print(table)


@budget_app.command("critic")
def budget_critic_agent(
    request: str = typer.Argument(..., help="自然语言预算层复盘请求。"),
    trace_path: Path | None = typer.Option(None, help="可选：将 BudgetCriticAgent 最终返回写入 JSON trace。"),
    max_iterations: int | None = typer.Option(None, help="可选：覆盖 DeepAgent 最大递归/迭代限制。"),
) -> None:
    """启动 DeepAgents 版 BudgetCriticAgent 处理预算层复盘任务。"""
    result = BudgetCriticAgent().run(
        request=request,
        trace_path=trace_path,
        max_iterations=max_iterations,
    )
    messages = result.raw_result.get("messages", []) if isinstance(result.raw_result, dict) else []
    final_message = ""
    for message in reversed(messages):
        content = getattr(message, "content", None) if not isinstance(message, dict) else message.get("content")
        if content:
            final_message = str(content)
            break
    payload = {
        "status": "success" if final_message else "partial",
        "workspace_dir": str(result.workspace_dir),
        "trace_path": str(result.trace_path) if result.trace_path else None,
        "final_message": final_message,
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2, default=str))


@data_app.command("agent")
def data_agent(
    request: str = typer.Argument(..., help="自然语言数据请求。"),
    trace_path: Path | None = typer.Option(None, help="可选：把 DataAgent 最终返回写入 JSON trace。"),
    max_iterations: int | None = typer.Option(None, help="可选：覆盖 DeepAgent 最大递归/迭代限制。"),
) -> None:
    """启动 DeepAgents 版 DataAgent 处理开放式数据任务。"""
    result = DataAgent().run(
        request=request,
        trace_path=trace_path,
        max_iterations=max_iterations,
    )
    console.print_json(result.model_dump_json(indent=2))


@signal_app.command("new-run")
def signal_new_run(
    symbol: str = typer.Argument(..., help="目标资产代码，例如 000300.SH。"),
    start_date: str = typer.Option(..., help="数据开始日期，YYYY-MM-DD。"),
    end_date: str = typer.Option(..., help="数据结束日期，YYYY-MM-DD。"),
    asset_type: str = typer.Option("index", help="资产类型，例如 index、stock、etf。"),
    frequency: str = typer.Option("1d", help="数据频率，例如 1d、5m。"),
    task: str | None = typer.Option(None, help="任务描述；不填则自动生成。"),
    task_name: str | None = typer.Option(None, help="可读任务名；不填则使用 run_id。"),
    run_id: str | None = typer.Option(None, help="可选 run_id；不填则系统按标的和时间戳生成。"),
    initial_cash: float | None = typer.Option(None, help="覆盖本次任务初始资金。"),
    commission: float | None = typer.Option(None, help="覆盖本次任务佣金比例。"),
    slippage_perc: float | None = typer.Option(None, help="覆盖本次任务滑点比例。"),
    allow_short: bool | None = typer.Option(None, "--allow-short/--no-allow-short", help="覆盖本次任务是否允许做空。"),
    benchmark_symbol: str | None = typer.Option(None, help="覆盖本次任务基准标的。"),
    generate_report: bool | None = typer.Option(None, "--generate-report/--no-generate-report", help="覆盖本次任务是否默认生成报告。"),
    report_level: str | None = typer.Option(None, help="覆盖本次任务报告级别。"),
    strategy_max_iterations: int | None = typer.Option(None, help="本次策略探索最多允许生成和评估的 attempt 数；不填默认 20。"),
) -> None:
    """创建信号层任务目录和标准 run_state.json。"""
    paths = SignalRunManager().create_run(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        asset_type=asset_type,
        frequency=frequency,
        task_description=task,
        task_name=task_name,
        run_id=run_id,
        strategy_max_iterations=strategy_max_iterations,
        backtest_overrides={
            "initial_cash": initial_cash,
            "commission": commission,
            "slippage_perc": slippage_perc,
            "allow_short": allow_short,
            "benchmark_symbol": benchmark_symbol,
            "generate_report": generate_report,
            "report_level": report_level,
        },
    )
    console.print_json(paths.model_dump_json(indent=2))


@signal_app.command("new-attempt")
def signal_new_attempt(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    attempt_id: str | None = typer.Option(None, help="可选：手动指定 attempt_id；不填则自动生成 attempt_001、attempt_002 等。"),
) -> None:
    """在 signal run 下创建标准策略探索 attempt 目录。"""
    paths = SignalRunManager().create_attempt(
        run_state_path=run_state_path,
        attempt_id=attempt_id,
    )
    console.print_json(paths.model_dump_json(indent=2))


@signal_app.command("update-data-acquisition")
def signal_update_data_acquisition(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    status: str = typer.Option(..., help="数据获取状态：success、partial 或 failed。"),
    primary_dataset: str | None = typer.Option(None, help="最终主数据文件路径。"),
    dataset_manifest: str | None = typer.Option(None, help="dataset_manifest.json 路径。"),
    data_source: str | None = typer.Option(None, help="数据来源，例如 miniqmt、akshare、local_file。"),
    summary: str | None = typer.Option(None, help="数据获取摘要。"),
    error: str | None = typer.Option(None, help="失败或部分失败的错误信息。"),
    row_count: int | None = typer.Option(None, help="最终主数据行数。"),
    start_date: str | None = typer.Option(None, help="最终主数据开始日期。"),
    end_date: str | None = typer.Option(None, help="最终主数据结束日期。"),
    columns: str | None = typer.Option(None, help="逗号分隔字段列表，例如 symbol,datetime,open,high,low,close。"),
) -> None:
    """由 DataAgent 调用：统一更新 run_state.json 的数据获取状态。"""
    parsed_columns = [item.strip() for item in (columns or "").split(",") if item.strip()]
    result = SignalRunManager().update_data_acquisition(
        run_state_path,
        status=status,
        primary_dataset=primary_dataset,
        dataset_manifest=dataset_manifest,
        data_source=data_source,
        summary=summary,
        error=error,
        row_count=row_count,
        start_date=start_date,
        end_date=end_date,
        columns=parsed_columns or None,
    )
    console.print_json(
        json.dumps(
            {
                "run_state_path": str(run_state_path),
                "status": result.get("steps", {}).get("data_acquisition", {}).get("status"),
                "data_acquisition": result.get("steps", {}).get("data_acquisition", {}),
                "primary_dataset": result.get("artifacts", {}).get("datasets", {}).get("primary", {}),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@signal_app.command("update-critic-review")
def signal_update_critic_review(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    attempt_id: str = typer.Argument(..., help="已完成复盘的 attempt_id。"),
    critic_review_path: str = typer.Option(..., help="critic_review.json 路径。"),
    critic_review_md_path: str = typer.Option(..., help="critic_review.md 路径。"),
    next_action_path: str = typer.Option(..., help="next_action.json 路径。"),
    summary: str | None = typer.Option(None, help="复盘摘要。"),
    status: str = typer.Option("reviewed", help="attempt 状态，默认 reviewed。"),
) -> None:
    """由 CriticAgent 调用：统一登记单 attempt 复盘产物并更新 attempt 状态。"""
    result = SignalRunManager().update_critic_review(
        run_state_path,
        attempt_id=attempt_id,
        critic_review_path=critic_review_path,
        critic_review_md_path=critic_review_md_path,
        next_action_path=next_action_path,
        summary=summary,
        status=status,
    )
    attempt = next((item for item in result.get("attempts", []) if item.get("attempt_id") == attempt_id), {})
    console.print_json(
        json.dumps(
            {
                "run_state_path": str(run_state_path),
                "attempt_id": attempt_id,
                "status": attempt.get("status"),
                "critic_review_path": attempt.get("critic_review_path"),
                "critic_review_md_path": attempt.get("critic_review_md_path"),
                "next_action_path": attempt.get("next_action_path"),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@signal_app.command("register-run-report")
def signal_register_run_report(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    report_key: str = typer.Argument(..., help="报告登记键，例如 critic_comparison_20260508。"),
    report_path: str = typer.Argument(..., help="主报告路径。"),
    report_type: str = typer.Option("critic_comparison", help="报告类型。"),
    summary: str | None = typer.Option(None, help="报告摘要。"),
    extra_json: str = typer.Option("{}", help="可选 JSON 对象，登记额外路径或元数据。"),
    extra_json_file: Path | None = typer.Option(None, help="可选：从 JSON 文件读取额外路径或元数据，避免 PowerShell 引号转义问题。"),
) -> None:
    """登记 run 级报告路径，例如 CriticAgent 多 attempt 横向比较报告。"""
    try:
        raw_extra = extra_json_file.read_text(encoding="utf-8") if extra_json_file else extra_json
        extra = json.loads(raw_extra)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("--extra-json 或 --extra-json-file 必须是 JSON object。") from exc
    if not isinstance(extra, dict):
        raise typer.BadParameter("--extra-json 或 --extra-json-file 必须是 JSON object。")
    result = SignalRunManager().register_run_report(
        run_state_path,
        report_key=report_key,
        report_path=report_path,
        report_type=report_type,
        summary=summary,
        extra=extra,
    )
    console.print_json(
        json.dumps(
            {
                "run_state_path": str(run_state_path),
                "report": result.get("artifacts", {}).get("run_reports", {}).get(report_key, {}),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@signal_app.command("backtest")
def signal_backtest(
    data_path: Path = typer.Argument(..., help="OHLCV 数据文件，支持 parquet/csv/json/jsonl。"),
    output_dir: Path | None = typer.Option(None, help="回测产物输出目录；不填则自动写入 artifacts/signal_backtests。"),
    run_state_path: Path | None = typer.Option(None, help="可选：signal run 的 run_state.json；提供后默认读取其中的 backtest_config。"),
    strategy: str = typer.Option(DEFAULT_STRATEGY, help="策略引用，格式为 module:Class 或 path/to/file.py:Class。"),
    params: str = typer.Option("{}", help="策略参数 JSON 字符串。"),
    initial_cash: float | None = typer.Option(None, help="初始资金；不填使用 configs/backtest.yaml。"),
    commission: float | None = typer.Option(None, help="佣金比例；不填使用 configs/backtest.yaml。"),
    slippage_perc: float | None = typer.Option(None, help="滑点比例；不填使用 configs/backtest.yaml。"),
    allow_short: bool | None = typer.Option(None, help="是否允许做空；不填使用 configs/backtest.yaml。"),
    run_id: str | None = typer.Option(None, help="可选 run_id。"),
    quantstats_html: bool = typer.Option(False, help="是否额外生成 QuantStats HTML 报告。"),
    evaluation_start: str | None = typer.Option(None, help="可选：评估开始日期；此前数据只作为策略历史上下文。"),
    evaluation_end: str | None = typer.Option(None, help="可选：评估结束日期。"),
) -> None:
    """运行信号层策略回测并生成 metrics/report/artifacts。"""
    strategy_params = _parse_strategy_params(params)

    result = SignalBacktestEvaluator().run(
        SignalBacktestRequest(
            data_path=data_path,
            output_dir=output_dir,
            run_state_path=run_state_path,
            strategy=strategy,
            strategy_params=strategy_params,
            initial_cash=initial_cash,
            commission=commission,
            slippage_perc=slippage_perc,
            allow_short=allow_short,
            run_id=run_id,
            quantstats_html=quantstats_html,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
        )
    )
    console.print_json(result.model_dump_json(indent=2))


@signal_app.command("market-profile")
def signal_market_profile(
    data_path: Path | None = typer.Argument(None, help="可选：OHLCV 数据文件。若不传，则必须通过 --run-state-path 读取 primary_dataset。"),
    run_state_path: Path | None = typer.Option(None, help="可选：signal run 的 run_state.json。"),
    output_dir: Path | None = typer.Option(None, help="可选：画像输出目录；不填时，有 run_state_path 则写入 directories.market_profile，否则写入 artifacts/market_profiles。"),
    profile_id: str = typer.Option("market_profile", help="画像文件名前缀。"),
    smooth_window: int = typer.Option(5, help="自适应阶段划分的标签平滑窗口，单位为交易日。"),
    min_segment_days: int = typer.Option(10, help="自适应阶段划分的最短阶段长度，单位为交易日。"),
    output_format: str = typer.Option("both", help="输出格式：json、md 或 both。"),
    chart: bool = typer.Option(True, "--chart/--no-chart", help="是否生成走势与阶段划分 PNG 图像。"),
) -> None:
    """生成信号层市场画像，包含收益、回撤、趋势、波动、阶段划分和事实描述。"""
    result = MarketProfileService().run(
        MarketProfileRequest(
            data_path=data_path,
            run_state_path=run_state_path,
            output_dir=output_dir,
            profile_id=profile_id,
            smooth_window=smooth_window,
            min_segment_days=min_segment_days,
            output_format=output_format,
            generate_chart=chart,
        )
    )
    console.print_json(result.model_dump_json(indent=2))


@signal_app.command("split-data")
def signal_split_data(
    data_path: Path | None = typer.Argument(None, help="可选：源 OHLCV 数据文件。不传时从 --run-state-path 读取 primary_dataset。"),
    run_state_path: Path | None = typer.Option(None, help="可选：signal run 的 run_state.json。"),
    output_dir: Path | None = typer.Option(None, help="可选：切分产物输出目录。不传且有 run_state_path 时写入 directories.data/splits。"),
    train_ratio: float = typer.Option(0.70, help="全样本 train/validation 切分中的训练集比例。"),
    fold_count: int = typer.Option(3, help="walk-forward fold 数量。"),
    fold_train_ratio: float = typer.Option(0.60, help="每个 walk-forward fold 的最小训练窗口比例。"),
    fold_validation_ratio: float = typer.Option(0.20, help="每个 walk-forward fold 的验证窗口比例。"),
    min_train_rows: int = typer.Option(60, help="每个训练窗口最少行数。"),
    min_validation_rows: int = typer.Option(20, help="每个验证窗口最少行数。"),
) -> None:
    """按时间顺序生成 train、validation 和 walk-forward 数据切分。"""
    result = DataSplitService().run(
        DataSplitRequest(
            data_path=data_path,
            run_state_path=run_state_path,
            output_dir=output_dir,
            train_ratio=train_ratio,
            fold_count=fold_count,
            fold_train_ratio=fold_train_ratio,
            fold_validation_ratio=fold_validation_ratio,
            min_train_rows=min_train_rows,
            min_validation_rows=min_validation_rows,
        )
    )
    console.print_json(result.model_dump_json(indent=2))


@signal_app.command("save-strategy")
def signal_save_strategy(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    attempt_id: str = typer.Argument(..., help="目标 attempt_id。"),
    strategy_path: Path | None = typer.Option(None, help="可选：SignalAgent 已生成的策略脚本路径。"),
    strategy_spec_path: Path | None = typer.Option(None, help="可选：策略说明文件路径。"),
    param_space_path: Path | None = typer.Option(None, help="可选：参数空间 JSON 文件路径。"),
    strategy_meta_path: Path | None = typer.Option(None, help="可选：策略元数据 JSON 文件路径。"),
    template: str | None = typer.Option(None, help="可选：使用内置模板，例如 ma_crossover。"),
    strategy_name: str | None = typer.Option(None, help="可选：策略名称。"),
    strategy_class_name: str = typer.Option("Strategy", help="策略脚本中的策略类名。"),
) -> None:
    """保存并校验一个 attempt 的策略脚本、策略说明、参数空间和元数据。"""
    result = StrategyArtifactService().run(
        StrategyArtifactRequest(
            run_state_path=run_state_path,
            attempt_id=attempt_id,
            strategy_path=strategy_path,
            strategy_spec_path=strategy_spec_path,
            param_space_path=param_space_path,
            strategy_meta_path=strategy_meta_path,
            template=template,
            strategy_name=strategy_name,
            strategy_class_name=strategy_class_name,
        )
    )
    console.print_json(result.model_dump_json(indent=2))


@signal_app.command("search-params")
def signal_search_params(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    attempt_id: str = typer.Argument(..., help="目标 attempt_id。"),
    data_split_manifest_path: Path | None = typer.Option(None, help="可选：split_manifest.json 路径。不传时从 run_state 读取。"),
    search_method: str = typer.Option("ga", help="参数搜索方法：grid、random 或 ga。"),
    max_candidates: int = typer.Option(30, help="最多评估候选个体数量。"),
    population_size: int = typer.Option(10, help="GA 初始种群数量。"),
    generations: int = typer.Option(5, help="GA 迭代代数。"),
    mutation_rate: float = typer.Option(0.20, help="GA 变异概率。"),
    ga_patience: int = typer.Option(3, help="GA 连续多少代没有显著提升后提前停止。"),
    min_improvement: float = typer.Option(1e-6, help="GA 判断有效提升的最小分数变化。"),
    max_workers: int = typer.Option(1, help="候选参数并行回测 worker 数；1 表示串行。"),
    cache_enabled: bool = typer.Option(True, "--cache/--no-cache", help="是否启用候选参数评估缓存。"),
    random_seed: int = typer.Option(42, help="随机种子。"),
    quantstats_html: bool = typer.Option(False, help="是否为每次回测生成 QuantStats HTML。默认关闭。"),
) -> None:
    """对一个 attempt 的策略参数空间执行 grid/random/ga 搜索，并保存最佳回测结果。"""
    result = ParameterSearchService().run(
        ParameterSearchRequest(
            run_state_path=run_state_path,
            attempt_id=attempt_id,
            data_split_manifest_path=data_split_manifest_path,
            search_method=search_method,
            max_candidates=max_candidates,
            population_size=population_size,
            generations=generations,
            mutation_rate=mutation_rate,
            ga_patience=ga_patience,
            min_improvement=min_improvement,
            max_workers=max_workers,
            cache_enabled=cache_enabled,
            random_seed=random_seed,
            quantstats_html=quantstats_html,
        )
    )
    console.print_json(result.model_dump_json(indent=2))


@signal_app.command("evaluate-attempt")
def signal_evaluate_attempt(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    attempt_id: str | None = typer.Option(None, help="可选：目标 attempt_id；不传则自动生成。"),
    strategy_path: Path | None = typer.Option(None, help="SignalAgent 已生成的策略脚本路径。"),
    strategy_spec_path: Path | None = typer.Option(None, help="策略说明 Markdown 文件路径。"),
    param_space_path: Path | None = typer.Option(None, help="参数空间 JSON 文件路径。"),
    strategy_meta_path: Path | None = typer.Option(None, help="策略元数据 JSON 文件路径。"),
    template: str | None = typer.Option(None, help="可选：使用内置模板，例如 ma_crossover。"),
    strategy_name: str | None = typer.Option(None, help="可选：策略名称。"),
    strategy_class_name: str = typer.Option("Strategy", help="策略脚本中的策略类名。"),
    data_split_manifest_path: Path | None = typer.Option(None, help="可选：split_manifest.json 路径。不传时从 run_state 读取。"),
    search_method: str = typer.Option("ga", help="参数搜索方法：grid、random 或 ga。"),
    max_candidates: int = typer.Option(30, help="最多评估候选个体数量。"),
    population_size: int = typer.Option(10, help="GA 初始种群数量。"),
    generations: int = typer.Option(5, help="GA 迭代代数。"),
    mutation_rate: float = typer.Option(0.20, help="GA 变异概率。"),
    ga_patience: int = typer.Option(3, help="GA 连续多少代没有显著提升后提前停止。"),
    min_improvement: float = typer.Option(1e-6, help="GA 判断有效提升的最小分数变化。"),
    max_workers: int = typer.Option(1, help="候选参数并行回测 worker 数；1 表示串行。"),
    cache_enabled: bool = typer.Option(True, "--cache/--no-cache", help="是否启用候选参数评估缓存。"),
    random_seed: int = typer.Option(42, help="随机种子。"),
    quantstats_html: bool = typer.Option(False, help="是否为每次回测生成 QuantStats HTML。默认关闭。"),
    stage_chart: bool = typer.Option(True, "--stage-chart/--no-stage-chart", help="是否生成阶段归因图。"),
    output_format: str = typer.Option("summary", help="输出格式：summary 或 full。SignalAgent 调用时建议使用 summary。"),
) -> None:
    """SignalAgent 写好策略后，一键完成 attempt 评估和复盘前数据准备。"""
    try:
        result = AttemptEvaluationService().run(
            AttemptEvaluationRequest(
                run_state_path=run_state_path,
                attempt_id=attempt_id,
                strategy_path=strategy_path,
                strategy_spec_path=strategy_spec_path,
                param_space_path=param_space_path,
                strategy_meta_path=strategy_meta_path,
                template=template,
                strategy_name=strategy_name,
                strategy_class_name=strategy_class_name,
                data_split_manifest_path=data_split_manifest_path,
                search_method=search_method,
                max_candidates=max_candidates,
                population_size=population_size,
                generations=generations,
                mutation_rate=mutation_rate,
                ga_patience=ga_patience,
                min_improvement=min_improvement,
                max_workers=max_workers,
                cache_enabled=cache_enabled,
                random_seed=random_seed,
                quantstats_html=quantstats_html,
                stage_chart=stage_chart,
            )
        )
    except AttemptEvaluationError as exc:
        console.print_json(json.dumps(exc.payload, ensure_ascii=False, indent=2, default=str))
        raise typer.Exit(code=1) from exc
    if output_format.lower() == "full":
        console.print_json(result.model_dump_json(indent=2))
        return
    if output_format.lower() != "summary":
        raise typer.BadParameter("output-format 只能是 summary 或 full。")
    console.print_json(
        json.dumps(
            {
                "run_state_path": str(result.run_state_path),
                "attempt_id": result.attempt_id,
                **result.summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@signal_app.command("evaluate-attempts")
def signal_evaluate_attempts(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    strategies_dir: Path | None = typer.Option(None, help="包含多个策略子目录的目录；每个子目录需包含 strategy.py、strategy_spec.md、param_space.json、strategy_meta.json。"),
    strategy_manifest_path: Path | None = typer.Option(None, help="可选：策略清单 JSON 路径，数组或包含 strategies 数组的对象。"),
    attempt_prefix: str = typer.Option("attempt_alpha", help="未显式指定 attempt_id 时使用的前缀。"),
    data_split_manifest_path: Path | None = typer.Option(None, help="可选：split_manifest.json 路径。不传时从 run_state 读取。"),
    search_method: str = typer.Option("ga", help="参数搜索方法：grid、random 或 ga。"),
    max_candidates: int = typer.Option(30, help="每个策略最多评估候选参数数量。"),
    population_size: int = typer.Option(10, help="GA 初始种群数量。"),
    generations: int = typer.Option(5, help="GA 迭代代数。"),
    mutation_rate: float = typer.Option(0.20, help="GA 变异概率。"),
    ga_patience: int = typer.Option(3, help="GA 连续多少代没有显著提升后提前停止。"),
    min_improvement: float = typer.Option(1e-6, help="GA 判断有效提升的最小分数变化。"),
    max_workers: int = typer.Option(1, help="单个策略内部候选参数并行回测 worker 数。"),
    batch_workers: int = typer.Option(1, help="多个策略 attempt 并行评估 worker 数；首次建议 1-2。"),
    cache_enabled: bool = typer.Option(True, "--cache/--no-cache", help="是否启用候选参数评估缓存。"),
    random_seed: int = typer.Option(42, help="随机种子；批量中每个策略会自动加偏移。"),
    quantstats_html: bool = typer.Option(False, help="是否为每次回测生成 QuantStats HTML。默认关闭。"),
    stage_chart: bool = typer.Option(True, "--stage-chart/--no-stage-chart", help="是否生成阶段归因图。"),
    output_dir: Path | None = typer.Option(None, help="批量汇总报告输出目录；不传则写入 run reports 目录。"),
    output_format: str = typer.Option("summary", help="输出格式：summary 或 full。"),
) -> None:
    """批量评估多个策略 attempt，适用于阶段 1 Alpha 批量探索。"""
    result = BatchAttemptEvaluationService().run(
        BatchAttemptEvaluationRequest(
            run_state_path=run_state_path,
            strategies_dir=strategies_dir,
            strategy_manifest_path=strategy_manifest_path,
            attempt_prefix=attempt_prefix,
            data_split_manifest_path=data_split_manifest_path,
            search_method=search_method,
            max_candidates=max_candidates,
            population_size=population_size,
            generations=generations,
            mutation_rate=mutation_rate,
            ga_patience=ga_patience,
            min_improvement=min_improvement,
            max_workers=max_workers,
            batch_workers=batch_workers,
            cache_enabled=cache_enabled,
            random_seed=random_seed,
            quantstats_html=quantstats_html,
            stage_chart=stage_chart,
            output_dir=output_dir,
        )
    )
    if output_format.lower() == "full":
        console.print_json(result.model_dump_json(indent=2))
        return
    if output_format.lower() != "summary":
        raise typer.BadParameter("output-format 只能是 summary 或 full。")
    console.print_json(
        json.dumps(
            {
                "run_state_path": str(result.run_state_path),
                "status": result.status,
                "attempted_count": result.attempted_count,
                "success_count": result.success_count,
                "failed_count": result.failed_count,
                "summary_json_path": str(result.summary_json_path),
                "summary_md_path": str(result.summary_md_path),
                "results": result.results,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@signal_app.command("summarize-attempt")
def signal_summarize_attempt(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    attempt_id: str = typer.Argument(..., help="目标 attempt_id。"),
) -> None:
    """汇总一个 attempt 的策略、搜索和回测产物，生成 CriticAgent 输入摘要。"""
    result = AttemptSummaryService().run(
        AttemptSummaryRequest(
            run_state_path=run_state_path,
            attempt_id=attempt_id,
        )
    )
    console.print_json(result.model_dump_json(indent=2))


@signal_app.command("stage-attribution")
def signal_stage_attribution(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    attempt_id: str = typer.Argument(..., help="目标 attempt_id。"),
    output_dir: Path | None = typer.Option(None, help="可选：阶段归因输出目录。"),
    chart: bool = typer.Option(True, "--chart/--no-chart", help="是否生成阶段收益对比图。"),
) -> None:
    """按市场画像阶段归因单个 attempt 的策略表现。"""
    result = StageAttributionService().run(
        StageAttributionRequest(
            run_state_path=run_state_path,
            attempt_id=attempt_id,
            output_dir=output_dir,
            generate_chart=chart,
        )
    )
    console.print_json(result.model_dump_json(indent=2))


@signal_app.command("compare-attempts")
def signal_compare_attempts(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    attempt_ids: str = typer.Option("", help="逗号分隔的 attempt_id 列表；为空时比较所有可复盘 attempt。"),
    output_dir: Path | None = typer.Option(None, help="可选：横向比较输出目录。"),
    charts: bool = typer.Option(True, "--charts/--no-charts", help="是否生成横向比较图。"),
) -> None:
    """生成多个 attempt 的横向比较数据和图表。"""
    parsed_attempt_ids = [item.strip() for item in attempt_ids.split(",") if item.strip()]
    result = AttemptComparisonService().run(
        AttemptComparisonRequest(
            run_state_path=run_state_path,
            attempt_ids=parsed_attempt_ids,
            output_dir=output_dir,
            generate_charts=charts,
        )
    )
    console.print_json(result.model_dump_json(indent=2))


@signal_app.command("final-select")
def signal_final_select(
    run_state_path: Path = typer.Argument(..., help="signal run 的 run_state.json 路径。"),
    attempt_id: str = typer.Argument(..., help="最终选择的 attempt_id。"),
    strategy_path: Path | None = typer.Option(None, help="可选：最终策略文件路径；不传则使用 attempt 的 strategy/strategy.py。"),
    metrics_path: Path | None = typer.Option(None, help="可选：最终指标文件路径；不传则使用 attempt 的 backtests/full/metrics.json。"),
    reason: str | None = typer.Option(None, help="可选：最终选择理由。"),
    reason_file: Path | None = typer.Option(None, help="可选：从文件读取最终选择理由。"),
    score: float | None = typer.Option(None, help="可选：最终分数；不传则使用 run_state 中 attempt.score。"),
) -> None:
    """登记最终策略选择，并统一更新 final_selection、strategy_search 和 run 顶层状态。"""
    manager = SignalRunManager()
    state = manager.load_state(run_state_path)
    attempt = next((item for item in state.get("attempts", []) if item.get("attempt_id") == attempt_id), None)
    if attempt is None:
        raise typer.BadParameter(f"run_state.json 中不存在 attempt: {attempt_id}")

    actual_strategy_path = strategy_path or Path(str(attempt.get("strategy_dir", ""))) / "strategy.py"
    actual_metrics_path = metrics_path or Path(str(attempt.get("full_backtest_dir", ""))) / "metrics.json"
    if reason_file is not None:
        actual_reason = reason_file.read_text(encoding="utf-8").strip()
    else:
        actual_reason = reason or "SignalAgent 根据回测、复盘和横向比较结果选择该 attempt。"

    result = manager.update_final_selection(
        run_state_path,
        best_attempt_id=attempt_id,
        best_strategy_path=str(actual_strategy_path),
        best_metrics_path=str(actual_metrics_path),
        reason=actual_reason,
        best_score=score,
        status="success",
    )
    console.print_json(
        json.dumps(
            {
                "run_state_path": str(run_state_path),
                "selected_attempt_id": attempt_id,
                "selected_strategy_path": str(actual_strategy_path),
                "selected_metrics_path": str(actual_metrics_path),
                "status": result.get("status"),
                "strategy_search": result.get("steps", {}).get("strategy_search", {}),
                "final_selection": result.get("steps", {}).get("final_selection", {}),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@signal_app.command("critic")
def signal_critic_agent(
    request: str = typer.Argument(..., help="自然语言复盘请求。"),
    trace_path: Path | None = typer.Option(None, help="可选：将 CriticAgent 最终返回写入 JSON trace。"),
    max_iterations: int | None = typer.Option(None, help="可选：覆盖 DeepAgent 最大递归/迭代限制。"),
) -> None:
    """启动 DeepAgents 版 CriticAgent 处理策略复盘任务。"""
    result = CriticAgent().run(
        request=request,
        trace_path=trace_path,
        max_iterations=max_iterations,
    )
    console.print_json(result.model_dump_json(indent=2))


@signal_app.command("agent")
def signal_agent(
    request: str = typer.Argument(..., help="自然语言信号层任务。"),
    trace_path: Path | None = typer.Option(None, help="可选：将 SignalAgent 主代理执行 trace 写入 JSONL。"),
    max_iterations: int | None = typer.Option(None, help="可选：覆盖 DeepAgent 最大递归/迭代限制。"),
) -> None:
    """启动 DeepAgents 版 SignalAgent 处理单轮信号层任务。"""
    agent_runner = SignalAgent()
    agent = agent_runner.create_agent()
    final_state = stream_agent_turn(
        agent,
        request,
        label="SignalAgent",
        max_iterations=max_iterations,
        trace_path=trace_path,
    )
    answer = last_ai_text(final_state)
    console.print_json(
        json.dumps(
            {
                "status": "success" if answer else "partial",
                "final_message": answer,
                "trace_path": str(trace_path) if trace_path else None,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


@signal_app.command("chat")
def signal_agent_chat(
    max_iterations: int | None = typer.Option(None, help="可选：覆盖 DeepAgent 最大递归/迭代限制。"),
    thread_id: str | None = typer.Option(None, help="可选：恢复或创建指定 thread_id 的持久化会话。"),
    resume_latest: bool = typer.Option(False, "--resume-latest", help="恢复最近一次 SignalAgent 持久化会话。"),
    persist: bool = typer.Option(True, "--persist/--no-persist", help="是否使用 SQLite checkpoint 持久化会话。"),
) -> None:
    """启动 SignalAgent 交互式终端。"""
    run_chat_loop(
        max_iterations=max_iterations,
        thread_id=thread_id,
        resume_latest=resume_latest,
        persist=persist,
    )


@signal_app.command("sessions")
def signal_chat_sessions(
    limit: int = typer.Option(20, help="最多显示多少个最近会话。"),
) -> None:
    """列出 SignalAgent 持久化聊天会话。"""
    from rich.table import Table

    manager = SignalSessionManager()
    table = Table(title="SignalAgent chat sessions", expand=True)
    table.add_column("thread_id", no_wrap=True, overflow="ignore", min_width=32)
    table.add_column("updated")
    table.add_column("messages", justify="right")
    table.add_column("run_state")
    table.add_column("title")
    sessions = manager.list_sessions(limit=limit)
    for session in sessions:
        table.add_row(
            session.thread_id,
            session.updated_at,
            str(session.message_count),
            session.current_run_state_path or "",
            session.title or "",
        )
    console.print(table)


@signal_app.command("report-html")
def signal_report_html(
    backtest_dir: Path = typer.Argument(..., help="已有回测产物目录，目录中必须包含 equity_curve.parquet。"),
    output_path: Path | None = typer.Option(None, help="可选：HTML 输出路径；不填则写入回测目录下的 quantstats_report.html。"),
) -> None:
    """从已有回测产物补生成 QuantStats HTML 报告。"""
    result = SignalBacktestEvaluator().generate_quantstats_html(
        backtest_dir=backtest_dir,
        output_path=output_path,
    )
    console.print_json(json.dumps(result, ensure_ascii=False))


@image_app.command("review")
def image_review(
    image_path: Path = typer.Argument(..., help="本地图片路径，支持绝对路径或项目相对路径。"),
    question: str = typer.Option(
        "请描述图片中的关键信息，并说明与量化策略复盘相关的可见事实。",
        help="希望多模态模型重点查看的问题。",
    ),
    output_path: Path | None = typer.Option(None, help="可选：JSON 结果输出路径；不填则写入 artifacts/image_reviews。"),
    model: str | None = typer.Option(None, help="可选：覆盖模型名；不填使用 MOONSHOT_MODEL 或 kimi-k2.6。"),
) -> None:
    """调用独立 Kimi 多模态模型识别图片并输出文字结果。"""
    result = ImageReviewService().run(
        ImageReviewRequest(
            image_path=image_path,
            question=question,
            output_path=output_path,
            model=model,
        )
    )
    console.print_json(json.dumps(result.model_dump(mode="json"), ensure_ascii=True))


if __name__ == "__main__":
    app()
