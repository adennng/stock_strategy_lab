from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from strategy_lab.agents.model_factory import ReasoningContentChatOpenAI
from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.config.loader import load_config_file
from strategy_lab.services.data_format import load_wide_parquet, normalize_symbol
from strategy_lab.services.portfolio_run import PortfolioRunManager


class PortfolioSignalProfileRequest(BaseModel):
    portfolio_run_state_path: Path
    output_dir: Path | None = None
    use_llm: bool = True
    max_memory_chars: int = 0
    max_workers: int = Field(default=1, ge=1)
    symbols: list[str] | None = None
    update_run_state: bool = True


class PortfolioSignalProfileResult(BaseModel):
    portfolio_run_state_path: Path
    output_dir: Path
    signal_profiles_path: Path
    signal_profiles_md_path: Path
    signal_profiles_csv_path: Path
    manifest_path: Path
    daily_calibrated_signal_strength_path: Path
    daily_signal_state_path: Path
    per_asset_dir: Path
    warnings: list[str] = Field(default_factory=list)


class PortfolioSignalProfileService:
    """组合层信号画像服务。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = PortfolioRunManager(config=self.config)

    def run(self, request: PortfolioSignalProfileRequest) -> PortfolioSignalProfileResult:
        state_path = self._resolve_path(request.portfolio_run_state_path)
        state = self.run_manager.load_state(state_path)
        reference_symbols = self._reference_symbols_from_state(state)
        targets_path = self._resolve_daily_signal_targets_path(state)
        signal_targets = load_wide_parquet(targets_path, reference_symbols=reference_symbols).fillna(0.0).clip(lower=0.0, upper=1.0)

        output_dir = self._resolve_output_dir(request.output_dir, state=state)
        per_asset_dir = output_dir / "per_asset"
        output_dir.mkdir(parents=True, exist_ok=True)
        per_asset_dir.mkdir(parents=True, exist_ok=True)

        llm = self._create_model() if request.use_llm else None
        warnings: list[str] = []
        profiles: list[dict[str, Any]] = []
        calibrated = pd.DataFrame(index=signal_targets.index)
        signal_state = pd.DataFrame(index=signal_targets.index)

        items = state.get("source_artifacts", {}).get("signals", {}).get("items", [])
        if not items:
            raise ValueError("portfolio_run_state.json 缺少 source_artifacts.signals.items。")
        selected_symbols = self._normalize_symbol_filter(request.symbols, reference_symbols=reference_symbols)
        if selected_symbols:
            items = [
                item
                for item in items
                if normalize_symbol(item.get("symbol"), reference_symbols=reference_symbols) in selected_symbols
            ]
            if not items:
                raise ValueError(f"symbols 过滤后没有可处理资产：{sorted(selected_symbols)}")

        def build_one(item: dict[str, Any]) -> tuple[str, dict[str, Any], pd.Series, pd.Series, list[str]]:
            symbol = normalize_symbol(item.get("symbol"), reference_symbols=reference_symbols)
            if symbol not in signal_targets.columns:
                pre_warnings = [f"{symbol} 不在 daily_signal_targets.parquet 中，按空信号处理。"]
                series = pd.Series(0.0, index=signal_targets.index, name=symbol)
            else:
                pre_warnings = []
                series = signal_targets[symbol].fillna(0.0).clip(lower=0.0, upper=1.0)

            profile, strength_series, state_series, item_warnings = self._build_asset_profile(
                item=item,
                symbol=symbol,
                series=series,
                request=request,
                llm=llm,
                reference_symbols=reference_symbols,
            )
            item_warnings = [*pre_warnings, *item_warnings]
            return symbol, profile, strength_series, state_series, item_warnings

        worker_count = min(max(int(request.max_workers or 1), 1), len(items))
        if worker_count <= 1:
            results = [build_one(item) for item in items]
        else:
            results = []
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(build_one, item) for item in items]
                for future in as_completed(futures):
                    results.append(future.result())

        for symbol, profile, strength_series, state_series, item_warnings in sorted(results, key=lambda item: item[0]):
            warnings.extend(item_warnings)
            profiles.append(profile)
            calibrated[symbol] = strength_series
            signal_state[symbol] = state_series
            self._write_per_asset_files(per_asset_dir=per_asset_dir, profile=profile)

        profiles = sorted(profiles, key=lambda item: item["symbol"])
        calibrated.index.name = "datetime"
        signal_state.index.name = "datetime"

        signal_profiles_path = output_dir / "signal_profiles.json"
        signal_profiles_csv_path = output_dir / "signal_profiles.csv"
        signal_profiles_md_path = output_dir / "signal_profiles.md"
        daily_calibrated_path = output_dir / "daily_calibrated_signal_strength.parquet"
        daily_state_path = output_dir / "daily_signal_state.parquet"
        manifest_path = output_dir / "signal_profile_manifest.json"

        summary = self._build_summary(profiles)
        payload = {
            "schema_version": "0.1.0",
            "created_at": datetime.now().isoformat(),
            "portfolio_run_id": state.get("portfolio_run_id"),
            "source_daily_signal_targets_path": self._relative(targets_path),
            "llm_enabled": bool(request.use_llm),
            "max_workers": worker_count,
            "symbol_filter": sorted(selected_symbols) if selected_symbols else None,
            "asset_count": len(profiles),
            "summary": summary,
            "profiles": profiles,
            "warnings": warnings,
        }
        signal_profiles_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._profiles_to_frame(profiles).to_csv(signal_profiles_csv_path, index=False, encoding="utf-8-sig")
        signal_profiles_md_path.write_text(self._format_markdown(payload), encoding="utf-8")
        calibrated.to_parquet(daily_calibrated_path)
        signal_state.to_parquet(daily_state_path)

        manifest = {
            "schema_version": "0.1.0",
            "portfolio_run_id": state.get("portfolio_run_id"),
            "created_at": datetime.now().isoformat(),
            "output_dir": self._relative(output_dir),
            "signal_profiles_path": self._relative(signal_profiles_path),
            "signal_profiles_md_path": self._relative(signal_profiles_md_path),
            "signal_profiles_csv_path": self._relative(signal_profiles_csv_path),
            "daily_calibrated_signal_strength_path": self._relative(daily_calibrated_path),
            "daily_signal_state_path": self._relative(daily_state_path),
            "per_asset_dir": self._relative(per_asset_dir),
            "summary": summary,
            "warnings": warnings,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        if request.update_run_state:
            self._update_run_state(
                state_path=state_path,
                state=state,
                manifest=manifest,
                output_dir=output_dir,
                signal_profiles_path=signal_profiles_path,
                signal_profiles_md_path=signal_profiles_md_path,
                signal_profiles_csv_path=signal_profiles_csv_path,
                daily_calibrated_path=daily_calibrated_path,
                daily_state_path=daily_state_path,
            )

        return PortfolioSignalProfileResult(
            portfolio_run_state_path=state_path,
            output_dir=output_dir,
            signal_profiles_path=signal_profiles_path,
            signal_profiles_md_path=signal_profiles_md_path,
            signal_profiles_csv_path=signal_profiles_csv_path,
            manifest_path=manifest_path,
            daily_calibrated_signal_strength_path=daily_calibrated_path,
            daily_signal_state_path=daily_state_path,
            per_asset_dir=per_asset_dir,
            warnings=warnings,
        )

    def _build_asset_profile(
        self,
        *,
        item: dict[str, Any],
        symbol: str,
        series: pd.Series,
        request: PortfolioSignalProfileRequest,
        llm: Any | None,
        reference_symbols: list[str],
    ) -> tuple[dict[str, Any], pd.Series, pd.Series, list[str]]:
        copied = item.get("copied_files") if isinstance(item.get("copied_files"), dict) else {}
        files = self._resolve_signal_files(item=item, copied=copied)
        stats = self._signal_distribution(series)
        performance = self._load_performance(files)
        texts = self._load_signal_texts(files=files, max_memory_chars=request.max_memory_chars)
        semantic = self._heuristic_semantic_profile(symbol=symbol, texts=texts, stats=stats, performance=performance)
        warnings: list[str] = []
        if llm is not None:
            llm_semantic, warning = self._llm_semantic_profile(
                llm=llm,
                symbol=symbol,
                texts=texts,
                stats=stats,
                performance=performance,
            )
            if warning:
                warnings.append(warning)
            semantic = self._merge_semantic(semantic, llm_semantic)

        reliability = self._reliability_score(stats=stats, performance=performance, semantic=semantic)
        fusion_guidance = self._fusion_guidance(stats=stats, performance=performance, semantic=semantic, reliability=reliability)
        strength_series, state_series = self._calibrate_series(series=series, stats=stats, semantic=semantic, reliability=reliability)
        profile = {
            "symbol": symbol,
            "source_signal_run_id": item.get("signal_run_id"),
            "selected_attempt_id": item.get("selected_attempt_id") or self._selected_attempt_id(files.get("run_state_path")),
            "input_files": {key: self._relative(path) for key, path in files.items() if path and path.exists()},
            "signal_distribution": stats,
            "performance": performance,
            "semantic_profile": semantic,
            "reliability": reliability,
            "fusion_guidance": fusion_guidance,
            "position_interpretation": self._position_interpretation(stats=stats, semantic=semantic),
        }
        return profile, strength_series, state_series, warnings

    def _create_model(self) -> Any | None:
        agent_cfg = load_config_file("agent")
        llm_cfg = agent_cfg.get("agents", {}).get("llm", {})
        signal_profile_cfg = agent_cfg.get("agents", {}).get("portfolio_signal_profile", {})
        provider = str(
            signal_profile_cfg.get("provider")
            or os.getenv("PORTFOLIO_SIGNAL_PROFILE_PROVIDER")
            or llm_cfg.get("provider")
            or "deepseek"
        ).lower()
        api_key = (
            signal_profile_cfg.get("api_key")
            or os.getenv("PORTFOLIO_SIGNAL_PROFILE_API_KEY")
            or llm_cfg.get("api_key")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        base_url = (
            signal_profile_cfg.get("base_url")
            or os.getenv("PORTFOLIO_SIGNAL_PROFILE_BASE_URL")
            or llm_cfg.get("base_url")
            or os.getenv("DEEPSEEK_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
        )
        model_name = (
            signal_profile_cfg.get("model")
            or os.getenv("PORTFOLIO_SIGNAL_PROFILE_MODEL")
            or llm_cfg.get("model")
            or os.getenv("DEEPSEEK_MODEL")
            or "deepseek-v4-pro"
        )
        thinking = str(signal_profile_cfg.get("thinking") or os.getenv("PORTFOLIO_SIGNAL_PROFILE_THINKING") or llm_cfg.get("thinking") or "enabled")
        reasoning_effort = (
            signal_profile_cfg.get("reasoning_effort")
            or os.getenv("PORTFOLIO_SIGNAL_PROFILE_REASONING_EFFORT")
            or llm_cfg.get("reasoning_effort")
        )
        if not api_key or not base_url:
            raise RuntimeError("缺少 portfolio_signal_profile 的大模型 API Key 或 Base URL。可使用 --no-llm 跳过语义提取。")

        if provider == "deepseek":
            return ReasoningContentChatOpenAI.create_deepseek(
                model=model_name,
                api_key=api_key,
                api_base=base_url,
                reasoning_effort=reasoning_effort if thinking == "enabled" else None,
                extra_body={"thinking": {"type": thinking}},
            )

        kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": api_key,
            "base_url": base_url,
        }
        if provider in {"moonshot", "kimi"}:
            kwargs["payload_token_param"] = "max_tokens"
        return ReasoningContentChatOpenAI.create_openai_compatible(**kwargs)

    def _llm_semantic_profile(
        self,
        *,
        llm: Any,
        symbol: str,
        texts: dict[str, str],
        stats: dict[str, Any],
        performance: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        prompt = self._build_llm_prompt(symbol=symbol, texts=texts, stats=stats, performance=performance)
        try:
            response = llm.invoke(
                [
                    SystemMessage(content=self._llm_system_prompt()),
                    HumanMessage(content=prompt),
                ]
            )
            content = str(response.content or "")
            parsed = self._parse_json_object(content)
            if not isinstance(parsed, dict):
                return None, f"{symbol} LLM 语义画像未返回有效 JSON，已使用规则画像。"
            parsed["profile_method"] = "llm_with_heuristic_fallback"
            return parsed, None
        except Exception as exc:  # noqa: BLE001
            return None, f"{symbol} LLM 语义画像失败，已使用规则画像：{exc}"

    def _llm_system_prompt(self) -> str:
        return """你是 stock_strategy_lab 项目里的组合层信号画像分析器，负责为 PortfolioAgent 提供结构化的“信号层策略使用说明”。

