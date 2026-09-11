"""infra/template.yamlのEventBridge Scheduler再試行設定に関する不変条件(Issue #318)。

2026-09-11時点で、Scheduler経由の全ScheduleにRetryPolicyの記述が1行も無く、
AWSの既定値(MaximumRetryAttempts=185 / MaximumEventAgeInSeconds=86400)が
そのまま効いていた。**誰も決めていない値**が本番の再試行挙動を決めている状態で
あり、さらに失敗の行き先(DeadLetterConfig)も無いため、再試行を使い切った事象は
どこにも残らず捨てられていた。

本テストは「将来Scheduleを増やしたときに設定漏れをCIで止める」ことを目的とし、
次の不変条件を固定する。

  1 ScheduleV2のイベントには必ずRetryPolicyがある(新しいScheduleを足したら落ちる)
  2 MaximumRetryAttemptsは**既定値185より十分小さい**(回数を決めたことの証拠)
  3 MaximumEventAgeInSecondsは**次の起動間隔を超えない**(遅れて走らせない)
  4 非同期呼び出しを行う関数にはEventInvokeConfigとOnFailure先がある

★ 本テストはYAMLを読むだけである。**CloudFormation/SAMの構文検証ではない。**
  SAMのtransform結果(IAMロールの生成等)は検証できないため、構文・意味の妥当性は
  ChangeSetのCREATE時に確認する必要がある(ローカルにもCIにもtemplate検証は無い)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

# AWSの既定値。これを上回る/等しい値が入っていたら「決めていない」に等しい。
_AWS_DEFAULT_MAX_RETRY_ATTEMPTS = 185
_AWS_DEFAULT_MAX_EVENT_AGE_SECONDS = 86400

# 毎時起動のScheduleは、次の回と重ならないよう間隔未満で打ち切る。
_HOURLY_INTERVAL_SECONDS = 3600

# _fanout.dispatch_async(InvocationType="Event")で**非同期に呼ばれる**関数。
# 呼び出し元ではなく呼び出し先である点に注意する。
#   買い候補/保有は自己呼び出し(handler内でresolve_function_name(context))、
#   加えてReconcilerがBUY_CANDIDATES_FUNCTION_NAME/HOLDINGS_WATCHLIST_FUNCTION_NAME
#   経由で完了リカバリを非同期投入する(実測: 呼び出し3か所)。
_ASYNC_INVOKED_FUNCTIONS = ("BuyCandidatesFunction", "HoldingsWatchlistFunction")


class _CfnLoader(yaml.SafeLoader):
    """CloudFormationの短縮形組み込み関数を素朴なdictへ変換する構文解析専用Loader。"""


def _cfn_multi_constructor(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return {tag_suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag_suffix: loader.construct_sequence(node)}
    assert isinstance(node, yaml.MappingNode)  # noqa: S101 - CFNタグはこの3種のみ
    return {tag_suffix: loader.construct_mapping(node)}


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)  # type: ignore[no-untyped-call]


def _load_template() -> dict[str, Any]:
    loaded = yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_CfnLoader)
    assert isinstance(loaded, dict)  # noqa: S101 - templateはmapping
    return loaded


def _schedule_events() -> list[tuple[str, str, dict[str, Any]]]:
    """(関数の論理ID, イベント名, イベントのProperties) を全件返す。"""
    resources = _load_template()["Resources"]
    found: list[tuple[str, str, dict[str, Any]]] = []
    for logical_id, resource in resources.items():
        events = (resource.get("Properties") or {}).get("Events") or {}
        for event_name, event in events.items():
            if (event or {}).get("Type") == "ScheduleV2":
                found.append((logical_id, event_name, event.get("Properties") or {}))
    return found


def test_template_has_schedule_events() -> None:
    """母集団そのものが空になっていないこと(テストが素通りするのを防ぐ)。"""
    assert _schedule_events(), "ScheduleV2のイベントが1件も見つからない"


@pytest.mark.parametrize(("logical_id", "event_name", "properties"), _schedule_events())
def test_every_schedule_declares_retry_policy(
    logical_id: str, event_name: str, properties: dict[str, Any]
) -> None:
    """全ScheduleがRetryPolicyを**明示**していること(Issue #318の中核)。

    新しいScheduleを追加してRetryPolicyを書き忘れると、このテストが落ちる。
    """
    assert "RetryPolicy" in properties, (
        f"{logical_id}.{event_name} にRetryPolicyが無い。"
        "AWS既定(185回/24時間)がそのまま効くため、値を明示すること(Issue #318)"
    )


@pytest.mark.parametrize(("logical_id", "event_name", "properties"), _schedule_events())
def test_retry_attempts_are_decided_not_defaulted(
    logical_id: str, event_name: str, properties: dict[str, Any]
) -> None:
    """再試行回数がAWS既定値より十分小さいこと。

    ★ 「回復しない障害は185回繰り返しても回復しない」「ファンアウトする経路では
    再試行がそのまま子Lambdaの二重起動になる」の2点から、少ない回数にしている。
    """
    attempts = properties["RetryPolicy"]["MaximumRetryAttempts"]
    assert isinstance(attempts, int)
    assert 0 <= attempts < _AWS_DEFAULT_MAX_RETRY_ATTEMPTS, (
        f"{logical_id}.{event_name} のMaximumRetryAttempts={attempts} が既定値相当"
    )
    assert attempts <= 3, (
        f"{logical_id}.{event_name} のMaximumRetryAttempts={attempts} は多すぎる。"
        "定期バッチであり、回復しない障害は回数を増やしても回復しない(Issue #318)"
    )


@pytest.mark.parametrize(("logical_id", "event_name", "properties"), _schedule_events())
def test_event_age_does_not_outlive_its_purpose(
    logical_id: str, event_name: str, properties: dict[str, Any]
) -> None:
    """打ち切り時間がAWS既定(24時間)より短いこと。

    08:00のバッチを翌07:59に実行しても意味が無い(むしろ有害)ため、
    当日中に意味を失う長さで打ち切る。
    """
    age = properties["RetryPolicy"]["MaximumEventAgeInSeconds"]
    assert isinstance(age, int)
    assert age < _AWS_DEFAULT_MAX_EVENT_AGE_SECONDS, (
        f"{logical_id}.{event_name} のMaximumEventAgeInSeconds={age} が既定値相当"
    )


def test_hourly_schedule_stops_before_the_next_run() -> None:
    """毎時起動のScheduleは、次の回が来る前に打ち切ること。

    ★ 前の回の再試行と次の回が重なると、**重なり自体が二重実行**になる。
    """
    hourly = [
        (logical_id, event_name, properties)
        for logical_id, event_name, properties in _schedule_events()
        if str(properties.get("ScheduleExpression", "")).replace(" ", "") == "rate(1hour)"
    ]
    assert hourly, "rate(1 hour)のScheduleが見つからない(前提が変わった可能性)"
    for logical_id, event_name, properties in hourly:
        age = properties["RetryPolicy"]["MaximumEventAgeInSeconds"]
        assert age < _HOURLY_INTERVAL_SECONDS, (
            f"{logical_id}.{event_name} のMaximumEventAgeInSeconds={age} は"
            f"起動間隔{_HOURLY_INTERVAL_SECONDS}秒以上で、次の回と重なりうる"
        )


@pytest.mark.parametrize("logical_id", _ASYNC_INVOKED_FUNCTIONS)
def test_async_invoked_functions_have_failure_destination(logical_id: str) -> None:
    """非同期で呼ばれる関数が、失敗の行き先を持つこと。

    ★ 行き先が無いと、再試行を使い切った呼び出しは**どこにも残らず消える**。
    ★ ただしこれはLambdaの非同期呼び出しの失敗のみを受ける。EventBridge Scheduler
      がLambdaを起動できなかった場合(delivery failure)は**ここには入らない**
      (帰属先はIssue #132)。
    """
    resources = _load_template()["Resources"]
    config = (resources[logical_id].get("Properties") or {}).get("EventInvokeConfig")
    assert config is not None, f"{logical_id} にEventInvokeConfigが無い(Issue #318)"
    on_failure = (config.get("DestinationConfig") or {}).get("OnFailure") or {}
    assert on_failure.get("Type") == "SQS", f"{logical_id} のOnFailure先がSQSでない"
    assert on_failure.get("Destination"), f"{logical_id} のOnFailure先が空"
    attempts = config["MaximumRetryAttempts"]
    assert isinstance(attempts, int) and attempts <= 2, (
        f"{logical_id} のEventInvokeConfig.MaximumRetryAttempts={attempts} は多い。"
        "この関数はleaseを取らないため、再試行はそのまま判定の二重実行になる"
    )


def test_async_failure_queue_is_not_shared_with_watchlist_queues() -> None:
    """非同期失敗のDLQが、watchlist系のキューと別物であること。

    用途も読み手も違うため混ぜない(Issue #318の設計判断)。
    """
    resources = _load_template()["Resources"]
    assert "AsyncInvokeFailureDLQ" in resources, "非同期失敗用のDLQが無い"

    # watchlist系のキューがredrive先として本DLQを指していないこと(= 相乗りしていない)。
    for logical_id, resource in resources.items():
        if resource.get("Type") != "AWS::SQS::Queue" or logical_id == "AsyncInvokeFailureDLQ":
            continue
        redrive = (resource.get("Properties") or {}).get("RedrivePolicy") or {}
        target = redrive.get("deadLetterTargetArn")
        assert "AsyncInvokeFailureDLQ" not in str(target), (
            f"{logical_id} が非同期失敗用DLQをredrive先にしている。用途も読み手も違うため混ぜない"
        )
