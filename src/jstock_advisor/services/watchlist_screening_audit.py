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


def record_candidate_audit(
    stock_code: str,
    result: WatchlistScreeningResult | None,
    evaluation_result: str,
    now: dt.datetime,
) -> None:
    """銘柄ごとのスクリーニング結果を記録する。

    実際にウォッチリストへ追加されたかどうか(added_to_watchlist)は、全銘柄の
    評価が完了した後のランキング・上限適用で初めて確定するため、この記録には
    含めない。最終的にどの銘柄が追加されたかは、バッチ単位の監査記録・LINE通知・
    WatchlistItem.registration_source/registration_policyから確認できる。
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
        input_values={},
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
) -> None:
    input_values: dict[str, Any] = {
        "execution_mode": execution_mode,
        "universe_provider": universe_provider,
        "screening_policies": screening_policies,
    }
    if batch_id is not None:
        input_values["batch_id"] = batch_id
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
