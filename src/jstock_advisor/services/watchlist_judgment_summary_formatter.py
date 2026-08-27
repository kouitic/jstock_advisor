"""ウォッチリスト判定サマリ文言の生成(LINE UI第二弾、表示専用、2026-08)。

既に確定した判定結果(BuyCandidateEvaluationRecord/Recommendation)を読み取る
だけの純粋関数群。依存方向は常に「既存判定結果→本モジュール→LINE表示」の
一方向であり、`decide_buy_action()`/`compute_score()`等の投資判断コードは
一切呼び出さない(逆に投資判断側からこのモジュールへの依存も発生しない)。

「不足点」(= 配点満点 - 実際の得点)による代表懸念項目の選定は、本モジュール
専用のLINE表示優先順位付けである。既存の`score_areas()`(buy_signal.py)が
行う「弱い項目」抽出(score/max_weight < 0.3)そのものは踏襲するが、弱い項目が
複数ある場合にどれを代表表示するかを決める処理(不足点の比較)は本モジュールで
新設したものであり、`BuyAction`/`PurchaseCategory`/`company_quality_score`/
`purchase_attractiveness_score`/`RecommendationType`/`notification_eligible`/
`notification_rank`/`unified_rank`/日次サマリー分類など、いかなる投資判断・
通知判断にも一切使用・書き込みしない。表示文言も「この項目が原因で格下げ
された」という因果断定を避け、常に「既存スコア上で不足点が最も大きい弱点」
という記述に留める。
"""

from __future__ import annotations

from jstock_advisor.config.models import ScoreWeights
from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
)
from jstock_advisor.domain.entities.buy_decision import BuyDecisionReason
from jstock_advisor.domain.entities.common import ScoreBreakdown
from jstock_advisor.domain.entities.enums import BuyAction, PurchaseCategory
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.valuation.valuation_confidence import (
    CODE_NO_VALID_VALUATION_METHODS,
    CODE_TOO_FEW_VALUATION_METHODS,
    CODE_VALUATION_ANCHOR_CALCULATION_FAILED,
    CODE_VALUATION_DISPERSION_TOO_HIGH,
)

# 既存buy_signal.py._WEAK_SCORE_RATIOと同じ値。投資判断モジュールへの依存を
# 避けるため値のみ独立して定義する(既存定数の変更に追従する必要が生じた
# 場合は、ここも合わせて見直すこと)。
_WEAK_SCORE_RATIO = 0.3

# 既存buy_signal.py._SCORE_LABELSと同じ7項目・同じラベル(config/scoring_weights.yaml
# のコメントとも一致)。
_SCORE_LABELS: dict[str, str] = {
    "total_yield_attractiveness": "総合利回りの魅力度",
    "dividend_sustainability": "配当持続性",
    "financial_health": "財務健全性",
    "undervaluation": "割安度",
    "shareholder_benefit_value": "株主優待価値",
    "earnings_stability": "業績安定性",
    "price_stability": "株価安定性",
}

# 現行の買い候補サマリー通知(line_notification_service.py notify_batch_summary)
# が実際に使っているラベルをそのまま再利用する(独自の分類を新設しない)。
_CATEGORY_LABELS: dict[PurchaseCategory, str] = {
    PurchaseCategory.BUY_CANDIDATE: "買い候補",
    PurchaseCategory.NEAR_BUY: "買い間近",
    PurchaseCategory.WATCH_FOR_PRICE: "買い待ち",
    PurchaseCategory.WATCH_BEFORE_EARNINGS: "買い待ち",
    PurchaseCategory.NOT_ATTRACTIVE: "買い対象外",
    PurchaseCategory.EXCLUDED: "買い対象外",
    PurchaseCategory.MANUAL_REVIEW: "要確認",
    PurchaseCategory.DATA_INSUFFICIENT: "データ不足",
    PurchaseCategory.FAILED: "処理失敗",
}

