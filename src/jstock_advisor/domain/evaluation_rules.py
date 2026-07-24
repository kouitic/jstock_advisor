"""推奨の定点評価ラベル判定(要求仕様29〜36節)。

推奨後の株価実績(自社株・ベンチマーク)のみから、機械的に判定できる範囲で
EvaluationLabelを決定する。判定に必要なデータが揃わない場合は憶測せず
DATA_ISSUE/INCONCLUSIVEとする(要求仕様12節「推測で補完しない」原則)。

LATE/PROFIT_TAKE_TOO_LATEは、推奨"前"の株価推移(いつ本来売るべきだったか)が
必要になるため、現時点の実装では自動付与しない(将来、価格履歴の遡り取得に
対応した際の拡張ポイントとする)。
"""

from __future__ import annotations

from jstock_advisor.config.models import EvaluationRulesConfig
from jstock_advisor.domain.entities.enums import EvaluationLabel, RecommendationType

_ENTRY_TYPES = (RecommendationType.BUY, RecommendationType.WATCH_BUY, RecommendationType.HOLD)
_EXIT_TYPES = (
    RecommendationType.PARTIAL_PROFIT_TAKE,
    RecommendationType.FULL_PROFIT_TAKE,
    RecommendationType.SELL,
    RecommendationType.URGENT_REVIEW,
)
_PROFIT_TAKE_TYPES = (RecommendationType.PARTIAL_PROFIT_TAKE, RecommendationType.FULL_PROFIT_TAKE)


def determine_evaluation_label(
    recommendation_type: RecommendationType,
    price_return_pct: float | None,
    excess_return_pct: float | None,
    max_drawdown_pct: float | None,
    config: EvaluationRulesConfig,
) -> tuple[EvaluationLabel, str]:
    if price_return_pct is None:
        return EvaluationLabel.DATA_ISSUE, "評価時点の株価データが取得できませんでした"

    if recommendation_type in _ENTRY_TYPES:
        return _label_entry(price_return_pct, excess_return_pct, max_drawdown_pct, config)
    if recommendation_type in _EXIT_TYPES:
        return _label_exit(recommendation_type, price_return_pct, config)
    return EvaluationLabel.INCONCLUSIVE, f"{recommendation_type.value}は自動評価の対象外です"


def _label_entry(
    price_return_pct: float,
    excess_return_pct: float | None,
    max_drawdown_pct: float | None,
    config: EvaluationRulesConfig,
) -> tuple[EvaluationLabel, str]:
    if max_drawdown_pct is not None and max_drawdown_pct <= config.severe_decline_after_buy_pct:
        return (
            EvaluationLabel.RISK_UNDERESTIMATED,
            f"推奨後の最大下落率が{max_drawdown_pct:.1f}%に達し、想定リスクを超える下落が発生しました",
        )
    if price_return_pct > 0:
        if excess_return_pct is not None and excess_return_pct > 0:
            return (
                EvaluationLabel.SUCCESS,
                f"株価は{price_return_pct:.1f}%上昇し、ベンチマークを"
                f"{excess_return_pct:.1f}%上回りました",
            )
        return (
            EvaluationLabel.ACCEPTABLE,
            f"株価は{price_return_pct:.1f}%上昇しましたが、ベンチマーク対比では優位ではありませんでした",
        )
    return (
        EvaluationLabel.PRICE_TOO_HIGH,
        f"株価は{price_return_pct:.1f}%下落し、推奨価格が割高だった可能性があります",
    )


def _label_exit(
    recommendation_type: RecommendationType,
    price_return_pct: float,
    config: EvaluationRulesConfig,
) -> tuple[EvaluationLabel, str]:
    exit_cfg = config.exit_evaluation
    if price_return_pct <= exit_cfg.decline_confirms_good_call_pct:
        return (
            EvaluationLabel.SUCCESS,
            f"推奨後に株価は{price_return_pct:.1f}%下落しており、判断は妥当でした",
        )
    if price_return_pct >= exit_cfg.rally_flags_too_early_or_too_sensitive_pct:
        if recommendation_type in _PROFIT_TAKE_TYPES:
            return (
                EvaluationLabel.PROFIT_TAKE_TOO_EARLY,
                f"推奨後に株価はさらに{price_return_pct:.1f}%上昇しており、利確が早すぎた可能性があります",
            )
        return (
            EvaluationLabel.SELL_TOO_SENSITIVE,
            f"推奨後に株価は{price_return_pct:.1f}%上昇して回復しており、判定が過敏だった可能性があります",
        )
    return (
        EvaluationLabel.ACCEPTABLE,
        f"推奨後の株価変動は{price_return_pct:.1f}%にとどまり、明確な結論は得られませんでした",
    )
