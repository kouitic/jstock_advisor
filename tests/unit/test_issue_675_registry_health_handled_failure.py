"""Issue #675(HF-10): 株主優待registryの健全性チェック自体の技術的失敗を、USERへ通知する。

## 何を固定するのか

```
check_registry_health() の件数取得(list_all())が失敗したとき、
  ・HANDLED_FAILURE の envelope を1件、HF-0 の契約(#665)で発行する
  ・従来の ERROR ログと fail-soft 契約(例外を外へ出さない)は変えない
  ・通知の発行自体が失敗しても、fail-soft 契約を破らない(WARNING を残す)
  ・「件数が少ない WARNING」(#493)は別の事象であり、通知の対象にしない(HF10-AC2)
  ・LINE の本文は「対象: 株主優待データの確認」(USER確定の表示名)で、GitHub Issue は起票しない
```

## ★ 「内容」行の文言は PROVISIONAL(暫定)

「内容」行の文 `IncidentContent.SHAREHOLDER_BENEFIT_REGISTRY_HEALTH_CHECK_FAILED` の文言について、
USER の承認は無い(USER が確定したのは表示名と job_name の固定値だけ)。本テストが固定するのは
「その member が解決される」ことと「暫定であることがソースに明記されていること」であり、
文言そのものが最終であるとは主張しない。deploy 前に USER の確認が要る。

## 検査していない範囲

```
・実際の SNS への publish(boto3 をスタブへ差し替える)・LINE 送信(スタブ)・DynamoDB(スタブ)
・Production での到達: BuyCandidates / HoldingsWatchlist の Lambda は、環境変数
  INCIDENT_NOTIFICATION_TOPIC_ARN も sns:Publish も template に無い(#725 が追跡)。
  それが済むまで、通知の発行は失敗する(本テストは、その失敗が fail-soft を壊さないことを固定する)
・2つの呼び出し元(handler)の変更: handler は変更していない(呼び出しの形が変わらないことだけ固定)
```

fixture は架空値のみ。Production・AWS へは触れない。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import boto3
import pytest

from jstock_advisor.domain.notification import incident_message
from jstock_advisor.domain.notification.incident_message import (
    IncidentContent,
    IncidentJob,
    resolve_incident_content,
    resolve_incident_job,
)
from jstock_advisor.domain.notification.incident_signal import FailureClass
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.lambda_handlers import incident_notifier_handler
from jstock_advisor.services import incident_envelope_publisher
from jstock_advisor.services import shareholder_benefit_registry_service as registry_module
from jstock_advisor.services.shareholder_benefit_registry_service import (
    ShareholderBenefitRegistryService,
    check_registry_health,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NOW = dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.UTC)


class _FailingService:
    """list_all() が技術的に失敗するレジストリ(件数取得の失敗を模す)。"""

    def list_all(self) -> list[Any]:
        raise RuntimeError("scan failed: synthetic")


class _CountingService:
    def __init__(self, count: int) -> None:
        self._count = count

    def list_all(self) -> list[Any]:
        return [object()] * self._count


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """発行された envelope を記録する(実際の publish は呼ばない)。"""
    envelopes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        registry_module, "publish_incident_envelope", lambda envelope: envelopes.append(envelope)
    )
    return envelopes


def _check(service: Any, min_expected: int = 1) -> None:
    check_registry_health(min_expected, service=service, now=_NOW)


# =============================================================================
# 失敗したとき: 通知が1件発行される
# =============================================================================


def test_a_failed_count_publishes_exactly_one_handled_failure_envelope(
    published: list[dict[str, Any]],
) -> None:
    _check(_FailingService())

    assert len(published) == 1
    assert published[0] == {
        "source": "shareholder_benefit_registry",
        "job_name": "shareholder-benefit-registry",
        "failure_stage": "REGISTRY_HEALTH_CHECK",
        "failure_type": "UNHANDLED_EXCEPTION",
        "reason_code": "SHAREHOLDER_BENEFIT_REGISTRY_HEALTH_CHECK_FAILED",
        "occurred_at": _NOW.isoformat(),
        "failure_count": 1,
        "failure_class": "HANDLED_FAILURE",
    }


def test_the_envelope_has_only_allowlisted_keys_and_no_identifiers(
    published: list[dict[str, Any]],
) -> None:
    _check(_FailingService())

    envelope = published[0]
    assert set(envelope) <= incident_envelope_publisher.INCIDENT_ENVELOPE_ALLOWLIST
    text = json.dumps(envelope, ensure_ascii=False)
    # 生の例外メッセージ・stack trace・識別子の断片を含めない
    assert "synthetic" not in text and "scan failed" not in text
    assert "Traceback" not in text and "RuntimeError" not in text


def test_the_error_log_and_the_fail_soft_contract_are_unchanged(
    published: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    """通知を足しても、従来の ERROR ログは残り、例外は外へ出ない。"""
    with caplog.at_level(logging.INFO):
        check_registry_health(1, service=_FailingService(), now=_NOW)  # type: ignore[arg-type]

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "event=shareholder_benefit_registry_health_check_failed" in errors[0].getMessage()
    assert len(published) == 1
    # 失敗したあとは、件数が不明なまま「登録件数」のINFO・件数少のWARNINGへ進まない
    # (失敗の後ろへ落ちて、不明な件数を 0 件と誤って記録しない)
    assert not [r for r in caplog.records if "loaded" in r.getMessage()]
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_the_envelope_does_not_depend_on_the_caller(published: list[dict[str, Any]]) -> None:
    """どちらのバッチ(買い候補 / 保有株)から呼ばれても同じ内容 = 通知の identity が同じ。

    USER決定: 呼び出し元ごとに分けない(同日に両バッチから同じ失敗があっても、利用者にとって
    意味のある事実は「registry が壊れている」の1つ。重複通知を避ける)。
    """
    _check(_FailingService())
    _check(_FailingService())

    assert published[0] == published[1]


# =============================================================================
# 通知の発行自体が失敗しても、fail-soft を破らない
# =============================================================================


@pytest.mark.parametrize(
    "error",
    [KeyError("INCIDENT_NOTIFICATION_TOPIC_ARN"), RuntimeError("sns down"), ValueError("bad key")],
    ids=["env_missing", "runtime_error", "allowlist_violation"],
)
def test_a_publish_failure_does_not_break_the_fail_soft_contract(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    def _raise(_envelope: dict[str, Any]) -> None:
        raise error

    monkeypatch.setattr(registry_module, "publish_incident_envelope", _raise)

    with caplog.at_level(logging.INFO):
        check_registry_health(1, service=_FailingService(), now=_NOW)  # type: ignore[arg-type]

    # 例外が外へ出ない(ここへ到達できること自体が契約)
    messages = [r.getMessage() for r in caplog.records]
    # 従来の ERROR は残り、発行失敗は WARNING(例外の型のみ)として残る
    assert any("health_check_failed" in m and "notify" not in m for m in messages)
    warning = next(m for m in messages if "health_check_notify_failed" in m)
    assert f"error_type={type(error).__name__}" in warning
    assert str(error) not in warning  # 例外のメッセージは出さない


def test_the_real_publisher_without_the_topic_env_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """実際の publisher で、環境変数が無い Lambda(現在の BuyCandidates / HoldingsWatchlist)でも
    健全性チェックが止まらない(#725 の infra 配線が済むまでの実態)。"""
    monkeypatch.delenv("INCIDENT_NOTIFICATION_TOPIC_ARN", raising=False)

    with caplog.at_level(logging.WARNING):
        check_registry_health(1, service=_FailingService(), now=_NOW)  # type: ignore[arg-type]

    assert any("health_check_notify_failed" in r.getMessage() for r in caplog.records)


def test_the_real_publisher_publishes_the_envelope_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実際の publish_incident_envelope() が、この envelope を受理して SNS へ JSON で渡す。"""
    sent: list[dict[str, Any]] = []

    class _Sns:
        def publish(self, **kwargs: Any) -> None:
            sent.append(kwargs)

    monkeypatch.setenv("INCIDENT_NOTIFICATION_TOPIC_ARN", "arn:aws:sns:test:000000000000:topic")
    monkeypatch.setattr(boto3, "client", lambda _name: _Sns())

    _check(_FailingService())

    assert len(sent) == 1
    assert sent[0]["TopicArn"] == "arn:aws:sns:test:000000000000:topic"
    assert json.loads(sent[0]["Message"])["reason_code"] == (
        "SHAREHOLDER_BENEFIT_REGISTRY_HEALTH_CHECK_FAILED"
    )


# =============================================================================
# HF10-AC2: 件数少 WARNING などは、通知の対象ではない
# =============================================================================


@pytest.mark.parametrize(
    ("count", "min_expected"),
    [(0, 5), (3, 5), (5, 5), (10, 5), (0, 0), (7, 0)],
    ids=["empty_below", "below", "exact", "above", "disabled_empty", "disabled_nonempty"],
)
def test_a_successful_check_never_publishes(
    published: list[dict[str, Any]], count: int, min_expected: int
) -> None:
    """健全性チェック自体が成功したとき(件数が少ない場合を含む)は、新しい通知を出さない。"""
    check_registry_health(min_expected, service=_CountingService(count), now=_NOW)  # type: ignore[arg-type]

    assert published == []


def test_the_low_count_warning_is_still_a_log_only_event(
    published: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        check_registry_health(5, service=_CountingService(2), now=_NOW)  # type: ignore[arg-type]

    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert published == []


# =============================================================================
# LINE の本文(USER確定の表示名・暫定の内容文)
# =============================================================================


def test_the_internal_job_name_resolves_to_the_user_confirmed_display_name() -> None:
    assert resolve_incident_job("shareholder-benefit-registry") is (
        IncidentJob.SHAREHOLDER_BENEFIT_REGISTRY
    )
    assert IncidentJob.SHAREHOLDER_BENEFIT_REGISTRY.value == "株主優待データの確認"


def test_the_reason_code_resolves_to_a_content_sentence_not_the_fallback() -> None:
    content = resolve_incident_content("SHAREHOLDER_BENEFIT_REGISTRY_HEALTH_CHECK_FAILED")

    assert content is IncidentContent.SHAREHOLDER_BENEFIT_REGISTRY_HEALTH_CHECK_FAILED
    # 値が他の「内容」文と重複しない(StrEnum の alias にならない。#740 のレビューで実際に起きた)
    assert sum(1 for member in IncidentContent if member.value == content.value) == 1, (
        "「内容」文の値が他と重複している"
    )


def test_the_content_sentence_follows_the_existing_sentence_form_and_does_not_imply_a_record() -> (
    None
):
    """暫定の文言が、既存の文型「<対象>の<処理>に失敗しました」に揃い、恒久記録を示唆しない。"""
    sentence = IncidentContent.SHAREHOLDER_BENEFIT_REGISTRY_HEALTH_CHECK_FAILED.value

    assert sentence.endswith("に失敗しました")
    assert "調査情報を記録" not in sentence and "記録しました" not in sentence


def test_the_content_sentence_is_marked_provisional_in_the_source() -> None:
    """★ 「内容」文が暫定(USER の承認なし)であることが、ソースに明記されている。"""
    source = Path(incident_message.__file__).read_text(encoding="utf-8")
    member_line = source.index("SHAREHOLDER_BENEFIT_REGISTRY_HEALTH_CHECK_FAILED = ")
    preceding = source[max(0, member_line - 600) : member_line]

    assert "PROVISIONAL" in preceding
    assert "USERの承認は無い" in preceding


def test_the_line_body_uses_the_neutral_headline_the_display_name_and_the_content_line(
    monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    """発行された envelope から、notifier が組み立てる LINE の本文(HANDLED_FAILURE)を確認する。"""
    _check(_FailingService())
    signal = incident_notifier_handler._normalize_internal_message(published[0], _NOW)
    assert signal.failure_class is FailureClass.HANDLED_FAILURE

    pushed: list[str] = []

    class _Line:
        def push_message(self, text: str) -> None:
            pushed.append(text)

    monkeypatch.setattr(tracker, "get_incident_state", lambda _fp: None)
    monkeypatch.setattr(tracker, "mark_sent", lambda *_a, **_k: None)
    monkeypatch.setattr(
        incident_notifier_handler, "build_live_line_client_from_env", lambda: _Line()
    )

    error = incident_notifier_handler._send_line(
        "fingerprint", "token", tracker.IncidentClaimOutcome.CLAIMED_NEW, signal, _NOW
    )

    assert error is None
    assert len(pushed) == 1
    body = pushed[0]
    assert "対象: 株主優待データの確認\n" in body
    assert "内容: 株主優待データの確認処理に失敗しました\n" in body
    assert "件数: 1件" in body
    assert "システム側で調査情報を記録しました" not in body  # 恒久記録を示唆する固定文言は出さない
    assert "shareholder-benefit-registry" not in body  # 内部名は本文に出ない


# =============================================================================
# 呼び出し元(handler)は変更しない
# =============================================================================


@pytest.mark.parametrize(
    "handler_file", ["buy_candidates_handler.py", "holdings_watchlist_handler.py"]
)
def test_the_callers_still_call_the_health_check_without_the_new_parameter(
    handler_file: str,
) -> None:
    """handler は変更していない: 呼び出しは従来どおり(新しい任意引数 now を渡していない)。"""
    tree = ast.parse(
        (_REPO_ROOT / "src" / "jstock_advisor" / "lambda_handlers" / handler_file).read_text(
            encoding="utf-8"
        )
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "check_registry_health"
    ]

    assert len(calls) == 1
    assert len(calls[0].args) + len(calls[0].keywords) == 1
    assert not any(kw.arg == "now" for kw in calls[0].keywords)


def test_the_service_class_is_unchanged() -> None:
    """レジストリの読み書きの契約は変えていない(list_all の存在を前提にする)。"""
    assert hasattr(ShareholderBenefitRegistryService, "list_all")