# PRICE_TIERが最終理由の場合の文言(final_buy_actionで一意に決まる。承認済み)。
_PRICE_TIER_REASON_TEXT: dict[BuyAction, str] = {
    BuyAction.STRONG_BUY: "現在値が積極買付価格以内",
    BuyAction.BUY: "現在値が標準買付価格以内",
    BuyAction.SMALL_ENTRY: "現在値が打診買付価格以内",
    BuyAction.WATCH_FOR_PRICE: "現在値が買付価格を上回る",
}
# レビュー対応(2026-08、ウォッチリスト表示改善): NO_VALUATION_ANCHORの直接
# 原因(判定時点スナップショットbuy_score_input_facts["no_valuation_anchor_
# reason"]のcode)を、ウォッチリストの1行表示向けに短く変換する。銘柄分析
# (stock_analysis_view_service.py)ほど詳細(実測値・基準値)は表示せず、
# ラベルのみとする(詳細は銘柄分析側で確認する役割分担)。codeが未知、また
# スナップショット自体が無い(旧Recommendation)場合は、いずれも原因を推測
# せず同一の非断定表示へフォールバックする(valuation_methods等の別の事実
# へフォールバックしない。stock_analysis_view_service.pyと同じ設計原則)。
_NO_VALUATION_ANCHOR_REASON_LABELS: dict[str, str] = {
    CODE_NO_VALID_VALUATION_METHODS: "適正価格を算出できず",
    CODE_TOO_FEW_VALUATION_METHODS: "適正価格の算出方式が不足",
    CODE_VALUATION_DISPERSION_TOO_HIGH: "適正価格のばらつき大",
    CODE_VALUATION_ANCHOR_CALCULATION_FAILED: "購入基準価格を算出できず",
}
_NO_VALUATION_ANCHOR_FALLBACK_TEXT = "購入基準価格を決定できず"
_EARNINGS_WINDOW_TEXT = "次回決算が近いため保留"
_BUY_PRICE_RELIABILITY_LOW_TEXT = "価格算出の信頼度が低い"
_VALUATION_DISPERSION_TOO_HIGH_TEXT = "評価手法間のばらつきが大きい"
# SCORE_BELOW_THRESHOLDが最終理由で、final_buy_actionがBUY_FAMILY外まで
# 格下げされた場合の文言(decide_buy_action()の到達可能性を実コードで確認済み、
# raw_buy_actionのチェックは不要 — 詳細はユーザーとのやり取り記録参照)。
_SCORE_BELOW_THRESHOLD_WATCH_TEXT = "総合評価により買付を見送り"
_SCORE_BELOW_THRESHOLD_NOT_ATTRACTIVE_TEXT = "総合評価が購入基準を下回る"

_SCORE_BELOW_THRESHOLD_CODE = "SCORE_BELOW_THRESHOLD"
_PRICE_TIER_FAMILY = (BuyAction.STRONG_BUY, BuyAction.BUY, BuyAction.SMALL_ENTRY)


def category_label(category: PurchaseCategory) -> str:
    return _CATEGORY_LABELS.get(category, category.value)


def _category_reason_text(
    record: BuyCandidateEvaluationRecord, recommendation: Recommendation | None
) -> str:
    """区分理由(なぜこの判定区分になったか)。recommendationが無い
    (EXCLUDED/DATA_INSUFFICIENT/FAILED、またはbuy_decision_reasonsが空)場合は
    理由データ自体が存在しないため空文字列を返す(呼び出し側はカテゴリー
    ラベルのみを表示する)。
    """
    if recommendation is None or not recommendation.buy_decision_reasons:
        return ""
    last_reason: BuyDecisionReason = recommendation.buy_decision_reasons[-1]
    final_action = record.final_buy_action

    if last_reason.code == _SCORE_BELOW_THRESHOLD_CODE:
        if final_action in _PRICE_TIER_FAMILY:
            # 価格帯自体はまだ買い候補水準にあり続けている(スコアで1段階
            # だけ格下げされたが範囲内に留まった)。事実として矛盾しない
            # 価格帯ベースの文言を使う。
            return _PRICE_TIER_REASON_TEXT[final_action]
        if final_action == BuyAction.WATCH_FOR_PRICE:
            return _SCORE_BELOW_THRESHOLD_WATCH_TEXT
        if final_action == BuyAction.NOT_ATTRACTIVE:
            return _SCORE_BELOW_THRESHOLD_NOT_ATTRACTIVE_TEXT
        return ""
    if last_reason.code == "PRICE_TIER":
        return _PRICE_TIER_REASON_TEXT.get(final_action, "") if final_action else ""
    if last_reason.code == "NO_VALUATION_ANCHOR":
        return _no_valuation_anchor_reason_text(recommendation)
    if last_reason.code == "EARNINGS_WINDOW":
        return _EARNINGS_WINDOW_TEXT
    if last_reason.code == "BUY_PRICE_RELIABILITY_LOW":
        return _BUY_PRICE_RELIABILITY_LOW_TEXT
    if last_reason.code == "VALUATION_DISPERSION_TOO_HIGH":
        return _VALUATION_DISPERSION_TOO_HIGH_TEXT
    return ""


