"""Issue #669(HF-4): HoldingDecisionRuntimeConfig の取得失敗によるfallbackを、USERへ通知する。

## 何を固定するのか

```
RuntimeConfigの取得に失敗して、fallbackで処理を続けたとき(service層で捕まえる)
  B1a  get_config(): 直近のcacheを使用          -> HANDLED_FAILURE 1件(stale cache)
  B1b  get_config(): cacheも無く既定値へfallback -> HANDLED_FAILURE 1件(fallback)
  B2   get_notification_enabled(): kill switchの取得失敗
       -> HANDLED_FAILURE 1件(通知を止める側へ倒れた)
  ・従来のfail-safe(fallbackで継続・kill switchは通知しない側)・戻り値・ログは変えない
  ・取得に成功した通常時は、通知を足さない(HF4-AC3)
  ・kill switchが「通知しない」へ倒れた状況でも、本通知は発行される(HF4-AC2。通知の経路が独立)
  ・通知の発行自体が失敗しても、fallbackでの処理継続を止めない(fail-soft)
  ・運用者のCLI(INCIDENT_NOTIFICATION_TOPIC_ARNが無い環境)では送らない
```

## ★ 「内容」行の文言は PROVISIONAL(暫定)

3件の「内容」文の文言について、USERの承認は無い(USERが承認したのは、暫定の文面で実装してよく、
deploy前にUSERが文面を承認する、という進め方のみ。#122 issuecomment-5978254843)。
本テストが固定するのは「その member が解決される」ことと「暫定であることがソースに
明記されていること」であり、文言そのものが最終であるとは主張しない。

## 検査していない範囲

```
・実際のSNSへのpublish(publish関数をスタブへ差し替える)・LINE送信(スタブ)
・Production(deploy前は、LINEに届かない)。Lambdaの環境変数・sns:Publishは既存のtemplateの配線を使う
・handler(holdings_watchlist_handler.py)の変更: 変更していない
```

fixtureは架空値のみ。Production・AWSへは触れない。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import RuntimeConfigMode
from jstock_advisor.domain.notification import incident_message
from jstock_advisor.domain.notification.incident_fingerprint import (
    IncidentFingerprintInput,
    compute_fingerprint,
)
from jstock_advisor.domain.notification.incident_message import (
    IncidentContent,
    IncidentJob,
    IncidentNotice,
    build_incident_message,
    resolve_incident_content,
    resolve_incident_job,
)
from jstock_advisor.domain.notification.incident_signal import FailureClass
from jstock_advisor.lambda_handlers import incident_notifier_handler
from jstock_advisor.services import holding_decision_runtime_config_service as mod
from jstock_advisor.services import incident_envelope_publisher
from jstock_advisor.services.holding_decision_runtime_config_service import (
    HoldingDecisionRuntimeConfigService,
)

_NOW = dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.UTC)
_LATER = _NOW + dt.timedelta(seconds=600)  # TTL(既定60秒)を過ぎた時刻
_TOPIC_ENV = "INCIDENT_NOTIFICATION_TOPIC_ARN"

_STALE = "HOLDINGS_WATCHLIST_RUNTIME_CONFIG_STALE_CACHE_USED"
_FALLBACK = "HOLDINGS_WATCHLIST_RUNTIME_CONFIG_FALLBACK_USED"
_KILL_SWITCH = "HOLDINGS_WATCHLIST_KILL_SWITCH_FETCH_FAILED"


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Lambda相当の環境(topicのenvあり)で、発行された envelope を記録する。

    実際の publish は呼ばない。
    """
    envelopes: list[dict[str, Any]] = []
    monkeypatch.setenv(_TOPIC_ENV, "synthetic-topic")
    monkeypatch.setattr(mod, "publish_incident_envelope", lambda e: envelopes.append(e))
    return envelopes


def _fail_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_store_dir: Path | None = None) -> None:
        raise RuntimeError("synthetic fetch failure: secret-detail-123")

    monkeypatch.setattr(mod._repo, "get", _boom)


def _empty_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod._repo, "get", lambda _store_dir=None: None)


def _service(store_dir: Path, **kwargs: Any) -> HoldingDecisionRuntimeConfigService:
    return HoldingDecisionRuntimeConfigService(store_dir=store_dir, **kwargs)


