"""Issue #725: HANDLED_FAILURE通知(`publish_incident_envelope()`)のinfra配線の契約テスト。

`publish_incident_envelope()`は、`INCIDENT_NOTIFICATION_TOPIC_ARN`環境変数と、Lambda実行ロールの
`sns:Publish`(identity-based policy)、**SNS Topic側のresource policyのPrincipal許可**の3つが
揃って初めて通知まで届く。#725までは`WatchlistBatchReconcilerFunction`(#506)だけが配線済みで、
同じ関数を呼ぶ他の8 Lambdaは配線漏れだった(検知コードはあるが到達経路が無く沈黙する。#529と同型)。

## 固定すること(設計: #725 issuecomment-5969894536 / 確定対象8件の正本: issuecomment-5968379379)

```
T1  8関数のEnvironmentへINCIDENT_NOTIFICATION_TOPIC_ARN = !Ref IncidentNotificationTopic
T2  8関数のPoliciesへ PublishIncidentNotification(sns:Publish単一・Resource=Topic単一)
T3  Topic policy 10 Statement(既存2 + 新規8)・Sid一意・Principalは8実行ロールと1対1
T4  reconcilerの配線が不変
T5  対象外の関数にPublishが付かない(15関数という総数も固定)
T6  8関数にRoleプロパティが無い(SAMが`<論理ID>Role`を暗黙生成する前提)
T7  追加のみ: 8関数のenv keyとStatementのSid集合をリテラルで固定
T8  全SNS TopicPolicyでワイルドカードPrincipal・Resource "*"が無い
```

期待値はすべて**リテラル**(実装・templateから導出しない)。templateの静的解析のみで、AWSへのアクセスは行わない。
`sam validate --lint`ではSidの重複を検出できず、ChangeSet EXECUTE時に初めて失敗する
(#557。Release W9)ため、重複はCREATEの前にここで静的に固定する。
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.support.iam_contract_helpers import resources

_TOPIC_REF = {"Fn::Ref": "IncidentNotificationTopic"}
_ENV_KEY = "INCIDENT_NOTIFICATION_TOPIC_ARN"

# 確定対象8件(#725 issuecomment-5968379379。論理ID → Topic policyの新Sid)
_TARGETS: dict[str, str] = {
    "BuyCandidatesFunction": "AllowBuyCandidatesPublish",
    "BuyCandidateWorkerFunction": "AllowBuyCandidateWorkerPublish",
    "HoldingsWatchlistFunction": "AllowHoldingsWatchlistPublish",
    "HoldingsWatchlistWorkerFunction": "AllowHoldingsWatchlistWorkerPublish",
    "WatchlistDispatcherFunction": "AllowWatchlistDispatcherPublish",
    "WatchlistWorkerFunction": "AllowWatchlistWorkerPublish",
    "WatchlistTerminalFailureHandlerFunction": "AllowWatchlistTerminalFailureHandlerPublish",
    "EvaluationFunction": "AllowEvaluationPublish",
}
_RECONCILER = "WatchlistBatchReconcilerFunction"
# 対象外の6関数(publish_incident_envelope()を経由しない。grepで確認)
_OTHERS = (
    "DisclosureCheckFunction",
    "IncidentNotifierFunction",
    "LineWebhookFunction",
    "MonthlyReviewFunction",
    "QuarterlyReviewFunction",
    "WeeklyReviewFunction",
)
_ALL_FUNCTIONS = sorted([*_TARGETS, _RECONCILER, *_OTHERS])


def _function(name: str) -> dict[str, Any]:
    return resources()[name]["Properties"]


def _statements(name: str) -> list[dict[str, Any]]:
    """Policiesのうち、Statementを持つエントリのStatementを平坦化して返す
    (SAMのpolicy templateはStatementを持たないため対象外)。"""
    out: list[dict[str, Any]] = []
    for entry in _function(name).get("Policies", []):
        if isinstance(entry, dict) and "Statement" in entry:
            out.extend(entry["Statement"])
    return out


def _actions(statement: dict[str, Any]) -> list[str]:
    action = statement["Action"]
    return action if isinstance(action, list) else [action]


def _publish_statements(name: str) -> list[dict[str, Any]]:
    return [s for s in _statements(name) if s.get("Sid") == "PublishIncidentNotification"]


def _topic_policy_statements() -> list[dict[str, Any]]:
    return resources()["IncidentNotificationTopicPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ]


# --- T1 -------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(_TARGETS))
def test_t1_target_function_has_the_topic_arn_env(name: str) -> None:
    variables = _function(name)["Environment"]["Variables"]
    assert variables[_ENV_KEY] == _TOPIC_REF


def test_t1_all_eight_targets_are_covered() -> None:
    assert len(_TARGETS) == 8
    assert len(set(_TARGETS.values())) == 8


# --- T2 -------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(_TARGETS))
def test_t2_target_function_can_publish_only_to_the_incident_topic(name: str) -> None:
    [statement] = _publish_statements(name)  # ちょうど1件
    assert statement["Effect"] == "Allow"
    assert _actions(statement) == [
        "sns:Publish"
    ]  # 単一。他のactionと束ねない・ワイルドカードでない
    assert statement["Resource"] == [_TOPIC_REF]  # 単一のTopic ARN。"*"でない
    assert "Condition" not in statement  # reconcilerと同じ(条件なし)


# --- T3 -------------------------------------------------------------------------


def test_t3_topic_policy_has_ten_statements_with_unique_sids() -> None:
    statements = _topic_policy_statements()
    sids = [s["Sid"] for s in statements]
    assert sids == [
        "AllowCloudWatchAlarmPublish",
        "AllowWatchlistBatchReconcilerPublish",
        "AllowBuyCandidatesPublish",
        "AllowBuyCandidateWorkerPublish",
        "AllowHoldingsWatchlistPublish",
        "AllowHoldingsWatchlistWorkerPublish",
        "AllowWatchlistDispatcherPublish",
        "AllowWatchlistWorkerPublish",
        "AllowWatchlistTerminalFailureHandlerPublish",
        "AllowEvaluationPublish",
    ]
    assert len(set(sids)) == len(sids) == 10  # Sidの重複はEXECUTE時に失敗する(#557)


@pytest.mark.parametrize(("name", "sid"), list(_TARGETS.items()))
def test_t3_new_statement_grants_exactly_the_function_role(name: str, sid: str) -> None:
    [statement] = [s for s in _topic_policy_statements() if s["Sid"] == sid]
    assert statement == {
        "Sid": sid,
        "Effect": "Allow",
        "Principal": {"AWS": {"Fn::GetAtt": f"{name}Role.Arn"}},
        "Action": "sns:Publish",
        "Resource": _TOPIC_REF,
    }


def test_t3_principals_map_one_to_one_to_the_eight_roles() -> None:
    new = [s for s in _topic_policy_statements() if s["Sid"] in _TARGETS.values()]
    principals = sorted(s["Principal"]["AWS"]["Fn::GetAtt"] for s in new)
    assert principals == sorted(f"{name}Role.Arn" for name in _TARGETS)
    assert len(set(principals)) == 8


def test_t3_cloudwatch_alarm_statement_is_the_only_service_principal() -> None:
    service_principals = [
        s for s in _topic_policy_statements() if "Service" in s.get("Principal", {})
    ]
    assert [s["Sid"] for s in service_principals] == ["AllowCloudWatchAlarmPublish"]


# --- T4 -------------------------------------------------------------------------


def test_t4_reconciler_wiring_is_unchanged() -> None:
    assert _function(_RECONCILER)["Environment"]["Variables"][_ENV_KEY] == _TOPIC_REF
    [statement] = _publish_statements(_RECONCILER)
    assert statement == {
        "Sid": "PublishIncidentNotification",
        "Effect": "Allow",
        "Action": "sns:Publish",
        "Resource": [_TOPIC_REF],
    }
    [topic_statement] = [
        s for s in _topic_policy_statements() if s["Sid"] == "AllowWatchlistBatchReconcilerPublish"
    ]
    assert topic_statement == {
        "Sid": "AllowWatchlistBatchReconcilerPublish",
        "Effect": "Allow",
        "Principal": {"AWS": {"Fn::GetAtt": "WatchlistBatchReconcilerFunctionRole.Arn"}},
        "Action": "sns:Publish",
        "Resource": _TOPIC_REF,
    }


# --- T5 -------------------------------------------------------------------------


def test_t5_the_template_has_exactly_these_fifteen_functions() -> None:
    """15関数という総数を固定する。新しい関数が追加されたら、このテストが落ちて、
    publish_incident_envelope()を経由するかの判断(8対象への追加か、対象外か)を要求する。"""
    actual = sorted(
        name for name, r in resources().items() if r["Type"] == "AWS::Serverless::Function"
    )
    assert actual == _ALL_FUNCTIONS
    assert len(actual) == 15


@pytest.mark.parametrize("name", _OTHERS)
def test_t5_other_functions_do_not_get_publish_access(name: str) -> None:
    env = (_function(name).get("Environment") or {}).get("Variables") or {}
    assert _ENV_KEY not in env
    for statement in _statements(name):
        assert "sns:Publish" not in _actions(statement), (name, statement.get("Sid"))
        assert "sns:*" not in _actions(statement), (name, statement.get("Sid"))
        assert "*" not in _actions(statement), (name, statement.get("Sid"))
    # Topic policyにも、対象外の関数の実行ロールが現れない
    granted = {
        s["Principal"]["AWS"]["Fn::GetAtt"]
        for s in _topic_policy_statements()
        if "AWS" in s.get("Principal", {})
    }
    assert f"{name}Role.Arn" not in granted


# --- T6 -------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(_TARGETS))
def test_t6_target_functions_use_the_implicit_sam_role(name: str) -> None:
    """Topic policyのPrincipal(`!GetAtt <論理ID>Role.Arn`)は、SAMが暗黙に生成する実行ロールに
    依存する。`Role`プロパティを持つと暗黙ロールが生成されず、GetAttが解決できない。"""
    assert "Role" not in _function(name)


# --- T7 -------------------------------------------------------------------------

# 追加のみ(既存の項目の変更・削除なし)の固定: 配線後のenv keyとStatementのSid集合(リテラル)。
_EXPECTED_ENV_KEYS = {
    "BuyCandidatesFunction": [
        "BUY_CANDIDATE_QUEUE_URL",
        "BUY_CANDIDATE_SQS_DISPATCH_ENABLED",
        "CANDIDATE_UNIVERSE_CACHE_BUCKET",
        "INCIDENT_NOTIFICATION_TOPIC_ARN",
    ],
    "BuyCandidateWorkerFunction": [
        "CANDIDATE_UNIVERSE_CACHE_BUCKET",
        "INCIDENT_NOTIFICATION_TOPIC_ARN",
    ],
    "HoldingsWatchlistFunction": [
        "HOLDINGS_WATCHLIST_QUEUE_URL",
        "HOLDINGS_WATCHLIST_SQS_DISPATCH_ENABLED",
        "INCIDENT_NOTIFICATION_TOPIC_ARN",
    ],
    "HoldingsWatchlistWorkerFunction": ["INCIDENT_NOTIFICATION_TOPIC_ARN"],
    "WatchlistDispatcherFunction": [
        "ALLOW_FULL_MARKET_SCREENING",
        "CANDIDATE_UNIVERSE_CACHE_BUCKET",
        "INCIDENT_NOTIFICATION_TOPIC_ARN",
        "WATCHLIST_DISPATCHER_FUNCTION_NAME",
        "WATCHLIST_SCREENING_QUEUE_URL",
    ],
    "WatchlistWorkerFunction": [
        "INCIDENT_NOTIFICATION_TOPIC_ARN",
        "WATCHLIST_DISPATCHER_FUNCTION_NAME",
    ],
    "WatchlistTerminalFailureHandlerFunction": ["INCIDENT_NOTIFICATION_TOPIC_ARN"],
    "EvaluationFunction": [
        "INCIDENT_NOTIFICATION_TOPIC_ARN",
        "WEEKLY_AGGREGATE_WRITE_ENABLED",
        "WEEKLY_EVALUATION_AGGREGATE_TABLE",
    ],
}
_EXPECTED_STATEMENT_SIDS = {
    "BuyCandidatesFunction": [
        "DynamoDbCrudAccess",
        "DynamoDbReadOnlyAccess",
        None,
        "CandidateUniverseCacheReadForJpxShadow",
        "PublishIncidentNotification",
    ],
    "BuyCandidateWorkerFunction": [
        "DynamoDbCrudAccess",
        "DynamoDbReadOnlyAccess",
        "CandidateUniverseCacheReadForJpxShadow",
        "PublishIncidentNotification",
    ],
    "HoldingsWatchlistFunction": [
        "DynamoDbCrudAccess",
        "DynamoDbReadOnlyAccess",
        None,
        "PublishIncidentNotification",
    ],
    "HoldingsWatchlistWorkerFunction": [
        "DynamoDbCrudAccess",
        "DynamoDbReadOnlyAccess",
        "PublishIncidentNotification",
    ],
    "WatchlistDispatcherFunction": [
        "DynamoDbCrudAccess",
        "DynamoDbReadOnlyAccess",
        "SelfInvokeForMaintenanceTrigger",
        "PublishIncidentNotification",
    ],
    "WatchlistWorkerFunction": [
        "DynamoDbCrudAccess",
        "DynamoDbReadOnlyAccess",
        "InvokeDispatcherForMaintenanceTrigger",
        "PublishIncidentNotification",
    ],
    "WatchlistTerminalFailureHandlerFunction": ["PublishIncidentNotification"],
    "EvaluationFunction": [
        "UpdateWeeklyEvaluationAggregate",
        "PutEvaluationRunSummaryAudit",
        "PublishIncidentNotification",
    ],
}
# Policiesのエントリ数(SAMのpolicy templateを含む。配線で+1)
_EXPECTED_POLICY_ENTRIES = {
    "BuyCandidatesFunction": 6,
    "BuyCandidateWorkerFunction": 3,
    "HoldingsWatchlistFunction": 5,
    "HoldingsWatchlistWorkerFunction": 2,
    "WatchlistDispatcherFunction": 5,
    "WatchlistWorkerFunction": 3,
    "WatchlistTerminalFailureHandlerFunction": 16,
    "EvaluationFunction": 5,
}


@pytest.mark.parametrize("name", list(_TARGETS))
def test_t7_only_the_expected_env_keys_and_statements_exist(name: str) -> None:
    variables = (_function(name).get("Environment") or {}).get("Variables") or {}
    assert sorted(variables) == _EXPECTED_ENV_KEYS[name]
    assert [s.get("Sid") for s in _statements(name)] == _EXPECTED_STATEMENT_SIDS[name]
    assert len(_function(name)["Policies"]) == _EXPECTED_POLICY_ENTRIES[name]


# --- T8 -------------------------------------------------------------------------


def test_t8_no_sns_topic_policy_grants_a_wildcard_principal_or_resource() -> None:
    topic_policies = [
        (lid, r) for lid, r in resources().items() if r["Type"] == "AWS::SNS::TopicPolicy"
    ]
    assert topic_policies  # 少なくともIncidentNotificationTopicPolicyがある
    for logical_id, resource in topic_policies:
        sids = [s.get("Sid") for s in resource["Properties"]["PolicyDocument"]["Statement"]]
        assert len(sids) == len(set(sids)), (logical_id, sids)  # Sid一意(#557)
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            principal = statement.get("Principal")
            assert principal != "*", (logical_id, statement.get("Sid"))
            assert principal != {"AWS": "*"}, (logical_id, statement.get("Sid"))
            assert statement.get("Resource") != "*", (logical_id, statement.get("Sid"))
