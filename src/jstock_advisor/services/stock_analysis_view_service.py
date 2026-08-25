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

from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
)
from jstock_advisor.domain.entities.common import BuyPriceLevels
from jstock_advisor.domain.entities.enums import BuyAction, PurchaseCategory, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
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


class StockAnalysisViewService:
    def __init__(
        self,
        evaluation_record_repository: BuyCandidateEvaluationRecordRepository | None = None,
        latest_batch_pointer_repository: LatestBuyCandidateBatchPointerRepository | None = None,
        recommendation_repository: RecommendationRepository | None = None,
        holding_evaluation_record_repository: HoldingEvaluationRecordRepository | None = None,
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

        reason = _buy_reason_text(record, recommendation)
        if reason:
            lines += ["", "■ 理由", reason]

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

        lines = ["【銘柄分析】", f"{stock_code}（{owner}）", "", "■ 判定"]
        lines.append(
            _HOLDING_JUDGMENT_LABEL.get(recommendation.recommendation_type, "要確認")
            if recommendation is not None
            else "保有継続"
        )

        if recommendation is None:
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