项目背景：
1. 信号层 Signal Layer：每个资产都有一个已经训练完成的单资产策略。该策略每日输出 target_S，范围 0 到 1。target_S 不是账户最终仓位，而是该资产自身策略认为“当前应使用多少上层预算”的参与度。
2. 预算层 Budget Layer：预算层每日输出 budget_weight_i，表示组合资产池中每个资产的预算参考权重，解决资金在资产之间如何分配的问题。
3. 组合层 Portfolio Layer：组合层不重新训练信号层或预算层，而是把 budget_weight_i、target_S_i、收益、波动、换手和约束融合成最终账户仓位 final_weight_i。

你的任务：
阅读单个资产的信号层策略说明、SignalAgent 记忆、参数空间、策略代码摘要、机器统计和绩效摘要，判断这个资产的 target_S 在组合层里应该如何被理解和使用。你的输出会被后续组合层策略编写使用，用于决定该信号适合做预算 veto、预算折扣、预算增强或预算漏选补位。

重要规则：
1. 只做事实归纳和组合层可用语义提取，不重新设计信号层策略，不写新的交易策略。
2. 不要编造输入材料没有支持的结论；如果证据不足，写入 uncertainty_notes。
3. 不要把不同资产的原始 target_S 机械比较。不同资产的信号层策略可能有不同仓位档位，例如 0/0.5/1 与 0/0.2/0.6/1 的含义不同。
4. 你必须结合机器统计、绩效摘要和文本材料共同判断。文本材料优先级：signal_agent_memory.md > strategy_spec.md > strategy_meta/param_space > strategy.py 摘要。
5. 只输出一个 JSON object，不要输出 Markdown、解释性前后缀或代码块。
6. JSON 字段名必须严格使用要求的英文名；字段值可以使用中文说明。
7. 布尔字段必须输出 true 或 false，不要输出“是/否”。"""

    def _build_llm_prompt(
        self,
        *,
        symbol: str,
        texts: dict[str, str],
        stats: dict[str, Any],
        performance: dict[str, Any],
    ) -> str:
        return f"""请为资产 {symbol} 生成组合层信号画像。

