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

DECISION_TYPE_BATCH = "watchlist_auto_addition_batch"
DECISION_TYPE_CANDIDATE = "watchlist_auto_addition_candidate_evaluation"
# finalize後の銘柄単位Repository書き込み結果専用のdecision_type(レビュー対応)。
# DECISION_TYPE_CANDIDATE(スクリーニング評価結果)とは別のAuditLogとして記録し、
# 既存のDECISION_TYPE_CANDIDATEの内容・意味は変更しない。
DECISION_TYPE_REPOSITORY_RESULT = "watchlist_auto_addition_repository_result"

REPOSITORY_RESULT_ADDED = "added"
REPOSITORY_RESULT_SKIPPED_EXISTING = "skipped_existing"
REPOSITORY_RESULT_SKIPPED_OVER_LIMIT = "skipped_over_limit"
REPOSITORY_RESULT_FAILED = "repository_failed"

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
                    }
                    for pr in result.policy_results
                ],
                "matched_criteria": [c.value for c in result.matched_criteria],
                "exclusion_reasons": [r.value for r in result.exclusion_reasons],
                "missing_required_fields": result.missing_required_fields,
                "missing_scoring_fields": result.missing_scoring_fields,
                "main_metrics": result.main_metrics,
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
