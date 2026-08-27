"""銘柄分析(Phase 2-B、LINE表示専用、2026-08)。

既存の投資判断結果(Recommendation/BuyCandidateEvaluationRecord/
HoldingEvaluationRecord)をユーザーへ分かりやすく説明するだけの読み取り専用
サービス。判定ロジック・スコア・価格・数量計算は一切行わない(既存の
`decide_buy_action()`/`compute_score()`/`evaluate_profit_taking()`等の投資
判断コードを一切呼び出さない)。

文言設計ルール(Phase 2-B文章仕様最終案で承認済み):
1. あるフィールドが結果値のみを保存しており、その要因を特定の言葉で
   言い切れない場合(例: buy_price_reliability=LOWの内部要因)は、
   「〜が原因です」ではなく「詳細な要因は現行データからは区別できません」
   という事実ベースの表現に留める(断定禁止)。
2. 「懸念なし」を明示的に判定・保存していないフィールドについては、
   「特に懸念はありません」と書かない。該当データが無ければセクション
   自体を省略する。
3. 実際の通知権限を持たないエンジン(SHADOW中のholding_decision_service)
   由来の情報は、初期実装では一切表示しない(参考評価と判定根拠を混同
   させないため)。
4. 数量算出ロジックを持たないエンジン(Legacy SELL・HoldingDecisionScore)
   の判定結果には、他エンジンの数量ロジックを合成表示しない。
5. 保存されていない情報は「保存されていない」と正直に示す。取得できな
   かった理由そのものを推測して書かない。
"""

from __future__ import annotations

