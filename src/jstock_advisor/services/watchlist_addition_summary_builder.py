"""ウォッチリスト追加通知のPresentation DTO組み立て(LINE通知品質改善、2026-08)。

このモジュールは通知チャネル(LINE等)を一切知らない。`WatchlistAdditionSummary`
(Presentation DTO)の組み立てのみを行い、実際のレンダリング・送信は
`line_notification_service.py`(またはSlack等の将来の他チャネル)側が担当する。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from jstock_advisor.config.models import (
    WatchlistScreeningScoringConfig,
    WatchlistScreeningThresholds,
)
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.ranking import RankingResult
from jstock_advisor.domain.signals.watchlist_screening import WatchlistScoreDetail
from jstock_advisor.services.watchlist_score_detail import SCORE_CRITERION_DEFINITIONS

_MAX_HIGHLIGHTS_PER_ITEM = 3

_SCREENING_POLICY_LABELS: dict[str, str] = {
    "high_dividend_financial_health": "高配当・財務健全性",
    "multi_style_monitoring": "高配当・連続増配・成長・割安・優良(複合スタイル監視)",
}


@dataclass(frozen=True)
class EvaluationHighlight:
    """1銘柄・1配点項目分の「評価が高かった理由」表示用データ。
    ウォッチリスト通知に限らず、保有銘柄・BUY通知等でも再利用できる汎用的な
    命名とする(本ラウンドでの実際の適用はウォッチリスト追加通知のみ)。
    """

    label: str
    detail: str  # 実測値の文字列。無ければ実スコアの文字列
    score: float


@dataclass(frozen=True)
class WatchlistAdditionItemView:
    stock_code: str
    display_name: str
    rank: int
    total_score: float
    highlights: list[EvaluationHighlight]


@dataclass(frozen=True)
class WatchlistAdditionSummary:
    """ウォッチリスト追加通知のPresentation DTO(LINE等の通知チャネルには
    非依存)。順位の母数はranked_countのみで管理し、item側に個別の母数
    フィールド(rank_out_of等)は持たせない。
    """

    policy_name: str
    policy_label: str
    policy_conditions: list[str]
    total_target_count: int
    ranked_count: int
    data_unavailable_count: int
    added_count: int
    addition_rate_pct: float
    evaluated_at: dt.datetime
    items: list[WatchlistAdditionItemView]


def _policy_label(policy_name: str) -> str:
    return _SCREENING_POLICY_LABELS.get(policy_name, policy_name)


def build_evaluation_highlights(detail: WatchlistScoreDetail | None) -> list[EvaluationHighlight]:
    if detail is None:
        return []
    positive = [c for c in detail.criteria if c.score > 0]
    top = sorted(positive, key=lambda c: -c.score)[:_MAX_HIGHLIGHTS_PER_ITEM]
    return [
        EvaluationHighlight(
            label=c.label,
            detail=c.metric_value if c.metric_value is not None else f"{c.score:.1f}点",
            score=c.score,
        )
        for c in top
    ]


_MULTI_STYLE_MONITORING_CONDITIONS: list[str] = [
    "高配当・連続増配・成長・割安・優良のいずれか1タイプ以上に該当",
    "重大リスク(債務超過・継続企業の前提に重大な疑義・開示リスクキーワード・"
    "流動性不足・重大な業績悪化・ETF/REIT)に該当しない",
]


def describe_screening_policy_conditions(
    scoring_config: WatchlistScreeningScoringConfig,
    thresholds_config: WatchlistScreeningThresholds,
    policy_name: str = "high_dividend_financial_health",
) -> list[str]:
    """評価ポリシーの条件をconfig値から動的に生成する。criterion名による
    if分岐は行わず、SCORE_CRITERION_DEFINITIONSのdescribe_conditionを
    そのまま呼び出すのみ(high_dividend_financial_health向け)。

    multi_style_monitoringは価格・スコア閾値方式ではなく「対象タイプへの該当
    有無」で合否判定するため(ウォッチリスト自動追加基準の再設計、2026-08)、
    scoring_config/thresholds_configには依存しない固定の説明文を返す。
    """
    if policy_name == "multi_style_monitoring":
        return list(_MULTI_STYLE_MONITORING_CONDITIONS)
    lines = [
        definition.describe_condition(scoring_config) for definition in SCORE_CRITERION_DEFINITIONS
    ]
    if thresholds_config.require_positive_operating_cash_flow:
        lines.append("営業CFプラス")
    if thresholds_config.exclude_dividend_cut_announced:
        lines.append("重大な減配なし")
    lines.append(f"総合スコア{scoring_config.minimum_total_score:.1f}点以上")
    return lines


def build_watchlist_addition_summary(
    *,
    added_items: list[WatchlistItem],
    total_score_by_code: dict[str, float],
    notification_detail_by_code: dict[str, WatchlistScoreDetail],
    rank_by_code: dict[str, RankingResult],
    total_target_count: int,
    ranked_count: int,
    data_unavailable_count: int,
    policy_name: str,
    scoring_config: WatchlistScreeningScoringConfig,
    thresholds_config: WatchlistScreeningThresholds,
    evaluated_at: dt.datetime,
) -> WatchlistAdditionSummary:
    """`added_items`(WatchlistRepository.add_if_new()が実際にTrueを返した銘柄、
    stock_nameは呼び出し側で既にStockDisplayNameResolver.resolve()済みである
    こと)からPresentation DTOを組み立てる。
    """
    ordered_items = sorted(
        added_items,
        key=lambda item: (-total_score_by_code.get(item.stock_code, 0.0), item.stock_code),
    )
    items = [
        WatchlistAdditionItemView(
            stock_code=item.stock_code,
            display_name=item.stock_name or item.stock_code,
            rank=rank_by_code[item.stock_code].rank,
            total_score=total_score_by_code[item.stock_code],
            highlights=build_evaluation_highlights(
                notification_detail_by_code.get(item.stock_code)
            ),
        )
        for item in ordered_items
    ]
    added_count = len(items)
    addition_rate_pct = (added_count / total_target_count * 100) if total_target_count else 0.0
    return WatchlistAdditionSummary(
        policy_name=policy_name,
        policy_label=_policy_label(policy_name),
        policy_conditions=describe_screening_policy_conditions(
            scoring_config, thresholds_config, policy_name=policy_name
        ),
        total_target_count=total_target_count,
        ranked_count=ranked_count,
        data_unavailable_count=data_unavailable_count,
        added_count=added_count,
        addition_rate_pct=addition_rate_pct,
        evaluated_at=evaluated_at,
        items=items,
    )
