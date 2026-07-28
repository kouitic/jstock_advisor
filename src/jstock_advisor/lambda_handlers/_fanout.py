"""Lambda自身への非同期再帰呼び出しによる銘柄単位のファンアウト処理。

1回のLambda実行で全保有銘柄・全ウォッチリスト銘柄を直列に処理すると、
実データ取得(yfinance/EDINET)のレイテンシが積み上がりLambdaの最大タイムアウト
(900秒)を超えることがある(要求仕様18節、本番運用で実際に発生した障害)。

これを避けるため、通常のスケジュール起動(EventBridge Scheduler、event引数に
"task"キーを含まない)では銘柄一覧の取得と、銘柄ごとの非同期自己呼び出し
(InvocationType="Event")の発行のみを行い、即座に制御を返す(ディスパッチ役)。
実際のデータ取得・判定・通知は、"task"キー付きで再帰的に呼び出された各Lambda
インスタンスが1銘柄のみを担当して行う(実行役)。これにより各銘柄が独立した
900秒のタイムアウト予算を持ち、1銘柄の遅延・異常が他銘柄に波及しない。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3

logger = logging.getLogger(__name__)


def dispatch_async(function_name: str, payload: dict[str, Any]) -> None:
    """自分自身(または指定した関数)を非同期(fire-and-forget)で呼び出す。"""
    client = boto3.client("lambda")
    client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def resolve_function_name(context: object, env_fallback: str) -> str:
    """Lambdaコンテキストオブジェクトから関数名を取得する(テスト時は環境変数にフォールバック)。"""
    name = getattr(context, "function_name", None)
    return name if isinstance(name, str) and name else env_fallback