请先理解输入材料：

1. 机器统计 signal_distribution
   - mean_target：该资产平均 target_S。越高表示信号层长期参与度越高。
   - zero_ratio：target_S 等于 0 的日期比例。越高表示信号层经常要求空仓或回避，可作为预算 veto 或折扣依据，但也可能导致组合资金闲置。
   - active_ratio：target_S 大于 0 的日期比例。
   - gt_0_6_ratio：target_S 大于等于 0.6 的日期比例。越高表示强信号日较多，可考虑 boost 或预算漏选补位，但必须结合可靠度。
   - positive_levels：该资产出现过的正 target_S 档位。离散档位越少，越应按该资产自身档位解释强弱。
   - signal_shape：empty/discrete/hybrid/continuous。用于判断 target_S 是离散仓位映射还是连续仓位映射。

2. 绩效摘要 performance
   用于判断信号层策略是否可靠。重点看 sharpe、max_drawdown、annual_return/annualized_return、best_score、best_params 和阶段归因信息。绩效差或回撤大时，不应轻易建议放大该信号。

3. signal_agent_memory.md
   这是 SignalAgent 对该资产最终策略探索过程、策略定位、问题和最终选择理由的记忆。它是判断策略语义最重要的文本材料。

4. strategy_spec.md / strategy_meta.json / param_space.json / strategy.py 摘要
   用于确认策略结构、Alpha、Filter、ExitPolicy、PositionMapper、状态规则、参数档位和代码中实际存在的逻辑。代码摘要只作辅助，不要因为代码里存在备用函数就误判为最终策略实际使用了全部风格。

输出字段说明和填写规则：

strategy_role：
  必须从以下值中选择一个：
  - risk_gate：主要价值是控制风险、空仓、降仓、过滤预算层持仓。适用于 zero_ratio 高、防御/风控特征强、强信号少但回撤控制重要的策略。
  - trend_participation：主要价值是参与趋势。适用于趋势/动量/均线类策略，且 active_ratio 不过低。
  - breakout_booster：主要价值是捕捉突破或强行情。适用于突破、通道、放量、低波后爆发类策略，且 gt_0_6_ratio 或强信号逻辑清晰。
  - range_timing：主要价值是震荡/均值回归择时。适用于 RSI、Bollinger、ZScore、超买超卖、区间回归类策略。
  - defensive_participation：主要价值是在防御条件下有限参与。适用于仓位保守、重视回撤控制但仍有一定参与度的策略。
  - generic_timing：信息不足或无法明确归类时使用。

style_tags：
  输出不超过 8 个标签，优先从以下标签选择，也可少量补充：
  trend_following, breakout_capture, mean_reversion, defensive_timing,
  low_vol_participation, regime_switching, multi_timeframe, cash_heavy,
  high_turnover_risk, low_reliability, strong_signal_candidate。
  标签必须有材料依据，不要把所有标签都填上。

signal_strength_interpretation：
  解释该资产 target_S 的强弱如何理解。必须说明：
  - 它是离散档位还是连续值；
  - 哪些档位/区间可视为弱、中、强信号；
  - 为什么不能和其他资产的原始 target_S 机械比较。

