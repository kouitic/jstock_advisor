"""infra/template.yamlのIAMがIssue #529 corrective fixの最小権限どおりであることの
回帰テスト。

## 背景

PR #572(#529 F-C11 Phase 2)は`WatchlistBatchReconcilerFunction`へ
`reconcile_pending_trade_events()`(trade_event_reconciliation_service.py)を
追加したが、`infra/template.yaml`のIAM policyへの反映が漏れていた。
2026-09-27のProduction自然実行で`dynamodb:Query`
AccessDeniedException(`TradeEventRecordsTable`/`pending-marker-index`)が
毎時発生し、trade event reconciliation経路がProductionで一度も成功しなかった
(既存のwatchlist batch reconciliation本体はtry/except境界により無傷)。

fresh code readで確認した必要権限は次の2グループのみ:

```
list_pending_with_raw()   -> query_by_index()(Query, GSI) + get_raw_data()(GetItem)
mark_consumed()            -> replace_if_raw_matches()(PutItem, CAS)
  対象: TradeEventRecordsTable + pending-marker-index

end_for_trade_events()     -> get_active()/get_with_raw()(GetItem)
                               + replace_if_raw_matches()(PutItem, CAS)
  対象: WatchStateTable(primary keyのみ。Query/Scanは呼ばない)
```

推測でのIAM追加(dynamodb:*やResource:*)を防ぐため、上記2 Statementの
Action/Resourceを厳密に固定する。

テンプレートの静的検証のみで、AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_RECONCILER_FUNCTION = "WatchlistBatchReconcilerFunction"
_TRADE_EVENT_SID = "TradeEventReconciliationAccess"
_WATCH_STATE_SID = "WatchStateEndForTradeEventsAccess"


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _resources() -> dict[str, Any]:
    return _load_template()["Resources"]


def _all_statements(function_name: str) -> list[dict[str, Any]]:
    policies = _resources()[function_name]["Properties"].get("Policies", [])
    statements: list[dict[str, Any]] = []
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        for statement in policy.get("Statement", []) or []:
            statements.append(statement)
    return statements


def _statements_by_sid(function_name: str, sid: str) -> list[dict[str, Any]]:
    return [s for s in _all_statements(function_name) if s.get("Sid") == sid]


def _actions(statement: dict[str, Any]) -> set[str]:
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


def _resources_of(statement: dict[str, Any]) -> list[Any]:
    resource = statement["Resource"]
    return [resource] if not isinstance(resource, list) else resource


def test_reconciler_has_trade_event_records_query_get_put_access() -> None:
    """T1+T2: TradeEventRecordsTableへのQuery(pending-marker-index経由)と
    GetItem・PutItemが、table ARNとindex ARNの双方に対して付与されていること。
    """
    [statement] = _statements_by_sid(_RECONCILER_FUNCTION, _TRADE_EVENT_SID)
    assert statement["Effect"] == "Allow"
    assert _actions(statement) == {"dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"}
    resources = _resources_of(statement)
    assert {"Fn::GetAtt": "TradeEventRecordsTable.Arn"} in resources
    assert {"Fn::Sub": "${TradeEventRecordsTable.Arn}/index/*"} in resources
    assert len(resources) == 2


def test_trade_event_records_grant_excludes_unused_actions() -> None:
    """T3: list_pending_with_raw()/mark_consumed()が呼ばないaction
    (Scan/Delete/BatchWrite/UpdateItem/BatchGetItem/DescribeTable/
    ConditionCheckItem)を付与していないこと(最小権限)。
    """
    [statement] = _statements_by_sid(_RECONCILER_FUNCTION, _TRADE_EVENT_SID)
    unused = {
        "dynamodb:Scan",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:UpdateItem",
        "dynamodb:BatchGetItem",
        "dynamodb:DescribeTable",
        "dynamodb:ConditionCheckItem",
    }
    assert _actions(statement).isdisjoint(unused)


def test_reconciler_has_watch_state_get_and_put_access_for_trade_event_end() -> None:
    """end_for_trade_events()(watch_state_service.py)が呼ぶGetItem/PutItemのみを
    WatchStateTable(primary keyアクセスのみ。GSIなし)へ付与すること。
    """
    [statement] = _statements_by_sid(_RECONCILER_FUNCTION, _WATCH_STATE_SID)
    assert statement["Effect"] == "Allow"
    assert _actions(statement) == {"dynamodb:GetItem", "dynamodb:PutItem"}
    resources = _resources_of(statement)
    assert resources == [{"Fn::GetAtt": "WatchStateTable.Arn"}]


def test_watch_state_grant_has_no_query_scan_or_index_resource() -> None:
    """T3: end_for_trade_events()はwatch_idによるGetItem/PutItemのみで、
    Query/ScanもGSIアクセスも行わない(build_watch_id()による決定的な
    primary keyアクセスのみ)。ValidationWatchStateTableへのアクセスも
    不要(reconcilerは常にNORMAL execution contextで実行される)。
    """
    [statement] = _statements_by_sid(_RECONCILER_FUNCTION, _WATCH_STATE_SID)
    assert _actions(statement).isdisjoint({"dynamodb:Query", "dynamodb:Scan"})
    resources = _resources_of(statement)
    assert all("index" not in str(r) for r in resources)
    assert all("ValidationWatchState" not in str(r) for r in resources)


def test_trade_event_reconciliation_grants_avoid_wildcard_action_or_resource() -> None:
    """T3: dynamodb:*やResource:*のような「動けばよい」修正を許容しない。
    最小権限のleast privilege契約を固定する。
    """
    for sid in (_TRADE_EVENT_SID, _WATCH_STATE_SID):
        [statement] = _statements_by_sid(_RECONCILER_FUNCTION, sid)
        assert "dynamodb:*" not in _actions(statement)
        assert statement["Resource"] != "*"


def test_existing_dynamodb_crud_and_readonly_grants_are_unchanged() -> None:
    """T4: 既存のwatchlist batch reconciliation本体が使うDynamoDbCrudAccess /
    DynamoDbReadOnlyAccessのResource一覧が、今回の#529是正によって変更・
    削除されていないこと(既存機能への影響が無いことの固定)。
    """
    [crud] = _statements_by_sid(_RECONCILER_FUNCTION, "DynamoDbCrudAccess")
    crud_resources = set(map(str, _resources_of(crud)))
    expected_crud_tables = {
        "AuditLogTable",
        "BatchRunsTable",
        "WatchlistCandidateProgressTable",
        "WatchlistTable",
        "NotificationLogTable",
        "NotificationClaimsTable",
        "EdinetFilingCacheTable",
        "EdinetDisclosureCacheTable",
        "EdinetDailyDocumentListCacheTable",
        "WatchlistPriceCacheTable",
        "WatchlistFinancialCacheTable",
        "WatchlistScreeningRotationStateTable",
        "WatchlistRemovalHistoryTable",
        "WatchlistRotationDispatchLeaseTable",
    }
    for table in expected_crud_tables:
        assert any(table in r for r in crud_resources), f"missing {table} in DynamoDbCrudAccess"
    # TradeEventRecordsTable/WatchStateTableは専用Statementへ分離しており、
    # 既存のCrudAccess/ReadOnlyAccessへ混入させていないことも確認する。
    assert not any("TradeEventRecordsTable" in r for r in crud_resources)
    assert not any("WatchStateTable" in r for r in crud_resources)

    [readonly] = _statements_by_sid(_RECONCILER_FUNCTION, "DynamoDbReadOnlyAccess")
    readonly_resources = set(map(str, _resources_of(readonly)))
    assert not any("TradeEventRecordsTable" in r for r in readonly_resources)
    assert not any("WatchStateTable" in r for r in readonly_resources)