def _no_valuation_anchor_reason_text(recommendation: Recommendation) -> str:
    """NO_VALUATION_ANCHORの直接原因を、判定時点スナップショット
    (buy_score_input_facts["no_valuation_anchor_reason"])から短い1行表現へ
    変換する(2026-08、ウォッチリスト表示改善)。

    スナップショットが存在しcodeが既知の場合のみ具体的なラベルを返す。
    スナップショット自体が存在しない(旧Recommendation)場合、および
    スナップショットは存在するがcodeが未知(将来追加されたcode等)の場合は、
    いずれも原因を推測せず同一の非断定表示へフォールバックする
    (stock_analysis_view_service.pyと同じ設計原則。valuation_methods等の
    別の事実へフォールバックしない)。
    """
    facts = recommendation.buy_score_input_facts or {}
    reason = facts.get("no_valuation_anchor_reason")
    if isinstance(reason, dict):
        label = _NO_VALUATION_ANCHOR_REASON_LABELS.get(str(reason.get("code")))
        if label is not None:
            return label
    return _NO_VALUATION_ANCHOR_FALLBACK_TEXT


def _select_supplementary_concern(
    score_breakdown: ScoreBreakdown | None,
    weights: ScoreWeights,
    buy_decision_reasons: tuple[BuyDecisionReason, ...],
) -> str | None:
    """補足懸念(何が懸念か)を選ぶ。既存データから具体的な項目名を特定できる
    場合は必ず具体名を示し、「総合評価に一部懸念あり」のような抽象表現へは
    フォールバックしない。
    """
    if score_breakdown is None:
        return None

    components: list[tuple[float, float, str]] = []
    for field_name, label in _SCORE_LABELS.items():
        max_weight = getattr(weights, field_name)
        score = getattr(score_breakdown, field_name)
        if max_weight <= 0:
            continue
        components.append((score, max_weight, label))

    weak = [
        (max_weight - score, label)
        for score, max_weight, label in components
        if score / max_weight < _WEAK_SCORE_RATIO
    ]
    if weak:
        max_gap = max(gap for gap, _ in weak)
        labels = [label for gap, label in weak if gap == max_gap]
        return "・".join(labels) + "に懸念"

    # 0.3基準の「弱い項目」は無いが、SCORE_BELOW_THRESHOLDは発生している
    # (複数項目の小さな不足点の積み重ねと考えられるケース)。「弱い項目」認定
    # ではないため「に懸念」という断定は避け、「相対的に低め」という表現に
    # 留める。final_buy_actionが「買い候補」に残っているケースでも意味的に
    # 矛盾しない(2-1修正)。
    if any(r.code == _SCORE_BELOW_THRESHOLD_CODE for r in buy_decision_reasons):
        all_gaps = [(max_weight - score, label) for score, max_weight, label in components]
        max_gap = max(gap for gap, _ in all_gaps)
        labels = [label for gap, label in all_gaps if gap == max_gap]
        return f"評価内訳では{'・'.join(labels)}が相対的に低め"

    return None


