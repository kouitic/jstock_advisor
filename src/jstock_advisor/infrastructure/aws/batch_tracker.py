"""Lambda銘柄単位ファンアウト(lambda_handlers/_fanout.py)の完了検知用カウンタ。

DynamoDBの原子的なADD操作(UpdateItem)で完了件数・区分別内訳をカウントし、
最後の1件を処理したワーカーが「自分が最後だった」と検知してサマリー通知を送信する
(Step Functions等の追加インフラを使わない軽量な集約方式)。

ローカル(非Lambda)環境では常にNoneを返す。_fanout.py自体がLambda上でのみ
非同期再帰呼び出しを行う設計であり、ローカルCLIはこの機構を使わないため。
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.evaluation_audit import SUMMARY_CATEGORIES
from jstock_advisor.domain.signals.watchlist_screening import WatchlistScoreDetail
from jstock_advisor.infrastructure.collection_store import resolve_table_name, running_on_lambda

logger = logging.getLogger(__name__)

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
    再取得を防ぐため)。復旧は新しいbatch_idでのバッチ再実行(次回のスケジュール
    起動、または手動でのCLI/EventBridge再実行)によって行う想定であり、同一batch_idを
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
    # 通知検証モード機能(2026-08追加)。VALIDATION実行時のみ、_process_single_candidateが
    # 保存したRecommendationのrecommendation_idを報告する(このバッチ=1回の
    # VALIDATION実行で保存された全件を把握し、_finalize_batch完了後に検証用
    # テーブルから削除するため)。NORMAL実行では常に空。デフォルト空リストとし、
    # 既存のBatchProgress()呼び出し(テスト含む)を変更不要にする。
    validation_recommendation_ids: list[str] = field(default_factory=list)
    # NEAR BUY/WATCH_BEFORE_EARNINGS用のランキングエントリ(BUY候補裾野拡大
    # 機能2026-08)。ranking_entriesとは別集計とし、finalize側で独立した
    # ループ(日次上限5件等)を回す。
    near_buy_ranking_entries: list[str] = field(default_factory=list)
    # WATCH終了通知の対象recommendation_id一覧(コードレビュー対応2026-08、§3)。
    # ランキング(順位)は不要なため、値はrecommendation_idそのもの。
    watch_end_ranking_entries: list[str] = field(default_factory=list)
    # 保有銘柄バッチサマリーのユーザー行動中心4分類集計向け(コードレビュー
    # 対応2026-08、LINE通知/監査分離)。実際にLINE送信された銘柄について
    # "{NotificationCategory.value}|{stock_code}"の形式でDynamoDBの文字列
    # セットへ集約される(Setは重複を許さないため、stock_codeを含めて銘柄
    # ごとに一意にする。ranking_entriesと同じ設計パターン)。finalize側で
    # "|"より前を取り出しCounterで件数化する。
    notification_categories: list[str] = field(default_factory=list)

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
    validation_recommendation_id: str | None = None,
    near_buy_ranking_entry: str | None = None,
    watch_end_ranking_entry: str | None = None,
    notification_category_entry: str | None = None,
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

    validation_recommendation_idを渡すと、通知検証モード機能(2026-08追加)向けに、
    VALIDATION実行時に保存されたRecommendationのrecommendation_idをDynamoDBの
    文字列セットへ原子的に追加する。_finalize_batchが正常完了後にこの一覧を
    走査し、検証用テーブルから削除する(functional_spec.md参照)。NORMAL実行では
    渡さない。

    notification_category_entryを渡すと、保有銘柄バッチサマリーのユーザー
    行動中心4分類集計(コードレビュー対応2026-08、LINE通知/監査分離)向けに、
    "{NotificationCategory.value}|{stock_code}"形式の文字列をDynamoDBの
    文字列セットへ原子的に追加する(stock_codeを含めるのは、Setが重複を
    許さないため同一カテゴリの複数銘柄がまとめて1件に潰れるのを防ぐため)。
    実際にLINE送信された銘柄についてのみ呼び出し側が渡すこと。
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
    if validation_recommendation_id is not None:
        names["#validation_ids"] = "validation_recommendation_ids"
        update_expr += ", #validation_ids :validation_ids"
        values[":validation_ids"] = {validation_recommendation_id}
    if near_buy_ranking_entry is not None:
        names["#near_buy_ranking_entries"] = "near_buy_ranking_entries"
        update_expr += ", #near_buy_ranking_entries :near_buy_ranking_entries"
        values[":near_buy_ranking_entries"] = {near_buy_ranking_entry}
    if watch_end_ranking_entry is not None:
        names["#watch_end_ranking_entries"] = "watch_end_ranking_entries"
        update_expr += ", #watch_end_ranking_entries :watch_end_ranking_entries"
        values[":watch_end_ranking_entries"] = {watch_end_ranking_entry}
    if notification_category_entry is not None:
        names["#notification_categories"] = "notification_categories"
        update_expr += ", #notification_categories :notification_categories"
        values[":notification_categories"] = {notification_category_entry}

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
        validation_recommendation_ids=sorted(item.get("validation_recommendation_ids", set())),
        near_buy_ranking_entries=sorted(item.get("near_buy_ranking_entries", set())),
        watch_end_ranking_entries=sorted(item.get("watch_end_ranking_entries", set())),
        notification_categories=sorted(item.get("notification_categories", set())),
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
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
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


# ============================================================================
# ウォッチリスト自動追加(候補ユニバース本格対応・2026-08、第6版修正プラン)専用の
# 関数群。上記のBUY/holdings向け関数群(record_result/BatchProgress等)とは独立した
# コード経路とする。本機能のバッチはDISPATCHING/TIMEOUT_FINALIZING等、BUY/holdings
# には存在しない状態を持ち、SUMMARY_CATEGORIESベースの集計ではなく銘柄単位テーブル
# (WatchlistCandidateProgressTable)で進捗を追跡するため、既存関数の流用ではなく
# 新規に書き起こす。
# ============================================================================

_PROGRESS_TABLE_FILE_NAME = "watchlist_candidate_progress.json"

_serializer = TypeSerializer()


def _ser(value: Any) -> Any:
    return _serializer.serialize(value)


def _progress_table() -> Any:
    return boto3.resource("dynamodb").Table(resolve_table_name(_PROGRESS_TABLE_FILE_NAME))


def _progress_table_name() -> str:
    return resolve_table_name(_PROGRESS_TABLE_FILE_NAME)


def _batch_runs_table_name() -> str:
    return resolve_table_name(_TABLE_FILE_NAME)


class WatchlistBatchStatus(StrEnum):
    """候補ユニバース本格対応(第6版修正プラン1節)のバッチ状態。

    運用ハードニング第2弾2節: finalize処理を4段階(FINALIZE_PREPARING→
    WATCHLIST_WRITE_COMPLETED→NOTIFICATION_PENDING→NOTIFICATION_SENT)へ
    細分化し、各段階の成果物をBatchRunsTable項目へ永続化する(旧FINALIZING単一
    状態を改名・分割)。これにより、どの段階でLambdaが異常終了しても、
    再試行(retry_finalize)がフィールドの有無から再開地点を判定できる
    (statusはあくまで進捗表示用のマーカーであり、再開ロジックの分岐条件には
    使わない。詳細はwatchlist_batch_finalizer.pyのdocstring参照)。

    DISPATCHING → RUNNING → FINALIZE_PREPARING → WATCHLIST_WRITE_COMPLETED
         ↓             ↓  (429/欠損率閾値超過時はここからCOMPLETED/ABORTEDへ直行)
    DISPATCH_FAILED    → NOTIFICATION_PENDING → NOTIFICATION_SENT → COMPLETED/ABORTED
                     (いずれの段階でも例外時 ↘ FINALIZE_FAILED)
    RUNNING → TIMEOUT_FINALIZING → TIMED_OUT
                  ↘ TIMEOUT_FINALIZE_FAILED → (Reconciler再試行)TIMEOUT_FINALIZING

    20節: statusは処理ライフサイクルのみを表す。終了理由はexecution_result属性
    (COMPLETED時のみEXECUTION_RESULT_NORMAL、ABORTED時は
    _ABORTED_EXECUTION_RESULTSのいずれか)で区別する。
    """

    DISPATCHING = "DISPATCHING"
    RUNNING = "RUNNING"
    FINALIZE_PREPARING = "FINALIZE_PREPARING"
    WATCHLIST_WRITE_COMPLETED = "WATCHLIST_WRITE_COMPLETED"
    NOTIFICATION_PENDING = "NOTIFICATION_PENDING"
    NOTIFICATION_SENT = "NOTIFICATION_SENT"
    # 運用ハードニング第3弾1節: 通知送信が例外になった場合の専用状態。
    # finalize全体をFINALIZE_FAILEDにはせず、通知のみをReconciler/CLIが
    # 再試行できるようにする(ウォッチリスト追加結果はWATCHLIST_WRITE_COMPLETED
    # 時点で既に確定・保持されたまま)。
    NOTIFICATION_FAILED = "NOTIFICATION_FAILED"
    COMPLETED = "COMPLETED"
    # 運用ハードニング第3弾1節: 通知再試行が上限(max_notification_retry_attempts)に
    # 達しても送信できなかった場合の終端状態。ウォッチリスト追加自体は正常完了
    # している(execution_result=NORMAL)ため、ABORTEDとは区別する。
    COMPLETED_WITH_NOTIFICATION_FAILURE = "COMPLETED_WITH_NOTIFICATION_FAILURE"
    DISPATCH_FAILED = "DISPATCH_FAILED"
    FINALIZE_FAILED = "FINALIZE_FAILED"
    TIMEOUT_FINALIZING = "TIMEOUT_FINALIZING"
    TIMED_OUT = "TIMED_OUT"
    TIMEOUT_FINALIZE_FAILED = "TIMEOUT_FINALIZE_FAILED"
    ABORTED = "ABORTED"


class WatchlistProgressStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# ウォッチリスト自動運用の改善(ローテーション・自動メンテナンス、2026-08)で追加。
# 同一のDispatcher/Worker/Queue/Reconciler基盤を、新規候補スクリーニングと
# 既存AUTO_SCREENING銘柄の再評価(メンテナンス)の両方で共用するための識別子
# (計画Part C-7案A)。rotation commitはJOB_TYPE_NEW_CANDIDATE_SCREENINGの
# 場合のみ行う(計画Part A-9)。
#
# 横断整合性レビュー対応(2026-08、指摘1・High): job_typeを自由文字列として
# 扱わず、`WatchlistJobType`を唯一の定義元とする。Dispatcher/Worker/
# Finalizer/Reconciler/BatchTracker/SQSメッセージ生成・復元の全経路が
# この型(または`resolve_watchlist_job_type()`が返す値)を経由すること。
# 「未知値はmaintenance扱い」「未知値はnew candidate扱い」という暗黙の
# else-fallbackを行うと、Dispatcher側とWorker側で解釈が食い違う経路不整合
# (typo等の未知job_typeがDispatcherではmaintenance、Workerではnew candidate
# として処理される)が生じるため、全面的に禁止する。
class WatchlistJobType(StrEnum):
    NEW_CANDIDATE_SCREENING = "NEW_CANDIDATE_SCREENING"
    WATCHLIST_MAINTENANCE = "WATCHLIST_MAINTENANCE"


# 後方互換のための別名(二重定義ではなく、上記Enumメンバーそのものを指す単なる
# エイリアス)。StrEnumはstrのサブクラスのため、既存コード中の
# `job_type == JOB_TYPE_NEW_CANDIDATE_SCREENING`という比較・f-string埋め込み・
# DynamoDB文字列属性への書き込みはいずれも変更なしでそのまま動作する。
JOB_TYPE_NEW_CANDIDATE_SCREENING = WatchlistJobType.NEW_CANDIDATE_SCREENING
JOB_TYPE_WATCHLIST_MAINTENANCE = WatchlistJobType.WATCHLIST_MAINTENANCE


class UnknownWatchlistJobTypeError(ValueError):
    """job_typeが`WatchlistJobType`のいずれの値とも一致しない場合に送出する
    専用例外(2026-08横断整合性レビュー指摘1)。呼び出し側はこれを捕捉して
    fail-closed(処理を進めない)に倒すこと。"""


def resolve_watchlist_job_type(
    raw: str | None, *, default: WatchlistJobType | None = None
) -> WatchlistJobType:
    """job_type文字列を`WatchlistJobType`へ変換する唯一の関数。

    `raw`がNone(キー自体が存在しない)の場合のみ`default`を返す
    (EventBridge Scheduleのevent未指定時など、既定値が許される場面専用)。
    `default`未指定でNoneが来た場合、またはNone以外の既知でない値が来た
    場合は`UnknownWatchlistJobTypeError`を送出する。呼び出し側で
    「elseならmaintenance」「elseならnew candidate」のような暗黙fallbackを
    実装しないための唯一の正規入口とする。
    """
    if raw is None:
        if default is not None:
            return default
        raise UnknownWatchlistJobTypeError(
            "watchlist job_type is missing and no default is allowed here"
        )
    try:
        return WatchlistJobType(raw)
    except ValueError as exc:
        raise UnknownWatchlistJobTypeError(f"unknown watchlist job_type: {raw!r}") from exc

EXECUTION_RESULT_NORMAL = "NORMAL"
EXECUTION_RESULT_HIGH_THROTTLE_RATE = "HIGH_THROTTLE_RATE"
# 運用ハードニング3節: 429疑い率以外に、主要スコア項目の欠損率が閾値を超えた
# 場合のABORTED理由。statusはHIGH_THROTTLE_RATEと同じくABORTEDへ揃える。
# 運用ハードニング第2弾5節でSCORING_DATA_QUALITY_DEGRADEDへ改名
# (REQUIRED_DATA_QUALITY_DEGRADEDと対にするため、対象がスコア項目であることを明示)。
EXECUTION_RESULT_SCORING_DATA_QUALITY_DEGRADED = "SCORING_DATA_QUALITY_DEGRADED"
# --- 運用ハードニング第2弾5節: 未知の障害パターンでも安全に中止できる独立の安全弁 ---
EXECUTION_RESULT_EXCESSIVE_DATA_ERRORS = "EXCESSIVE_DATA_ERRORS"
EXECUTION_RESULT_EXCESSIVE_NOT_FOUND = "EXCESSIVE_NOT_FOUND"
EXECUTION_RESULT_EXCESSIVE_TERMINAL_FAILURES = "EXCESSIVE_TERMINAL_FAILURES"
EXECUTION_RESULT_REQUIRED_DATA_QUALITY_DEGRADED = "REQUIRED_DATA_QUALITY_DEGRADED"
_ABORTED_EXECUTION_RESULTS = frozenset(
    {
        EXECUTION_RESULT_HIGH_THROTTLE_RATE,
        EXECUTION_RESULT_SCORING_DATA_QUALITY_DEGRADED,
        EXECUTION_RESULT_EXCESSIVE_DATA_ERRORS,
        EXECUTION_RESULT_EXCESSIVE_NOT_FOUND,
        EXECUTION_RESULT_EXCESSIVE_TERMINAL_FAILURES,
        EXECUTION_RESULT_REQUIRED_DATA_QUALITY_DEGRADED,
    }
)

# Reconcilerのタイムアウト確定処理(17節)専用のevaluation_result。
EVALUATION_RESULT_BATCH_TIMED_OUT = "BATCH_TIMED_OUT"
# Dispatcherが3回再送してもSendMessageBatchが成功しなかった銘柄(1節)。
EVALUATION_RESULT_DISPATCH_SEND_FAILED = "DISPATCH_SEND_FAILED"
# SQSのmaxReceiveCountを使い果たしTerminalFailureQueueへ回った銘柄(4節)。
EVALUATION_RESULT_SQS_MAX_RECEIVE_EXCEEDED = "SQS_MAX_RECEIVE_EXCEEDED"

# TransactionConflictExceptionは、本番運用中に実際に観測された(ウォッチリスト
# 自動追加パイプラインの並行Worker実行下で、complete_candidateのTransactWriteItemsと
# try_finalize_if_ready等の単純UpdateItemが同一BatchRunsTable項目へほぼ同時に
# アクセスした際に発生)。ConditionalCheckFailedExceptionと同様「他の呼び出しが
# 同じ項目を処理中/処理済み」という想定内の競合を意味し、このモジュール全体の
# 排他制御関数(以下のConditionExpression付きUpdateItem/TransactWriteItems)が
# 一律Falseを返し呼び出し側の冪等な再試行・Reconciler確認に委ねる対象に含める。
_TRANSACTION_CONDITION_FAILURE_CODES = (
    "TransactionCanceledException",
    "ConditionalCheckFailedException",
    "TransactionConflictException",
)

# 障害対応(2026-08-15、本番incident対応): 上記コメントの前提
# 「呼び出し側の冪等な再試行に委ねる」が、complete_candidate()の実際の呼び出し元
# (watchlist_worker_handler.py)では成立していなかった。TransactWriteItemsが
# WatchlistCandidateProgressTable項目(所有権チェック付き、正当)とBatchRunsTable
# 項目(`ADD completed :one`、条件なし・全Workerが同一項目へ同時書き込み)の
# 2項目を1トランザクションにまとめているため、複数Workerがほぼ同時に完了報告した
# だけでBatchRunsTable項目側がTransactionConflictExceptionを起こし、
# トランザクション全体(進捗行の正当な条件付き更新も含む)がロールバックされる
# ことが確認された(本番286件中1件で発生、進捗行がPROCESSINGのまま完了せず
# 手動復旧が必要だった)。TransactionConflictExceptionは「別のWorkerが既に
# この銘柄を完了させた」という意味ではなく、無関係なトランザクション同士が
# たまたま同じ共有カウンタ項目に触れた一時的な競合であり、リトライすれば
# 高確率で成功する。ConditionalCheckFailedException(本物の所有権喪失、
# リトライすべきでない)とは区別し、_transact_write_items_with_conflict_retry()
# 経由でのみ短いバックオフ付きリトライを行う。
_RETRYABLE_TRANSACTION_CONFLICT_CODES = frozenset({"TransactionConflictException"})
_MAX_TRANSACTION_CONFLICT_RETRY_ATTEMPTS = 4
_TRANSACTION_CONFLICT_RETRY_BASE_DELAY_SECONDS = 0.05


def _is_retryable_transaction_conflict(error: ClientError) -> bool:
    """TransactionConflictExceptionによる失敗かどうかを判定する。

    TransactWriteItemsは以下2種類の例外を投げうる:
    - 単独の`TransactionConflictException`: 同一項目への別トランザクションの
      同時実行によるもの。本物の条件不成立ではないため、リトライ対象。
    - `TransactionCanceledException`: 各TransactItemごとの`CancellationReasons`
      (Code)を持つ。`ConditionalCheckFailed`が1件でも含まれる場合は本物の
      条件不成立(所有権喪失等)のためリトライしない。`TransactionConflict`
      のみが理由の場合はリトライ対象とする。
    """
    code = error.response["Error"]["Code"]
    if code in _RETRYABLE_TRANSACTION_CONFLICT_CODES:
        return True
    if code != "TransactionCanceledException":
        return False
    reasons = error.response.get("CancellationReasons") or []
    reason_codes = {reason.get("Code") for reason in reasons}
    if "ConditionalCheckFailed" in reason_codes:
        return False
    return "TransactionConflict" in reason_codes


def _transact_write_items_with_conflict_retry(
    client: Any, transact_items: list[dict[str, Any]]
) -> None:
    """TransactWriteItemsを実行し、一時的なTransactionConflictExceptionのみ
    短いバックオフ(指数バックオフ+ジッター)でリトライする。

    本物の条件不成立(ConditionalCheckFailedException、または
    CancellationReasonsにConditionalCheckFailedを含むTransactionCanceledException)
    はリトライせず、呼び出し元(このモジュールの各関数)がこれまでどおり
    _TRANSACTION_CONDITION_FAILURE_CODES経由でFalseを返す経路へそのまま伝播させる。
    """
    delay = _TRANSACTION_CONFLICT_RETRY_BASE_DELAY_SECONDS
    for attempt in range(1, _MAX_TRANSACTION_CONFLICT_RETRY_ATTEMPTS + 1):
        try:
            client.transact_write_items(TransactItems=transact_items)
            return
        except ClientError as e:
            is_last_attempt = attempt >= _MAX_TRANSACTION_CONFLICT_RETRY_ATTEMPTS
            if is_last_attempt or not _is_retryable_transaction_conflict(e):
                raise
            time.sleep(delay + random.uniform(0, delay))
            delay *= 2


@dataclass(frozen=True)
class CandidateProgressRecord:
    batch_id: str
    stock_code: str
    status: str
    dispatched: bool
    evaluation_result: str | None
    ranking_entry: str | None
    lease_owner_id: str | None
    attempt_count: int
    total_processing_duration_ms: int
    # 運用ハードニング3節: 429だけでなく403/5xx/タイムアウト/接続切断/yfinance
    # 固有例外等を広く「データ提供元障害の疑い」として扱う(旧is_rate_limit_suspected
    # を実態に合わせて改名。本番未デプロイのため後方互換は不要)。
    is_provider_failure_suspected: bool
    # 欠損したスコア項目名(最大7件程度の短い文字列)。主要項目ごとの取得率集計に使う。
    missing_field_names: list[str]
    # --- LINE通知品質改善(2026-08)で追加 ---------------------------------------
    # evaluate()が実行された全銘柄(PASSED/FAILED_SCORE/FAILED_REQUIRED等)で
    # セットされる(NOT_FOUND/DATA_ERRORの場合のみNone)。表示順位(RankingCalculator)
    # の算出母数として使う。RankingEntry.total_score(passed銘柄のみ)とは別物。
    total_score: float | None
    # passed銘柄のみセットされる、通知再構築用のスコア詳細(モデル型のまま保持、
    # JSON化はこのファイル内部にのみ存在する)。
    notification_detail: WatchlistScoreDetail | None
    # --- ウォッチリスト自動運用の改善(2026-08)で追加 ---------------------------
    # JOB_TYPE_WATCHLIST_MAINTENANCEの場合のみセットされる(ranking_entryの
    # メンテナンス版、計画Part C-7案A)。
    screening_summary_json: str | None = None
    # フェーズ別計測(計画Part B-1)。いずれもevaluate()が実行できた銘柄のみ
    # セットされる(NOT_FOUND/DATA_ERRORの場合はdata_fetch_duration_msのみ
    # セットされうる)。
    data_fetch_duration_ms: int | None = None
    scoring_duration_ms: int | None = None


def _parse_notification_detail(raw: str | None) -> WatchlistScoreDetail | None:
    if raw is None:
        return None
    try:
        return WatchlistScoreDetail.model_validate_json(raw)
    except Exception:
        logger.exception("watchlist notification_detail parse failed, treating as absent")
        return None


def _to_progress_record(item: dict[str, Any]) -> CandidateProgressRecord:
    total_score_raw = item.get("total_score")
    return CandidateProgressRecord(
        batch_id=item["batch_id"],
        stock_code=item["stock_code"],
        status=item["status"],
        dispatched=bool(item.get("dispatched", False)),
        evaluation_result=item.get("evaluation_result"),
        ranking_entry=item.get("ranking_entry"),
        lease_owner_id=item.get("lease_owner_id"),
        attempt_count=int(item.get("attempt_count", 0)),
        total_processing_duration_ms=int(item.get("total_processing_duration_ms", 0)),
        is_provider_failure_suspected=bool(item.get("is_provider_failure_suspected", False)),
        missing_field_names=list(item.get("missing_field_names", [])),
        total_score=(float(total_score_raw) if total_score_raw is not None else None),
        notification_detail=_parse_notification_detail(item.get("notification_detail")),
        screening_summary_json=item.get("screening_summary_json"),
        data_fetch_duration_ms=(
            int(item["data_fetch_duration_ms"])
            if item.get("data_fetch_duration_ms") is not None
            else None
        ),
        scoring_duration_ms=(
            int(item["scoring_duration_ms"])
            if item.get("scoring_duration_ms") is not None
            else None
        ),
    )


def query_all_candidate_progress(
    batch_id: str, *, consistent_read: bool = False
) -> list[CandidateProgressRecord]:
    """batch_id配下の全進捗行を、LastEvaluatedKeyが無くなるまでQueryして返す(16節)。

    WatchlistCandidateProgressTableは約3,122行を保持し1回のQuery応答サイズ上限
    (1MB)を超えうるため、全ページ取得を必須とする(13/11/17/15節の各用途で使う)。
    consistent_read=Trueは、自分自身が直前に書き込んだ内容を確実に読み取る必要が
    ある箇所(進捗行作成直後の件数照合、finalize直前の結果取得、17節のタイムアウト
    確定処理内の未完了行抽出・再確認)でのみ指定すること。
    """
    table = _progress_table()
    records: list[CandidateProgressRecord] = []
    query_kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("batch_id").eq(batch_id),
        "ConsistentRead": consistent_read,
    }
    while True:
        response = table.query(**query_kwargs)
        records.extend(_to_progress_record(item) for item in response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key
    return records


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _batch_write_with_retry(
    table_name: str,
    requests: Any,
    *,
    base_delay_seconds: float = 0.5,
    max_delay_seconds: float = 5.0,
    max_retries: int = 5,
) -> None:
    """13節: BatchWriteItemのUnprocessedItemsを指数バックオフ+ジッターで再送する。

    最大再試行回数を超えても残る場合はRuntimeErrorを送出し、呼び出し側
    (Dispatcher)がSQS送信を開始せずDISPATCH_FAILEDへ遷移できるようにする。
    requestsは低レベルDynamoDB API形式のPutRequest辞書列(TypeSerializerで
    シリアライズ済み)であり、boto3-stubsのTypedDict群と厳密に型付けせず
    Anyのまま扱う(この関数はDynamoDB低レベルAPIへの薄いラッパーのため)。
    """
    client = boto3.client("dynamodb")
    pending = requests
    attempt = 0
    while pending:
        response = client.batch_write_item(RequestItems={table_name: pending})
        pending = response.get("UnprocessedItems", {}).get(table_name, [])
        if not pending:
            return
        attempt += 1
        if attempt > max_retries:
            raise RuntimeError(
                f"BatchWriteItem: UnprocessedItemsが{max_retries}回の再送後も"
                f"残っています table={table_name} remaining={len(pending)}"
            )
        delay = min(max_delay_seconds, base_delay_seconds * (2 ** (attempt - 1)))
        delay *= 1 + random.uniform(-0.2, 0.2)
        time.sleep(max(0.0, delay))


def create_missing_candidate_progress_rows(
    batch_id: str, stock_codes: list[str], now: dt.datetime, ttl_hours: int
) -> None:
    """13/18節: 既存進捗行との差分(未作成分)のみをPENDING行として作成する。

    差分計算を先に行うことで、BatchWriteItemが項目単位のConditionExpressionを
    指定できない制約があっても、既存の(PROCESSING/COMPLETED/FAILEDへ進んでいる
    可能性がある)行を無条件のPutRequestで上書きしない(18節「対策2」、dispatch
    leaseによる多重実行排除(対策1)とは独立の安全策として両方実装する)。
    """
    existing = {r.stock_code for r in query_all_candidate_progress(batch_id, consistent_read=True)}
    missing = [code for code in stock_codes if code not in existing]
    if not missing:
        return

    ttl = int((now + dt.timedelta(hours=ttl_hours)).timestamp())
    table_name = _progress_table_name()
    for chunk in _chunked(missing, 25):
        requests = [
            {
                "PutRequest": {
                    "Item": {
                        "batch_id": _ser(batch_id),
                        "stock_code": _ser(code),
                        "status": _ser(WatchlistProgressStatus.PENDING.value),
                        "dispatched": _ser(False),
                        "attempt_count": _ser(0),
                        "total_processing_duration_ms": _ser(0),
                        "is_provider_failure_suspected": _ser(False),
                        "missing_field_names": _ser([]),
                        "ttl": _ser(ttl),
                    }
                }
            }
            for code in chunk
        ]
        _batch_write_with_retry(table_name, requests)


def try_acquire_dispatch_lease(
    batch_id: str, owner_id: str, now: dt.datetime, lease_seconds: int, ttl_hours: int
) -> bool:
    """1節ステップ0/18節「対策1」: 同一batch_idのDispatcher多重実行を排除する。

    項目が未作成の場合はConditionExpressionが自明に真となりUpdateItemがそのまま
    新規作成する(DISPATCHINGへの初回遷移も兼ねる)。started_atは初回のみ設定し
    (if_not_exists)、リース再取得(再開)時にタイムアウト判定の起点がリセット
    されないようにする。ttlはここでは仮の初期値としてのみ設定し(if_not_exists)、
    set_watchlist_batch_total()が正式なcandidate_progress_ttl_hours基準の値で
    上書きする。
    """
    lease_expires_at = (now + dt.timedelta(seconds=lease_seconds)).isoformat()
    now_iso = now.isoformat()
    fallback_ttl = int((now + dt.timedelta(hours=ttl_hours)).timestamp())
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression=(
                "SET dispatch_owner_id = :owner, "
                "dispatch_lease_acquired_at = :now, "
                "dispatch_lease_expires_at = :expires, "
                "dispatch_attempt_count = if_not_exists(dispatch_attempt_count, :zero) + :one, "
                "started_at = if_not_exists(started_at, :now), "
                "#ttl = if_not_exists(#ttl, :fallback_ttl), "
                "#status = :dispatching"
            ),
            ConditionExpression=(
                "(attribute_not_exists(dispatch_owner_id) OR dispatch_lease_expires_at < :now) "
                "AND (attribute_not_exists(#status) OR #status = :dispatching)"
            ),
            ExpressionAttributeNames={"#status": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":owner": owner_id,
                ":now": now_iso,
                ":expires": lease_expires_at,
                ":zero": 0,
                ":one": 1,
                ":fallback_ttl": fallback_ttl,
                ":dispatching": WatchlistBatchStatus.DISPATCHING.value,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def set_watchlist_batch_total(
    batch_id: str,
    total: int,
    ttl_hours: int,
    now: dt.datetime,
    *,
    staged_rollout_candidate_limit: int | None = None,
    staged_rollout_market_segment_filter: list[str] | None = None,
    universe_count: int = 0,
    staged_rollout_excluded_count: int = 0,
    job_type: WatchlistJobType = WatchlistJobType.NEW_CANDIDATE_SCREENING,
    eligible_universe_count: int = 0,
    rotation_cycle: int | None = None,
    rotation_start_key: list[str] | None = None,
    rotation_end_key: list[str] | None = None,
    rotation_wrapped: bool = False,
    universe_signature: str | None = None,
    triggered_by_batch_id: str | None = None,
    trigger_type: str | None = None,
) -> None:
    """1節ステップ2: 候補リスト確定後にtotalを設定し、dispatch_completedを
    falseで初期化する(この時点ではまだSQS送信を開始していないため)。

    運用ハードニング1節: 実際に適用された段階導入設定(candidate_limit・
    market_segment_filter)・絞り込み前の候補総数・除外件数もあわせて記録する。
    finalize時点の監査ログ(watchlist_batch_finalizer._finalize_completed)が
    この値を参照する。

    ウォッチリスト自動運用の改善(ローテーション・自動メンテナンス、2026-08)で
    追加: `job_type`("NEW_CANDIDATE_SCREENING"/"WATCHLIST_MAINTENANCE")は
    Dispatcher/Workerの共通インフラを流用するための識別子(計画Part C-7案A)。
    rotation commitは`job_type == "NEW_CANDIDATE_SCREENING"`の場合のみ行う
    (計画Part A-9、`watchlist_batch_finalizer._finish_batch()`が参照する)。
    `rotation_start_key`/`rotation_end_key`はdispatch時点で選択したwindowの
    開始・終了キー(`[market_segment, stock_code]`、`RotationCursor`をlist化
    したもの)で、rotation commit時にそのまま`try_commit_rotation_advance()`
    へ渡す(計画Part A-5: 業務処理確定までの間はステージングのみに使い、
    rotation stateへは書き込まない)。

    平日毎日起動化(2026-08)対応: `triggered_by_batch_id`/`trigger_type`は
    WATCHLIST_MAINTENANCEがNEW_CANDIDATE_SCREENINGの後続処理として起動された
    場合の親batch_id・起動種別("POST_NEW_CANDIDATE_SCREENING")を記録する
    (parent-child関係の監査用)。EventBridge Scheduleからの直接起動(現状は
    NEW_CANDIDATE_SCREENINGのみ)ではいずれもNoneのまま。
    """
    ttl = int((now + dt.timedelta(hours=ttl_hours)).timestamp())
    _table().update_item(
        Key={"batch_id": batch_id},
        UpdateExpression=(
            "SET #total = :total, dispatch_completed = :false, "
            "completed = if_not_exists(completed, :zero), #ttl = :ttl, "
            "staged_rollout_candidate_limit = :candidate_limit, "
            "staged_rollout_market_segment_filter = :market_segment_filter, "
            "universe_count = :universe_count, "
            "staged_rollout_excluded_count = :staged_rollout_excluded_count, "
            "job_type = :job_type, "
            "eligible_universe_count = :eligible_universe_count, "
            "rotation_cycle = :rotation_cycle, "
            "rotation_start_key = :rotation_start_key, "
            "rotation_end_key = :rotation_end_key, "
            "rotation_wrapped = :rotation_wrapped, "
            "universe_signature = :universe_signature, "
            "triggered_by_batch_id = :triggered_by_batch_id, "
            "trigger_type = :trigger_type"
        ),
        ExpressionAttributeNames={"#total": "total", "#ttl": "ttl"},
        ExpressionAttributeValues={
            ":total": total,
            ":false": False,
            ":zero": 0,
            ":ttl": ttl,
            ":candidate_limit": staged_rollout_candidate_limit,
            ":market_segment_filter": staged_rollout_market_segment_filter,
            ":universe_count": universe_count,
            ":staged_rollout_excluded_count": staged_rollout_excluded_count,
            ":job_type": job_type.value,
            ":eligible_universe_count": eligible_universe_count,
            ":rotation_cycle": rotation_cycle,
            ":rotation_start_key": rotation_start_key,
            ":rotation_end_key": rotation_end_key,
            ":rotation_wrapped": rotation_wrapped,
            ":universe_signature": universe_signature,
            ":triggered_by_batch_id": triggered_by_batch_id,
            ":trigger_type": trigger_type,
        },
    )


def mark_dispatch_completed(batch_id: str, now: dt.datetime) -> None:
    """1節ステップ5: 全候補がdispatched=trueまたはFAILED確定のいずれかになった
    時点でDISPATCHING→RUNNINGへ遷移する。"""
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression=(
                "SET dispatch_completed = :true, #status = :running, updated_at = :now"
            ),
            ConditionExpression="#status = :dispatching",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":true": True,
                ":running": WatchlistBatchStatus.RUNNING.value,
                ":dispatching": WatchlistBatchStatus.DISPATCHING.value,
                ":now": now.isoformat(),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return  # 既にDISPATCH_FAILED等へ遷移済み(想定外だが致命的ではない)
        raise


def mark_dispatch_failed(batch_id: str, now: dt.datetime) -> bool:
    """2節ステップ4(Reconciler): DISPATCHINGのままタイムアウトしたバッチを終端確定する。"""
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="SET #status = :failed, updated_at = :now",
            ConditionExpression="#status = :dispatching",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":failed": WatchlistBatchStatus.DISPATCH_FAILED.value,
                ":dispatching": WatchlistBatchStatus.DISPATCHING.value,
                ":now": now.isoformat(),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def mark_candidate_dispatched(batch_id: str, stock_code: str, now: dt.datetime) -> None:
    """12節: SendMessageBatch成功後、statusに依存しない条件式でdispatchedを記録する。

    SQS送信はat-least-once配信であり、この更新前にDispatcherが停止すると次回
    再開時に同じ銘柄が再送される可能性があるが、これは設計上許容し、銘柄評価側の
    冪等性(7節のリース機構)によって重複を吸収する。
    """
    try:
        _progress_table().update_item(
            Key={"batch_id": batch_id, "stock_code": stock_code},
            UpdateExpression="SET dispatched = :true, dispatched_at = :now",
            ConditionExpression="attribute_not_exists(dispatched) OR dispatched = :false",
            ExpressionAttributeValues={":true": True, ":false": False, ":now": now.isoformat()},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return  # 既にdispatched=true(SQS再送・重複処理等)。冪等スキップ。
        raise


def claim_candidate_lease(
    batch_id: str, stock_code: str, owner_id: str, now: dt.datetime, lease_seconds: int
) -> bool:
    """7節: Workerが銘柄1件分のPROCESSINGリースを取得する。"""
    lease_expires_at = (now + dt.timedelta(seconds=lease_seconds)).isoformat()
    now_iso = now.isoformat()
    try:
        _progress_table().update_item(
            Key={"batch_id": batch_id, "stock_code": stock_code},
            UpdateExpression=(
                "SET #status = :processing, lease_owner_id = :owner, "
                "lease_acquired_at = :now, lease_expires_at = :expires, "
                "attempt_count = if_not_exists(attempt_count, :zero) + :one, "
                "last_attempt_started_at = :now, "
                "first_started_at = if_not_exists(first_started_at, :now)"
            ),
            ConditionExpression=(
                "attribute_not_exists(stock_code) OR #status = :pending OR "
                "(#status = :processing AND lease_expires_at < :now)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":processing": WatchlistProgressStatus.PROCESSING.value,
                ":pending": WatchlistProgressStatus.PENDING.value,
                ":owner": owner_id,
                ":now": now_iso,
                ":expires": lease_expires_at,
                ":zero": 0,
                ":one": 1,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def complete_candidate(
    batch_id: str,
    stock_code: str,
    owner_id: str,
    *,
    terminal_status: WatchlistProgressStatus,
    evaluation_result: str,
    ranking_entry: str | None,
    is_provider_failure_suspected: bool,
    missing_field_names: list[str],
    processing_duration_ms: int,
    now: dt.datetime,
    total_score: float | None = None,
    notification_detail: WatchlistScoreDetail | None = None,
    screening_summary_json: str | None = None,
    data_fetch_duration_ms: int | None = None,
    scoring_duration_ms: int | None = None,
) -> bool:
    """7/11節: Workerの通常完了経路。TransactWriteItemsで進捗行の終端確定と
    BatchRunsTable.completedの+1を原子的に行う(通常経路。17節のタイムアウト
    確定処理専用の再計算方式(案C)とは別のまま維持する、17節参照)。

    完了条件(owner一致)が不成立の場合はFalseを返す(リース失効後に別Workerが
    再クレームしていた、Reconcilerが先にタイムアウト確定していた等)。

    total_score/notification_detail(LINE通知品質改善、2026-08)は`ranking_entry`
    と同じ「Noneでなければconditionally SET」パターンでDynamoDBへ書く。
    notification_detailはモデル型のまま引数として受け取り、JSON化はこの関数の
    内部にのみ存在する(呼び出し側はJSON文字列を一切扱わない)。

    ウォッチリスト自動運用の改善(2026-08)で追加:
    - `screening_summary_json`はJOB_TYPE_WATCHLIST_MAINTENANCEの場合のみ使う
      (passed/matched_target_types/total_score/exclusion_reasonsをまとめた
      JSON、`ranking_entry`のメンテナンス版に相当。計画Part C-7案A)。
    - `data_fetch_duration_ms`/`scoring_duration_ms`はフェーズ別計測
      (計画Part B-1)。いずれも同じ「Noneでなければconditionally SET」
      パターンを踏襲する。
    """
    notification_detail_json = (
        notification_detail.model_dump_json() if notification_detail is not None else None
    )
    update_expression = (
        "SET #status = :status, evaluation_result = :eval_result, completed_at = :now, "
        "is_provider_failure_suspected = :provider_failure, "
        "missing_field_names = :missing_fields"
        + (", ranking_entry = :ranking_entry" if ranking_entry is not None else "")
        + (", total_score = :total_score" if total_score is not None else "")
        + (
            ", notification_detail = :notification_detail"
            if notification_detail_json is not None
            else ""
        )
        + (
            ", screening_summary_json = :screening_summary_json"
            if screening_summary_json is not None
            else ""
        )
        + (
            ", data_fetch_duration_ms = :data_fetch_duration_ms"
            if data_fetch_duration_ms is not None
            else ""
        )
        + (
            ", scoring_duration_ms = :scoring_duration_ms"
            if scoring_duration_ms is not None
            else ""
        )
        + " ADD total_processing_duration_ms :duration_ms"
        + " REMOVE lease_owner_id, lease_expires_at"
    )
    values: dict[str, Any] = {
        ":status": terminal_status.value,
        ":eval_result": evaluation_result,
        ":now": now.isoformat(),
        ":provider_failure": is_provider_failure_suspected,
        ":missing_fields": missing_field_names,
        ":duration_ms": processing_duration_ms,
        ":processing": WatchlistProgressStatus.PROCESSING.value,
        ":owner": owner_id,
    }
    if ranking_entry is not None:
        values[":ranking_entry"] = ranking_entry
    if total_score is not None:
        # DynamoDBはPython float型を直接扱えないため(boto3 TypeSerializerが
        # TypeErrorを送出する)、Decimalへ変換してから渡す(既存コードの
        # Decimal(str(value))パターンを踏襲)。
        values[":total_score"] = Decimal(str(total_score))
    if notification_detail_json is not None:
        values[":notification_detail"] = notification_detail_json
    if screening_summary_json is not None:
        values[":screening_summary_json"] = screening_summary_json
    if data_fetch_duration_ms is not None:
        values[":data_fetch_duration_ms"] = data_fetch_duration_ms
    if scoring_duration_ms is not None:
        values[":scoring_duration_ms"] = scoring_duration_ms

    client = boto3.client("dynamodb")
    try:
        _transact_write_items_with_conflict_retry(
            client,
            [
                {
                    "Update": {
                        "TableName": _progress_table_name(),
                        "Key": {"batch_id": _ser(batch_id), "stock_code": _ser(stock_code)},
                        "UpdateExpression": update_expression,
                        "ConditionExpression": "#status = :processing AND lease_owner_id = :owner",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {k: _ser(v) for k, v in values.items()},
                    }
                },
                {
                    "Update": {
                        "TableName": _batch_runs_table_name(),
                        "Key": {"batch_id": _ser(batch_id)},
                        "UpdateExpression": "ADD completed :one",
                        "ExpressionAttributeValues": {":one": _ser(1)},
                    }
                },
            ],
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def record_dispatch_send_failure(batch_id: str, stock_code: str, now: dt.datetime) -> bool:
    """1節ステップ4: SendMessageBatchが3回再送しても成功しなかった銘柄を直接
    FAILED確定する(SQSに乗らなかった銘柄がいつまでも未完了扱いにならないようにする)。
    """
    client = boto3.client("dynamodb")
    values = {
        ":status": WatchlistProgressStatus.FAILED.value,
        ":eval_result": EVALUATION_RESULT_DISPATCH_SEND_FAILED,
        ":now": now.isoformat(),
        ":pending": WatchlistProgressStatus.PENDING.value,
    }
    try:
        _transact_write_items_with_conflict_retry(
            client,
            [
                {
                    "Update": {
                        "TableName": _progress_table_name(),
                        "Key": {"batch_id": _ser(batch_id), "stock_code": _ser(stock_code)},
                        "UpdateExpression": (
                            "SET #status = :status, evaluation_result = :eval_result, "
                            "completed_at = :now"
                        ),
                        "ConditionExpression": "#status = :pending",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {k: _ser(v) for k, v in values.items()},
                    }
                },
                {
                    "Update": {
                        "TableName": _batch_runs_table_name(),
                        "Key": {"batch_id": _ser(batch_id)},
                        "UpdateExpression": "ADD completed :one",
                        "ExpressionAttributeValues": {":one": _ser(1)},
                    }
                },
            ],
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def record_terminal_failure(batch_id: str, stock_code: str, now: dt.datetime) -> bool:
    """4節: SQSのmaxReceiveCountを使い果たしTerminalFailureQueueへ回った銘柄を
    FAILED確定する(WatchlistTerminalFailureHandlerから呼ぶ)。"""
    client = boto3.client("dynamodb")
    values = {
        ":status": WatchlistProgressStatus.FAILED.value,
        ":eval_result": EVALUATION_RESULT_SQS_MAX_RECEIVE_EXCEEDED,
        ":now": now.isoformat(),
        ":pending": WatchlistProgressStatus.PENDING.value,
        ":processing": WatchlistProgressStatus.PROCESSING.value,
    }
    try:
        _transact_write_items_with_conflict_retry(
            client,
            [
                {
                    "Update": {
                        "TableName": _progress_table_name(),
                        "Key": {"batch_id": _ser(batch_id), "stock_code": _ser(stock_code)},
                        "UpdateExpression": (
                            "SET #status = :status, evaluation_result = :eval_result, "
                            "completed_at = :now REMOVE lease_owner_id, lease_expires_at"
                        ),
                        "ConditionExpression": "#status = :pending OR #status = :processing",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {k: _ser(v) for k, v in values.items()},
                    }
                },
                {
                    "Update": {
                        "TableName": _batch_runs_table_name(),
                        "Key": {"batch_id": _ser(batch_id)},
                        "UpdateExpression": "ADD completed :one",
                        "ExpressionAttributeValues": {":one": _ser(1)},
                    }
                },
            ],
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def try_finalize_if_ready(batch_id: str, now: dt.datetime) -> bool:
    """11節: dispatch_completed AND completed>=total AND status=RUNNINGの場合のみ、
    RUNNING→FINALIZE_PREPARINGへの排他遷移を試みる。Worker/Dispatcher/Terminal
    Failure Handler/Reconcilerのすべてから、それぞれが担当する完了確定の直後に
    呼ぶこと。条件付き更新に成功した1回の呼び出しだけがTrueを返し、呼び出し側は
    その場合のみ後続のfinalize処理(合格銘柄のウォッチリスト追加・LINE通知等)を
    実行する(このモジュールはDynamoDB上の排他制御のみを担う)。

    TIMED_OUTはこの関数を使わない(17節: 判定条件・処理内容が異なる別経路)。
    finalizing_started_atは呼び出し側から渡された`now`をそのまま使う(運用
    ハードニング5節: mark_finalizing_stuck_as_failedのスタック検知が同じ時刻軸で
    比較できるようにするため、この関数の内部でdt.datetime.now()を独自に取得しない)。
    """
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="SET #status = :preparing, finalizing_started_at = :now",
            ConditionExpression=(
                "#status = :running AND dispatch_completed = :true AND completed >= #total"
            ),
            ExpressionAttributeNames={"#status": "status", "#total": "total"},
            ExpressionAttributeValues={
                ":preparing": WatchlistBatchStatus.FINALIZE_PREPARING.value,
                ":running": WatchlistBatchStatus.RUNNING.value,
                ":true": True,
                ":now": now.isoformat(),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def try_retry_finalize(batch_id: str) -> bool:
    """運用ハードニング5節: FINALIZE_FAILED→FINALIZE_PREPARINGへの再試行遷移。

    運用ハードニング第2弾2節: 実際にどの段階(FINALIZE_PREPARING/
    WATCHLIST_WRITE_COMPLETED/NOTIFICATION_PENDING/NOTIFICATION_SENT)で失敗して
    いたかに関わらず、statusは一律FINALIZE_PREPARINGへ戻す。既に完了している
    段階はwatchlist_batch_finalizer.py側がBatchRunsTable項目のフィールドの
    有無から判定して読み飛ばし、同じ呼び出し内で残りの段階まで進める
    (このDynamoDB関数自体は排他制御のみを担い、どこから再開するかの判断は
    persistedフィールドを見るfinalizer側の責務とする)。

    finalizing_started_atをあわせて更新し(スタック検知の起点をリセットする)、
    Reconciler(試行回数上限あり)・CLI(`retry-finalize --execute`、上限を無視して
    1回試みる)の両方から呼ぶ。
    """
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="SET #status = :preparing, finalizing_started_at = :now",
            ConditionExpression="#status = :failed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":preparing": WatchlistBatchStatus.FINALIZE_PREPARING.value,
                ":failed": WatchlistBatchStatus.FINALIZE_FAILED.value,
                ":now": dt.datetime.now(dt.UTC).isoformat(),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


_FINALIZE_IN_PROGRESS_STATUSES = (
    WatchlistBatchStatus.FINALIZE_PREPARING,
    WatchlistBatchStatus.WATCHLIST_WRITE_COMPLETED,
    WatchlistBatchStatus.NOTIFICATION_PENDING,
    WatchlistBatchStatus.NOTIFICATION_SENT,
)


def mark_finalizing_stuck_as_failed(
    batch_id: str, now: dt.datetime, stuck_threshold_minutes: int
) -> bool:
    """運用ハードニング5節・第2弾2節: finalizing_started_atから閾値分を超えて
    finalize処理中の状態(FINALIZE_PREPARING/WATCHLIST_WRITE_COMPLETED/
    NOTIFICATION_PENDING/NOTIFICATION_SENTのいずれか)のままのバッチを、
    Reconcilerが異常とみなしFINALIZE_FAILEDへ強制遷移する(finalizeは少数銘柄の
    みを処理するため短時間で完了するはずという前提。これにより
    `try_retry_finalize`による自動復旧の対象になる)。
    """
    threshold_iso = (now - dt.timedelta(minutes=stuck_threshold_minutes)).isoformat()
    status_values = {f":s{i}": s.value for i, s in enumerate(_FINALIZE_IN_PROGRESS_STATUSES)}
    status_condition = " OR ".join(f"#status = {placeholder}" for placeholder in status_values)
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression=(
                "SET #status = :failed, finalize_failed_at = :now, "
                "finalize_error_message = :reason, updated_at = :now"
            ),
            ConditionExpression=f"({status_condition}) AND finalizing_started_at < :threshold",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":failed": WatchlistBatchStatus.FINALIZE_FAILED.value,
                **status_values,
                ":threshold": threshold_iso,
                ":now": now.isoformat(),
                ":reason": (
                    f"finalizing stuck past {stuck_threshold_minutes} minutes "
                    "(Lambda likely terminated mid-finalize)"
                ),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def record_finalize_target(
    batch_id: str, now: dt.datetime, target_stock_codes: list[str], ranking_json: str
) -> bool:
    """運用ハードニング第2弾2節: FINALIZE_PREPARING段階で、対象銘柄コード一覧
    (合格銘柄をランキングし追加件数上限を適用した後の対象、ウォッチリスト書き込み
    対象そのもの)とランキング情報を永続化する(まだ書き込み前)。呼び出し側
    (watchlist_batch_finalizer.py)は、既にこのフィールドが存在する場合は
    再計算せずそのまま再利用し、この関数を呼ばない(再試行のたびに異なる
    ランキングが計算されることを防ぐ)。

    同時にrepository_resultsを空のmapとして初期化する(record_repository_result_item
    が銘柄コード単位でこのmapへ追記していく、後述)。
    """
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression=(
                "SET finalize_target_stock_codes = :codes, "
                "finalize_ranking_json = :ranking, repository_results = :empty_results, "
                "updated_at = :now"
            ),
            ConditionExpression="#status = :preparing",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":codes": target_stock_codes,
                ":ranking": ranking_json,
                ":empty_results": {},
                ":now": now.isoformat(),
                ":preparing": WatchlistBatchStatus.FINALIZE_PREPARING.value,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def record_repository_result_item(
    batch_id: str, now: dt.datetime, stock_code: str, result: str
) -> None:
    """運用ハードニング第2弾2節: WatchlistRepository.add_if_new()の結果が確定した
    銘柄1件ごとに、repository_results(銘柄コード→REPOSITORY_RESULT_*文字列)へ
    即座に追記する(finalize_target_stock_codesのうち何件が処理済みかを銘柄単位で
    永続化することで、この関数を含むループの途中でLambdaが異常終了しても、次回は
    未処理の銘柄のみを再処理できるようにする)。ベストエフォート(この更新自体が
    失敗しても、次回`add_if_new`が再度呼ばれるだけで実害はない、
    WatchlistRepository自体の冪等性により重複追加は起きないため)。
    """
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="SET repository_results.#code = :result, updated_at = :now",
            ExpressionAttributeNames={"#code": stock_code},
            ExpressionAttributeValues={":result": result, ":now": now.isoformat()},
        )
    except ClientError:
        logger.exception(
            "watchlist finalize: record_repository_result_item failed batch_id=%s stock_code=%s",
            batch_id,
            stock_code,
        )


def mark_watchlist_write_completed(batch_id: str, now: dt.datetime) -> bool:
    """運用ハードニング第2弾2節: FINALIZE_PREPARING→WATCHLIST_WRITE_COMPLETED。
    finalize_target_stock_codesの全件についてrepository_resultsへの記録
    (record_repository_result_item)が完了した後に呼ぶ、純粋な状態遷移
    (データは既に銘柄単位で永続化済みのため、ここでは運びません)。再開時に
    (前回の実行で既に全件処理済みだった場合)ここへ到達しても、その時点で
    既にWATCHLIST_WRITE_COMPLETED以降へ進んでいればConditionExpression不成立で
    Falseを返すのみで、呼び出し側はこの戻り値を再開ロジックの分岐には使わない
    (再開の判定はrepository_resultsのフィールドの有無で行う)。
    """
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="SET #status = :write_completed, updated_at = :now",
            ConditionExpression="#status = :preparing",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":write_completed": WatchlistBatchStatus.WATCHLIST_WRITE_COMPLETED.value,
                ":now": now.isoformat(),
                ":preparing": WatchlistBatchStatus.FINALIZE_PREPARING.value,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def record_notification_pending(batch_id: str, now: dt.datetime, content_hash: str) -> bool:
    """運用ハードニング第2弾2節: WATCHLIST_WRITE_COMPLETED→NOTIFICATION_PENDING。
    LINE送信を試みる直前に呼ぶ(content_hashは実際に送信する内容から算出した値、
    line_notification_service.compute_watchlist_addition_content_hashで算出した
    ものと同じ値を渡すこと)。
    """
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression=(
                "SET #status = :pending, finalize_notification_content_hash = :hash, "
                "updated_at = :now"
            ),
            ConditionExpression="#status = :write_completed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":pending": WatchlistBatchStatus.NOTIFICATION_PENDING.value,
                ":hash": content_hash,
                ":now": now.isoformat(),
                ":write_completed": WatchlistBatchStatus.WATCHLIST_WRITE_COMPLETED.value,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


NOTIFICATION_OUTCOME_SENT = "SENT"
NOTIFICATION_OUTCOME_SKIPPED = "SKIPPED"
NOTIFICATION_OUTCOME_NOT_REQUIRED = "NOT_REQUIRED"
NOTIFICATION_OUTCOME_FAILED = "FAILED"


def record_notification_resolved(
    batch_id: str, now: dt.datetime, notified_stock_codes: list[str], outcome: str
) -> bool:
    """運用ハードニング第2弾2節・第3弾1節: NOTIFICATION_PENDING/
    WATCHLIST_WRITE_COMPLETED→NOTIFICATION_SENT。通知フェーズが例外を送出せずに
    解決した場合にのみ呼ぶ(送信成功・送信対象0件・notification_enabled=false・
    重複抑止による送信スキップのいずれも含む)。outcome
    (NOTIFICATION_OUTCOME_*)をfinalize_notification_outcomeへ永続化し、
    後から「実際に送信したのか、そもそも対象が無かったのか」を区別できるように
    する。通知対象が無かった場合(追加0件、またはnotification_enabled=false)は
    NOTIFICATION_PENDINGを経由せずWATCHLIST_WRITE_COMPLETEDから直接ここへ
    遷移してよい(その場合notified_stock_codes=[])。
    """
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression=(
                "SET #status = :sent, finalize_notified_stock_codes = :codes, "
                "finalize_notification_outcome = :outcome, updated_at = :now"
            ),
            ConditionExpression="#status = :pending OR #status = :write_completed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":sent": WatchlistBatchStatus.NOTIFICATION_SENT.value,
                ":codes": notified_stock_codes,
                ":outcome": outcome,
                ":now": now.isoformat(),
                ":pending": WatchlistBatchStatus.NOTIFICATION_PENDING.value,
                ":write_completed": WatchlistBatchStatus.WATCHLIST_WRITE_COMPLETED.value,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


_MAX_NOTIFICATION_ERROR_MESSAGE_LENGTH = 500


def record_notification_failed(batch_id: str, now: dt.datetime, error_message: str) -> int:
    """運用ハードニング第3弾1節: NOTIFICATION_PENDING→NOTIFICATION_FAILED。
    notify_watchlist_additions()が例外を送出した場合に呼ぶ。finalize全体は
    FINALIZE_FAILEDにしない(呼び出し側であるwatchlist_batch_finalizer.pyが
    この例外を外へ伝播させない設計のため)。notification_failure_countを+1し、
    更新後の値を返す(呼び出し側がmax_notification_retry_attempts以上かを
    判定するために使う)。
    """
    truncated = error_message[:_MAX_NOTIFICATION_ERROR_MESSAGE_LENGTH]
    response = _table().update_item(
        Key={"batch_id": batch_id},
        UpdateExpression=(
            "SET #status = :failed, last_notification_error = :error, updated_at = :now "
            "ADD notification_failure_count :one"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":failed": WatchlistBatchStatus.NOTIFICATION_FAILED.value,
            ":error": truncated,
            ":now": now.isoformat(),
            ":one": 1,
        },
        ReturnValues="UPDATED_NEW",
    )
    return int(response["Attributes"]["notification_failure_count"])


def try_retry_notification(batch_id: str, now: dt.datetime) -> bool:
    """運用ハードニング第3弾1節: NOTIFICATION_FAILED→NOTIFICATION_PENDINGへの
    再試行遷移。Phase1(対象決定)・Phase2(ウォッチリスト書込み)は
    finalize_target_stock_codes/repository_resultsが既に存在するため
    watchlist_batch_finalizer.py側で自動的にスキップされ、通知のみが
    再試行される(ウォッチリスト書込みは再実行されない)。Reconciler
    (試行回数上限あり)・CLI(`retry-notification --execute`、上限を無視して
    1回試みる)の両方から呼ぶ。
    """
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="SET #status = :pending, updated_at = :now",
            ConditionExpression="#status = :failed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":pending": WatchlistBatchStatus.NOTIFICATION_PENDING.value,
                ":failed": WatchlistBatchStatus.NOTIFICATION_FAILED.value,
                ":now": now.isoformat(),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def mark_batch_audit_recorded(batch_id: str, now: dt.datetime) -> None:
    """運用ハードニング第2弾2節: record_batch_auditを呼ぶ直前にセットする
    (batch audit重複防止フラグ)。ベストエフォート(この更新自体が失敗しても
    致命的ではないため条件チェックは行わない)。
    """
    _table().update_item(
        Key={"batch_id": batch_id},
        UpdateExpression="SET finalize_batch_audit_recorded = :true, updated_at = :now",
        ExpressionAttributeValues={":true": True, ":now": now.isoformat()},
    )


def try_operator_abort(batch_id: str, reason: str, now: dt.datetime) -> bool:
    """運用ハードニング6節: 運用者がCLI(`abort --execute`)経由で、終端状態
    でないバッチを強制的にABORTEDへ遷移させる(手動介入用)。
    """
    terminal_statuses = {
        WatchlistBatchStatus.COMPLETED.value,
        WatchlistBatchStatus.DISPATCH_FAILED.value,
        WatchlistBatchStatus.TIMED_OUT.value,
        WatchlistBatchStatus.ABORTED.value,
    }
    now_iso = now.isoformat()
    truncated_reason = f"OPERATOR_ABORTED: {reason}"[:MAX_FINALIZE_ERROR_MESSAGE_LENGTH]
    values = {f":s{i}": status for i, status in enumerate(terminal_statuses)}
    condition = " AND ".join(f"#status <> {placeholder}" for placeholder in values)
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression=(
                "SET #status = :aborted, execution_result = :reason, updated_at = :now"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                **values,
                ":aborted": WatchlistBatchStatus.ABORTED.value,
                ":reason": truncated_reason,
                ":now": now_iso,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def resolve_watchlist_batch_completion_status(
    execution_result: str, notification_permanently_failed: bool
) -> WatchlistBatchStatus:
    """`mark_watchlist_batch_completed()`が実際に書き込むstatusを、書き込みより
    前の時点で呼び出し元へ明示的に返す(平日毎日起動化2026-08対応の再修正:
    `maybe_trigger_maintenance()`が、finalize前に取得した古い`batch_item`の
    stateではなく、この確定済みstatusを直接受け取って起動可否を判断するため)。

    ロジック自体は`mark_watchlist_batch_completed()`と完全に同一(単一の
    判定箇所へ集約し、二重実装によるドリフトを防ぐ)。
    """
    if notification_permanently_failed and execution_result == EXECUTION_RESULT_NORMAL:
        return WatchlistBatchStatus.COMPLETED_WITH_NOTIFICATION_FAILURE
    if execution_result == EXECUTION_RESULT_NORMAL:
        return WatchlistBatchStatus.COMPLETED
    return WatchlistBatchStatus.ABORTED


def mark_watchlist_batch_completed(
    batch_id: str,
    execution_result: str,
    now: dt.datetime,
    notification_permanently_failed: bool = False,
) -> None:
    """11節: _finalize_completed相当の後続処理が成功した後に呼ぶ。

    execution_resultはEXECUTION_RESULT_NORMAL(通常完了)、または
    _ABORTED_EXECUTION_RESULTSのいずれか(10/3節のスロットリング率・主要項目
    欠損率判定該当時)。いずれの場合もstatusはABORTEDへ揃え、理由は
    execution_resultで区別する(20節: statusとexecution_resultの責務分離)。

    運用ハードニング第2弾5節: 複数の安全弁に同時該当した場合、execution_resultは
    該当理由を"|"区切りで連結した複合文字列になりうる(個々の理由は必ず
    _ABORTED_EXECUTION_RESULTSのいずれかのため、EXECUTION_RESULT_NORMALと
    完全一致するかどうかでCOMPLETED/ABORTEDを判定する方が複合文字列に対して
    頑健)。

    運用ハードニング第3弾1節: notification_permanently_failed=Trueの場合
    (通知再試行が上限に達した場合のみ、execution_result=NORMALの時に限り呼ばれる
    想定)、statusをCOMPLETEDではなくCOMPLETED_WITH_NOTIFICATION_FAILUREにする。
    ウォッチリスト追加自体は正常完了しているため、ABORTEDとは区別する。
    """
    status = resolve_watchlist_batch_completion_status(
        execution_result, notification_permanently_failed
    )
    _table().update_item(
        Key={"batch_id": batch_id},
        UpdateExpression="SET #status = :status, execution_result = :result, updated_at = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": status.value,
            ":result": execution_result,
            ":now": now.isoformat(),
        },
    )


def mark_watchlist_finalize_failed(
    batch_id: str, now: dt.datetime, error_message: str | None
) -> None:
    """FINALIZING→FINALIZE_FAILEDへ遷移し、finalize_attempt_countを+1する
    (運用ハードニング5節: Reconcilerの自動再試行回数の上限判定に使う)。
    """
    now_iso = now.isoformat()
    truncated = (
        (error_message or "")[:MAX_FINALIZE_ERROR_MESSAGE_LENGTH] if error_message else None
    )
    _table().update_item(
        Key={"batch_id": batch_id},
        UpdateExpression=(
            "SET #status = :status, finalize_failed_at = :now, "
            "finalize_error_message = :error_message, updated_at = :now "
            "ADD finalize_attempt_count :one"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": WatchlistBatchStatus.FINALIZE_FAILED.value,
            ":now": now_iso,
            ":error_message": truncated,
            ":one": 1,
        },
    )


def get_watchlist_batch(batch_id: str) -> dict[str, Any] | None:
    response = _table().get_item(Key={"batch_id": batch_id})
    item: dict[str, Any] | None = response.get("Item")
    return item


# --- 平日毎日起動化(2026-08)対応: NEW_CANDIDATE_SCREENINGの業務finalize確定後、
# WATCHLIST_MAINTENANCEを後続起動するexactly-once相当のトリガー状態機械。
# try_acquire_dispatch_lease/try_acquire_rotation_dispatch_leaseと同じ
# 「lease期限切れなら再取得可」パターンを踏襲する(Lambda異常終了時の永久
# ブロック防止)。TRIGGERED確定後はConditionExpressionが恒久的に不成立となり
# 二度と再取得できない。 ---
MAINTENANCE_TRIGGER_STATUS_TRIGGERING = "TRIGGERING"
MAINTENANCE_TRIGGER_STATUS_TRIGGERED = "TRIGGERED"
# invoke()呼び出し自体(非同期、通常は数百ミリ秒)のみを保護すればよいため、
# dispatch_lease(360秒)等より短い値とする。
MAINTENANCE_TRIGGER_LEASE_SECONDS = 120


def try_acquire_maintenance_trigger(
    batch_id: str,
    maintenance_batch_id: str,
    owner_id: str,
    now: dt.datetime,
    lease_seconds: int = MAINTENANCE_TRIGGER_LEASE_SECONDS,
) -> bool:
    """WATCHLIST_MAINTENANCE後続起動の権利を1回だけ取得する(平日毎日起動化
    2026-08対応)。`maintenance_batch_id`は呼び出し元が決定論的に算出した値
    (親batch_idから導出、`f"watchlist-maint-{batch_id}"`)を渡すこと。取得
    成功時にこの値を即座に記録するため、invoke前でも「どのchild batch_idに
    なる予定か」を親側から追跡できる。

    invoke失敗時はmaintenance_trigger_status=TRIGGERINGのまま残るため、
    lease_seconds経過後はlist_stale_maintenance_triggers()経由でReconcilerが
    再取得・再試行できる(処理を消失させない)。invoke成功後はmark_maintenance_
    triggered()でTRIGGEREDへ確定し、以後は本関数が恒久的にFalseを返す
    (exactly-once)。
    """
    lease_expires_at = (now + dt.timedelta(seconds=lease_seconds)).isoformat()
    now_iso = now.isoformat()
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression=(
                "SET maintenance_trigger_status = :triggering, "
                "maintenance_batch_id = :maintenance_batch_id, "
                "maintenance_trigger_owner_id = :owner, "
                "maintenance_trigger_lease_expires_at = :expires, "
                "maintenance_trigger_attempt_count = "
                "if_not_exists(maintenance_trigger_attempt_count, :zero) + :one, "
                "updated_at = :now"
            ),
            ConditionExpression=(
                "attribute_not_exists(maintenance_trigger_status) OR "
                "(maintenance_trigger_status = :triggering AND "
                "maintenance_trigger_lease_expires_at < :now)"
            ),
            ExpressionAttributeValues={
                ":triggering": MAINTENANCE_TRIGGER_STATUS_TRIGGERING,
                ":maintenance_batch_id": maintenance_batch_id,
                ":owner": owner_id,
                ":expires": lease_expires_at,
                ":zero": 0,
                ":one": 1,
                ":now": now_iso,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def mark_maintenance_triggered(batch_id: str, now: dt.datetime) -> None:
    """invoke成功後にtrigger状態をTRIGGERED(確定・恒久)へ遷移する。"""
    _table().update_item(
        Key={"batch_id": batch_id},
        UpdateExpression=(
            "SET maintenance_trigger_status = :triggered, "
            "maintenance_triggered_at = :now, updated_at = :now"
        ),
        ExpressionAttributeValues={
            ":triggered": MAINTENANCE_TRIGGER_STATUS_TRIGGERED,
            ":now": now.isoformat(),
        },
    )


def list_stale_maintenance_triggers(now: dt.datetime) -> list[dict[str, Any]]:
    """毎時Reconciler向け: invoke失敗等でTRIGGERINGのままlease期限切れになった
    親バッチ一覧(平日毎日起動化2026-08対応)。バッチは1日1件程度のためフル
    スキャンで十分(list_watchlist_batches_by_statusと同じ方針)。
    """
    table = _table()
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": (
            "maintenance_trigger_status = :triggering AND "
            "maintenance_trigger_lease_expires_at < :now"
        ),
        "ExpressionAttributeValues": {
            ":triggering": MAINTENANCE_TRIGGER_STATUS_TRIGGERING,
            ":now": now.isoformat(),
        },
    }
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return items


def list_watchlist_batches_by_status(statuses: list[WatchlistBatchStatus]) -> list[dict[str, Any]]:
    """Reconciler(毎時起動)向け。バッチは週1件程度のためフルスキャンで十分(2節)。"""
    table = _table()
    values = {f":s{i}": status.value for i, status in enumerate(statuses)}
    filter_expr = " OR ".join(f"#status = {placeholder}" for placeholder in values)
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": filter_expr,
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": values,
    }
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return items


def try_acquire_timeout_finalization(batch_id: str) -> bool:
    """17節: RUNNING(タイムアウト新規検出時)、またはTIMEOUT_FINALIZE_FAILED(再試行)
    からTIMEOUT_FINALIZINGへの排他遷移を試みる。既にTIMEOUT_FINALIZINGのバッチを
    そのまま続行する場合、この関数を呼ぶ必要はない(状態遷移が不要なため)。
    """
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="SET #status = :timeout_finalizing",
            ConditionExpression="#status = :running OR #status = :timeout_finalize_failed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":timeout_finalizing": WatchlistBatchStatus.TIMEOUT_FINALIZING.value,
                ":running": WatchlistBatchStatus.RUNNING.value,
                ":timeout_finalize_failed": WatchlistBatchStatus.TIMEOUT_FINALIZE_FAILED.value,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def _try_mark_row_timed_out(batch_id: str, stock_code: str, now: dt.datetime) -> bool:
    """17節ステップ3/4: 個別の条件付きUpdateItem(BatchWriteItemは使わない。
    既存項目の部分更新ができずattempt_count等を失うため)。条件不成立は他の主体が
    先に終端状態へ確定済みという意味であり、冪等スキップする。
    """
    try:
        _progress_table().update_item(
            Key={"batch_id": batch_id, "stock_code": stock_code},
            UpdateExpression=(
                "SET #status = :failed, evaluation_result = :reason, "
                "completed_at = :now REMOVE lease_owner_id, lease_expires_at"
            ),
            ConditionExpression="#status = :pending OR #status = :processing",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":failed": WatchlistProgressStatus.FAILED.value,
                ":reason": EVALUATION_RESULT_BATCH_TIMED_OUT,
                ":now": now.isoformat(),
                ":pending": WatchlistProgressStatus.PENDING.value,
                ":processing": WatchlistProgressStatus.PROCESSING.value,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


@dataclass(frozen=True)
class TimeoutFinalizationPassResult:
    all_records: list[CandidateProgressRecord]
    terminal_count: int
    total: int
    newly_failed_count: int


def run_timeout_finalization_pass(
    batch_id: str, now: dt.datetime, max_rows_per_run: int
) -> TimeoutFinalizationPassResult:
    """17節ステップ1〜6: 未完了行を条件付きUpdateItemで(上限件数まで)FAILED確定し、
    終端行数(terminal_count)を再計算して返す。completedカウンタへのSET補正
    (ステップ7、set_timeout_finalize_completed_count)・TIMED_OUT/
    TIMEOUT_FINALIZE_FAILEDへの遷移判定(ステップ8〜10)は、AuditLog記録・
    メトリクス集計と合わせて行う必要があるため、呼び出し側(Reconciler)が
    本関数の戻り値を見て行う。
    """
    batch_item = get_watchlist_batch(batch_id) or {}
    total = int(batch_item.get("total", 0))

    all_records = query_all_candidate_progress(batch_id, consistent_read=True)
    incomplete = [
        r
        for r in all_records
        if r.status
        in (WatchlistProgressStatus.PENDING.value, WatchlistProgressStatus.PROCESSING.value)
    ]

    newly_failed = 0
    for record in incomplete[:max_rows_per_run]:
        if _try_mark_row_timed_out(batch_id, record.stock_code, now):
            newly_failed += 1

    all_records_after = query_all_candidate_progress(batch_id, consistent_read=True)
    terminal_count = sum(
        1
        for r in all_records_after
        if r.status
        in (WatchlistProgressStatus.COMPLETED.value, WatchlistProgressStatus.FAILED.value)
    )
    return TimeoutFinalizationPassResult(
        all_records=all_records_after,
        terminal_count=terminal_count,
        total=total,
        newly_failed_count=newly_failed,
    )


def set_timeout_finalize_completed_count(
    batch_id: str, terminal_count: int, now: dt.datetime
) -> bool:
    """17節ステップ7: 案C(終端行数からの再計算)によるcompletedのSET補正
    (既存のADDではなく上書き)。terminal_countは直前のQuery時点での正の値であり、
    各進捗行の終端状態はConditionExpressionにより一意に確定しているため、この
    SETによる二重加算・カウント漏れは構造上発生しない(17節参照)。
    """
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="SET completed = :terminal_count, updated_at = :now",
            ConditionExpression="#status = :timeout_finalizing",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":terminal_count": terminal_count,
                ":now": now.isoformat(),
                ":timeout_finalizing": WatchlistBatchStatus.TIMEOUT_FINALIZING.value,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def transition_timeout_finalizing_to_timed_out(batch_id: str, now: dt.datetime) -> bool:
    """17節ステップ9。"""
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression="SET #status = :timed_out, updated_at = :now",
            ConditionExpression="#status = :timeout_finalizing",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":timed_out": WatchlistBatchStatus.TIMED_OUT.value,
                ":timeout_finalizing": WatchlistBatchStatus.TIMEOUT_FINALIZING.value,
                ":now": now.isoformat(),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return False
        raise


def transition_timeout_finalizing_to_failed(batch_id: str, now: dt.datetime, reason: str) -> None:
    """17節ステップ10: terminal_count>totalのデータ不整合、または想定外の例外を
    検出した場合に呼ぶ。ベストエフォート(この遷移自体が失敗しても、次回
    Reconciler実行がTIMEOUT_FINALIZINGのまま放置されたバッチとして手順1から
    再開できる)。
    """
    try:
        _table().update_item(
            Key={"batch_id": batch_id},
            UpdateExpression=(
                "SET #status = :failed, finalize_failed_at = :now, "
                "finalize_error_message = :reason, updated_at = :now"
            ),
            ConditionExpression="#status = :timeout_finalizing",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":failed": WatchlistBatchStatus.TIMEOUT_FINALIZE_FAILED.value,
                ":now": now.isoformat(),
                ":reason": reason[:MAX_FINALIZE_ERROR_MESSAGE_LENGTH],
                ":timeout_finalizing": WatchlistBatchStatus.TIMEOUT_FINALIZING.value,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in _TRANSACTION_CONDITION_FAILURE_CODES:
            return
        raise
