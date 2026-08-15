"""ウォッチリスト追加理由(スコア根拠)の構造化データ生成(LINE通知品質改善、2026-08)。

`ScoreCriterionDefinition`のリスト(`SCORE_CRITERION_DEFINITIONS`)が
ラベル・実測値抽出・config条件文生成を一元管理する。通知生成側
(watchlist_addition_summary_builder.py)に`if criterion_key == "..."`のような
分岐は一切存在させない。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from jstock_advisor.config.models import WatchlistScreeningScoringConfig
from jstock_advisor.domain.signals.watchlist_screening import (
    ScoreCriterionValue,
    WatchlistScoreDetail,
)
from jstock_advisor.services.screening_data_provider import WatchlistScreeningInput

# RankingEntry(MAX_RANKING_ENTRY_BYTES=500)とは独立した予算。合格銘柄1件分の
# スコア根拠(criterion 5件程度)は通常この予算に十分収まる。
MAX_NOTIFICATION_DETAIL_BYTES = 600


@dataclass(frozen=True)
class ScoreCriterionDefinition:
    criterion_key: str
    label: str
    extract_metric: Callable[[WatchlistScreeningInput], str | None]
    describe_condition: Callable[[WatchlistScreeningScoringConfig], str]


def _dividend_yield_metric(i: WatchlistScreeningInput) -> str | None:
    return f"{i.dividend_yield_pct:.1f}%" if i.dividend_yield_pct is not None else None


def _dividend_yield_condition(c: WatchlistScreeningScoringConfig) -> str:
    return f"配当利回り{c.dividend_yield.full_at_pct:.1f}%以上(満点)"


def _equity_ratio_metric(i: WatchlistScreeningInput) -> str | None:
    return f"{i.equity_ratio_pct:.1f}%" if i.equity_ratio_pct is not None else None


def _equity_ratio_condition(c: WatchlistScreeningScoringConfig) -> str:
    return f"自己資本比率{c.equity_ratio.full_at_pct:.1f}%以上(満点)"


def _payout_ratio_metric(i: WatchlistScreeningInput) -> str | None:
    return f"{i.payout_ratio_pct:.1f}%" if i.payout_ratio_pct is not None else None


def _payout_ratio_condition(c: WatchlistScreeningScoringConfig) -> str:
    return f"配当性向{c.payout_ratio.healthy_min_pct:.1f}〜{c.payout_ratio.healthy_max_pct:.1f}%"


def _dividend_growth_metric(i: WatchlistScreeningInput) -> str | None:
    years = i.consecutive_dividend_increase_years
    return f"{years}年連続" if years is not None and years > 0 else None


def _dividend_growth_condition(c: WatchlistScreeningScoringConfig) -> str:
    return f"増配{c.dividend_growth.full_at_years}年以上(満点)"


def _shareholder_benefit_metric(i: WatchlistScreeningInput) -> str | None:
    if i.shareholder_benefit_yield_pct is not None:
        return f"利回り{i.shareholder_benefit_yield_pct:.1f}%"
    return "あり" if i.shareholder_benefit_exists else None


def _shareholder_benefit_condition(c: WatchlistScreeningScoringConfig) -> str:
    return f"株主優待利回り{c.shareholder_benefit.yield_full_at_pct:.1f}%以上(満点)"


# score_breakdown(HighDividendFinancialHealthPolicy.evaluate()が返すdict)の
# キーと一致させること。
SCORE_CRITERION_DEFINITIONS: list[ScoreCriterionDefinition] = [
    ScoreCriterionDefinition(
        "dividend_yield", "配当利回り", _dividend_yield_metric, _dividend_yield_condition
    ),
    ScoreCriterionDefinition(
        "equity_ratio", "自己資本比率", _equity_ratio_metric, _equity_ratio_condition
    ),
    ScoreCriterionDefinition(
        "payout_ratio", "配当性向", _payout_ratio_metric, _payout_ratio_condition
    ),
    ScoreCriterionDefinition(
        "dividend_growth", "増配実績", _dividend_growth_metric, _dividend_growth_condition
    ),
    ScoreCriterionDefinition(
        "shareholder_benefit",
        "株主優待",
        _shareholder_benefit_metric,
        _shareholder_benefit_condition,
    ),
]


# multi_style_monitoring Policy(ウォッチリスト自動追加基準の再設計、2026-08)の
# MonitoringScore内訳キー(domain/signals/watchlist_screening.py::
# MultiStyleMonitoringPolicy.evaluate()のbreakdown辞書キーと一致させること)の
# 表示ラベル。旧PolicyのSCORE_CRITERION_DEFINITIONSとは配点の性質が異なる
# (「対象タイプへの該当有無」ベースであり、config駆動の実測値抽出を持たない)
# ため、別のシンプルな辞書として管理する。
_MULTI_STYLE_MONITORING_SCORE_LABELS: dict[str, str] = {
    "base": "対象タイプ該当",
    "type_bonus": "複合タイプ",
    "equity_ratio_bonus": "自己資本比率良好",
    "cashflow_bonus": "営業CFプラス",
    "no_deficit_bonus": "黒字",
    "no_dividend_cut_bonus": "減配なし",
    "market_cap_bonus": "時価総額規模",
}


def build_notification_detail(
    stock_code: str,
    score_breakdown: dict[str, float],
    input: WatchlistScreeningInput,
    policy_name: str = "high_dividend_financial_health",
) -> WatchlistScoreDetail | None:
    """score_breakdown(ScreeningPolicyResult.score_breakdown)の値と、configベース
    のメタデータ(SCORE_CRITERION_DEFINITIONS)を組み合わせてWatchlistScoreDetailを
    組み立てる。MAX_NOTIFICATION_DETAIL_BYTESを超過する場合はNoneを返す
    (呼び出し側はnotification_detailをNoneのまま保存し、highlightsが空の通知と
    なるが通知全体は送信される)。

    policy_name="multi_style_monitoring"の場合、score_breakdownのキーをそのまま
    _MULTI_STYLE_MONITORING_SCORE_LABELSで表示ラベル化する(実測値抽出は行わず、
    build_evaluation_highlights()側のスコア数値フォールバック表示に委ねる)。
    """
    if policy_name == "multi_style_monitoring":
        criteria = [
            ScoreCriterionValue(
                criterion_key=key,
                label=_MULTI_STYLE_MONITORING_SCORE_LABELS.get(key, key),
                score=value,
                metric_value=None,
            )
            for key, value in score_breakdown.items()
        ]
    else:
        criteria = [
            ScoreCriterionValue(
                criterion_key=definition.criterion_key,
                label=definition.label,
                score=score_breakdown.get(definition.criterion_key, 0.0),
                metric_value=definition.extract_metric(input),
            )
            for definition in SCORE_CRITERION_DEFINITIONS
        ]
    detail = WatchlistScoreDetail(stock_code=stock_code, criteria=criteria)
    if len(detail.model_dump_json().encode("utf-8")) > MAX_NOTIFICATION_DETAIL_BYTES:
        return None
    return detail
