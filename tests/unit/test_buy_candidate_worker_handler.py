"""買い候補SQS worker Lambda(Issue #533。#319 Phase 2)のテスト。

`buy_candidate_worker_handler.handler()`が、既存の非同期再帰呼び出し経路
(`buy_candidates_handler.handler()`のtask=="buy_candidate"分岐)と等価な
処理を行うことを固定する。watchlist_worker_handler.pyのテスト構成
(SQS event fixture・heavy dependencyのmonkeypatch)を参考にした。
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from jstock_advisor.lambda_handlers import buy_candidate_worker_handler as worker_module
from jstock_advisor.lambda_handlers import buy_candidates_handler


def _sqs_event(body: dict[str, object]) -> dict[str, object]:
    return {"Records": [{"body": json.dumps(body)}]}


def _fake_snapshot() -> SimpleNamespace:
    """test_buy_candidates_handler.py::_fake_snapshot()と同型の最小限double。"""
    return SimpleNamespace(
        current_price=Decimal("2000"),
        financial=SimpleNamespace(industry="Auto Parts", sector="Consumer Cyclical"),
        stock_type_classification=SimpleNamespace(types=[]),
    )


class _NoopAuditService:
    def record(self, *args: object, **kwargs: object) -> None:
        return None

    def record_if_absent(self, *args: object, **kwargs: object) -> None:
        return None


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    # worker自身が構築する依存(_process_one内)。
    monkeypatch.setattr(worker_module, "build_real_provider_bundle", lambda now, config: object())
    monkeypatch.setattr(worker_module, "build_line_client_for_run", lambda **kw: object())
    monkeypatch.setattr(
        worker_module,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc", (), {"notify_data_error": lambda self, *a, **kw: False}
        )(),
    )
    # _process_single_candidate()内部(buy_candidates_handler.py自身の名前空間から
    # 解決される。呼び出し元がworkerでもhandler()task分岐でも同じ)。
    monkeypatch.setattr(
        buy_candidates_handler, "AuditService", lambda *a, **kw: _NoopAuditService()
    )
    monkeypatch.setattr(
        buy_candidates_handler, "build_stock_snapshot", lambda *a, **kw: (_fake_snapshot(), None)
    )


def test_handler_processes_one_record_via_process_single_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        buy_candidates_handler.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )

    event = _sqs_event({"task": "buy_candidate", "stock_code": "2914", "source": "WATCHLIST"})

    result = worker_module.handler(event, object())

    assert result == {"processed": 1}


def test_process_one_matches_direct_task_branch_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """workerが呼ぶ`_process_single_candidate()`は、既存の
    `buy_candidates_handler.handler()`のtask=="buy_candidate"分岐が呼ぶものと
    完全に同一の関数であるため、同じ入力に対して同じ戻り値になることを固定する
    (SQS経由という配送手段の違いだけで処理結果が変わらないことの回帰テスト)。
    """
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        buy_candidates_handler, "build_real_provider_bundle", lambda now, config: object()
    )
    monkeypatch.setattr(
        buy_candidates_handler, "build_line_client_for_run", lambda **kw: object()
    )
    monkeypatch.setattr(
        buy_candidates_handler,
        "LineNotificationService",
        lambda **kwargs: type(
            "_Svc", (), {"notify_data_error": lambda self, *a, **kw: False}
        )(),
    )
    monkeypatch.setattr(
        buy_candidates_handler, "TradeCooldownService", lambda **kw: type(
            "_TC",
            (),
            {
                "detect_and_apply": lambda self, *a, **kw: type(
                    "_Outcome", (), {"confirmed": True, "events": []}
                )()
            },
        )()
    )

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        buy_candidates_handler.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )

    body = {"stock_code": "2914", "source": "WATCHLIST", "batch_id": None}
    worker_result = worker_module._process_one(body)

    direct_result = buy_candidates_handler.handler(
        {"task": "buy_candidate", **body}, type("_Ctx", (), {"function_name": "x"})()
    )

    assert worker_result == direct_result == {
        "stock_code": "2914",
        "recommended": False,
        "notified": False,
    }
