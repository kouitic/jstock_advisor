"""Issue #109: disclosure_check_handler の ExecutionContext 契約テスト。

本 handler は以前 `event` を一切読まず、`execution_mode` を黙殺していた。
そのため VALIDATION 指定でも既定の NORMAL として扱われ、**実 LINE 送信・
本番 NotificationLog 書き込みが行われる**状態だった。

VALIDATION 時の抑止(外部 push / NotificationLog / NotificationClaim)は
`LineNotificationService` 側に既に実装済みであるため、ここでは
**実物の LineNotificationService を通した end-to-end** で
「push 0 件 / log 0 件 / claim 0 件」を検証する
(handler が context を注入し忘れれば必ず落ちる)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import ExecutionMode, NotificationMode
from jstock_advisor.infrastructure.line.client import LineClient
from jstock_advisor.infrastructure.local_repository.notification_claim_repository import (
    NotificationClaimRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.interfaces.types import Disclosure
from jstock_advisor.lambda_handlers import disclosure_check_handler as handler_module
from jstock_advisor.services.disclosure_check_service import DisclosureRiskAlert
from jstock_advisor.services.line_notification_service import LineNotificationService

_NOW = dt.datetime(2026, 9, 1, 3, 30, tzinfo=dt.UTC)


class _FakeLineClient(LineClient):
    """外部 LINE API の代わり。push された本文をそのまま溜める。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


class _FakeContext:
    function_name = "jstock-advisor-disclosure-check"


