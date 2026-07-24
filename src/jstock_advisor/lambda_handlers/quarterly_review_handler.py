"""四半期ロジックレビューLambda(schedule.yaml quarterly_logic_review、1,4,7,10月第1土曜11:00)。

ルール改善提案(RuleProposal)の作成にはリスク影響・過学習リスクの評価等、
人間の判断による自由記述が必須であり、Lambda側で自動生成することは
要求仕様45節の「人間承認必須」原則に反する。そのため本ハンドラは提案の
自動生成は行わず、レビュー時期が来たことをLINEでリマインドするに留める。
実際の`jstock rules backtest`/`rules propose`はユーザーが手動で実行する。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.lambda_handlers._scheduling import is_first_saturday_of_month

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_QUARTERLY_MONTHS = {1, 4, 7, 10}
_REMINDER_TEXT = (
    "【四半期ロジックレビューの時期です】\n"
    "jstock rules backtest / jstock rules propose で改善余地を確認してください。\n"
    "※最終的な投資判断は利用者が行ってください。"
)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    if now.month not in _QUARTERLY_MONTHS or not is_first_saturday_of_month(now.date()):
        logger.info("quarterly_review_handler skipped: not a quarterly review date")
        return {"skipped": True}

    build_line_client_from_env().push_message(_REMINDER_TEXT)
    logger.info("quarterly_review_handler done")
    return {"skipped": False}
