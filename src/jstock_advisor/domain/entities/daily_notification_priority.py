"""cross-pipeline通知優先度記録(コードレビュー対応2026-08、指摘5。M3で
保有銘柄オーナー機能対応)。

BUY候補Lambdaと保有銘柄Lambdaは別々のLambdaであり、共有の排他制御機構を
持たない。本エンティティは「当日・当該銘柄(またはholding)について、これまでに
送信された通知のうち最も高い優先度」を記録し、best-effortで低優先度の重複通知を
抑止するために使う(完全な排他制御ではない、要求仕様どおりの合意事項)。

M3: スコープはstock-scope("_STOCK_"固定サフィックス、BUY/PROMOTED_TO_BUY系)と
holding-scope(holding_id、SELL/PARTIAL/ATTENTION等)の2種類。
- holding-scope判定時: 自holding keyとstockの"_STOCK_" keyの両方を見る。
- stock-scope判定時: "_STOCK_" keyだけを見る。
- holding側からstock側の記録を書き換えることはない(逆も同様)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import Entity

# stock-scope(BUY候補、holding_id概念が無い)を表す固定サフィックス。
STOCK_SCOPE_SUFFIX = "_STOCK_"


def build_daily_notification_priority_id(
    stock_code: str, business_date: dt.date, holding_id: str | None = None
) -> str:
    """holding_id指定時はholding-scopeキー({date}:{stock_code}:{holding_id})、
    未指定時はstock-scopeキー({date}:{stock_code}:_STOCK_)を返す。"""
    scope = holding_id if holding_id is not None else STOCK_SCOPE_SUFFIX
    return f"{business_date.isoformat()}:{stock_code}:{scope}"


class DailyNotificationPriorityRecord(Entity):
    record_id: str
    stock_code: str
    # M3: holding-scopeの記録のみ設定される(stock-scopeはNoneのまま)。
    holding_id: str | None = None
    # このbusiness_date(および上記build_daily_notification_priority_id()へ渡す
    # business_date)はJST暦日基準(domain/jst.pyのevaluation_date_jst()経由)。
    # LambdaのnowはUTCのため、この変換を経ずnow.date()を直接使うとJST 08:00〜09:00台で
    # 別レコード扱いになる(再コードレビュー対応2026-08、JST暦日境界修正)。
    business_date: dt.date
    # 数値が大きいほど優先度が高い(CRITICAL_RISK=6 > PROMOTED_TO_BUY=5 >
    # SELL/PARTIAL_SELL=4 > BUY=3 > ATTENTION=2 > その他=0。NEAR_BUY/
    # WATCH_BEFORE_EARNINGSは独立した階層ではなく「その他=0」に含まれる。
    # line_notification_service.py _notification_priority()参照)。
    priority: int
    category: str