def _alert() -> DisclosureRiskAlert:
    return DisclosureRiskAlert(
        stock_code="7203",
        stock_name="テスト自動車",
        disclosure=Disclosure(
            stock_code="7203",
            title="特別損失の計上に関するお知らせ",
            summary="特別損失を計上します。",
            published_at=_NOW - dt.timedelta(hours=1),
            url=None,
            source=DataSourceReference(provider="test", fetched_at=_NOW),
        ),
        matched_keywords=["特別損失"],
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """handler の外部依存だけを差し替え、LineNotificationService は実物を使う。

    実物を使うのは、**context の注入漏れを検出できる唯一の方法**だからである
    (fake service を挟むと「context を渡したこと」しか検証できず、
    実際に push / log / claim が抑止されるかを固定できない)。
    """
    store_dir = tmp_path / "local_store"
    client = _FakeLineClient()
    log_repo = NotificationLogRepository(store_dir=store_dir)
    claim_repo = NotificationClaimRepository(store_dir=store_dir)
    config = load_config()

    created: list[LineNotificationService] = []

    def _fake_service_factory(**kwargs: Any) -> LineNotificationService:
        svc = LineNotificationService(
            line_client=client,
            notification_log_repository=log_repo,
            notification_claim_repository=claim_repo,
            recommendation_repository=RecommendationRepository(store_dir=store_dir),
            config=config,
            execution_context=kwargs["execution_context"],
        )
        created.append(svc)
        return svc

    class _FakeDisclosureCheckService:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def check_holdings(self, _now: dt.datetime) -> list[DisclosureRiskAlert]:
            return [_alert()]

    monkeypatch.setattr(handler_module, "load_config", lambda: config)
    monkeypatch.setattr(
        handler_module,
        "build_real_provider_bundle",
        lambda *a, **kw: SimpleNamespace(disclosure=object()),
    )
    monkeypatch.setattr(handler_module, "DisclosureCheckService", _FakeDisclosureCheckService)
    monkeypatch.setattr(handler_module, "build_line_client_from_env", lambda: client)
    monkeypatch.setattr(handler_module, "NotificationLogRepository", lambda: log_repo)
    monkeypatch.setattr(handler_module, "NotificationClaimRepository", lambda: claim_repo)
    monkeypatch.setattr(
        handler_module, "RecommendationRepository", lambda: RecommendationRepository(
            store_dir=store_dir
        )
    )
    monkeypatch.setattr(handler_module, "LineNotificationService", _fake_service_factory)
    return client, log_repo, claim_repo, created


# --- T1 / T10: Scheduler の自然実行(regression) --------------------------------


def test_t1_empty_event_keeps_normal_send(wired) -> None:
    """T1/T10: EventBridge Scheduler は mode を渡さない(template に Input 指定なし)。

    `{}` で NORMAL + SEND のまま動作し、実送信・NotificationLog 保存が行われる
    という**従来どおりの挙動**を固定する。#109 の修正で自然実行が
    fail-close へ倒れてはならない。
    """
    client, log_repo, _claim_repo, created = wired

    result = handler_module.handler({}, _FakeContext())

    assert result == {"alerts": 1, "notified": 1}
    assert len(client.sent) == 1, "NORMAL では実送信されること"
    ctx = created[0]._execution_context
    assert ctx.mode is ExecutionMode.NORMAL
    assert ctx.notification_mode is NotificationMode.SEND
    assert ctx.is_dry_run is False
    assert len(log_repo.list_all()) == 1, "NORMAL では NotificationLog が保存されること"


# --- T2〜T5: VALIDATION + DRY_RUN ----------------------------------------------


def test_t2_t5_validation_dry_run_suppresses_all_production_side_effects(wired) -> None:
    """T2/T3/T4/T5: VALIDATION + DRY_RUN で外部 push・本番 NotificationLog・
    本番 NotificationClaim のいずれも発生せず、handler が正常完走する。

    修正前は event が読まれず既定 NORMAL となるため、この test は
    **push 1 件 / log 1 件**で必ず失敗する。
    """
    client, log_repo, claim_repo, created = wired

    result = handler_module.handler(
        {"execution_mode": "VALIDATION", "notification_mode": "DRY_RUN"}, _FakeContext()
    )

    # T5: handler completes
    assert result == {"alerts": 1, "notified": 1}
    ctx = created[0]._execution_context
    assert ctx.mode is ExecutionMode.VALIDATION
    assert ctx.notification_mode is NotificationMode.DRY_RUN
    assert ctx.is_dry_run is True

    # T2: real LINE push = 0
    assert client.sent == [], "VALIDATION+DRY_RUN で外部 LINE push が発生してはならない"
    # T3: production NotificationLog write = 0
    assert log_repo.list_all() == [], "VALIDATION で NotificationLog を保存してはならない"
    # T4: production NotificationClaim write = 0
    assert claim_repo.list_all() == [], "VALIDATION で NotificationClaim を保存してはならない"


# --- T6: VALIDATION + SEND(既存共通契約の踏襲) --------------------------------


def test_t6_validation_send_follows_existing_common_contract(wired) -> None:
    """T6: VALIDATION + SEND は既存 ExecutionContext 契約どおり**送信される**。

    `is_dry_run` は `is_validation AND notification_mode == DRY_RUN` の AND 条件で
    あり、buy / holdings も同じ契約である。**disclosure だけ独自仕様にしない**。
    NotificationLog は VALIDATION のため保存しない(service 側の既存ガード)。
    """
    client, log_repo, claim_repo, created = wired

    result = handler_module.handler(
        {"execution_mode": "VALIDATION", "notification_mode": "SEND"}, _FakeContext()
    )

    assert result == {"alerts": 1, "notified": 1}
    ctx = created[0]._execution_context
    assert ctx.mode is ExecutionMode.VALIDATION
    assert ctx.notification_mode is NotificationMode.SEND
    assert ctx.is_dry_run is False
    assert len(client.sent) == 1, "VALIDATION+SEND は既存契約どおり送信される"
    assert log_repo.list_all() == [], "VALIDATION では NotificationLog を保存しない"
    assert claim_repo.list_all() == [], "VALIDATION では claim を使わない"


# --- T7 / T8 / T9: fail-close ---------------------------------------------------


def test_t7_normal_with_dry_run_is_fail_closed(wired) -> None:
    """T7: NORMAL + notification_mode 指定は既存 resolver が禁止する組み合わせ。
    黙殺せず invocation ごと失敗させる。"""
    client, _log_repo, _claim_repo, _created = wired

    with pytest.raises(ValueError, match="notification_mode requires execution_mode=VALIDATION"):
        handler_module.handler(
            {"execution_mode": "NORMAL", "notification_mode": "DRY_RUN"}, _FakeContext()
        )
    assert client.sent == [], "fail-close 時は一切送信しない"


@pytest.mark.parametrize("raw", ["foo", "validation", "Validation", ""])
def test_t8_unknown_execution_mode_is_fail_closed(wired, raw: str) -> None:
    """T8: 未知の execution_mode は fail-close(小文字表記も既存 enum 上は未知)。"""
    client, _log_repo, _claim_repo, _created = wired

    with pytest.raises(ValueError, match="unknown execution_mode"):
        handler_module.handler({"execution_mode": raw}, _FakeContext())
    assert client.sent == []


@pytest.mark.parametrize("raw", ["foo", "dry_run", ""])
def test_t9_unknown_notification_mode_is_fail_closed(wired, raw: str) -> None:
    """T9: 未知の notification_mode は fail-close。"""
    client, _log_repo, _claim_repo, _created = wired

    with pytest.raises(ValueError, match="unknown notification_mode"):
        handler_module.handler(
            {"execution_mode": "VALIDATION", "notification_mode": raw}, _FakeContext()
        )
    assert client.sent == []


def test_t8b_notification_mode_without_execution_mode_is_fail_closed(wired) -> None:
    """execution_mode 未指定 + notification_mode 指定も既存 resolver が禁止する。"""
    client, _log_repo, _claim_repo, _created = wired

    with pytest.raises(ValueError, match="notification_mode requires execution_mode=VALIDATION"):
        handler_module.handler({"notification_mode": "DRY_RUN"}, _FakeContext())
    assert client.sent == []


# --- T11: EDINET cache の side-effect 契約 ---------------------------------------


def test_t11_edinet_cache_write_contract_is_unchanged_by_issue_109(wired) -> None:
    """T11: EDINET cache は **VALIDATION でも write を許容する**(Issue #53 の既存判断)。

    #109 は cache persistence semantics を変更していない。これを構造的に固定するため、
    `DisclosureCheckService` が `execution_context` を受け取らない(= cache 層が
    実行モードに依存しない)ことを明示する。ここが変わる場合は #53 の判断を
    見直したうえで意図的に行う必要がある。
    """
    import inspect

    from jstock_advisor.services.disclosure_check_service import DisclosureCheckService

    params = inspect.signature(DisclosureCheckService.__init__).parameters
    assert "execution_context" not in params, (
        "DisclosureCheckService が execution_context を受け取るようになった場合、"
        "EDINET cache の VALIDATION 時 write 可否(Issue #53 の判断)を再確認すること"
    )


# --- #85 contract matrix との一致 ------------------------------------------------


def test_issue_85_matrix_records_disclosure_handler_as_propagates() -> None:
    """#85 の context propagation matrix が本 handler を PROPAGATES として記録し、
    KNOWN_GAP 専用 metadata を持たないことを固定する。

    実装(上記 T1〜T9)と inventory の宣言が乖離しないよう、両者を同じ変更で
    更新することを強制する。
    """
    from tests.unit.test_cross_pipeline_invariants import (  # noqa: PLC0415
        _CONTEXT_CONTRACT_MATRIX,
        _ContractStatus,
        _Dimension,
    )

    cells = _CONTEXT_CONTRACT_MATRIX["disclosure_check_handler"]
    for dimension in (_Dimension.EXECUTION_MODE, _Dimension.NOTIFICATION_MODE):
        cell = cells[dimension]
        assert cell.status is _ContractStatus.PROPAGATES, (
            f"disclosure_check_handler の {dimension} は Issue #109 で PROPAGATES になった"
        )
        assert cell.related_issue is None
        assert cell.finding_id is None
