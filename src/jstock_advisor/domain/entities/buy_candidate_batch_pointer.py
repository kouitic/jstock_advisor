"""最新完了BUY候補batchポインタ(LINE UI第二弾「対象確認」機能、2026-08)。

単一行(pointer_id="default"固定)のみ保持する永続ポインタ。「対象確認」機能が
"直近の完了したBUY候補分析batch"を、テーブル全体のScanやTTL6時間で消える
BatchRunsTableに頼らずO(1)で特定するために使う。

更新は`buy_candidates_handler._finalize_batch()`から、以下2条件を両方満たす
場合のみ行う(詳細は実装プランのやり取り・functional_spec.md参照):
  1. execution_context.mode == ExecutionMode.NORMAL(VALIDATION/DRY_RUNでは
     絶対に更新しない)
  2. そのbatchの全対象銘柄についてBuyCandidateEvaluationRecordの保存が
     成功している(evaluation_record_saved_stock_codesの件数が総対象数と一致)

条件を満たさない場合はポインタを更新せず、直前の正常完了batchの値を
維持したままにする(前回正常batchへのフォールバック)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import Entity

DEFAULT_POINTER_ID = "default"


class LatestBuyCandidateBatchPointer(Entity):
    pointer_id: str = DEFAULT_POINTER_ID
    latest_completed_batch_id: str
    completed_at: dt.datetime
    total_candidates: int