def _resolve_weights(
    recommendation: Recommendation | None, fallback_weights: ScoreWeights
) -> ScoreWeights:
    """判定時点で実際に使われたScoreWeightsを、Recommendation.config_values_used
    (2026-08-25追加の"scoring_weights"スナップショット、buy_signal_service.py
    参照)から復元する。過去の判定を、後からconfig変更後の"現在の"weightsで
    誤って再解釈しないための措置(スコア配点を変更しても、既に確定した過去
    batchの説明文が変化しないようにする)。

    既知の制約: このスナップショットが無い(本修正より前に作成された)
    既存のRecommendationについてはやむを得ずfallback_weights(呼び出し側が
    渡す現在のconfig)を使う。その場合のみ「判定時点のweights」ではなく
    「表示時点の現在weights」で解釈することになる。
    """
    if recommendation is None:
        return fallback_weights
    snapshot = recommendation.config_values_used.get("scoring_weights")
    if not isinstance(snapshot, dict):
        return fallback_weights
    try:
        return ScoreWeights(**snapshot)
    except Exception:
        # 過去に永続化された外部データの形式不整合に対する防御的フォール
        # バックであり、本モジュール自身が生成したデータではない。
        return fallback_weights


def format_watchlist_line(
    display_name: str,
    stock_code: str,
    record: BuyCandidateEvaluationRecord | None,
    recommendation: Recommendation | None,
    fallback_weights: ScoreWeights,
) -> str:
    """ウォッチリスト1銘柄1行の表示文字列を組み立てる(表示専用、副作用なし)。

    形式: 「社名（銘柄コード）｜区分｜区分理由｜補足懸念」。区分理由・補足懸念は
    それぞれ無ければ省略する(説明要素は最大2件、必ず2件表示する必要はない)。
    区分理由(buy_decision_reasons由来)と補足懸念(score_breakdown由来)は
    互いに独立した別々の判定結果であり因果関係が無いため、「、」ではなく
    「｜」で区切り、2つの独立した情報であることを明示する(2026-08、
    ウォッチリスト表示改善)。補足懸念の算出には、判定時点のScoreWeights
    (_resolve_weights参照)を使う。
    """
    header = f"{display_name}（{stock_code}）"
    if record is None:
        return f"{header}｜判定履歴なし"

    label = category_label(record.purchase_category)
    reason = _category_reason_text(record, recommendation)
    concern = (
        _select_supplementary_concern(
            recommendation.score_breakdown,
            _resolve_weights(recommendation, fallback_weights),
            recommendation.buy_decision_reasons,
        )
        if recommendation is not None
        else None
    )

    parts = [part for part in (reason, concern) if part]
    if not parts:
        return f"{header}｜{label}"
    return f"{header}｜{label}｜{'｜'.join(parts)}"


def format_watchlist_line_body(
    display_name: str,
    stock_code: str,
    record: BuyCandidateEvaluationRecord | None,
    recommendation: Recommendation | None,
    fallback_weights: ScoreWeights,
) -> str:
    """ウォッチリスト1銘柄1行の表示文字列を、区分ラベルを含めずに組み立てる
    (ウォッチリスト表示改善2026-08)。7区分見出し単位でグルーピングし、
    区分ラベルは見出し側で1回だけ表示する新方式(WatchlistViewService)向け。

    形式: 「社名（銘柄コード）｜区分理由｜補足懸念」。区分理由・補足懸念が
    共に無ければ「｜」以降を省略する(format_watchlist_line()と同じロジックを
    区分ラベル抜きで再利用する)。区切りを「｜」とする理由は
    format_watchlist_line()のdocstring参照。
    """
    header = f"{display_name}（{stock_code}）"
    if record is None:
        return f"{header}｜判定履歴なし"

    reason = _category_reason_text(record, recommendation)
    concern = (
        _select_supplementary_concern(
            recommendation.score_breakdown,
            _resolve_weights(recommendation, fallback_weights),
            recommendation.buy_decision_reasons,
        )
        if recommendation is not None
        else None
    )

    parts = [part for part in (reason, concern) if part]
    if not parts:
        return header
    return f"{header}｜{'｜'.join(parts)}"