def _envelope(failure_stage: str, reason_code: str, at: dt.datetime = _NOW) -> dict[str, Any]:
    return {
        "source": "holding_decision_runtime_config",
        "job_name": "holdings-watchlist",
        "failure_stage": failure_stage,
        "failure_type": "FETCH_FAILED",
        "reason_code": reason_code,
        "occurred_at": at.isoformat(),
        "failure_class": "HANDLED_FAILURE",
    }


# =============================================================================
# 失敗したとき: 通知が1件発行される(B1a / B1b / B2)
# =============================================================================


def test_b1b_no_cache_and_fetch_exception_publishes_the_fallback_notice(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    _fail_fetch(monkeypatch)

    lookup = _service(store_dir).get_config(_NOW)

    assert lookup.is_fallback is True  # 既存の契約(is_fallback)は変えない
    assert published == [_envelope("RUNTIME_CONFIG_FETCH", _FALLBACK)]


def test_b1b_uninitialized_record_publishes_the_fallback_notice(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    """レコード未作成(例外ではない)でも、fallbackで動くことは同じなので通知する。"""
    lookup = _service(store_dir).get_config(_NOW)

    assert lookup.is_fallback is True
    assert published == [_envelope("RUNTIME_CONFIG_FETCH", _FALLBACK)]


def test_b1a_stale_cache_used_publishes_the_stale_cache_notice(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    seed = _service(store_dir, notify_fetch_failures=False)
    seed.init_config(updated_by="tester", mode=RuntimeConfigMode.SHADOW, now=_NOW)
    assert seed.get_config(_NOW).config.mode is RuntimeConfigMode.SHADOW  # cacheへ格納
    _fail_fetch(monkeypatch)

    lookup = _service(store_dir).get_config(_LATER)

    # 既存の契約: cacheがあれば、それを使い is_fallback は False のまま(#72 D-4b は変えない)
    assert lookup.is_fallback is False
    assert lookup.config.mode is RuntimeConfigMode.SHADOW
    assert published == [_envelope("RUNTIME_CONFIG_FETCH", _STALE, _LATER)]


@pytest.mark.parametrize("failure", ["exception", "uninitialized"])
def test_b2_kill_switch_fetch_failure_publishes_the_notice_and_keeps_the_fail_safe(
    store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: list[dict[str, Any]],
    failure: str,
) -> None:
    if failure == "exception":
        _fail_fetch(monkeypatch)

    enabled = _service(store_dir).get_notification_enabled(_NOW)

    assert enabled is False  # fail-safe(通知しない側)は変えない
    assert published == [_envelope("KILL_SWITCH_FETCH", _KILL_SWITCH)]


def test_hf4_ac2_the_notice_is_published_even_when_the_kill_switch_stops_investment_notices(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    """HF4-AC2: kill switch が「通知しない」へ倒れた(= 投資判断の通知が止まる)状況でも、
    運用通知(本通知)は発行される。通知の経路は kill switch の値を参照しない。"""
    _fail_fetch(monkeypatch)
    service = _service(store_dir)

    enabled = service.get_notification_enabled(_NOW)

    assert enabled is False
    assert [e["reason_code"] for e in published] == [_KILL_SWITCH]


def test_the_naive_wall_clock_default_is_timezone_aware_utc(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    """get_notification_enabled() の now を省略したとき(handler の現在の呼び方)も、
    occurred_at は timezone-aware(通知の組み立てが aware を要求する)。"""
    _fail_fetch(monkeypatch)

    _service(store_dir).get_notification_enabled()

    occurred_at = dt.datetime.fromisoformat(published[0]["occurred_at"])
    assert occurred_at.tzinfo is not None


# =============================================================================
# 失敗していないとき: 通知を足さない(HF4-AC3)
# =============================================================================


def test_hf4_ac3_a_successful_fetch_adds_no_notice(
    store_dir: Path, published: list[dict[str, Any]]
) -> None:
    service = _service(store_dir)
    service.init_config(updated_by="tester", now=_NOW)

    lookup = service.get_config(_NOW)
    enabled = service.get_notification_enabled(_NOW)

    assert lookup.is_fallback is False
    assert enabled is False  # 初期値(notification_enabled=False)。取得は成功している
    assert published == []


def test_a_cache_hit_within_the_ttl_adds_no_notice(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    service = _service(store_dir)
    service.init_config(updated_by="tester", now=_NOW)
    service.get_config(_NOW)
    _fail_fetch(monkeypatch)  # cache有効期間内は取得しないので、失敗は観測されない

    lookup = service.get_config(_NOW + dt.timedelta(seconds=10))

    assert lookup.is_fallback is False
    assert published == []


# =============================================================================
# 同一instance・別instance での重複の扱い
# =============================================================================


def test_one_notice_per_reason_code_per_service_instance(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    _fail_fetch(monkeypatch)
    service = _service(store_dir)

    for _ in range(3):
        service.get_config(_NOW)
        service.get_notification_enabled(_NOW)

    assert [e["reason_code"] for e in published] == [_FALLBACK, _KILL_SWITCH]


def test_a_new_service_instance_notifies_again(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    """handler は holding ごとに service を作るため、別instanceは再び通知する(以後の重複は
    HF-0 の fingerprint dedup が抑える。新しい永続カウンタ・module-global は足さない)。"""
    _fail_fetch(monkeypatch)

    _service(store_dir).get_config(_NOW)
    _service(store_dir).get_config(_NOW)

    assert [e["reason_code"] for e in published] == [_FALLBACK, _FALLBACK]


# =============================================================================
# 送らない環境・送りたくない呼び出し元
# =============================================================================


def test_no_topic_env_sends_nothing_and_leaves_the_behavior_unchanged(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """運用者のCLI(INCIDENT_NOTIFICATION_TOPIC_ARNが無い環境)では、publishを試みない。"""
    monkeypatch.delenv(_TOPIC_ENV, raising=False)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(mod, "publish_incident_envelope", lambda e: calls.append(e))

    with caplog.at_level(logging.WARNING):
        lookup = _service(store_dir).get_config(_NOW)
        enabled = _service(store_dir).get_notification_enabled(_NOW)

    assert calls == []
    assert lookup.is_fallback is True and enabled is False
    assert not [r for r in caplog.records if "HANDLED_FAILURE" in r.getMessage()]


def test_notify_fetch_failures_false_sends_nothing(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    _fail_fetch(monkeypatch)
    service = _service(store_dir, notify_fetch_failures=False)

    service.get_config(_NOW)
    service.get_notification_enabled(_NOW)

    assert published == []


def test_the_notification_does_not_change_the_return_values(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """通知あり(env設定・publishスタブ)と通知なし(envなし)で、戻り値が同じ(回帰確認)。"""
    _fail_fetch(monkeypatch)
    monkeypatch.setattr(mod, "publish_incident_envelope", lambda _e: None)

    monkeypatch.delenv(_TOPIC_ENV, raising=False)
    without = (
        _service(store_dir).get_config(_NOW),
        _service(store_dir).get_notification_enabled(_NOW),
    )
    monkeypatch.setenv(_TOPIC_ENV, "synthetic-topic")
    mod._cached_config = None
    mod._cached_at = None
    with_notice = (
        _service(store_dir).get_config(_NOW),
        _service(store_dir).get_notification_enabled(_NOW),
    )

    assert with_notice[0].is_fallback == without[0].is_fallback
    assert with_notice[0].config == without[0].config
    assert with_notice[1] == without[1]


# =============================================================================
# 通知の発行自体が失敗しても、fallbackでの処理継続を止めない
# =============================================================================


@pytest.mark.parametrize(
    "error",
    [KeyError(_TOPIC_ENV), RuntimeError("sns down: secret-detail-456"), ValueError("bad key")],
    ids=["env_missing", "runtime_error", "allowlist_violation"],
)
def test_a_publish_failure_does_not_break_the_fallback(
    store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    monkeypatch.setenv(_TOPIC_ENV, "synthetic-topic")

    def _raise(_envelope: dict[str, Any]) -> None:
        raise error

    monkeypatch.setattr(mod, "publish_incident_envelope", _raise)
    _fail_fetch(monkeypatch)

    with caplog.at_level(logging.WARNING):
        lookup = _service(store_dir).get_config(_NOW)
        enabled = _service(store_dir).get_notification_enabled(_NOW)

    assert lookup.is_fallback is True  # 例外が外へ出ない
    assert enabled is False
    warnings = [r.getMessage() for r in caplog.records if "failed to publish" in r.getMessage()]
    assert len(warnings) == 2
    # 例外のメッセージは出さない(stage名のみ)
    assert all("secret-detail" not in w for w in warnings)


# =============================================================================
# envelope の内容(allowlist・識別子を含めない)
# =============================================================================


def test_the_envelope_has_only_allowlisted_keys_and_no_exception_text(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    _fail_fetch(monkeypatch)

    _service(store_dir).get_config(_NOW)
    _service(store_dir).get_notification_enabled(_NOW)

    assert len(published) == 2
    for envelope in published:
        assert set(envelope) <= incident_envelope_publisher.INCIDENT_ENVELOPE_ALLOWLIST
        text = json.dumps(envelope, ensure_ascii=False)
        assert "secret-detail" not in text and "synthetic fetch failure" not in text
        assert "Traceback" not in text and "RuntimeError" not in text


# =============================================================================
# LINE の本文・fingerprint(notifier 側の解決)
# =============================================================================


@pytest.mark.parametrize(
    ("reason_code", "content"),
    [
        (_STALE, IncidentContent.HOLDINGS_WATCHLIST_RUNTIME_CONFIG_STALE_CACHE_USED),
        (_FALLBACK, IncidentContent.HOLDINGS_WATCHLIST_RUNTIME_CONFIG_FALLBACK_USED),
        (_KILL_SWITCH, IncidentContent.HOLDINGS_WATCHLIST_KILL_SWITCH_FETCH_FAILED),
    ],
)
def test_each_reason_code_resolves_to_its_own_content_sentence(
    reason_code: str, content: IncidentContent
) -> None:
    assert resolve_incident_content(reason_code) is content
    assert content is not IncidentContent.OTHER


def test_the_job_resolves_to_the_existing_display_name() -> None:
    """表示名は既存の「保有株チェック」(新しい表示名は作らない)。"""
    assert resolve_incident_job("holdings-watchlist") is IncidentJob.HOLDINGS_WATCHLIST
    assert IncidentJob.HOLDINGS_WATCHLIST.value == "保有株チェック"


def test_the_content_sentences_are_marked_provisional_in_the_source() -> None:
    """★ 「内容」文が暫定(USER の承認なし)であることが、ソースに明記されている。"""
    source = Path(incident_message.__file__).read_text(encoding="utf-8")
    member_line = source.index("HOLDINGS_WATCHLIST_RUNTIME_CONFIG_STALE_CACHE_USED = ")
    preceding = source[max(0, member_line - 700) : member_line]

    assert "PROVISIONAL" in preceding
    assert "USERの承認は無い" in preceding


def test_the_line_body_shows_the_display_name_and_the_content_without_internal_names(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]
) -> None:
    _fail_fetch(monkeypatch)
    _service(store_dir).get_config(_NOW)
    _service(store_dir).get_notification_enabled(_NOW)

    for envelope in published:
        signal = incident_notifier_handler._normalize_internal_message(envelope, _NOW)
        assert signal.failure_class is FailureClass.HANDLED_FAILURE
        body = build_incident_message(
            IncidentNotice(
                job=resolve_incident_job(signal.job_name),
                occurred_at=signal.occurred_at,
                failure_class=signal.failure_class,
                content=resolve_incident_content(signal.error_type),
            )
        )
        assert "対象: 保有株チェック\n" in body
        assert "内容: " in body
        assert (
            "システム側で調査情報を記録しました" not in body
        )  # 恒久記録を示唆する固定文言は出さない
        # 内部の名前・reason code・段階名は本文に出ない
        for internal in (
            "holdings-watchlist",
            "HOLDINGS_WATCHLIST",
            "KILL_SWITCH",
            "RUNTIME_CONFIG",
        ):
            assert internal not in body


def test_the_three_notices_have_distinct_fingerprints() -> None:
    """B1a / B1b / B2 は別々の通知として識別される(同じ時間窓で互いを抑止しない)。"""
    inputs = [
        IncidentFingerprintInput(
            environment="test",
            job_name="holdings-watchlist",
            failure_stage=stage,
            failure_type="FETCH_FAILED",
            error_type=reason,
            error_message=reason,
        )
        for stage, reason in (
            ("RUNTIME_CONFIG_FETCH", _STALE),
            ("RUNTIME_CONFIG_FETCH", _FALLBACK),
            ("KILL_SWITCH_FETCH", _KILL_SWITCH),
        )
    ]

    assert len({compute_fingerprint(i) for i in inputs}) == 3