from typing import Any

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
)
from jstock_advisor.domain.entities.common import BuyPriceLevels, ScoreBreakdown
from jstock_advisor.domain.entities.enums import BuyAction, PurchaseCategory, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.valuation.valuation_confidence import (
    CODE_NO_VALID_VALUATION_METHODS,
    CODE_TOO_FEW_VALUATION_METHODS,
    CODE_VALUATION_ANCHOR_CALCULATION_FAILED,
    CODE_VALUATION_DISPERSION_TOO_HIGH,
)
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_evaluation_record_repository import (
    HoldingEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.latest_batch_records_provider import (
    STILL_PROPAGATING_MESSAGE,
    LatestBatchStillPropagating,
    fetch_latest_normal_batch_records,
)
from jstock_advisor.services.watchlist_display_name import StockDisplayNameResolver

_UNRESTORABLE = "現行データでは、判定根拠の詳細を復元できません。"

# domain/signals/buy_signal.py::_SCORE_LABELSと同じ7項目・同じラベル
# (投資判断モジュールへの依存を避けるため、watchlist_judgment_summary_
# formatter.pyと同様に値のみ独立して定義する。既存定数の変更に追従する
# 必要が生じた場合はここも合わせて見直すこと)。
_SCORE_COMPONENT_LABELS: dict[str, str] = {
    "total_yield_attractiveness": "総合利回りの魅力度",
    "dividend_sustainability": "配当持続性",
    "financial_health": "財務健全性",
    "undervaluation": "割安度",
    "shareholder_benefit_value": "株主優待価値",
    "earnings_stability": "業績安定性",
    "price_stability": "株価安定性",
}
# buy_signal.py::_STRONG_SCORE_RATIO/_WEAK_SCORE_RATIOと同じ値(意図的に同期。
# 表示層で新しい閾値を作らず、既存の「強い/弱い」判定基準をそのまま流用する)。
_STRONG_SCORE_RATIO = 0.7
_WEAK_SCORE_RATIO = 0.3

_BUY_ACTION_LABEL: dict[BuyAction, str] = {
    BuyAction.STRONG_BUY: "積極買い候補",
    BuyAction.BUY: "買い候補",
    BuyAction.SMALL_ENTRY: "打診購入候補",
    BuyAction.WATCH_FOR_PRICE: "監視継続（価格待ち）",
    BuyAction.WATCH_BEFORE_EARNINGS: "監視継続（決算発表待ち）",
    BuyAction.MANUAL_REVIEW: "要確認",
    BuyAction.NOT_ATTRACTIVE: "購入見送り",
    BuyAction.EXCLUDED: "買い対象外",
    BuyAction.DATA_INSUFFICIENT: "データ不足",
}

# PurchaseCategoryにはあるがBuyActionには存在しない区分(EXCLUDED/
# DATA_INSUFFICIENT/FAILED)の判定ラベル。final_buy_actionがNoneになりうる
# これらのケースは、BuyActionではなくPurchaseCategory側から直接ラベルを
# 決定する。
_PURCHASE_CATEGORY_JUDGMENT_LABEL: dict[PurchaseCategory, str] = {
    PurchaseCategory.EXCLUDED: "買い対象外",
    PurchaseCategory.DATA_INSUFFICIENT: "データ不足",
    PurchaseCategory.FAILED: "処理失敗",
}

# raw_buy_action(価格条件のみによる仮判定)のBuyAction→
# BuyDecisionRulesConfig.score_thresholdsのフィールド名。SCORE_BELOW_
# THRESHOLDによる格下げが実際にどの基準を下回ったかを、判定時点の
# company_quality_scoreとscore_thresholdsスナップショットの比較から
# 一意に特定する(BuyDecisionReason.threshold_valueは常にwatch閾値のみを
# 記録する精度限界があるため、この比較で補う)。
# レビュー対応(2026-08、本番実データUATで発覚): domain/signals/buy_decision.py
# のスコア格下げカスケード(STRONG_BUY→BUY→SMALL_ENTRY→WATCH_FOR_PRICE→
# NOT_ATTRACTIVE)を確認したところ、raw_action(価格条件のみの仮判定)として
# 実際に到達しうるのはSTRONG_BUY/BUY/SMALL_ENTRY/WATCH_FOR_PRICEの4種のみ
# (価格条件自体でNOT_ATTRACTIVEになった銘柄はこのカスケード自体を通らない)。
# WATCH_FOR_PRICE→"watch"の対応が抜けていたため、score_thresholdsスナップ
# ショットが実際に保存されているにもかかわらず「スナップショットが無い」と
# 誤表示する事実矛盾があった。4種全てを網羅したことで、他のBuyActionが
# raw_actionとして渡ることは無く、同種の抜けは存在しない。
_TIER_THRESHOLD_FIELD: dict[BuyAction, str] = {
    BuyAction.STRONG_BUY: "strong_buy",
    BuyAction.BUY: "buy",
    BuyAction.SMALL_ENTRY: "small_entry",
    BuyAction.WATCH_FOR_PRICE: "watch",
}


def _buy_price_lines(buy_prices: BuyPriceLevels | None) -> list[str]:
    if buy_prices is None:
        return []
    lines: list[str] = []
    if buy_prices.strong is not None:
        lines.append(f"積極買付：{buy_prices.strong.price}円以下")
    if buy_prices.standard is not None:
        lines.append(f"標準買付：{buy_prices.standard.price}円以下")
    if buy_prices.entry is not None:
        lines.append(f"打診買付：{buy_prices.entry.price}円以下")
    return lines


def _decimal_str_to_display(value: Any, digits: int = 2) -> str | None:
    """buy_score_input_facts内のstr化されたDecimal値を表示用に整形する
    (JSON保存のためstr化された値を、そのまま長い桁数で表示しないため)。"""
    if value is None:
        return None
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return None


# ratio, score, max_weight, field_name, label
_RankedComponent = tuple[float, float, float, str, str]


def _rank_score_components(
    score_breakdown: ScoreBreakdown, weights: dict[str, Any]
) -> tuple[list[_RankedComponent], list[_RankedComponent]]:
    """既存の判定結果(score_breakdown、判定時点の実点数)と判定時点weight
    (config_values_used["scoring_weights"]スナップショット)だけを使い、
    配点比の高い順/低い順に項目をランキングする(表示専用の集計処理)。

    採否の閾値はdomain/signals/buy_signal.py::score_areas()が既に使っている
    _STRONG_SCORE_RATIO(0.7)/_WEAK_SCORE_RATIO(0.3)をそのまま流用し、表示層で
    新しい評価基準・投資判断ルールを新設しない。score_areas()自体は閾値で
    フィルタするだけで大きい順に並べる機能を持たないため、ここでは実際の
    配点比でsorted()し、真に上位/下位の項目だけを選ぶ(6節で提案した設計)。
    field_nameを併せて返し、呼び出し側が項目別の事実文を組み立てられるように
    する(レビュー対応2026-08、修正条件1)。
    """
    entries: list[_RankedComponent] = []
    for field_name, label in _SCORE_COMPONENT_LABELS.items():
        max_weight = weights.get(field_name)
        score = getattr(score_breakdown, field_name, None)
        if not max_weight or score is None:
            continue
        entries.append((score / max_weight, float(score), float(max_weight), field_name, label))

    positive_candidates = sorted(
        (e for e in entries if e[0] >= _STRONG_SCORE_RATIO), key=lambda e: e[0], reverse=True
    )[:3]
    negative_candidates = sorted(
        (e for e in entries if e[0] < _WEAK_SCORE_RATIO), key=lambda e: e[0]
    )[:3]
    return positive_candidates, negative_candidates


# undervaluation項目の解釈文組み立てに使う、UndervaluationSignals(compute_score()が
# 保存するinput_facts["undervaluation_signals"]、6シグナルの真偽値)そのものの
# 自然文訳。domain/scoring/score.py::UndervaluationSignalsのフィールド名・意味と
# 完全に対応しており、表示層で新しい割安判定基準を作るものではない(既存シグナルの
# 言い換えのみ)。
# レビュー対応(2026-08、事実反転バグ修正): True時とFalse時で意味が逆になるため、
# 同じラベルをis_positiveの真偽で使い回さず、シグナルごとにTrue用/False用の
# 文言を明示的に分けて持つ(表示専用の修正。UndervaluationSignals自体の算出
# ロジックは変更しない)。
_UNDERVALUATION_SIGNAL_LABELS_TRUE: dict[str, str] = {
    "per_below_median": "PERが自社の過去中央値を下回っている",
    "pbr_below_median": "PBRが自社の過去中央値を下回っている",
    "dividend_yield_above_historical_average": "配当利回りが自社の過去平均を上回っている",
    "drawdown_from_52w_high": "52週高値から一定以上下落している",
    "below_fair_value": "現在値が算出された適正価格を下回っている",
    "price_down_despite_stable_earnings": "業績は安定している一方で株価が下落している",
}
_UNDERVALUATION_SIGNAL_LABELS_FALSE: dict[str, str] = {
    "per_below_median": "PERは自社の過去中央値を下回っていない",
    "pbr_below_median": "PBRは自社の過去中央値を下回っていない",
    "dividend_yield_above_historical_average": "配当利回りは自社の過去平均を上回っていない",
    "drawdown_from_52w_high": "52週高値から一定以上の下落には該当していない",
    "below_fair_value": "現在値は算出された適正価格を下回っていない",
    "price_down_despite_stable_earnings": "「業績安定下の株価下落」条件には該当していない",
}


def _component_fact_clause(field_name: str, facts: dict[str, Any], is_positive: bool) -> str | None:
    """該当スコア項目について、判定時点に実際に保存された入力事実
    (buy_score_input_facts)のみから、その項目のスコアへの寄与を説明する
    1文(語尾の句点なし)を組み立てる。表示層で新しい投資判断基準・PER/PBRの
    絶対水準による割安判定を作らない(修正条件1)。参照する事実自体が
    保存されていない場合はNoneを返し、呼び出し側で該当行を省略する。
    """
    if field_name == "total_yield_attractiveness":
        pct = facts.get("total_yield_pct")
        if pct is None:
            return None
        return f"総合利回り(配当+優待)は{pct:.2f}%です"
    if field_name == "dividend_sustainability":
        parts = []
        if facts.get("is_progressive_or_doe_policy"):
            parts.append("累進配当/DOE方針を採用")
        years = facts.get("consecutive_dividend_increase_years")
        if years:
            parts.append(f"連続増配{years}年")
        payout = facts.get("payout_ratio_pct")
        if payout is not None:
            parts.append(f"配当性向{payout:.1f}%")
        if not parts:
            return None
        return "、".join(parts)
    if field_name == "financial_health":
        equity = facts.get("equity_ratio_pct")
        if equity is None:
            return "自己資本比率のデータがありません"
        return f"自己資本比率は{equity:.1f}%です"
    if field_name == "undervaluation":
        signals: dict[str, bool] = facts.get("undervaluation_signals") or {}
        labels = (
            _UNDERVALUATION_SIGNAL_LABELS_TRUE
            if is_positive
            else _UNDERVALUATION_SIGNAL_LABELS_FALSE
        )
        matched = [
            label for name, label in labels.items() if signals.get(name) is is_positive
        ]
        if not matched:
            return None
        return "、".join(matched)
    if field_name == "shareholder_benefit_value":
        benefit = facts.get("benefit_yield_pct")
        if not benefit:
            return "株主優待利回りのデータがないか、優待がありません"
        return f"株主優待利回りは{benefit:.2f}%です"
    if field_name == "earnings_stability":
        ratio = facts.get("operating_income_non_decrease_ratio")
        if ratio is None:
            return "四半期業績データが不足しています"
        return f"四半期営業利益が前期比で悪化しなかった期間の割合は{ratio * 100:.0f}%です"
    if field_name == "price_stability":
        vol = facts.get("annualized_volatility_pct")
        if vol is None:
            return "株価履歴が不足しています"
        return f"年率換算ボラティリティは{vol:.1f}%です"
    return None


def _direction_suffix(label: str, is_positive: bool) -> str:
    if is_positive:
        return f"、{label}評価のプラス要因となっています。"
    return f"、{label}評価の注意材料となっています。"


def _buy_facts_lines(recommendation: Recommendation) -> list[str]:
    """■ 判断根拠となった事実。判定時点に実際に保存された値のみを表示する
    (現在値の再取得・現在configの流用は一切行わない)。"""
    lines = [f"判定時株価：{recommendation.price_at_recommendation}円"]
    if recommendation.company_quality_score is not None:
        lines.append(f"企業魅力度スコア：{recommendation.company_quality_score}点")
    if recommendation.dividend_yield_pct_at_recommendation is not None:
        lines.append(f"配当利回り：{recommendation.dividend_yield_pct_at_recommendation:.2f}%")
    if recommendation.shareholder_benefit_yield_pct_at_recommendation is not None:
        lines.append(
            f"優待利回り：{recommendation.shareholder_benefit_yield_pct_at_recommendation:.2f}%"
        )
    if recommendation.total_yield_pct_at_recommendation is not None:
        lines.append(f"総合利回り：{recommendation.total_yield_pct_at_recommendation:.2f}%")

    facts = recommendation.buy_score_input_facts or {}
    per = _decimal_str_to_display(facts.get("current_per"), digits=1)
    if per is not None:
        median = _decimal_str_to_display(facts.get("historical_per_median"), digits=1)
        suffix = f"（自社の過去中央値{median}倍）" if median is not None else ""
        lines.append(f"PER：{per}倍{suffix}")
    pbr = _decimal_str_to_display(facts.get("current_pbr"), digits=2)
    if pbr is not None:
        median = _decimal_str_to_display(facts.get("historical_pbr_median"), digits=2)
        suffix = f"（自社の過去中央値{median}倍）" if median is not None else ""
        lines.append(f"PBR：{pbr}倍{suffix}")

    equity_ratio = facts.get("equity_ratio_pct")
    if equity_ratio is not None:
        lines.append(f"自己資本比率：{equity_ratio:.1f}%")
    payout_ratio = facts.get("payout_ratio_pct")
    if payout_ratio is not None:
        lines.append(f"配当性向：{payout_ratio:.1f}%")
    dividend_years = facts.get("consecutive_dividend_increase_years")
    if dividend_years:
        lines.append(f"連続増配年数：{dividend_years}年")
    if facts.get("is_progressive_or_doe_policy"):
        lines.append("累進配当/DOE方針：あり")
    income_ratio = facts.get("operating_income_non_decrease_ratio")
    if income_ratio is not None:
        lines.append(f"営業利益が前期比で悪化しなかった割合：{income_ratio * 100:.0f}%")
    volatility = facts.get("annualized_volatility_pct")
    if volatility is not None:
        lines.append(f"年率換算ボラティリティ：{volatility:.1f}%")
    return lines


def _interpretation_bullets(
    entries: list[_RankedComponent], facts: dict[str, Any], is_positive: bool
) -> list[str]:
    bullets: list[str] = []
    for _ratio, score, max_weight, field_name, label in entries:
        clause = _component_fact_clause(field_name, facts, is_positive)
        if clause is not None:
            bullets.append(f"・{clause}{_direction_suffix(label, is_positive)}")
        bullets.append(f"・{label}は{score:.1f}/{max_weight:.0f}点です。")
    return bullets


def _buy_interpretation_lines(recommendation: Recommendation) -> list[str]:
    """■ 解釈。PER/PBR単体の絶対値から独自に「割安」等を断定せず、既存の
    score_breakdown(実際の判定結果)を配点比でランキングしたうえで、各項目
    について保存済みの判定時点事実(buy_score_input_facts)がそのスコアへ
    どう寄与したかを説明する(レビュー対応2026-08、修正条件1)。事実自体が
    保存されていない項目は、スコア行のみを示し文章を捏造しない。
    """
    if recommendation.score_breakdown is None:
        return []
    weights = (recommendation.config_values_used or {}).get("scoring_weights")
    if not weights:
        return []
    facts = recommendation.buy_score_input_facts or {}
    positive, negative = _rank_score_components(recommendation.score_breakdown, weights)
    lines: list[str] = []
    positive_bullets = _interpretation_bullets(positive, facts, True)
    if positive_bullets:
        lines.append("主なプラス材料")
        lines += positive_bullets
    negative_bullets = _interpretation_bullets(negative, facts, False)
    if negative_bullets:
        if lines:
            lines.append("")
        lines.append("注意材料")
        lines += negative_bullets
    return lines


def _score_below_threshold_text(recommendation: Recommendation) -> str | None:
    """SCORE_BELOW_THRESHOLDによる格下げの理由を、判定時点のスコアと
    閾値の実数値で説明する(定性的な「わずかに」「大きく」は使わない)。
    score_thresholdsスナップショット(config_values_used)が無い過去
    データではNoneを返し、呼び出し側が非断定の代替文言を使う。
    """
    thresholds = (recommendation.config_values_used or {}).get("score_thresholds")
    raw_action = recommendation.raw_buy_action
    if thresholds is None or raw_action not in _TIER_THRESHOLD_FIELD:
        return None
    field_name = _TIER_THRESHOLD_FIELD[raw_action]
    threshold_value = thresholds.get(field_name)
    if threshold_value is None or recommendation.company_quality_score is None:
        return None
    tier_label = _BUY_ACTION_LABEL[raw_action]
    return (
        f"価格条件は{tier_label}の水準を満たしていましたが、企業魅力度スコア"
        f"（{recommendation.company_quality_score}点）が{tier_label}の基準"
        f"（{threshold_value}点）を下回ったため、判定を引き下げています。"
    )


def _buy_reason_text(
    record: BuyCandidateEvaluationRecord, recommendation: Recommendation | None
) -> str | None:
    if recommendation is None or not recommendation.buy_decision_reasons:
        return None
    last = recommendation.buy_decision_reasons[-1]
    price = recommendation.price_at_recommendation
    prices = recommendation.buy_prices
    if last.code == "PRICE_TIER":
        final_action = record.final_buy_action
        if final_action == BuyAction.STRONG_BUY and prices and prices.strong:
            return f"判定時点の現在値{price}円は積極買付価格{prices.strong.price}円以内でした。"
        if final_action == BuyAction.BUY and prices and prices.standard:
            return f"判定時点の現在値{price}円は標準買付価格{prices.standard.price}円以内でした。"
        if final_action == BuyAction.SMALL_ENTRY and prices and prices.entry:
            return f"判定時点の現在値{price}円は打診買付価格{prices.entry.price}円以内でした。"
        if final_action == BuyAction.WATCH_FOR_PRICE and prices and prices.entry:
            return (
                f"判定時点の現在値{price}円は打診買付価格{prices.entry.price}円を"
                "上回っていました。"
            )
        return None
    if last.code == "SCORE_BELOW_THRESHOLD":
        return _score_below_threshold_text(recommendation) or (
            "企業魅力度スコアが基準を下回ったため、判定を引き下げています。"
            "（判定時点の閾値スナップショットが無いため、実数値は表示できません）"
        )
    if last.code == "EARNINGS_WINDOW":
        return (
            "価格・企業魅力度とも購入条件を満たしていますが、次回決算発表が近い"
            "ため、決算内容確認後まで新規購入を保留しています。"
        )
    if last.code == "BUY_PRICE_RELIABILITY_LOW":
        return _buy_price_reliability_low_text(recommendation)
    if last.code == "VALUATION_DISPERSION_TOO_HIGH":
        return (
            "適正価格の算出手法間でばらつきが大きく（算出方法によって評価額が"
            "大きく異なる状態）、自動判定を見合わせています。"
        )
    if last.code == "NO_VALUATION_ANCHOR":
        return _no_valuation_anchor_text(recommendation)
    return None


# レビュー対応(2026-08、本番実データUAT横断確認で発覚): recommendation.
# valuation_methods(各適正価格算出方式の結果、既存フィールド)は、方式ごとに
# 実際に採用しなかった理由(exclusion_reason、判定エンジン自身が生成した
# 人が読める文字列)を保持しているにもかかわらず、NO_VALUATION_ANCHORの
# 表示では一切参照せず「具体的な要因は現行データからは区別できません」と
# 一律表示していた(score_thresholds同様、実際には保存されているデータを
# 「無い」と表示する不備)。表示層で新たな除外理由を推測・算出せず、
# 既存フィールドをそのまま使う。
_VALUATION_METHOD_LABELS: dict[str, str] = {
    "target_yield": "配当利回り法",
    "per": "PER法",
    "pbr": "PBR法",
    "historical_range": "価格レンジ法",
    "dcf": "DCF法",
}

# レビュー対応(2026-08、NO_VALUATION_ANCHOR表示不備の是正): Recommendation.
# valuation_methodsには標準5方式に加えて"industry"(業種別モデル)が含まれるが、
# industryは専用モデルが未実装のため全銘柄・常にexclusion_reasonを持つ
# (buy_signal_service.py: industry_model_appliedは常にFalse固定。個別銘柄の
# 判定結果と無関係な恒常的事実であり、industryの理由だけがNO_VALUATION_ANCHOR
# の原因であるかのように誤って表示されていた不備の原因)。標準5方式のみを
# 対象とすることで、この混同を防ぐ。
_STANDARD_VALUATION_METHODS: frozenset[str] = frozenset(_VALUATION_METHOD_LABELS)


def _standard_valuation_method_exclusion_reasons(recommendation: Recommendation) -> list[str]:
    """recommendation.valuation_methods(既存フィールド)から、標準5方式
    (target_yield/per/pbr/historical_range/dcf)についてのみ、実際に保存済みの
    除外理由(exclusion_reason)を方式名付きで取り出す(表示層で新しい除外理由を
    推測・算出しない)。industryは対象外とする理由は上記コメント参照。
    NO_VALUATION_ANCHORの旧データ(no_valuation_anchor_reasonスナップショット
    未保存)フォールバック専用。"""
    return [
        f"{_VALUATION_METHOD_LABELS[m.method]}: {m.exclusion_reason}"
        for m in recommendation.valuation_methods
        if m.method in _STANDARD_VALUATION_METHODS and m.exclusion_reason
    ]


_NO_VALUATION_ANCHOR_LEAD_TEXT = "本銘柄については、購入判断に使う適正価格を決定できませんでした。"


def _no_valuation_anchor_detail_text(
    code: str, actual_value: object, threshold_value: object
) -> str | None:
    """buy_score_input_facts["no_valuation_anchor_reason"]["code"]を日本語の
    説明文へ変換する(2026-08、NO_VALUATION_ANCHOR表示不備の是正)。

    原因の判定(なぜvaluation_anchorを生成できなかったか)はドメイン層
    (valuation_confidence.py::determine_valuation_confidence() /
    valuation_methods.py::compute_valuation_anchor())側で完結しており、
    ここではそのcodeを日本語文言へ変換するだけで、閾値比較等の再判定は
    一切行わない。actual_value/threshold_valueも判定時点に保存された値を
    そのまま表示に使い、現在configを取得し直さない。
    """
    if code == CODE_NO_VALID_VALUATION_METHODS:
        return "有効な適正価格の算出方式が一つもありませんでした。"
    if code == CODE_TOO_FEW_VALUATION_METHODS:
        actual = _decimal_str_to_display(actual_value, digits=0) or "不明"
        threshold = _decimal_str_to_display(threshold_value, digits=0) or "不明"
        return (
            f"有効な適正価格の算出方式が{actual}件しかなく"
            f"（{threshold}件必要）、結果を一本化できませんでした。"
        )
    if code == CODE_VALUATION_DISPERSION_TOO_HIGH:
        actual_ratio = _decimal_str_to_display(actual_value, digits=2)
        threshold_ratio = _decimal_str_to_display(threshold_value, digits=2)
        actual_text = f"{actual_ratio}倍" if actual_ratio is not None else "不明"
        threshold_text = f"{threshold_ratio}倍超" if threshold_ratio is not None else "不明"
        return (
            "算出方式間の結果のばらつきが大きく、基準価格を一本化できませんでした。\n"
            f"判定時点のばらつき：{actual_text}\n"
            f"自動買付を行わない基準：{threshold_text}"
        )
    if code == CODE_VALUATION_ANCHOR_CALCULATION_FAILED:
        return "算出処理で有効な結果を得られませんでした。"
    return None


def _no_valuation_anchor_text(recommendation: Recommendation) -> str:
    facts = recommendation.buy_score_input_facts or {}
    reason = facts.get("no_valuation_anchor_reason")
    if isinstance(reason, dict):
        # レビュー対応(2026-08、コードレビュー指摘): no_valuation_anchor_reason
        # スナップショット自体が保存されている場合、原因は既にこのスナップ
        # ショットへ一元化されている(=判定時点にvaluation_anchorが実際にNone
        # になった唯一の理由)。codeが現在の表示処理で未知(将来追加された
        # blocking reason等)であっても、無関係な標準方式のexclusion_reasonへ
        # フォールバックしてはならない(それは別事象であり、代用すると
        # industryの理由を誤って原因として表示していた不具合と同型の問題を
        # 再発させる)。この場合は非断定・fail-safeな表示にとどめ、保存済みの
        # codeそのものをユーザー向けに出すこともしない。
        code = reason.get("code")
        detail = (
            _no_valuation_anchor_detail_text(
                str(code), reason.get("actual_value"), reason.get("threshold_value")
            )
            if code
            else None
        )
        if detail is not None:
            return f"{_NO_VALUATION_ANCHOR_LEAD_TEXT}\n{detail}"
        return f"{_NO_VALUATION_ANCHOR_LEAD_TEXT}判定理由の詳細を現在の表示処理では解釈できません。"

    # 旧データ(no_valuation_anchor_reason自体が未保存)専用フォールバック:
    # 標準5方式に保存済みのexclusion_reasonがあればそれのみ表示し、無関係な
    # 理由(industryの恒常的なexclusion_reason等)を代用しない。理由自体が
    # 無い場合は、原因を推測せず非断定表示にとどめる。
    reasons = _standard_valuation_method_exclusion_reasons(recommendation)
    if reasons:
        return f"{_NO_VALUATION_ANCHOR_LEAD_TEXT}各算出方式の結果: " + "／".join(reasons)
    return f"{_NO_VALUATION_ANCHOR_LEAD_TEXT}判定時点の詳細な理由は保存されていません。"


# --- BUY_PRICE_RELIABILITY_LOW具体的理由表示(2026-08、本番実データUAT対応) ---
#
# 設計原則(承認済み調査報告どおり):
# 1. どのconcernが実際に発火したかは、buy_score_input_facts
#    ["buy_price_reliability_concerns"](判定時点にdetermine_buy_price_
#    reliability()が返したconcernsそのもの)を唯一のauthoritativeな根拠とする。
#    valuation_methods/valuation_dispersion_ratio/earnings_date_status等の
#    他の保存済み事実は、発火の有無を表示層で再判定するためには使わず、
#    「なぜそのconcernが発火したかを人間向けに補足説明する」ためだけに使う。
# 2. 現在のconfig値・現在値は一切参照しない(表示するのは判定時点の
#    スナップショットのみ)。
# 3. concernsが保存されていない(=buy_price_reliability_concernsキーが
#    Noneの)旧レコードでは、従来どおりの非断定フォールバックのままとする。
# 4. 複数concern該当時は、表示層で並び替え・重要度付けをせず、保存された
#    順序のまま全件表示する。

# TOO_FEW_VALUATION_METHODSの閾値(2件)はdomain/valuation/buy_price_
# reliability.py::determine_buy_price_reliability()内のハードコード定数
# (config化されていない)。表示専用の参考情報としてのみ複製し、判定ロジック
# には一切使用しない(このファイルの_STRONG_SCORE_RATIO等と同じ、意図的な
# 同期。値が変わった場合はここも合わせて見直すこと)。
_TOO_FEW_VALUATION_METHODS_THRESHOLD = 2

_RELIABILITY_CONCERN_LABELS: dict[str, str] = {
    "ENTRY_MARGIN_EXCEEDS_CAP": "安全余裕率が上限を超過",
    "HIGH_VALUATION_DISPERSION": "適正価格のばらつきが大きい",
    "TOO_FEW_VALUATION_METHODS": "適正価格の算出に使えた手法が少ない",
    "DATA_QUALITY_WARNING": "データ品質に懸念がある",
    "STALE_EARNINGS_DATE": "次回決算予定日の情報が古い",
    "VALUATION_OUTLIER_EXCLUDED": "適正価格の算出方式に外れ値が含まれていた",
    "TOO_FEW_METHODS_AFTER_OUTLIER_FILTER": (
        "外れ値除外の結果、比較に使える手法が不足したため除外前の結果へ戻した"
    ),
}


def _reliability_concern_line(
    concern: str,
    facts: dict[str, Any],
    config_snapshot: dict[str, Any],
    recommendation: Recommendation,
) -> str:
    """1件のconcernコードから、判定時点に保存済みの補足事実を添えた説明文を
    組み立てる。concern自体は既にauthoritativeな発火結果として確定して
    おり、ここでは「なぜ」を補足するだけで発火有無の判断はしない。補足事実が
    安全に取得できない場合はconcern名(ラベル)のみを返す(推測しない)。"""
    label = _RELIABILITY_CONCERN_LABELS.get(concern, concern)

    if concern == "ENTRY_MARGIN_EXCEEDS_CAP":
        # entry_margin_before_cap/maximum_entry_marginはいずれも0.30=30%形式の
        # 割合(margin_of_safety.pyのconfig単位)。_decimal_str_to_display()は
        # PER/PBRのような「倍率」表示専用のため、ここでは使わずそのまま
        # %表示(×100)に変換する。
        entry_margin_raw = facts.get("entry_margin_before_cap")
        cap = config_snapshot.get("maximum_entry_margin")
        if entry_margin_raw is not None and cap is not None:
            try:
                entry_margin_pct = float(entry_margin_raw) * 100
                cap_pct = float(cap) * 100
            except (TypeError, ValueError):
                return f"・{label}"
            return f"・{label}（判定時の安全余裕率{entry_margin_pct:.1f}%、上限{cap_pct:.1f}%）"
        return f"・{label}"

    if concern == "HIGH_VALUATION_DISPERSION":
        ratio = recommendation.valuation_dispersion_ratio
        threshold = config_snapshot.get("valuation_dispersion_medium_max")
        if ratio is not None and threshold is not None:
            try:
                threshold_value = float(threshold)
            except (TypeError, ValueError):
                return f"・{label}"
            return (
                f"・{label}（判定時のばらつき{float(ratio):.2f}倍、"
                f"基準{threshold_value:.2f}倍以下）"
            )
        return f"・{label}"

    if concern == "TOO_FEW_VALUATION_METHODS":
        count = facts.get("valuation_methods_used_count")
        if isinstance(count, int):
            return (
                f"・{label}（判定時に使用できた手法{count}件、"
                f"{_TOO_FEW_VALUATION_METHODS_THRESHOLD}件以下が対象）"
            )
        return f"・{label}"

    if concern == "DATA_QUALITY_WARNING":
        details = []
        data_age = facts.get("data_age_business_days")
        if isinstance(data_age, int):
            details.append(f"取得データの経過日数{data_age}営業日")
        business_days = recommendation.business_days_to_earnings
        if business_days is not None:
            details.append(f"次回決算まで{business_days}営業日")
        else:
            details.append("次回決算予定日が未確定")
        return f"・{label}（{'、'.join(details)}）"

    if concern == "STALE_EARNINGS_DATE":
        status = recommendation.earnings_date_status
        if status is not None:
            return f"・{label}（判定時のステータス: {status.value}）"
        return f"・{label}"

    if concern == "VALUATION_OUTLIER_EXCLUDED":
        # レビュー対応(2026-08、commit f546473再レビューで発覚): この分岐は
        # 以前_valuation_method_exclusion_reasons(recommendation)(= Recommendation.
        # valuation_methodsのexclusion_reason)を使っていたが、valuation_methodsは
        # 外れ値フィルタ適用「前」のオブジェクトであり、外れ値フィルタが実際に
        # 設定するexclusion_detail/exclusion_reasonは反映されない(build_
        # valuation_summary()内部でmodel_copy()された別オブジェクトにのみ設定
        # される)。そのため算出不能・業種モデル未実装等の無関係な理由を外れ値
        # 理由として誤表示しうる不具合があった。実際に外れ値フィルタで除外
        # された方式・理由のみを保存したbuy_score_input_facts
        # ["valuation_outlier_exclusions"](新規スナップショット)を参照する。
        exclusions = facts.get("valuation_outlier_exclusions")
        reasons = [
            f"{_VALUATION_METHOD_LABELS.get(str(e.get('method')), str(e.get('method')))}: "
            f"{e.get('message')}"
            for e in exclusions
            if isinstance(e, dict) and e.get("method") and e.get("message")
        ] if isinstance(exclusions, list) else []
        if reasons:
            return f"・{label}（{'／'.join(reasons)}）"
        return f"・{label}"

    if concern == "TOO_FEW_METHODS_AFTER_OUTLIER_FILTER":
        return f"・{label}"

    # 未知のconcernコード(将来追加されうる)は、保存された文字列をそのまま
    # 表示するだけに留め、意味を推測しない。
    return f"・{label}"


def _buy_price_reliability_low_text(recommendation: Recommendation) -> str:
    facts = recommendation.buy_score_input_facts or {}
    concerns = facts.get("buy_price_reliability_concerns")
    if not isinstance(concerns, list) or not concerns:
        # concerns自体が保存されていない(旧レコード)場合は、従来どおり
        # 非断定のフォールバックのままとする(推測・再計算しない)。
        return (
            "自動算出した買付価格の信頼性が低い状態のため、算出した価格をそのまま"
            "購入判断には使用していません。信頼性低下の具体的な要因は、現行データ"
            "からは一意に特定できません。"
        )
    config_snapshot = recommendation.config_values_used or {}
    lines = [
        _reliability_concern_line(str(concern), facts, config_snapshot, recommendation)
        for concern in concerns
    ]
    return (
        "自動算出した買付価格の信頼性が低い状態のため、算出した価格をそのまま"
        "購入判断には使用していません。判定時点に確認された要因は以下のとおり"
        "です。\n" + "\n".join(lines)
    )


# Legacy SELLの17ルールのうちラベル表示が必要なもの(3節の監査証跡拡張で
# current_valueを実際に持ちうるルールのみ)。domain/signals/sell_signal.py
# ::_RULE_LABELSと同じ文字列(意図的に同期。judgment moduleへの依存を避ける
# ため値のみ独立して持つ、watchlist_judgment_summary_formatter.pyと同じ方針)。
_SELL_RULE_LABELS: dict[str, str] = {
    "dividend_cut": "減配(推測)",
    "dividend_omission": "無配転落(推測)",
    "continuous_operating_income_decline": "営業利益の継続悪化",
    "continuous_operating_cashflow_decline": "営業キャッシュフローの継続悪化",
    "financial_health_severe_deterioration": "財務健全性の重大な悪化(一般事業会社基準)",
    "balance_sheet_insolvency": "債務超過",
    "shareholder_benefit_abolished": "株主優待の廃止",
    "shareholder_benefit_major_downgrade": "株主優待の大幅改悪",
    "major_scandal": "重大な不祥事",
    "accounting_problem": "会計問題",
    "listing_maintenance_risk": "上場維持リスク・継続企業前提の重要事象",
}

_SELL_FACT_VALUE_TRANSLATIONS: dict[str, str] = {
    "True": "該当あり",
    "False": "該当なし",
    "official_confirmed": "一次情報で確認",
    "inferred_only": "二次情報のみ(未確認)",
    "not_detected": "検出なし",
    "MATERIAL_EVENT_CONFIRMED": "重大事象を確認",
    "RISK_KEYWORD_DETECTED": "リスクキーワードのみ検出",
    "NONE": "検出なし",
}

_CONTINUOUS_DECLINE_RULE_NAMES = frozenset(
    {"continuous_operating_income_decline", "continuous_operating_cashflow_decline"}
)

# レビュー対応(2026-08、本番実データUATで発覚): AuditLogEntryのstatus
# (TriggerStatus.TRIGGERED/NOT_TRIGGERED、文字列で保存)を「該当あり/該当なし」
# へそのまま翻訳する。表示層で閾値比較の向きを独自に再計算・推測しない
# (原因側のsell_signal.pyが既に判定済みのstatusをそのまま使うだけ)。
_STATUS_WORD: dict[str, str] = {
    "TRIGGERED": "該当あり",
    "NOT_TRIGGERED": "該当なし",
}

# レビュー対応(2026-08、修正条件3): AuditLogには全17ルールの証跡を保存する設計は
# 維持したまま、LINE表示側だけを「ユーザーの判断に有用な事実を優先」する方式へ
# 変更する(監査証跡の完全性とLINE表示の簡潔性を分離)。以下は単純な真偽値/
# 検出レベルのみを持つルール(継続悪化2ルール・財務健全性2ルールのような実数値・
# トレンドを持たない)で、かつ「該当なし」が判定結果として最も多く出現する
# ルール。これらが軒並み「該当なし」の場合、1行ずつ列挙すると「減配：該当なし」
# 「優待廃止：該当なし」等が多数並ぶだけになりユーザーにとって有用性が低いため、
# 1行に集約する。個別ルールが実際に該当あり(=ユーザーが読むべき情報)の場合は、
# 従来どおり個別行として表示する。
_FLAT_NEGATIVE_SUMMARY_LABEL: dict[str, str] = {
    "dividend_cut": "減配",
    "dividend_omission": "無配転落",
    "shareholder_benefit_abolished": "株主優待の廃止",
    "shareholder_benefit_major_downgrade": "株主優待の大幅改悪",
    "major_scandal": "重大な不祥事",
    "accounting_problem": "会計問題",
    "listing_maintenance_risk": "上場維持リスク",
}
_FLAT_NEGATIVE_CURRENT_VALUES = frozenset({"False", "not_detected", "NONE"})


def _legacy_sell_hold_fact_line(detail: dict[str, Any]) -> str | None:
    """Legacy SELLの1ルール分の監査証跡から、実際に値が残っているものだけを
    事実の1行として組み立てる(内部enum名/真偽値をそのまま出さず自然文へ
    翻訳する。3節の監査証跡拡張で追加したcurrent_valueが無いルールは
    Noneを返し、呼び出し側で除外する)。

    レビュー対応(2026-08、本番実データUATで発覚): 定量値+閾値形式のルール
    (balance_sheet_insolvency/financial_health_severe_deterioration)は、
    数値・閾値だけを機械的に並べており、実際にはNOT_TRIGGEREDであっても
    「債務超過：36.4%」のようにラベルだけを見ると該当しているかのように
    誤読されうる不備があった。表示層で閾値比較の向きを独自に再計算・推測
    せず、AuditLogEntryに保存済みのstatus(TRIGGERED/NOT_TRIGGERED)と
    explanation(判定エンジン自身が生成した説明文)をそのまま使い、
    「該当あり/該当なし」を明示する。explanationが保存されている場合は
    それを優先して使い、無い場合のみ現在値・基準値で安全に補足する。
    継続悪化2ルール(前期→今期の実数値を示す形式)でも同様にstatusを明示する
    (実数値自体は引き続き表示し、explanationは補足として付記する)。
    """
    current_value = detail.get("current_value")
    if current_value is None:
        return None
    rule_name = str(detail.get("rule_name"))
    label = _SELL_RULE_LABELS.get(rule_name, rule_name)
    status_value = detail.get("status")
    status_word = _STATUS_WORD.get(str(status_value)) if status_value is not None else None
    explanation = detail.get("explanation") or None

    if rule_name in _CONTINUOUS_DECLINE_RULE_NAMES:
        previous_value = detail.get("previous_value")
        period = detail.get("comparison_period")
        if previous_value is not None:
            period_note = f"、{period}" if period else ""
            trend = f"前期{previous_value}円→今期{current_value}円{period_note}"
        else:
            trend = f"{current_value}円"
        body = f"{trend}、{explanation}" if explanation else trend
        if status_word is not None:
            return f"{label}：{status_word}（{body}）"
        return f"{label}：{body}"

    threshold = detail.get("threshold")
    if threshold is not None:
        if status_word is not None:
            # explanationが無い場合の括弧多重ネスト("該当なし（36.4%（基準…）」)
            # を避けるため、status_word有りの場合は「、」区切りの平文にする。
            body = explanation if explanation else f"{current_value}、基準{threshold}"
            return f"{label}：{status_word}（{body}）"
        return f"{label}：{current_value}（基準{threshold}）"

    return f"{label}：{_SELL_FACT_VALUE_TRANSLATIONS.get(current_value, current_value)}"


def _legacy_sell_hold_facts_lines(audit_entry: AuditLogEntry) -> list[str]:
    """純粋HOLD(Recommendation非生成)時、Legacy SELLの監査ログに残る
    ルール別実データのうち、実際に値を保持しているものだけを事実として
    示す(全17ルールの実数値を復元できるわけではないため、値が存在する
    ものに限定する。データ不足で失われている項目を推測で埋めない)。

    AuditLog自体は全ルールの証跡をそのまま保持する(3節の設計を変更しない)。
    ここでの集約はLINE表示専用であり、単純な真偽値/検出レベルのみのルール
    (_FLAT_NEGATIVE_SUMMARY_LABEL)が軒並み「該当なし」の場合は1行に集約し、
    実数値・トレンドを持つルール、および実際に該当ありのルールは従来どおり
    個別行のまま表示する(修正条件3)。
    """
    rule_evidence_details = audit_entry.input_values.get("rule_evidence_details")
    if not rule_evidence_details:
        return []
    lines: list[str] = []
    flat_negative_labels: list[str] = []
    for detail in rule_evidence_details:
        current_value = detail.get("current_value")
        if current_value is None:
            continue
        line = _legacy_sell_hold_fact_line(detail)
        if line is None:
            continue
        rule_name = str(detail.get("rule_name"))
        summary_label = _FLAT_NEGATIVE_SUMMARY_LABEL.get(rule_name)
        if summary_label is not None and str(current_value) in _FLAT_NEGATIVE_CURRENT_VALUES:
            flat_negative_labels.append(summary_label)
            continue
        lines.append(line)
    if flat_negative_labels:
        lines.append(
            "その他の投資前提悪化ルール(" + "・".join(flat_negative_labels) + ")については、"
            "いずれも該当する事実は確認されませんでした。"
        )
    return lines


class StockAnalysisViewService:
    def __init__(
        self,
        evaluation_record_repository: BuyCandidateEvaluationRecordRepository | None = None,
        latest_batch_pointer_repository: LatestBuyCandidateBatchPointerRepository | None = None,
        recommendation_repository: RecommendationRepository | None = None,
        holding_evaluation_record_repository: HoldingEvaluationRecordRepository | None = None,
        audit_log_repository: AuditLogRepository | None = None,
        display_name_resolver: StockDisplayNameResolver | None = None,
    ) -> None:
        self._evaluation_records = (
            evaluation_record_repository or BuyCandidateEvaluationRecordRepository()
        )
        self._pointer = (
            latest_batch_pointer_repository or LatestBuyCandidateBatchPointerRepository()
        )
        self._recommendations = recommendation_repository or RecommendationRepository()
        self._holding_evaluation_records = (
            holding_evaluation_record_repository or HoldingEvaluationRecordRepository()
        )
        self._audit_log = audit_log_repository or AuditLogRepository()
        self._display_name_resolver = display_name_resolver

    # --- BUY側 -----------------------------------------------------------

    def has_buy_analysis(self, stock_code: str) -> bool:
        batch_records = fetch_latest_normal_batch_records(self._pointer, self._evaluation_records)
        if isinstance(batch_records, LatestBatchStillPropagating) or batch_records is None:
            return False
        return stock_code in batch_records.records_by_stock_code

    def build_buy_analysis_text(self, stock_code: str) -> str:
        batch_records = fetch_latest_normal_batch_records(self._pointer, self._evaluation_records)
        if isinstance(batch_records, LatestBatchStillPropagating):
            return STILL_PROPAGATING_MESSAGE
        record = (
            batch_records.records_by_stock_code.get(stock_code) if batch_records else None
        )
        if record is None:
            return f"{stock_code}の直近の購入判定データが見つかりませんでした。"

        display_name = (
            self._display_name_resolver.resolve(stock_code)
            if self._display_name_resolver is not None
            else stock_code
        )
        recommendation = (
            self._recommendations.get(record.recommendation_id)
            if record.recommendation_id is not None
            else None
        )

        judgment = _PURCHASE_CATEGORY_JUDGMENT_LABEL.get(record.purchase_category) or (
            _BUY_ACTION_LABEL.get(record.final_buy_action, "データ不足")
            if record.final_buy_action is not None
            else "データ不足"
        )
        lines = ["【銘柄分析】", f"{display_name}（{stock_code}）", "", "■ 判定", judgment]

        if record.purchase_category == PurchaseCategory.EXCLUDED:
            lines += ["", "■ 理由"]
            if record.exclusion_reasons:
                lines += list(record.exclusion_reasons)
            else:
                lines.append("現行データでは、対象外となった具体的な理由を保存していません。")
            return "\n".join(lines)

        if record.purchase_category == PurchaseCategory.DATA_INSUFFICIENT:
            lines += [
                "",
                "■ 理由",
                "分析に必要なデータを十分取得できなかったため、判定を行えませんでした。",
            ]
            return "\n".join(lines)

        if record.purchase_category == PurchaseCategory.FAILED:
            lines += [
                "",
                "■ 理由",
                "判定処理中に想定外のエラーが発生したため、結果を表示できません。",
            ]
            return "\n".join(lines)

        if recommendation is not None:
            facts_lines = _buy_facts_lines(recommendation)
            if facts_lines:
                lines += ["", "■ 判断根拠となった事実", *facts_lines]

            interpretation_lines = _buy_interpretation_lines(recommendation)
            if interpretation_lines:
                lines += ["", "■ 解釈", *interpretation_lines]

        reason = _buy_reason_text(record, recommendation)
        if reason:
            lines += ["", "■ 総合判断", reason]

        if recommendation is not None:
            price_lines = _buy_price_lines(recommendation.buy_prices)
            if price_lines:
                lines += ["", "■ 価格目安（判定時点）", *price_lines]

        return "\n".join(lines)

    # --- SELL/HOLD側 -------------------------------------------------------

    def build_holding_analysis_text(self, owner: str, stock_code: str) -> str:
        holding_id = f"{owner}#{stock_code}"
        record = self._holding_evaluation_records.get_latest_by_holding_id(holding_id)
        if record is None:
            return f"{stock_code}（{owner}）の直近の保有判定データが見つかりませんでした。"

        recommendation = (
            self._recommendations.get(record.authoritative_recommendation_id)
            if record.authoritative_recommendation_id is not None
            else None
        )

        # 修正(UAT指摘): 括弧内は会社名(BUY側と同じ形式)とし、所有者は別行で
        # 示す(以前は括弧内が誤って所有者名になっていた)。resolver未接続の
        # 場合はrecommendation.stock_name、それも無ければ銘柄コードへ
        # フォールバックする。
        display_name = (
            self._display_name_resolver.resolve(stock_code)
            if self._display_name_resolver is not None
            else (recommendation.stock_name if recommendation is not None else stock_code)
        )
        lines = [
            "【銘柄分析】",
            f"{display_name}（{stock_code}）",
            f"所有者：{owner}",
            "",
            "■ 判定",
        ]
        lines.append(
            _HOLDING_JUDGMENT_LABEL.get(recommendation.recommendation_type, "要確認")
            if recommendation is not None
            else "保有継続"
        )

        if recommendation is None:
            # Phase 2-B追加調査(2026-08)対応: Legacy SELL担当の純粋HOLDのみ、
            # authoritative_audit_log_id経由でその評価サイクルのAuditLogEntryを
            # 参照する(HoldingEvaluationRecord自身が持つポインタのため、owner
            # 取り違えは構造的に発生しない)。ProfitTaking/HoldingDecisionScore
            # (SHADOW)担当のHOLDはこの経路の対象外(3節はLegacy SELLの証跡拡張
            # のみが対象、7節ルール#8によりSHADOWは使わない)。
            facts_lines: list[str] = []
            if (
                record.authoritative_engine == "LEGACY_SELL"
                and record.authoritative_audit_log_id is not None
            ):
                audit_entry = self._audit_log.get(record.authoritative_audit_log_id)
                if audit_entry is not None:
                    facts_lines = _legacy_sell_hold_facts_lines(audit_entry)
            if facts_lines:
                lines += ["", "■ 判断根拠となった事実（投資前提悪化ルールの状況）", *facts_lines]
                lines += [
                    "",
                    "■ 総合判断",
                    "投資前提の悪化を示すルールには該当しませんでした。",
                ]
            else:
                lines += ["", "■ 理由", _UNRESTORABLE]
            return "\n".join(lines)

        lines += ["", "■ 理由"]
        if recommendation.reasons:
            lines += list(recommendation.reasons)
        else:
            lines.append(_UNRESTORABLE)

        quantity_lines = _sell_quantity_lines(recommendation)
        if quantity_lines:
            lines += ["", "■ 売却目安の根拠", *quantity_lines]

        return "\n".join(lines)


_HOLDING_JUDGMENT_LABEL: dict[RecommendationType, str] = {
    RecommendationType.PARTIAL_PROFIT_TAKE: "一部売却を検討",
    RecommendationType.FULL_PROFIT_TAKE: "全部売却を検討",
    RecommendationType.SELL: "売却を検討",
    RecommendationType.URGENT_REVIEW: "緊急確認を推奨",
    RecommendationType.REVIEW: "要確認",
    RecommendationType.WATCH: "利確を監視中",
    RecommendationType.WATCH_BEFORE_EARNINGS: "監視継続（決算発表待ち）",
    RecommendationType.REVIEW_BEFORE_EARNINGS: "監視継続（決算発表待ち）",
    RecommendationType.REVIEW_AFTER_EARNINGS: "要確認（決算内容反映済み）",
    RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW: "要確認（保有比率）",
    RecommendationType.SELL_CONSIDERATION: "売却を検討",
    RecommendationType.STRONG_SELL_CONSIDERATION: "売却を強く検討",
    RecommendationType.URGENT_HOLDING_REVIEW: "緊急確認を推奨",
}


def _sell_quantity_lines(recommendation: Recommendation) -> list[str]:
    """PARTIAL_PROFIT_TAKEのみ数量算出フローを表示する(F節ルール: 数量算出
    ロジックを持たないエンジンの判定には数量を表示・合成しない)。"""
    if recommendation.recommendation_type != RecommendationType.PARTIAL_PROFIT_TAKE:
        return []
    if recommendation.suggested_sell_shares is None:
        return []
    ratios = (recommendation.config_values_used or {}).get("partial_sell_ratios")
    holding_shares = recommendation.shares_at_recommendation
    lines = []
    if holding_shares is not None:
        lines.append(f"保有株数：{holding_shares}株")
    if ratios is not None and recommendation.sell_intensity is not None:
        ratio_value = ratios.get(recommendation.sell_intensity.lower())
        if ratio_value is not None and holding_shares is not None:
            theoretical = round(holding_shares * ratio_value)
            lines.append(f"目標売却比率：{ratio_value * 100:.0f}%")
            lines.append(f"比率適用後の理論株数：{theoretical}株相当")
    lines.append(f"単元株単位への調整後の売却目安：{recommendation.suggested_sell_shares}株")
    return lines
