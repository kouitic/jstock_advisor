"""Shadow運用比較レポート(実装プラン修正6、コードレビュー対応)。

指定銘柄(または全保有銘柄)を現在のデータで旧方式(SellSignalService)・
新方式(HoldingDecisionService)の両方にかけ、判定・スコア・通知有無の差分に
加えて、backtest.pyのBacktestRowには含まれない詳細情報(coverage・
ハードゲート・主な加点/減点理由)を1行にまとめて出力する。運用者がshadow
モードで新旧の乖離を確認し、本稼働へ切り替えてよいか判断するための道具。

非保有銘柄は旧方式(SellSignalService)を評価しない(架空の取得単価・保有期間に
よる誤評価を防ぐ)。新方式は非保有銘柄でも安全に評価できる(placeholder_holding
参照)。
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.enums import ExecutionPlanReason
from jstock_advisor.domain.entities.holding_decision import ReasonImpact
from jstock_advisor.services.holding_decision_backtest_service import placeholder_holding
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

_CSV_HEADER = (
    "stock_code",
    "legacy_category",
    "new_category",
    "score",
    "category_diff",
    "should_notify_diff",
    "coverage_overall",
    "hard_gate_triggered",
    "hard_gate_reason_codes",
    "positive_reasons",
    "negative_reasons",
)


class ShouldNotifyComparison(StrEnum):
    """legacy_should_notify/new_should_notifyのいずれかがNone(非保有銘柄で
    旧方式を評価していない等)の場合、bool比較(None != False → True)による
    誤った差分表示を避けるため三値で表現する(コードレビュー対応)。"""

    MATCH = "MATCH"
    DIFFERENT = "DIFFERENT"
    NOT_COMPARABLE = "NOT_COMPARABLE"


def _format_reasons(reasons: tuple[ReasonImpact, ...]) -> str:
    return "; ".join(f"{r.reason_code}({r.score_impact:+.1f})" for r in reasons)


@dataclass(frozen=True)
class CompareRow:
    stock_code: str
    # 旧方式の判定(推奨が無ければ"HOLD"、非保有銘柄は"NOT_EVALUATED_NON_HOLDING")
    legacy_category: str
    legacy_should_notify: bool | None  # 非保有銘柄で旧方式を評価していない場合はNone
    legacy_reason_codes: tuple[str, ...]
    new_category: str | None  # 新方式の判定(データ不足等でNoneのことがある)
    new_score: float | None
    new_should_notify: bool | None
    coverage_overall: float | None
    hard_gate_triggered: bool
    hard_gate_reason_codes: tuple[str, ...]
    positive_reasons: tuple[ReasonImpact, ...]
    negative_reasons: tuple[ReasonImpact, ...]
    data_error: str | None = None

    @property
    def category_diff(self) -> str:
        """新旧の判定区分が実質的に一致しているかを5値で要約する。

        「一致」の判定は文字列の完全一致ではなく、両エンジンとも通知に
        値する強さの判定を出しているか(≒is_sell_like相当)で揃える
        (新旧でラベル体系そのものが異なるため)。
        """
        if self.data_error is not None:
            return "データ取得エラー"
        if self.legacy_should_notify is None or self.new_should_notify is None:
            return "対象外(非保有・比較不能)"
        if self.legacy_should_notify and self.new_should_notify:
            return "一致(両方検討)"
        if not self.legacy_should_notify and not self.new_should_notify:
            return "一致(両方見送り)"
        if self.legacy_should_notify and not self.new_should_notify:
            return "差分(旧のみ検討)"
        return "差分(新のみ検討)"

    @property
    def should_notify_diff(self) -> ShouldNotifyComparison:
        if self.legacy_should_notify is None or self.new_should_notify is None:
            return ShouldNotifyComparison.NOT_COMPARABLE
        if self.legacy_should_notify == self.new_should_notify:
            return ShouldNotifyComparison.MATCH
        return ShouldNotifyComparison.DIFFERENT

    def as_csv_row(self) -> tuple[str, ...]:
        return (
            self.stock_code,
            self.legacy_category,
            self.new_category or "",
            "" if self.new_score is None else f"{self.new_score:.2f}",
            self.category_diff,
            self.should_notify_diff.value,
            "" if self.coverage_overall is None else f"{self.coverage_overall:.2f}",
            str(self.hard_gate_triggered),
            ";".join(self.hard_gate_reason_codes),
            _format_reasons(self.positive_reasons),
            _format_reasons(self.negative_reasons),
        )


def run_compare(
    stock_codes: list[str],
    providers: ProviderBundle,
    config: AppConfig,
    now: dt.datetime,
    sell_service: SellSignalService | None = None,
    holding_decision_service: HoldingDecisionService | None = None,
    portfolio_service: PortfolioService | None = None,
) -> list[CompareRow]:
    sell_service = sell_service or SellSignalService(providers=providers, config=config)
    holding_decision_service = holding_decision_service or HoldingDecisionService(providers, config)
    portfolio = portfolio_service or PortfolioService()

    rows: list[CompareRow] = []
    for stock_code in stock_codes:
        snapshot, error = build_stock_snapshot(providers, stock_code, now, config)
        if snapshot is None:
            rows.append(
                CompareRow(
                    stock_code=stock_code,
                    legacy_category="DATA_ERROR",
                    legacy_should_notify=None,
                    legacy_reason_codes=(),
                    new_category=None,
                    new_score=None,
                    new_should_notify=None,
                    coverage_overall=None,
                    hard_gate_triggered=False,
                    hard_gate_reason_codes=(),
                    positive_reasons=(),
                    negative_reasons=(),
                    data_error=error,
                )
            )
            continue

        holding = portfolio.get_holding(stock_code)
        legacy_category: str
        legacy_should_notify: bool | None
        legacy_reason_codes: tuple[str, ...]
        if holding is None:
            legacy_category = "NOT_EVALUATED_NON_HOLDING"
            legacy_should_notify = None
            legacy_reason_codes = ()
        else:
            legacy_outcome = sell_service.analyze(holding, now, snapshot=snapshot)
            legacy_category = (
                legacy_outcome.recommendation.recommendation_type.value
                if legacy_outcome.recommendation is not None
                else "HOLD"
            )
            legacy_should_notify = legacy_outcome.recommendation is not None
            legacy_reason_codes = legacy_outcome.triggered_rule_names

        new_holding = holding or placeholder_holding(stock_code, now)
        new_outcome = holding_decision_service.evaluate(
            new_holding,
            now,
            ExecutionPlanReason.NORMAL_SHADOW,
            snapshot=snapshot,
            legacy_reason_codes=legacy_reason_codes,
        )

        if new_outcome.integrity_error or new_outcome.result is None:
            rows.append(
                CompareRow(
                    stock_code=stock_code,
                    legacy_category=legacy_category,
                    legacy_should_notify=legacy_should_notify,
                    legacy_reason_codes=legacy_reason_codes,
                    new_category="DATA_INTEGRITY_ERROR" if new_outcome.integrity_error else None,
                    new_score=None,
                    new_should_notify=None,
                    coverage_overall=None,
                    hard_gate_triggered=False,
                    hard_gate_reason_codes=(),
                    positive_reasons=(),
                    negative_reasons=(),
                )
            )
            continue

        result = new_outcome.result
        rows.append(
            CompareRow(
                stock_code=stock_code,
                legacy_category=legacy_category,
                legacy_should_notify=legacy_should_notify,
                legacy_reason_codes=legacy_reason_codes,
                new_category=result.category.value,
                new_score=result.final_score,
                new_should_notify=result.should_notify,
                coverage_overall=result.coverage.overall,
                hard_gate_triggered=result.hard_gate.triggered,
                hard_gate_reason_codes=result.hard_gate.reason_codes,
                positive_reasons=result.positive_reasons,
                negative_reasons=result.negative_reasons,
            )
        )
    return rows


def write_compare_csv(rows: list[CompareRow], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(_CSV_HEADER)
        for row in rows:
            writer.writerow(row.as_csv_row())