best_market_conditions：
  该信号更适合发挥作用的市场状态，例如趋势延续、突破行情、震荡回归、高波动防御、低波后扩张等。必须根据策略说明和统计判断。

weak_market_conditions：
  该信号可能失效、低效或造成踏空/误伤的市场状态。必须结合策略风格和 zero_ratio、active_ratio、换手或绩效判断。

strengths / weaknesses：
  分别最多 5 条。必须围绕组合层使用价值，不要泛泛评价。

fusion_usage：
  - can_veto_budget：如果该信号能可靠提示预算层应回避该资产，填 true。常见依据是 zero_ratio 高、防御风控清晰、回撤控制有效。
  - can_discount_budget：如果该信号至少可用于降低预算权重，通常填 true；除非信号几乎无效或信息不足。
  - can_boost_budget：如果强信号日较多、强信号有清晰含义且可靠度不差，填 true。
  - can_override_zero_budget：如果预算层给 0 但信号层强烈看好时可以小幅突破预算层，才填 true。要求强信号含义清晰、绩效或记忆支持、风险可控。默认偏 false。
  - recommended_role：一句话说明组合层应如何用该信号。

risk_notes：
  写组合层使用时需要注意的风险，例如容易踏空、换手过高、强信号太少、绩效不稳、预算突破风险等。

uncertainty_notes：
  写信息不足、证据冲突、无法判断的地方。如果没有，输出空数组。

你需要输出如下 JSON object，字段必须完整：

{{
  "strategy_role": "risk_gate | trend_participation | breakout_booster | range_timing | defensive_participation | generic_timing",
  "style_tags": ["不超过8个标签，例如 trend_following、regime_switching、cash_heavy"],
  "signal_strength_interpretation": "解释该资产 target_S 的强弱如何理解，必须结合该资产自身仓位档位，不要跨资产机械比较",
  "best_market_conditions": ["该信号更适合的市场状态"],
  "weak_market_conditions": ["该信号容易失效或低效的市场状态"],
  "strengths": ["优势，最多5条"],
  "weaknesses": ["问题，最多5条"],
  "fusion_usage": {{
    "can_veto_budget": true,
    "can_discount_budget": true,
    "can_boost_budget": false,
    "can_override_zero_budget": false,
    "recommended_role": "组合层推荐用法一句话"
  }},
  "risk_notes": ["组合层使用时的风险提示"],
  "uncertainty_notes": ["信息不足或需要谨慎的地方"]
}}

输出约束：
- 字段必须完整，不确定也不能省略字段。
- 如果某个数组没有内容，输出空数组 []。
- 如果某个布尔判断证据不足，优先输出 false，并在 uncertainty_notes 说明。
- 不要输出除 JSON object 以外的任何文字。

机器统计：
{json.dumps(stats, ensure_ascii=False, indent=2, default=str)}

绩效摘要：
{json.dumps(performance, ensure_ascii=False, indent=2, default=str)}

signal_agent_memory.md 摘要：
{texts.get("memory", "")}

strategy_spec.md：
{texts.get("strategy_spec", "")}

strategy_meta.json：
{texts.get("strategy_meta", "")}

param_space.json：
{texts.get("param_space", "")}

