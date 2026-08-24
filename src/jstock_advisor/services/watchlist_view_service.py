"""ウォッチリスト一覧表示(LINE UI第二弾、読み取り専用、2026-08)。

WatchlistItem/BuyCandidateEvaluationRecord/Recommendationを一切書き換えない
読み取り専用サービス。直近購入判定は「直近NORMAL完了batchにおける当該銘柄の
判定」と定義する(全履歴からの最新1件ではない)。
"""

from __future__ import annotations

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import ScoreWeights
from jstock_advisor.domain.entities.enums import Priority
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.latest_batch_records_provider import (
    STILL_PROPAGATING_MESSAGE,
    LatestBatchStillPropagating,
    fetch_latest_normal_batch_records,
)
from jstock_advisor.services.watchlist_display_name import StockDisplayNameResolver
from jstock_advisor.services.watchlist_judgment_summary_formatter import format_watchlist_line

_PRIORITY_SORT_ORDER: dict[Priority, int] = {
    Priority.HIGH: 0,
    Priority.MEDIUM: 1,
    Priority.LOW: 2,
}


class WatchlistViewService:
    def __init__(
        self,
        watchlist_repository: WatchlistRepository | None = None,
        evaluation_record_repository: BuyCandidateEvaluationRecordRepository | None = None,
        recommendation_repository: RecommendationRepository | None = None,
        latest_batch_pointer_repository: LatestBuyCandidateBatchPointerRepository | None = None,
        display_name_resolver: StockDisplayNameResolver | None = None,
        score_weights: ScoreWeights | None = None,
    ) -> None:
        self._watchlist = watchlist_repository or WatchlistRepository()
        self._evaluation_records = (
            evaluation_record_repository or BuyCandidateEvaluationRecordRepository()
        )
        self._recommendations = recommendation_repository or RecommendationRepository()
        self._pointer = (
            latest_batch_pointer_repository or LatestBuyCandidateBatchPointerRepository()
        )
        self._display_name_resolver = display_name_resolver
        self._score_weights = score_weights or load_config().scoring.weights

    def build_lines(self) -> list[str] | str:
        """戻り値: 表示行のリスト(1銘柄1行)、またはGSI反映待ちを示す単一の
        安全側メッセージ(str)。呼び出し側はstrの場合そのままLINE本文として
        使うこと(不完全な一覧を完全な一覧として表示しないため)。
        """
        items = sorted(
            self._watchlist.list_all(),
            key=lambda item: (_PRIORITY_SORT_ORDER.get(item.priority, 1), item.stock_code),
        )

        batch_records = fetch_latest_normal_batch_records(self._pointer, self._evaluation_records)
        if isinstance(batch_records, LatestBatchStillPropagating):
            return STILL_PROPAGATING_MESSAGE
        records_by_stock_code = batch_records.records_by_stock_code if batch_records else {}

        lines: list[str] = []
        for item in items:
            record = records_by_stock_code.get(item.stock_code)
            recommendation = (
                self._recommendations.get(record.recommendation_id)
                if record is not None and record.recommendation_id is not None
                else None
            )
            display_name = (
                self._display_name_resolver.resolve(item.stock_code, fallback_name=item.stock_name)
                if self._display_name_resolver is not None
                else item.stock_name or item.stock_code
            )
            lines.append(
                format_watchlist_line(
                    display_name, item.stock_code, record, recommendation, self._score_weights
                )
            )
        return lines
