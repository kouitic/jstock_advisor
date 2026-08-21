"""売買記録の一時停止フラグ(保有銘柄オーナー機能移行時の書込停止に使用)。

再デプロイ不要でCLIから独立に切り替えるための専用DynamoDBテーブル
(HoldingDecisionRuntimeConfigと同じ考え方)。ただし当該既存実装はcreate
(CollectionStore経由のdataブロブ書き込み)とupdate(生boto3のトップレベル
属性UpdateExpression)とで永続化形式が一致していないという既知の不整合が
あるため、本エンティティはその考え方のみを流用し、DynamoDB版はcreate/get/
updateの3操作をすべてトップレベル属性のみで一貫させる専用実装とする
(infrastructure/aws/trading_pause_config.py参照)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import Entity


class TradingPauseConfig(Entity):
    config_id: str = "trading_pause"
    config_version: int
    pause_buy_sell: bool
    updated_at: dt.datetime
    updated_by: str
    change_reason: str
