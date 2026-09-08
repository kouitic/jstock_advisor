"""ウォッチリスト自動追加機能: AuditLogへの記録を集約する。

Lambdaハンドラ(fan-out)とCLI(単一プロセス)の両方から同じ記録ロジックを
呼び出すことで、監査ログの形式が経路によって食い違わないようにする。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningResult

# --- Issue #286 (#70 F-B8): batch auditの`execution_mode`の語彙 ---
# 修正前は呼び出し元がすべて "scheduled" をハードコードしており、手動起動も
# 連鎖起動も同じ値で記録されていた(監査から起動経路を区別できなかった)。
# 値はAuditLogへそのまま入る**説明用の文字列**であり、これを読んで分岐する
# コードはsrc内に存在しない(実測)。ExecutionMode enumとは無関係である
# (あちらはNORMAL/VALIDATIONという検証モードの軸で、こちらは起動経路の軸)。
EXECUTION_MODE_SCHEDULED = "scheduled"
EXECUTION_MODE_MANUAL = "manual"
EXECUTION_MODE_TRIGGERED = "triggered"

# 連鎖起動(finalizeがmaintenanceをinvokeする経路)だけが必ず持つキー。
_TRIGGERED_KEYS = ("trigger_type", "triggered_by_batch_id")
# EventBridge Scheduler(ScheduleV2)はどのScheduleもInputを持たないため、
# 自動実行のeventにはこれらのキーが**現れない**(infra/template.yamlを全走査)。
# したがってこれらがあるのは人がpayloadを与えて起動した場合に限られる。
_MANUAL_DISPATCH_KEYS = ("job_type", "batch_id")


def resolve_dispatch_execution_mode(event: dict[str, Any]) -> str:
    """dispatcherのeventから起動経路を決める(Issue #286 F-B8)。

    判定の根拠はeventのキーの**有無**だけであり、値は見ない
    (未知のjob_typeはhandler側が別途fail-closedで止めるため、ここで
    重ねて解釈しない)。
    """
    if any(event.get(key) is not None for key in _TRIGGERED_KEYS):
        return EXECUTION_MODE_TRIGGERED
    if any(event.get(key) is not None for key in _MANUAL_DISPATCH_KEYS):
        return EXECUTION_MODE_MANUAL
    return EXECUTION_MODE_SCHEDULED


def resolve_batch_execution_mode(batch_item: dict[str, Any]) -> str:
    """BatchRunsTableの行から、そのbatchのdispatch時の経路を復元する。

    finalize / reconcileはdispatchのeventを持たないため、dispatch時に
    行へ書かれた`trigger_type`/`triggered_by_batch_id`から復元する。

    ★ **既知の限界(Issue #286で解消しない)**
      `resolve_dispatch_execution_mode()`が返した値そのものは行へ
      永続化していない(BatchRunsTableへ列を足す変更になるため)。
      よって**手動でdispatchしたbatch**のfinalize / reconcile監査は
      ここで "scheduled" と記録される。dispatcher自身の監査5か所は
      正しく "manual" になるため、経路の判別は少なくとも1か所で残る。
    """
    if any(batch_item.get(key) is not None for key in _TRIGGERED_KEYS):
        return EXECUTION_MODE_TRIGGERED
    return EXECUTION_MODE_SCHEDULED


DECISION_TYPE_BATCH = "watchlist_auto_addition_batch"
DECISION_TYPE_CANDIDATE = "watchlist_auto_addition_candidate_evaluation"
# finalize後の銘柄単位Repository書き込み結果専用のdecision_type(レビュー対応)。
# DECISION_TYPE_CANDIDATE(スクリーニング評価結果)とは別のAuditLogとして記録し、
# 既存のDECISION_TYPE_CANDIDATEの内容・意味は変更しない。
DECISION_TYPE_REPOSITORY_RESULT = "watchlist_auto_addition_repository_result"
# --- ウォッチリスト自動運用の改善(ローテーション・自動メンテナンス、2026-08)で追加 ---
DECISION_TYPE_REMOVAL = "watchlist_auto_removal"
DECISION_TYPE_ROTATION_COMMIT = "watchlist_rotation_commit"

REPOSITORY_RESULT_ADDED = "added"
REPOSITORY_RESULT_SKIPPED_EXISTING = "skipped_existing"
REPOSITORY_RESULT_SKIPPED_OVER_LIMIT = "skipped_over_limit"
REPOSITORY_RESULT_FAILED = "repository_failed"
# 計画Part C-4: 自動削除後の再追加クールダウン(readd_cooldown_days)中のため
# 追加をスキップした場合。
REPOSITORY_RESULT_SKIPPED_COOLDOWN = "skipped_cooldown"

# --- Issue #62 Phase B(2026-09): 自動削除の非原子性の解消 ---
# 自動削除の監査記録が「削除時にその場で書かれた完全な記録」なのか、
# 「中断した実行を次回のfinalizeが削除履歴から補完した部分記録」なのかを
# 記録自体から区別できるようにする。
REMOVAL_AUDIT_COMPLETION_COMPLETE = "COMPLETE"
REMOVAL_AUDIT_COMPLETION_RECONSTRUCTED = "RECONSTRUCTED_FROM_REMOVAL_HISTORY"
# 補完経路では WatchlistItem が既に削除済みのため復元できない項目。
_REMOVAL_AUDIT_UNAVAILABLE_FIELDS = [
    "stock_name",
    "registered_at",
    "registration_policy",
    "last_monitoring_score",
    "last_matched_target_types",
    "consecutive_not_qualified_count",
    "hard_exclusion_reasons",
]

_MAX_ERROR_SUMMARY_LENGTH = 300


def record_candidate_audit(
    stock_code: str,
    result: WatchlistScreeningResult | None,
    evaluation_result: str,
    now: dt.datetime,
    batch_id: str | None,
) -> None:
    """銘柄ごとのスクリーニング結果を記録する。

    実際にウォッチリストへ追加されたかどうか(added_to_watchlist)は、全銘柄の
    評価が完了した後のランキング・上限適用で初めて確定するため、この記録には
    含めない。最終的にどの銘柄が追加されたかは、record_repository_result_audit()
    (DECISION_TYPE_REPOSITORY_RESULT)・バッチ単位の監査記録・LINE通知・
    WatchlistItem.registration_source/registration_policyから確認できる。

    batch_idはrecord_repository_result_audit()と同じ値を渡すことで、後から
    「この評価結果が最終的にどう処理されたか」をbatch_id経由で突き合わせられる
    ようにする(Lambda fan-out・CLI単一プロセス実行のいずれも、実行1回につき
    1つのbatch_idを発行して両方の記録へ一貫して渡すこと)。
    """
    output_values: dict[str, Any] = {"evaluation_result": evaluation_result}
    if result is not None:
        output_values.update(
            {
                "stock_name": result.stock_name,
                "total_score": result.total_score,
                "policy_results": [
                    {
                        "policy_name": pr.policy_name,
                        "passed": pr.passed,
                        "score": pr.score,
                        "score_breakdown": pr.score_breakdown,
                        "hard_exclusion_reasons": pr.hard_exclusion_reasons,
                    }
                    for pr in result.policy_results
                ],
                "matched_criteria": [c.value for c in result.matched_criteria],
                "exclusion_reasons": [r.value for r in result.exclusion_reasons],
                "missing_required_fields": result.missing_required_fields,
                "missing_scoring_fields": result.missing_scoring_fields,
                "main_metrics": result.main_metrics,
                # ウォッチリスト自動追加基準の再設計(2026-08)で追加。「なぜこの銘柄が
                # 対象タイプに該当した/しなかったか」をAuditだけから再現可能にする。
                "classification_basis": result.classification_basis,
            }
        )
    AuditService().record(
        decision_type=DECISION_TYPE_CANDIDATE,
        stock_code=stock_code,
        input_values={"batch_id": batch_id, "stock_code": stock_code},
        calculation_formulas={},
        output_values=output_values,
        data_sources=[],
        rule_version=RULE_VERSION_PLACEHOLDER,
        timestamp=now,
    )


def _safe_error_summary(exc: Exception) -> str:
    """AuditLogへ保存可能な長さへ切り詰めたエラー概要を作る。

    詳細なスタックトレースはCloudWatch Logs側のlogger.exceptionに譲り、ここには
    例外の型名+メッセージの概要のみを保存する(機密情報混入・AuditLog肥大化対策)。
    """
    return f"{type(exc).__name__}: {str(exc)}"[:_MAX_ERROR_SUMMARY_LENGTH]


def record_repository_result_audit(
    batch_id: str,
    stock_code: str,
    stock_name: str | None,
    rank: int,
    total_score: float,
    repository_result: str,
    added_to_watchlist: bool,
    registration_source: str,
    registration_policy: str,
    now: dt.datetime,
    error: Exception | None = None,
) -> None:
    """finalize後、銘柄ごとのWatchlistRepository書き込み結果を記録する。

    repository_resultは以下のいずれか:
    - REPOSITORY_RESULT_ADDED: 実際にウォッチリストへ追加された
    - REPOSITORY_RESULT_SKIPPED_EXISTING: 追加を試みたが既に登録済みだった
      (add_if_newの冪等性チェック、または並行実行による競合)
    - REPOSITORY_RESULT_SKIPPED_OVER_LIMIT: 合格しランキングされたが、
      追加件数上限(max_watchlist_additions_per_run)の外だったため追加されなかった
    - REPOSITORY_RESULT_FAILED: Repository書き込み自体が例外で失敗した

    rankは追加件数上限適用「前」の全合格ランキングにおける順位(1始まり)。
    skipped_over_limitの銘柄も含め、合格した全銘柄について呼ぶこと。
    """
    output_values: dict[str, Any] = {
        "stock_name": stock_name,
        "rank": rank,
        "total_score": total_score,
        "repository_result": repository_result,
        "added_to_watchlist": added_to_watchlist,
        "registration_source": registration_source,
        "registration_policy": registration_policy,
        "processed_at": now.isoformat(),
    }
    if error is not None:
        output_values["error_summary"] = _safe_error_summary(error)
    AuditService().record(
        decision_type=DECISION_TYPE_REPOSITORY_RESULT,
        stock_code=stock_code,
        input_values={"batch_id": batch_id, "stock_code": stock_code},
        calculation_formulas={},
        output_values=output_values,
        data_sources=[],
        rule_version=RULE_VERSION_PLACEHOLDER,
        timestamp=now,
    )


def build_removal_audit_id(stock_code: str, removed_at: dt.datetime) -> str:
    """自動削除の監査記録に使う決定的なaudit_id(Issue #62 Phase B)。

    削除履歴(`WatchlistRemovalHistory`)は`stock_code`と`removed_at`を保持する
    ため、**通常経路でも補完経路でも同じ値を再現できる**。これにより
    `record_if_absent()`が「同じ削除に対する監査記録は高々1件」を保証する。

    `removed_at`を含める理由: 同一銘柄が削除→クールダウン終了→再追加→再削除と
    複数回削除されうるため、`stock_code`だけでは2回目以降の削除の監査記録が
    「既に存在する」と誤判定されて失われる。
    """
    return f"{DECISION_TYPE_REMOVAL}:{stock_code}:{removed_at.isoformat()}"


def record_removal_audit(
    stock_code: str,
    stock_name: str | None,
    registered_at: dt.datetime | None,
    registration_policy: str | None,
    removed_at: dt.datetime,
    removal_reason: str,
    removal_category: str,
    last_monitoring_score: float | None,
    last_matched_target_types: list[str],
    consecutive_not_qualified_count: int | None,
    hard_exclusion_reasons: list[str],
    now: dt.datetime,
    batch_id: str | None,
    reconstructed_from_history: bool = False,
) -> bool:
    """AUTO_SCREENING銘柄の自動削除を記録する(計画Part C-6)。

    「なぜ自動で削除されたか」を後からこの記録だけで再現できることを最低限の
    要件とする。LINE通知は行わない(即時の売買アクションを求めるものではない
    ため、計画Part C全体の方針)。

    Issue #62 Phase B: `build_removal_audit_id()`の決定的audit_idと
    `record_if_absent()`を使う。削除後・監査記録前に中断した実行を次回の
    finalizeが補完する際(`reconstructed_from_history=True`)、既に記録済みなら
    何もしないため、補完は**何度走らせても監査記録が重複しない**。

    `reconstructed_from_history=True`の場合、削除済みのWatchlistItemから
    しか取れない項目(`stock_name` / `registered_at` /
    `last_monitoring_score` / `last_matched_target_types` /
    `consecutive_not_qualified_count` / `hard_exclusion_reasons`)は復元できない。
    **Noneや空リストを「値が無かった」ように見せず**、
    `audit_completion`で「履歴から補完した部分記録である」ことを明示する
    (欠測と復元不能を取り違えさせない)。

    戻り値は**実際に新しい監査記録を書いたか**である(レビュー対応 F-A)。
    既に同じ`audit_id`の記録があればFalseを返す。呼び出し側が
    「補完を試みた件数」と「実際に補完した件数」を区別できるようにするため、
    `record_if_absent()`の戻り値を捨てない。捨てると、同じバッチのfinalizeを
    再実行しただけでも補完件数が増え、「平常時は0件」という観測の意味が壊れる。
    """
    output_values: dict[str, Any] = {
        "stock_name": stock_name,
        "registered_at": registered_at.isoformat() if registered_at is not None else None,
        "registration_policy": registration_policy,
        "removed_at": removed_at.isoformat(),
        "removal_reason": removal_reason,
        "removal_category": removal_category,
        "last_monitoring_score": last_monitoring_score,
        "last_matched_target_types": last_matched_target_types,
        "consecutive_not_qualified_count": consecutive_not_qualified_count,
        "hard_exclusion_reasons": hard_exclusion_reasons,
        "audit_completion": (
            REMOVAL_AUDIT_COMPLETION_RECONSTRUCTED
            if reconstructed_from_history
            else REMOVAL_AUDIT_COMPLETION_COMPLETE
        ),
    }
    if reconstructed_from_history:
        # 復元できなかった項目を明示する。null が「そもそも値が無かった」のか
        # 「削除済みで取得できなかった」のかを、記録だけで区別できるようにする。
        output_values["unavailable_fields"] = _REMOVAL_AUDIT_UNAVAILABLE_FIELDS

    entry = AuditService().record_if_absent(
        audit_id=build_removal_audit_id(stock_code, removed_at),
        decision_type=DECISION_TYPE_REMOVAL,
        stock_code=stock_code,
        input_values={"batch_id": batch_id, "stock_code": stock_code},
        calculation_formulas={},
        output_values=output_values,
        data_sources=[],
        rule_version=RULE_VERSION_PLACEHOLDER,
        timestamp=now,
    )
    return entry is not None


def record_rotation_commit_audit(
    batch_id: str,
    rotation_cycle: int | None,
    rotation_start_key: list[str] | None,
    rotation_end_key: list[str] | None,
    wrapped: bool,
    selected_count: int,
    evaluation_result_counts: dict[str, int],
    committed: bool,
    now: dt.datetime,
    rotation_id: str | None = None,
    expected_version: int | None = None,
    observed_version: int | None = None,
) -> None:
    """rotation commitの成否・選択windowの内訳を記録する(計画Part A-6)。

    `evaluation_result_counts`はevaluation_result別の件数(query_all_candidate_
    progress()の結果を集計したもの、poison stock等の内訳を後から確認できる
    ようにする。新規の共有アトミックカウンタは追加しない)。

    `expected_version`/`observed_version`(本番検証2026-08対応): commit失敗時に
    「単なるconflict」だけでなく、期待したpointer_versionと実際に観測された
    pointer_versionを両方記録し、原因調査(実際の競合かバグか)を後から
    区別できるようにする。取得できなかった場合はNone。
    """
    AuditService().record(
        decision_type=DECISION_TYPE_ROTATION_COMMIT,
        stock_code=None,
        input_values={"batch_id": batch_id},
        calculation_formulas={},
        output_values={
            "rotation_id": rotation_id,
            "rotation_cycle": rotation_cycle,
            "rotation_start_key": rotation_start_key,
            "rotation_end_key": rotation_end_key,
            "wrapped": wrapped,
            "selected_count": selected_count,
            "evaluation_result_counts": evaluation_result_counts,
            "committed": committed,
            "expected_version": expected_version,
            "observed_version": observed_version,
        },
        data_sources=[],
        rule_version=RULE_VERSION_PLACEHOLDER,
        timestamp=now,
    )


def record_batch_audit(
    execution_mode: str,
    universe_provider: str,
    screening_policies: list[str],
    output_values: dict[str, Any],
    now: dt.datetime,
    batch_id: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    """idempotency_keyを指定すると、AuditService.record_if_absent()経由で
    決定的なaudit_idを使い保存する(運用ハードニング第3弾3節: batch audit保存
    成功後・呼び出し側のフラグ更新前に中断・再試行しても、audit本体が重複
    記録されないようにするため)。未指定時は従来どおりrecord()(ランダムな
    audit_id、重複記録の防止なし)を使う。
    """
    input_values: dict[str, Any] = {
        "execution_mode": execution_mode,
        "universe_provider": universe_provider,
        "screening_policies": screening_policies,
    }
    if batch_id is not None:
        input_values["batch_id"] = batch_id
    if idempotency_key is not None:
        AuditService().record_if_absent(
            audit_id=idempotency_key,
            decision_type=DECISION_TYPE_BATCH,
            stock_code=None,
            input_values=input_values,
            calculation_formulas={},
            output_values=output_values,
            data_sources=[],
            rule_version=RULE_VERSION_PLACEHOLDER,
            timestamp=now,
        )
        return
    AuditService().record(
        decision_type=DECISION_TYPE_BATCH,
        stock_code=None,
        input_values=input_values,
        calculation_formulas={},
        output_values=output_values,
        data_sources=[],
        rule_version=RULE_VERSION_PLACEHOLDER,
        timestamp=now,
    )
