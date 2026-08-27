"""対象確認(直近NORMAL完了BUY候補batch、カテゴリー別一覧)表示
(LINE UI第二弾、読み取り専用、2026-08)。

表示区分は現行の買い候補サマリー通知(line_notification_service.py
notify_batch_summary)が実際に使っているラベル・集約ルールをそのまま再利用する
(独自の分類を新設しない)。
"""

from __future__ import annotations

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import ScoreWeights
from jstock_advisor.domain.entities.enums import PurchaseCategory
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
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
from jstock_advisor.services.watchlist_judgment_summary_formatter import format_watchlist_line_body

# 現行サマリー通知(line_notification_service.py:3007-3018)と同じ7分類・
# 同じ表示順。
CATEGORY_DISPLAY_LABELS: tuple[str, ...] = (
    "買い候補",
    "買い間近",
    "買い待ち",
    "買い対象外",
    "要確認",
    "データ不足",
    "処理失敗",
)

# 表示ラベル→対応するPurchaseCategoryの集合。「買い待ち」「買い対象外」は
# 現行サマリー通知が複数のPurchaseCategoryを1つのラベルへ集約している
# ルールをそのまま踏襲する。
_LABEL_TO_CATEGORIES: dict[str, frozenset[PurchaseCategory]] = {
    "買い候補": frozenset({PurchaseCategory.BUY_CANDIDATE}),
    "買い間近": frozenset({PurchaseCategory.NEAR_BUY}),
    "買い待ち": frozenset(
        {PurchaseCategory.WATCH_FOR_PRICE, PurchaseCategory.WATCH_BEFORE_EARNINGS}
    ),
    "買い対象外": frozenset({PurchaseCategory.NOT_ATTRACTIVE, PurchaseCategory.EXCLUDED}),
    "要確認": frozenset({PurchaseCategory.MANUAL_REVIEW}),
    "データ不足": frozenset({PurchaseCategory.DATA_INSUFFICIENT}),
    "処理失敗": frozenset({PurchaseCategory.FAILED}),
}


def is_valid_category_label(label: str) -> bool:
    return label in _LABEL_TO_CATEGORIES


class BuyCandidateTargetViewService:
    def __init__(
        self,
        evaluation_record_repository: BuyCandidateEvaluationRecordRepository | None = None,
        latest_batch_pointer_repository: LatestBuyCandidateBatchPointerRepository | None = None,
        display_name_resolver: StockDisplayNameResolver | None = None,
        recommendation_repository: RecommendationRepository | None = None,
        fallback_score_weights: ScoreWeights | None = None,
    ) -> None:
        self._evaluation_records = (
            evaluation_record_repository or BuyCandidateEvaluationRecordRepository()
        )
        self._pointer = (
            latest_batch_pointer_repository or LatestBuyCandidateBatchPointerRepository()
        )
        self._display_name_resolver = display_name_resolver
        # レビュー対応(2026-08、対象確認の短文表示追加): ウォッチリスト表示
        # (watchlist_view_service.py)と全く同じ短文生成ロジック
        # (watchlist_judgment_summary_formatter.py)を再利用するための依存。
        # 独自の短文生成ロジックは新設しない(同一Recommendationに対して
        # ウォッチリストと対象確認で異なる短文が出ることを防ぐ)。
        self._recommendations = recommendation_repository or RecommendationRepository()
        self._fallback_score_weights = fallback_score_weights or load_config().scoring.weights

    def build_lines(self, category_label: str) -> list[str] | str:
        """戻り値: 表示行のリスト(1銘柄1行、「社名（銘柄コード）｜区分理由｜
        補足懸念」。区分理由・補足懸念はいずれも無ければ省略、
        watchlist_judgment_summary_formatter.format_watchlist_line_body()を
        そのまま再利用する)、またはGSI反映待ちを示す単一の安全側メッセージ
        (str)。
        """
        categories = _LABEL_TO_CATEGORIES.get(category_label)
        if categories is None:
            return []

        batch_records = fetch_latest_normal_batch_records(self._pointer, self._evaluation_records)
        if isinstance(batch_records, LatestBatchStillPropagating):
            return STILL_PROPAGATING_MESSAGE
        if batch_records is None:
            return []

        matched = [
            record
            for record in batch_records.records_by_stock_code.values()
            if record.purchase_category in categories
        ]
        matched.sort(
            key=lambda r: (r.unified_rank is None, r.unified_rank or 0, r.stock_code)
        )

        # レビュー対応(2026-08): 1銘柄ごとにRecommendationRepository.get()を
        # 呼ぶとN+1(最大で区分内の全銘柄数だけGetItem)になるため、
        # 対象のrecommendation_idをまとめてget_many()で一括取得する
        # (DynamoDB実装はBatchGetItemを使う。infrastructure/aws/
        # dynamodb_store.py::get_many()参照)。
        recommendation_ids = [r.recommendation_id for r in matched if r.recommendation_id]
        recommendations_by_id = self._recommendations.get_many(recommendation_ids)

        lines: list[str] = []
        for record in matched:
            display_name = (
                self._display_name_resolver.resolve(record.stock_code)
                if self._display_name_resolver is not None
                else record.stock_code
            )
            recommendation = (
                recommendations_by_id.get(record.recommendation_id)
                if record.recommendation_id is not None
                else None
            )
            lines.append(
                format_watchlist_line_body(
                    display_name,
                    record.stock_code,
                    record,
                    recommendation,
                    self._fallback_score_weights,
                )
            )
        return lines
