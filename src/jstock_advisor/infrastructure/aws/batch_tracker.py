"""Lambda銘柄単位ファンアウト(lambda_handlers/_fanout.py)の完了検知用カウンタ。

DynamoDBの原子的なADD操作(UpdateItem)で完了件数・区分別内訳をカウントし、
最後の1件を処理したワーカーが「自分が最後だった」と検知してサマリー通知を送信する
(Step Functions等の追加インフラを使わない軽量な集約方式)。

ローカル(非Lambda)環境では常にNoneを返す。_fanout.py自体がLambda上でのみ
非同期再帰呼び出しを行う設計であり、ローカルCLIはこの機構を使わないため。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import boto3
from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.evaluation_audit import SUMMARY_CATEGORIES
from jstock_advisor.infrastructure.collection_store import resolve_table_name, running_on_lambda

_TABLE_FILE_NAME = "batch_runs.json"  # resolve_table_nameの命名規則(jstock-batch_runs)に合わせる
_TTL_HOURS = 6  # 集計用の一時データのため、数時間で自動削除する

# 銘柄コード一覧を記録する区分(要求仕様§13: 処理失敗・データ不足は銘柄コードも表示する)
_CATEGORIES_WITH_STOCK_CODES = ("data_insufficient", "failed")

# --- 統合BUY候補パイプライン(2026-07)で追加。保有銘柄の業種・時価をsector_entries
# として集約し、ポートフォリオ集中度判定に使う(要求仕様§5後半・§8)。
# MAX_SECTOR_ENTRY_BYTES x MAX_SECTOR_ENTRIES = 160,000バイト(約156KB)。
# DynamoDB項目上限400KBに対し、ranking_entries等の他属性を含めても十分な余裕がある。
# MAX_SECTOR_ENTRIESはfinalize時の読み込み上限だけでなく、buy_candidates_handler.py
# 側のdispatch前ガード(保有銘柄数がこれを超える場合はfan-outしない)としても使う。 ---
MAX_SECTOR_ENTRY_BYTES = 80
MAX_SECTOR_ENTRIES = 2000

# --- ウォッチリスト自動追加機能(2026-08)で追加。合格銘柄のRankingEntry(JSON文字列)を
# ranking_entriesとして集約する。1件あたりの上限バイト数は、実際にJSON化を行う
# services/watchlist_screening_service.MAX_RANKING_ENTRY_BYTESと同じ値(500バイト)を
# 前提とする(レイヤ分離のためこのモジュールからはimportしない。値を変更する場合は
# 両方を揃えて変更すること)。MAX_RANKING_ENTRY_BYTES x MAX_RANKING_ENTRIES =
# 150,000バイト(約146KB)。評価対象銘柄数はdispatch前にMAX_RANKING_ENTRIESを超えないか
# 検証される(watchlist_auto_addition_handler.py)ため、data_insufficient_codes/
# failed_codes等の銘柄コード集合を合算してもDynamoDB項目上限400KBに対し十分な余裕がある
# (既存のsector_entries機構と同じ考え方)。 ---
MAX_RANKING_ENTRY_BYTES = 500
MAX_RANKING_ENTRIES = 300

# finalize失敗時にDynamoDB項目へ保存するエラーメッセージの最大文字数(機密情報混入の
# リスクとDynamoDB項目サイズの両方を考慮し、詳細はCloudWatch Logs側のlogger.exception
# に譲り、ここには概要のみを保存する)。
MAX_FINALIZE_ERROR_MESSAGE_LENGTH = 500


class BatchFinalizeStatus(StrEnum):
    """永続データ更新を伴うバッチのfinalize排他制御用状態(ウォッチリスト自動追加機能)。

    複数ワーカーが同時にis_complete==Trueを観測しても、RUNNING→FINALIZINGへの
    原子的な条件付き遷移(try_acquire_finalize)に成功した1ワーカーだけがfinalize
    処理へ進めるようにする。読み取り専用の集計・通知処理のみを行うBUY/holdings等の
    既存バッチは、このステータスを一切参照しない(対象外)。

    RUNNING→FINALIZING→COMPLETED(成功)またはFINALIZING→FINALIZE_FAILED(失敗)へ
    遷移する。FINALIZE_FAILEDは同一batch_idに対して終端状態として扱う(try_acquire_finalize
    はFINALIZE_FAILEDからの自動的な再取得を許可しない。通常の重複ワーカーによる
    再取得を防ぐため)。復旧は新しいbatch_idでのバッチ再実行(次回の週次スケジュール、
    または手動でのCLI/EventBridge再実行)によって行う想定であり、同一batch_idを
    そのまま再開する仕組みは実装しない(record_result等の他の仕組みと同様、
    batch_idは常に新規生成される前提のため)。
    """

    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FINALIZE_FAILED = "FINALIZE_FAILED"


@dataclass(frozen=True)
class BatchProgress:
    total: int
    completed: int
    category_counts: dict[str, int]
    data_insufficient_stock_codes: list[str]
    failed_stock_codes: list[str]
    # 買い候補分析の優先度付け通知(2026-07仕様追加)向け。record_resultに
    # ranking_entryを渡した銘柄が、生の"score|stock_code|recommendation_id"
    # 文字列のまま集約される(順序はDynamoDBの文字列セットのため保証されない。
    # 呼び出し側でパース・ソートすること)。
    ranking_entries: list[str]
    # 統合BUY候補パイプライン(2026-07)向け。保有銘柄ワーカーが報告する
    # "{sector}|{market_value}|{stock_code}"文字列の集合(全保有銘柄が対象、
    # BUY_FAMILY以外も含む)。finalize側でポートフォリオ総額・業種別総額を
    # 導出するために使う(順序保証なし、呼び出し側でパース・集計すること)。
    sector_entries: list[str]
    # dispatch時に確定した保有銘柄の総数(統合BUY候補パイプライン2026-07)。
    # finalize側がsector_entriesの集計件数と比較し、全保有銘柄分のエントリが
    # 揃っているか(=PortfolioValuationBasis.MARKET_VALUEとして信頼できるか)を
    # 判定するために使う。
    holding_count: int

    @property
    def is_complete(self) -> bool:
        return self.completed >= self.total


def _table() -> Any:
    return boto3.resource("dynamodb").Table(resolve_table_name(_TABLE_FILE_NAME))


def start_batch(batch_id: str, total: int, now: dt.datetime, holding_count: int = 0) -> None:
    """ファンアウト開始時に呼ぶ。ローカル環境・対象0件の場合は何もしない。

    holding_count(統合BUY候補パイプライン2026-07で追加)は、このバッチで
    dispatchされた保有銘柄(HOLDING/BOTH)の総数。finalize側がsector_entriesの
    集計件数と比較し、ポートフォリオ集中度計算の信頼性を判定するために使う。
    """
    if total <= 0 or not running_on_lambda():
        return
    ttl = int((now + dt.timedelta(hours=_TTL_HOURS)).timestamp())
    item: dict[str, Any] = {
        "batch_id": batch_id,
        "total": total,
        "completed": 0,
        "ttl": ttl,
        "holding_count": holding_count,
        # ウォッチリスト自動追加機能のfinalize排他制御(try_acquire_finalize)向け。
        # 既存のBUY/holdingsハンドラはこの属性を一切参照しないため、追加しても
        # 既存動作に影響しない。
        "status": BatchFinalizeStatus.RUNNING.value,
    }
    for category in SUMMARY_CATEGORIES:
        item[category] = 0
    _table().put_item(Item=item)


def record_result(
    batch_id: str,
    category: str,
    stock_code: str | None = None,
    ranking_entry: str | None = None,
    sector_entry: str | None = None,
) -> BatchProgress | None:
    """1銘柄の処理完了を原子的に記録し、現在の進捗を返す(ローカル環境ではNone)。

    categoryは"sent"/"hold"/"review"/"data_insufficient"/"suppressed"/"failed"/
    "candidate_not_ranked"(domain/entities/evaluation_audit.pyのSUMMARY_CATEGORIES
    と同じ集合)。data_insufficient/failedの場合、stock_codeを渡すとDynamoDBの
    文字列セットへ原子的に追加し、バッチサマリーで銘柄コードを表示できるようにする。

    ranking_entryを渡すと、優先度付け通知(要求仕様2026-07追加)向けに、任意の
    文字列(呼び出し側でスコア等を含めてエンコードする)をDynamoDBの文字列セットへ
    原子的に追加する。バッチ完了検知後、呼び出し側がこの一覧をパース・ソートして
    上位N件のみ通知する用途を想定している。

    sector_entryを渡すと、統合BUY候補パイプライン(2026-07追加)向けに、保有銘柄の
    業種・時価総額の集計用文字列をDynamoDBの文字列セットへ原子的に追加する。
    呼び出し側でMAX_SECTOR_ENTRY_BYTESを超えないことを事前に検証してから渡すこと
    (本関数はサイズ検証を行わない)。
    """
    if not running_on_lambda():
        return None
    if category not in SUMMARY_CATEGORIES:
        raise ValueError(f"unknown batch result category: {category}")

    # categoryは"hold"等、DynamoDBの予約語と衝突しうる文字列をそのまま属性名に使うため、
    # ExpressionAttributeNamesで必ずプレースホルダ経由にする(直書きするとUpdateItemが
    # ValidationException: reserved keywordで失敗する)。
    names: dict[str, str] = {"#category": category, "#completed": "completed"}
    update_expr = "ADD #category :one, #completed :one"
    values: dict[str, Any] = {":one": 1}
    if stock_code is not None and category in _CATEGORIES_WITH_STOCK_CODES:
        names["#category_codes"] = f"{category}_codes"
        update_expr += ", #category_codes :codes"
        values[":codes"] = {stock_code}
    if ranking_entry is not None:
        names["#ranking_entries"] = "ranking_entries"
        update_expr += ", #ranking_entries :ranking_entries"
        values[":ranking_entries"] = {ranking_entry}
    if sector_entry is not None:
        names["#sector_entries"] = "sector_entries"
        update_expr += ", #sector_entries :sector_entries"
        values[":sector_entries"] = {sector_entry}

    response = _table().update_item(
        Key={"batch_id": batch_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    item = response["Attributes"]
    return BatchProgress(
        total=int(item["total"]),
        completed=int(item["completed"]),
        category_counts={category: int(item.get(category, 0)) for category in SUMMARY_CATEGORIES},
        data_insufficient_stock_codes=sorted(item.get("data_insufficient_codes", set())),
        failed_stock_codes=sorted(item.get("failed_codes", set())),
        ranking_entries=sorted(item.get("ranking_entries", set())),
        sector_entries=sorted(item.get("sector_entries", set())),
        holding_count=int(item.get("holding_count", 0)),
    )


def try_acquire_finalize(batch_id: str) -> bool:
    """複数ワーカーが同時にis_complete==Trueを観測しても、1ワーカーだけが
    RUNNING→FINALIZINGへの原子的な条件付き遷移に成功する(ウォッチリスト自動追加機能)。

    永続データ(WatchlistRepository等)を更新するバッチでのみ使用する。BUY/holdings等の
    既存バッチは読み取り専用の集計・通知処理のみのため対象外(呼び出さない)。
    ローカル(非Lambda)環境では常にTrueを返す(単一プロセスのため排他不要)。
    """
    if not running_on_lambda():
        return True
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="SET #status = :finalizing",
            ConditionExpression="attribute_not_exists(#status) OR #status = :running",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":finalizing": BatchFinalizeStatus.FINALIZING.value,
                ":running": BatchFinalizeStatus.RUNNING.value,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def mark_finalize_complete(batch_id: str) -> None:
    """finalize処理が正常に完了した場合にのみ呼び、statusをCOMPLETEDへ遷移する。

    finalize処理が例外を送出した場合はmark_finalize_failed()を呼ぶこと(このため
    呼び出し側はtry/exceptで両者を明示的に使い分ける。以前はtry/finallyで常に
    COMPLETEDへ遷移させていたが、finalize失敗時にも成功扱いになってしまう不具合が
    あったため、FINALIZE_FAILEDという独立した終端状態を導入した)。
    """
    if not running_on_lambda():
        return
    _table().update_item(
        Key={"batch_id": batch_id},
        UpdateExpression="SET #status = :completed",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":completed": BatchFinalizeStatus.COMPLETED.value},
    )


def mark_finalize_failed(batch_id: str, error_message: str | None = None) -> None:
    """finalize処理が例外で失敗した場合に呼び、statusをFINALIZE_FAILEDへ遷移する。

    finalize_failed_at/finalize_error_message/updated_atを合わせて記録する。
    error_messageはMAX_FINALIZE_ERROR_MESSAGE_LENGTHで切り詰めてから保存する
    (DynamoDB項目サイズの節約に加え、詳細なスタックトレース等の機密情報を
    含みうる長い例外メッセージをそのまま保存しないための安全策。詳細な原因調査は
    CloudWatch Logs側のlogger.exceptionを参照する)。
    """
    if not running_on_lambda():
        return
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    truncated_message = (
        (error_message or "")[:MAX_FINALIZE_ERROR_MESSAGE_LENGTH] if error_message else None
    )
    _table().update_item(
        Key={"batch_id": batch_id},
        UpdateExpression=(
            "SET #status = :failed, #failed_at = :failed_at, "
            "#error_message = :error_message, #updated_at = :updated_at"
        ),
        ExpressionAttributeNames={
            "#status": "status",
            "#failed_at": "finalize_failed_at",
            "#error_message": "finalize_error_message",
            "#updated_at": "updated_at",
        },
        ExpressionAttributeValues={
            ":failed": BatchFinalizeStatus.FINALIZE_FAILED.value,
            ":failed_at": now_iso,
            ":error_message": truncated_message,
            ":updated_at": now_iso,
        },
    )
