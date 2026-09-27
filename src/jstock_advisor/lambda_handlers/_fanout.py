"""銘柄単位のファンアウト処理(Lambda自身への非同期再帰呼び出し、またはSQS経由)。

1回のLambda実行で全保有銘柄・全ウォッチリスト銘柄を直列に処理すると、
実データ取得(yfinance/EDINET)のレイテンシが積み上がりLambdaの最大タイムアウト
(900秒)を超えることがある(要求仕様18節、本番運用で実際に発生した障害)。

これを避けるため、通常のスケジュール起動(EventBridge Scheduler、event引数に
"task"キーを含まない)では銘柄一覧の取得と、銘柄ごとの非同期自己呼び出し
(InvocationType="Event")の発行のみを行い、即座に制御を返す(ディスパッチ役)。
実際のデータ取得・判定・通知は、"task"キー付きで再帰的に呼び出された各Lambda
インスタンスが1銘柄のみを担当して行う(実行役)。これにより各銘柄が独立した
900秒のタイムアウト予算を持ち、1銘柄の遅延・異常が他銘柄に波及しない。

Issue #533(#319 Phase 2): 上限を超える流入があったとき、非同期Event呼び出し
(DLQなし・再試行2回・保持6時間)ではデータが失われうる。`dispatch_sqs()`は
同じ銘柄単位のペイロードをSQS Queue(#532で追加済み。DLQ・再試行3回・
保持14日)へ送る代替経路であり、`*_SQS_DISPATCH_ENABLED`環境変数(既定false)で
切り替える。既定は従来どおり`dispatch_async()`(旧経路)であり、本Issueの
scopeはPhase 2(切替機構の実装)のみで、Phase 3(Production有効化)は対象外。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger(__name__)

#: BUY候補分析Lambdaのdispatchをsqs経由へ切り替える環境変数。既定=false(従来の
#: 非同期自己再帰呼び出し)。infrastructure/weekly_evaluation_aggregate_store.py
#: の_env_true()と同型の規約(大文字小文字を問わず"true"文字列のみ有効)。
BUY_CANDIDATE_SQS_DISPATCH_ENABLED_ENV = "BUY_CANDIDATE_SQS_DISPATCH_ENABLED"
#: 保有銘柄分析Lambdaの同型トグル(buy候補側と独立)。
HOLDINGS_WATCHLIST_SQS_DISPATCH_ENABLED_ENV = "HOLDINGS_WATCHLIST_SQS_DISPATCH_ENABLED"


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def buy_candidate_sqs_dispatch_enabled() -> bool:
    return _env_true(BUY_CANDIDATE_SQS_DISPATCH_ENABLED_ENV)


def holdings_watchlist_sqs_dispatch_enabled() -> bool:
    return _env_true(HOLDINGS_WATCHLIST_SQS_DISPATCH_ENABLED_ENV)


def dispatch_async(function_name: str, payload: dict[str, Any]) -> None:
    """自分自身(または指定した関数)を非同期(fire-and-forget)で呼び出す。"""
    client = boto3.client("lambda")
    client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def dispatch_sqs(queue_url: str, payload: dict[str, Any]) -> None:
    """銘柄単位のペイロードをSQS Queueへ送信する(Issue #533。#319 Phase 2)。

    `dispatch_async()`と同じ呼び出し規約(1回の呼び出しで1銘柄分のpayloadを
    1件送る)を保ち、呼び出し元のfan-outループ自体は変更しない。
    """
    client = boto3.client("sqs")
    client.send_message(QueueUrl=queue_url, MessageBody=json.dumps(payload))


def resolve_function_name(context: object, env_fallback: str) -> str:
    """Lambdaコンテキストオブジェクトから関数名を取得する(テスト時は環境変数にフォールバック)。"""
    name = getattr(context, "function_name", None)
    return name if isinstance(name, str) and name else env_fallback