strategy.py 摘要：
{texts.get("strategy_code_excerpt", "")}
"""

    def _resolve_signal_files(self, *, item: dict[str, Any], copied: dict[str, Any]) -> dict[str, Path | None]:
        return {
            "run_state_path": self._resolve_optional_path(copied.get("run_state_path")),
            "strategy_path": self._resolve_optional_path(copied.get("selected_strategy_path") or copied.get("strategy_path") or item.get("selected_strategy_path")),
            "strategy_spec_path": self._resolve_optional_path(copied.get("strategy_spec_path")),
            "strategy_meta_path": self._resolve_optional_path(copied.get("strategy_meta_path")),
            "param_space_path": self._resolve_optional_path(copied.get("param_space_path")),
            "selected_metrics_path": self._resolve_optional_path(copied.get("selected_metrics_path") or copied.get("metrics_path")),
            "signal_agent_memory_path": self._resolve_optional_path(copied.get("signal_agent_memory_path")),
        }

    def _signal_distribution(self, series: pd.Series) -> dict[str, Any]:
        values = pd.to_numeric(series, errors="coerce").fillna(0.0).clip(lower=0.0, upper=1.0)
        rounded = values.round(4)
        positive = rounded[rounded > 0]
        positive_levels = sorted(float(item) for item in positive.unique())
        change_count = int((rounded.diff().abs().fillna(0.0) > 1e-9).sum())
        signal_shape = "empty"
        if len(positive) > 0:
            signal_shape = "discrete" if len(positive_levels) <= 6 else "continuous"
            if len(positive_levels) > 6 and rounded.nunique() <= 10:
                signal_shape = "hybrid"
        return {
            "date_count": int(len(values)),
            "mean_target": self._safe_float(values.mean()),
            "median_target": self._safe_float(values.median()),
            "max_target": self._safe_float(values.max()),
            "min_target": self._safe_float(values.min()),
            "std_target": self._safe_float(values.std(ddof=0)),
            "zero_ratio": self._safe_float((values <= 1e-9).mean()),
            "active_ratio": self._safe_float((values > 1e-9).mean()),
            "gt_0_3_ratio": self._safe_float((values >= 0.3).mean()),
            "gt_0_6_ratio": self._safe_float((values >= 0.6).mean()),
            "gt_0_8_ratio": self._safe_float((values >= 0.8).mean()),
            "positive_levels": positive_levels[:30],
            "unique_level_count": int(rounded.nunique()),
            "change_count": change_count,
            "avg_holding_days_estimate": self._safe_float(len(values) / max(change_count, 1)),
            "signal_shape": signal_shape,
        }

    def _load_performance(self, files: dict[str, Path | None]) -> dict[str, Any]:
        metrics = self._read_json_if_exists(files.get("selected_metrics_path"))
        run_state = self._read_json_if_exists(files.get("run_state_path"))
        selected = self._selected_attempt_id(files.get("run_state_path"))
        if run_state and selected:
            for attempt in run_state.get("attempts", []):
                if attempt.get("attempt_id") == selected:
                    metrics.setdefault("best_score", attempt.get("best_score"))
                    metrics.setdefault("best_params", attempt.get("best_params"))
                    metrics.setdefault("stage_attribution", attempt.get("stage_attribution"))
                    break
        return metrics

    def _load_signal_texts(self, *, files: dict[str, Path | None], max_memory_chars: int) -> dict[str, str]:
        return {
            "memory": self._read_text_if_exists(files.get("signal_agent_memory_path"), limit=max_memory_chars),
            "strategy_spec": self._read_text_if_exists(files.get("strategy_spec_path"), limit=0),
            "strategy_meta": self._read_text_if_exists(files.get("strategy_meta_path"), limit=0),
            "param_space": self._read_text_if_exists(files.get("param_space_path"), limit=0),
            "strategy_code_excerpt": self._read_text_if_exists(files.get("strategy_path"), limit=0),
        }

    def _heuristic_semantic_profile(
        self,
        *,
        symbol: str,
        texts: dict[str, str],
        stats: dict[str, Any],
        performance: dict[str, Any],
    ) -> dict[str, Any]:
        primary_content = "\n".join(
            [
                texts.get("memory", ""),
                texts.get("strategy_spec", ""),
                texts.get("strategy_meta", ""),
                texts.get("param_space", ""),
            ]
        ).lower()
        code_content = texts.get("strategy_code_excerpt", "").lower()
        content = primary_content if len(primary_content.strip()) >= 200 else f"{primary_content}\n{code_content}"
        tags: list[str] = []
        keyword_tags = {
            "trend_following": ["趋势", "动量", "momentum", "均线", "multi_timeframe"],
            "breakout_capture": ["突破", "donchian", "channel", "breakout", "通道"],
            "mean_reversion": ["震荡", "回归", "rsi", "bollinger", "zscore", "超卖"],
            "defensive_timing": ["防御", "空仓", "避险", "回撤", "高波动降仓", "downtrend"],
            "low_vol_participation": ["低波", "volatility", "atr", "波动率收缩"],
            "regime_switching": ["regime", "market_state", "uptrend", "range", "downtrend", "状态"],
            "multi_timeframe": ["多周期", "短周期", "中周期", "长周期", "multi-timeframe"],
        }
        for tag, keywords in keyword_tags.items():
            if any(keyword in content for keyword in keywords):
                tags.append(tag)
        if stats.get("mean_target", 0) < 0.25 or stats.get("zero_ratio", 0) > 0.55:
            tags.append("cash_heavy")
        if not tags:
            tags.append("generic_signal")

        strengths: list[str] = []
        weaknesses: list[str] = []
        if stats.get("active_ratio", 0) >= 0.65:
            strengths.append("信号参与度较高，适合作为组合层持续配置候选。")
        if stats.get("zero_ratio", 0) >= 0.55:
            strengths.append("空仓或低仓纪律明显，适合作为预算仓位的风险折扣或 veto 依据。")
            weaknesses.append("参与度偏低，直接乘法融合容易造成资金闲置。")
        if stats.get("gt_0_6_ratio", 0) >= 0.25:
            strengths.append("强信号日期较多，可作为预算漏选补位或超配候选。")
        sharpe = self._to_float(performance.get("sharpe"))
        if sharpe is not None and sharpe >= 1:
            strengths.append("历史夏普较好，信号可信度相对较高。")
        elif sharpe is not None and sharpe < 0.3:
            weaknesses.append("历史夏普偏低，组合层不应无条件放大该信号。")

        return {
            "profile_method": "heuristic",
            "strategy_role": self._infer_strategy_role(tags, stats),
            "style_tags": sorted(set(tags)),
            "signal_strength_interpretation": "按该资产自身仓位档位和触发频率解释强弱，不要与其他资产原始 target_S 机械比较。",
            "best_market_conditions": self._infer_market_conditions(tags),
            "weak_market_conditions": self._infer_weak_conditions(tags),
            "strengths": strengths[:5],
            "weaknesses": weaknesses[:5],
            "fusion_usage": {
                "can_veto_budget": bool(stats.get("zero_ratio", 0) >= 0.5 or "defensive_timing" in tags),
                "can_discount_budget": True,
                "can_boost_budget": bool(stats.get("gt_0_6_ratio", 0) >= 0.2),
                "can_override_zero_budget": bool(stats.get("gt_0_6_ratio", 0) >= 0.25 and sharpe is not None and sharpe >= 0.6),
                "recommended_role": f"{symbol} 可作为组合层的 {self._infer_strategy_role(tags, stats)} 信号使用。",
            },
            "risk_notes": self._infer_risk_notes(tags, stats, symbol),
            "uncertainty_notes": [],
        }

    def _merge_semantic(self, base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
        if not override:
            return base
        merged = dict(base)
        for key, value in override.items():
            if value not in (None, "", [], {}):
                merged[key] = value
        merged["heuristic_backup"] = base
        return merged

    def _reliability_score(self, *, stats: dict[str, Any], performance: dict[str, Any], semantic: dict[str, Any]) -> dict[str, Any]:
        sharpe = self._to_float(performance.get("sharpe"))
        max_drawdown = abs(self._to_float(performance.get("max_drawdown")) or 0.0)
        annual_return = self._to_float(performance.get("annual_return") or performance.get("annualized_return"))
        active_ratio = self._to_float(stats.get("active_ratio")) or 0.0
        turnover_proxy = min(1.0, (self._to_float(stats.get("change_count")) or 0.0) / max(self._to_float(stats.get("date_count")) or 1.0, 1.0))
        sharpe_score = self._clip01(((sharpe if sharpe is not None else 0.0) + 0.3) / 2.3)
        drawdown_score = self._clip01(1.0 - max_drawdown / 0.35)
        return_score = self._clip01(((annual_return if annual_return is not None else 0.0) + 0.10) / 0.35)
        participation_score = self._clip01(0.5 + abs(active_ratio - 0.5))
        turnover_score = self._clip01(1.0 - turnover_proxy)
        score = 0.35 * sharpe_score + 0.25 * drawdown_score + 0.15 * return_score + 0.15 * participation_score + 0.10 * turnover_score
        if "generic_signal" in semantic.get("style_tags", []):
            score *= 0.9
        return {
            "score": round(float(score), 4),
            "grade": self._grade(score),
            "components": {
                "sharpe_score": round(float(sharpe_score), 4),
                "drawdown_score": round(float(drawdown_score), 4),
                "return_score": round(float(return_score), 4),
                "participation_score": round(float(participation_score), 4),
                "turnover_score": round(float(turnover_score), 4),
            },
        }

    def _fusion_guidance(
        self,
        *,
        stats: dict[str, Any],
        performance: dict[str, Any],
        semantic: dict[str, Any],
        reliability: dict[str, Any],
    ) -> dict[str, Any]:
        usage = semantic.get("fusion_usage") if isinstance(semantic.get("fusion_usage"), dict) else {}
        score = float(reliability.get("score") or 0.0)
        zero_ratio = float(stats.get("zero_ratio") or 0.0)
        strong_ratio = float(stats.get("gt_0_6_ratio") or 0.0)
        recommended_uses: list[str] = ["signal_discount"]
        if usage.get("can_veto_budget") or zero_ratio >= 0.5:
            recommended_uses.append("budget_veto")
        if usage.get("can_boost_budget") or strong_ratio >= 0.2:
            recommended_uses.append("signal_boost")
        if usage.get("can_override_zero_budget") and score >= 0.55:
            recommended_uses.append("soft_budget_override")
        return {
            "recommended_uses": sorted(set(recommended_uses)),
            "veto_power": self._bucket(zero_ratio, low=0.35, high=0.65),
            "boost_power": self._bucket(strong_ratio * max(score, 0.2), low=0.08, high=0.20),
            "budget_override_suitability": self._bucket(score * strong_ratio, low=0.08, high=0.18),
            "suggested_constraints": {
                "max_single_asset_weight_hint": 0.25 if score >= 0.6 else 0.18,
                "allow_zero_budget_override_hint": bool(score >= 0.6 and strong_ratio >= 0.2),
                "needs_floor_or_redeployment": bool(zero_ratio >= 0.35),
            },
            "explanation": usage.get("recommended_role") or "组合层应把该信号作为预算权重的质量修正，并单独处理信号低暴露造成的闲置资金。",
        }

    def _calibrate_series(
        self,
        *,
        series: pd.Series,
        stats: dict[str, Any],
        semantic: dict[str, Any],
        reliability: dict[str, Any],
    ) -> tuple[pd.Series, pd.Series]:
        values = pd.to_numeric(series, errors="coerce").fillna(0.0).clip(lower=0.0, upper=1.0)
        strength = pd.Series(0.0, index=values.index, dtype=float)
        states = pd.Series("zero", index=values.index, dtype=object)
        positive = values[values > 1e-9]
        if positive.empty:
            return strength, states
        reliability_score = float(reliability.get("score") or 0.5)
        if stats.get("signal_shape") in {"discrete", "hybrid"}:
            levels = sorted(float(item) for item in pd.Series(positive.round(4).unique()).dropna())
            rank_map = {level: (idx + 1) / len(levels) for idx, level in enumerate(levels)}
            strength = values.round(4).map(lambda item: rank_map.get(float(item), 0.0)).astype(float) * reliability_score
        else:
            ranks = positive.rank(pct=True)
            strength.loc[positive.index] = ranks * reliability_score
        strength = strength.clip(lower=0.0, upper=1.0)
        states.loc[(values > 1e-9) & (strength < 0.35)] = "weak"
        states.loc[(strength >= 0.35) & (strength < 0.65)] = "medium"
        states.loc[strength >= 0.65] = "strong"
        if "defensive_timing" in semantic.get("style_tags", []):
            states.loc[(values > 1e-9) & (strength < 0.45)] = "defensive_participation"
        return strength, states

    def _position_interpretation(self, *, stats: dict[str, Any], semantic: dict[str, Any]) -> dict[str, Any]:
        return {
            "mapping_type": stats.get("signal_shape"),
            "positive_levels": stats.get("positive_levels") or [],
            "interpretation": semantic.get("signal_strength_interpretation")
            or "按该资产自身仓位档位和触发频率解释强弱，不要与其他资产原始 target_S 机械比较。",
        }

    def _build_summary(self, profiles: list[dict[str, Any]]) -> dict[str, Any]:
        if not profiles:
            return {}
        frame = self._profiles_to_frame(profiles)
        return {
            "asset_count": int(len(frame)),
            "avg_signal_mean_target": self._safe_float(frame["mean_target"].mean()),
            "avg_signal_active_ratio": self._safe_float(frame["active_ratio"].mean()),
            "avg_signal_zero_ratio": self._safe_float(frame["zero_ratio"].mean()),
            "avg_reliability_score": self._safe_float(frame["reliability_score"].mean()),
            "high_reliability_assets": frame.loc[frame["reliability_score"] >= 0.6, "symbol"].tolist(),
            "cash_heavy_assets": frame.loc[frame["zero_ratio"] >= 0.55, "symbol"].tolist(),
            "strong_signal_assets": frame.loc[frame["gt_0_6_ratio"] >= 0.2, "symbol"].tolist(),
        }

    def _profiles_to_frame(self, profiles: list[dict[str, Any]]) -> pd.DataFrame:
        rows = []
        for profile in profiles:
            dist = profile.get("signal_distribution", {})
            semantic = profile.get("semantic_profile", {})
            guidance = profile.get("fusion_guidance", {})
            reliability = profile.get("reliability", {})
            rows.append(
                {
                    "symbol": profile.get("symbol"),
                    "strategy_role": semantic.get("strategy_role"),
                    "style_tags": ",".join(semantic.get("style_tags", [])),
                    "mean_target": dist.get("mean_target"),
                    "max_target": dist.get("max_target"),
                    "zero_ratio": dist.get("zero_ratio"),
                    "active_ratio": dist.get("active_ratio"),
                    "gt_0_6_ratio": dist.get("gt_0_6_ratio"),
                    "signal_shape": dist.get("signal_shape"),
                    "reliability_score": reliability.get("score"),
                    "reliability_grade": reliability.get("grade"),
                    "recommended_uses": ",".join(guidance.get("recommended_uses", [])),
                    "veto_power": guidance.get("veto_power"),
                    "boost_power": guidance.get("boost_power"),
                    "budget_override_suitability": guidance.get("budget_override_suitability"),
                }
            )
        return pd.DataFrame(rows)

    def _format_markdown(self, payload: dict[str, Any]) -> str:
        summary = payload.get("summary", {})
        lines = [
            "# 组合层信号画像",
            "",
            "本报告用于 PortfolioAgent 编写组合层融合策略前理解各资产信号层的仓位语义、可信度和组合层用法。",
            "",
            "## 总览",
            "",
            f"- 资产数量：{summary.get('asset_count')}",
            f"- 平均信号目标仓位：{self._fmt(summary.get('avg_signal_mean_target'))}",
            f"- 平均信号活跃比例：{self._fmt(summary.get('avg_signal_active_ratio'))}",
            f"- 平均信号空仓比例：{self._fmt(summary.get('avg_signal_zero_ratio'))}",
            f"- 平均可靠度：{self._fmt(summary.get('avg_reliability_score'))}",
            f"- 高可靠度资产：{', '.join(summary.get('high_reliability_assets') or []) or '无'}",
            f"- 高现金纪律资产：{', '.join(summary.get('cash_heavy_assets') or []) or '无'}",
            f"- 强信号资产：{', '.join(summary.get('strong_signal_assets') or []) or '无'}",
            "",
            "## 资产画像表",
            "",
            "| symbol | role | tags | mean_S | active | zero | strong | reliability | uses |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        for profile in payload.get("profiles", []):
            dist = profile.get("signal_distribution", {})
            semantic = profile.get("semantic_profile", {})
            guidance = profile.get("fusion_guidance", {})
            reliability = profile.get("reliability", {})
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(profile.get("symbol")),
                        str(semantic.get("strategy_role")),
                        ", ".join(semantic.get("style_tags", [])),
                        self._fmt(dist.get("mean_target")),
                        self._fmt(dist.get("active_ratio")),
                        self._fmt(dist.get("zero_ratio")),
                        self._fmt(dist.get("gt_0_6_ratio")),
                        self._fmt(reliability.get("score")),
                        ", ".join(guidance.get("recommended_uses", [])),
                    ]
                )
                + " |"
            )
        lines.extend(
            [
                "",
                "## 使用说明",
                "",
                "- `daily_calibrated_signal_strength.parquet` 是按资产自身仓位档位和可靠度校准后的信号强度，不等同于原始 target_S。",
                "- `daily_signal_state.parquet` 将每日信号分成 zero、weak、medium、strong、defensive_participation。",
                "- 如果某资产 `needs_floor_or_redeployment=true`，说明该资产信号长期低暴露，组合层应考虑 floor、soft cap 或闲置资金再分配。",
            ]
        )
        warnings = payload.get("warnings") or []
        if warnings:
            lines.extend(["", "## 警告", ""])
            lines.extend(f"- {item}" for item in warnings)
        return "\n".join(lines) + "\n"

    def _write_per_asset_files(self, *, per_asset_dir: Path, profile: dict[str, Any]) -> None:
        symbol_dir = per_asset_dir / self._safe_name(profile["symbol"])
        symbol_dir.mkdir(parents=True, exist_ok=True)
        (symbol_dir / "signal_profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        semantic = profile.get("semantic_profile", {})
        guidance = profile.get("fusion_guidance", {})
        dist = profile.get("signal_distribution", {})
        lines = [
            f"# {profile['symbol']} 信号画像",
            "",
            f"- 策略角色：{semantic.get('strategy_role')}",
            f"- 风格标签：{', '.join(semantic.get('style_tags', []))}",
            f"- 平均 target_S：{self._fmt(dist.get('mean_target'))}",
            f"- 活跃比例：{self._fmt(dist.get('active_ratio'))}",
            f"- 空仓比例：{self._fmt(dist.get('zero_ratio'))}",
            f"- 强信号比例：{self._fmt(dist.get('gt_0_6_ratio'))}",
            f"- 可靠度：{profile.get('reliability', {}).get('score')} ({profile.get('reliability', {}).get('grade')})",
            f"- 推荐组合层用法：{', '.join(guidance.get('recommended_uses', []))}",
            "",
            "## 组合层建议",
            "",
            guidance.get("explanation") or "无。",
        ]
        (symbol_dir / "signal_profile.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _update_run_state(
        self,
        *,
        state_path: Path,
        state: dict[str, Any],
        manifest: dict[str, Any],
        output_dir: Path,
        signal_profiles_path: Path,
        signal_profiles_md_path: Path,
        signal_profiles_csv_path: Path,
        daily_calibrated_path: Path,
        daily_state_path: Path,
    ) -> None:
        now = datetime.now().isoformat()
        payload = {
            "status": "success",
            "profile_dir": self._relative(output_dir),
            "signal_profiles_path": self._relative(signal_profiles_path),
            "signal_profiles_md_path": self._relative(signal_profiles_md_path),
            "signal_profiles_csv_path": self._relative(signal_profiles_csv_path),
            "daily_calibrated_signal_strength_path": self._relative(daily_calibrated_path),
            "daily_signal_state_path": self._relative(daily_state_path),
            "summary": manifest.get("summary", {}),
            "warnings": manifest.get("warnings", []),
            "updated_at": now,
        }
        state["signal_profile"] = payload
        state.setdefault("artifacts", {}).setdefault("profiles", {})["portfolio_signal_profile"] = payload
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "PortfolioSignalProfileService",
                "event": "portfolio_signal_profile_completed",
                "summary": "组合层信号画像已生成，可用于编写或调整 fusion policy。",
                "signal_profiles_path": payload["signal_profiles_path"],
                "signal_profiles_md_path": payload["signal_profiles_md_path"],
            }
        )
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)

    def _resolve_daily_signal_targets_path(self, state: dict[str, Any]) -> Path:
        raw = state.get("profile", {}).get("daily_signal_targets_path")
        if not raw:
            raw = state.get("artifacts", {}).get("profiles", {}).get("portfolio_profile", {}).get("daily_signal_targets_path")
        if not raw:
            raise ValueError("未找到 profile.daily_signal_targets_path，请先运行 portfolio profile。")
        return self._resolve_path(raw)

    def _resolve_output_dir(self, output_dir: Path | None, *, state: dict[str, Any]) -> Path:
        if output_dir:
            return self._resolve_path(output_dir)
        root = state.get("directories", {}).get("root")
        if not root:
            raise ValueError("portfolio_run_state.json 缺少 directories.root。")
        return self._resolve_path(Path(root) / "signal_profiles")

    def _reference_symbols_from_state(self, state: dict[str, Any]) -> list[str]:
        symbols = state.get("source_artifacts", {}).get("signals", {}).get("symbols")
        if isinstance(symbols, list):
            return sorted({str(item).upper() for item in symbols if item})
        return []

    def _normalize_symbol_filter(self, symbols: list[str] | None, *, reference_symbols: list[str]) -> set[str]:
        if not symbols:
            return set()
        return {normalize_symbol(item, reference_symbols=reference_symbols) for item in symbols if str(item).strip()}

    def _selected_attempt_id(self, run_state_path: Path | None) -> str | None:
        state = self._read_json_if_exists(run_state_path)
        if not state:
            return None
        return (
            state.get("steps", {}).get("final_selection", {}).get("selected_attempt_id")
            or state.get("steps", {}).get("strategy_search", {}).get("best_attempt_id")
        )

    def _read_json_if_exists(self, path: Path | None) -> dict[str, Any]:
        if not path or not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}

    def _read_text_if_exists(self, path: Path | None, *, limit: int) -> str:
        if not path or not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
            return text if limit <= 0 else text[:limit]
        except Exception:
            return ""

    def _resolve_optional_path(self, path: str | Path | None) -> Path | None:
        if not path:
            return None
        return self._resolve_path(path)

    def _resolve_path(self, path: str | Path | None) -> Path:
        if path is None:
            raise ValueError("路径不能为空。")
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: str | Path | None) -> str | None:
        if path is None:
            return None
        value = self._resolve_path(path)
        try:
            return str(value.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(value)

    def _parse_json_object(self, content: str) -> dict[str, Any] | None:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"```$", "", text).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not match:
                return None
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _infer_strategy_role(tags: list[str], stats: dict[str, Any]) -> str:
        tag_set = set(tags)
        if "defensive_timing" in tag_set or stats.get("zero_ratio", 0) > 0.6:
            return "risk_gate"
        if "breakout_capture" in tag_set:
            return "breakout_booster"
        if "mean_reversion" in tag_set:
            return "range_timing"
        if "trend_following" in tag_set:
            return "trend_participation"
        return "generic_timing"

    @staticmethod
    def _infer_market_conditions(tags: list[str]) -> list[str]:
        tag_set = set(tags)
        conditions = []
        if "trend_following" in tag_set:
            conditions.append("趋势延续或多周期同向阶段")
        if "breakout_capture" in tag_set:
            conditions.append("低波后突破或放量上行阶段")
        if "mean_reversion" in tag_set:
            conditions.append("震荡区间、超卖反弹或均值回归阶段")
        if "defensive_timing" in tag_set:
            conditions.append("高波动、下跌或风险释放阶段")
        return conditions or ["需结合组合层画像和复盘进一步判断"]

    @staticmethod
    def _infer_weak_conditions(tags: list[str]) -> list[str]:
        tag_set = set(tags)
        conditions = []
        if "mean_reversion" in tag_set:
            conditions.append("单边趋势阶段可能提前离场或逆势。")
        if "trend_following" in tag_set or "breakout_capture" in tag_set:
            conditions.append("震荡反复阶段可能出现假突破或频繁止损。")
        if "cash_heavy" in tag_set:
            conditions.append("强上涨但信号触发慢时可能踏空。")
        return conditions or ["暂无明确弱势场景。"]

    @staticmethod
    def _infer_risk_notes(tags: list[str], stats: dict[str, Any], symbol: str) -> list[str]:
        notes = []
        if stats.get("zero_ratio", 0) > 0.55:
            notes.append(f"{symbol} 信号空仓比例较高，直接乘法融合容易压低总体仓位。")
        if stats.get("change_count", 0) > max(stats.get("date_count", 1) * 0.35, 1):
            notes.append(f"{symbol} 信号变化较频繁，组合层需要关注换手。")
        if "mean_reversion" in tags:
            notes.append("均值回归信号在趋势单边行情中可能提前离场。")
        return notes

    @staticmethod
    def _bucket(value: float, *, low: float, high: float) -> str:
        if value >= high:
            return "high"
        if value >= low:
            return "medium"
        return "low"

    @staticmethod
    def _grade(value: float) -> str:
        if value >= 0.75:
            return "A"
        if value >= 0.60:
            return "B"
        if value >= 0.45:
            return "C"
        return "D"

    @staticmethod
    def _clip01(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if np.isnan(parsed) or np.isinf(parsed):
            return None
        return parsed

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if np.isnan(parsed) or np.isinf(parsed):
            return None
        return parsed

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value)).strip("_") or "unknown"

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _fmt(value: Any) -> str:
        if value is None:
            return "N/A"
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return str(value)
        if np.isnan(parsed) or np.isinf(parsed):
            return "N/A"
        return f"{parsed:.4f}"
