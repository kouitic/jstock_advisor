"""直近NORMAL完了BUY候補batchの評価レコード取得(LINE UI第二弾、2026-08)。

latest completed batch pointer + batch_id-index(GSI)を使い、「ウォッチリスト」
「対象確認」の両機能が共通で使う"直近NORMAL完了batchの全評価レコード"取得処理を
1箇所に集約する。

GSIは結果整合性のみ(強い整合性読み取りを選択できない)ため、pointerが保持する
total_candidates(そのbatchの全dispatch対象銘柄数)とQuery結果件数を比較し、
不一致なら短い再試行を1回だけ行う。それでも不一致ならLATEST_BATCH_STILL_
PROPAGATINGを返し、呼び出し側は不完全な一覧を完全な一覧として表示せず、
安全側メッセージを表示すること。

EvaluationRecordの保存欠損(GSI反映遅延とは別の懸念)は、latest batch pointer
自体の更新条件(buy_candidates_handler._finalize_batch参照、全対象銘柄分の
保存成功時のみpointerを更新)で既に防がれているため、ここでの不一致は
構造的にGSI反映遅延のみに絞り込まれる。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
)
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)

_GSI_CONSISTENCY_RETRY_DELAY_SECONDS = 1.5

# LINEへ表示する安全側メッセージ(不完全な一覧を完全な一覧として見せない)。
STILL_PROPAGATING_MESSAGE = "直近の分析結果を反映中です。少し時間をおいて再度お試しください。"


class LatestBatchStillPropagating:
    """GSIの反映待ちで完全な一覧を取得できなかったことを示すセンチネル型。"""


LATEST_BATCH_STILL_PROPAGATING = LatestBatchStillPropagating()


@dataclass(frozen=True)
class LatestBatchRecords:
    batch_id: str
    records_by_stock_code: dict[str, BuyCandidateEvaluationRecord]


def fetch_latest_normal_batch_records(
    pointer_repo: LatestBuyCandidateBatchPointerRepository,
    evaluation_record_repo: BuyCandidateEvaluationRecordRepository,
    sleep: Callable[[float], None] = time.sleep,
) -> LatestBatchRecords | None | LatestBatchStillPropagating:
    """戻り値: `LatestBatchRecords`(取得成功) / `None`(直近NORMAL完了batchが
    一度も存在しない) / `LATEST_BATCH_STILL_PROPAGATING`(GSI反映待ち、再試行
    後も件数が一致しなかった)。
    """
    pointer = pointer_repo.get()
    if pointer is None:
        return None

    records = evaluation_record_repo.list_by_batch(pointer.latest_completed_batch_id)
    if len(records) != pointer.total_candidates:
        sleep(_GSI_CONSISTENCY_RETRY_DELAY_SECONDS)
        records = evaluation_record_repo.list_by_batch(pointer.latest_completed_batch_id)
        if len(records) != pointer.total_candidates:
            return LATEST_BATCH_STILL_PROPAGATING

    return LatestBatchRecords(
        batch_id=pointer.latest_completed_batch_id,
        records_by_stock_code={record.stock_code: record for record in records},
    )
