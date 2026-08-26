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
_TIER_THRESHOLD_FIELD: dict[BuyAction, str] = {
    BuyAction.STRONG_BUY: "strong_buy",
    BuyAction.BUY: "buy",
    BuyAction.SMALL_ENTRY: "small_entry",
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
_UNDERVALUATION_SIGNAL_LABELS: dict[str, str] = {
    "per_below_median": "PERが自社の過去中央値を下回っている",
    "pbr_below_median": "PBRが自社の過去中央値を下回っている",
    "dividend_yield_above_historical_average": "配当利回りが自社の過去平均を上回っている",
    "drawdown_from_52w_high": "52週高値から一定以上下落している",
    "below_fair_value": "現在値が算出された適正価格を下回っている",
    "price_down_despite_stable_earnings": "業績は安定している一方で株価が下落している",
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
        matched = [
            label
            for name, label in _UNDERVALUATION_SIGNAL_LABELS.items()
            if signals.get(name) is is_positive
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
        return (
            "自動算出した買付価格の信頼性が低い状態のため、算出した価格をそのまま"
            "購入判断には使用していません。信頼性低下の具体的な要因は、現行データ"
            "からは一意に特定できません。"
        )
    if last.code == "VALUATION_DISPERSION_TOO_HIGH":
        return (
            "適正価格の算出手法間でばらつきが大きく（算出方法によって評価額が"
            "大きく異なる状態）、自動判定を見合わせています。"
        )
    if last.code == "NO_VALUATION_ANCHOR":
        return (
            "本銘柄については、適正価格の算出結果を得られませんでした。"
            "具体的な要因は現行データからは区別できません。"
        )
    return None


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
    Noneを返し、呼び出し側で除外する)。"""
    current_value = detail.get("current_value")
    if current_value is None:
        return None
    rule_name = str(detail.get("rule_name"))
    label = _SELL_RULE_LABELS.get(rule_name, rule_name)
    if rule_name in _CONTINUOUS_DECLINE_RULE_NAMES:
        previous_value = detail.get("previous_value")
        period = detail.get("comparison_period")
        if previous_value is not None:
            period_note = f"、{period}" if period else ""
            return f"{label}：前期{previous_value}円→今期{current_value}円{period_note}"
        return f"{label}：{current_value}円"
    threshold = detail.get("threshold")
    if threshold is not None:
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
