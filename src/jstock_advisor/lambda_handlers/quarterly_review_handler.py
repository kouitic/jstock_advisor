"""四半期ロジックレビューLambda(schedule.yaml quarterly_logic_review、1,4,7,10月第1土曜11:00)。

振り返り機能改修: 「ユーザーにアクションが必要な場合のみLINE通知する」という
方針と矛盾するため、従来の固定リマインド文言の自動LINE送信は廃止した。
四半期の長期戦略レビュー機能そのものの新規構築は今回のスコープ外(要求仕様3節)
のため、Lambda・スケジュール自体は残しつつ、内部記録(ログ)のみを行いLINEは
送信しない。実際の`jstock rules backtest`/`rules propose`は引き続きユーザーが
手動で実行できる。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.lambda_handlers._scheduling import is_first_saturday_of_month

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_QUARTERLY_MONTHS = {1, 4, 7, 10}


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    is_quarterly_review_day = (
        now.month in _QUARTERLY_MONTHS and is_first_saturday_of_month(now.date())
    )
    logger.info(
        "quarterly_review_handler: LINE通知は行わない(振り返り機能改修)。"
        "is_quarterly_review_day=%s",
        is_quarterly_review_day,
    )
    return {"skipped": True, "is_quarterly_review_day": is_quarterly_review_day}
