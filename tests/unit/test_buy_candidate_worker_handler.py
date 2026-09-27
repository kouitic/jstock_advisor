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


def test_handler_processes_every_record_not_just_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """サブちゃんレビュー対応(F2-ii。PR #627): `BatchSize`は既定1(infra/
    template.yaml)だが運用側がParameterで上げられるため、複数件のRecordsが
    来た場合も全件処理することを固定する(1件目だけ処理する変異がSURVIVED
    だった。BatchSizeを上げた瞬間、2件目以降が黙って消える経路)。
    """
    _patch_common(monkeypatch)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    processed_stock_codes: list[str] = []
    original_process_one = worker_module._process_one

    def _recording_process_one(body: dict[str, object]) -> dict[str, object]:
        processed_stock_codes.append(body["stock_code"])
        return original_process_one(body)

    monkeypatch.setattr(worker_module, "_process_one", _recording_process_one)
    monkeypatch.setattr(
        buy_candidates_handler.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )

    event = {
        "Records": [
            {
                "body": json.dumps(
                    {"task": "buy_candidate", "stock_code": code, "source": "WATCHLIST"}
                )
            }
            for code in ("2914", "8136")
        ]
    }

    result = worker_module.handler(event, object())

    assert result == {"processed": 2}
    assert processed_stock_codes == ["2914", "8136"]


def test_validation_mode_logs_the_same_diagnostic_marker_as_the_legacy_branch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """サブちゃんレビュー対応(その他の観察。PR #627): VALIDATION実行時の診断ログが
    旧task分岐にのみ存在しworkerに無かった(Phase 3のVALIDATION検証で証跡が
    揃わない)ため、同じログを追加したことを固定する。
    """
    _patch_common(monkeypatch)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        buy_candidates_handler.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )

    event = _sqs_event(
        {
            "task": "buy_candidate",
            "stock_code": "2914",
            "source": "WATCHLIST",
            "execution_mode": "VALIDATION",
        }
    )

    with caplog.at_level("INFO", logger=worker_module.logger.name):
        worker_module.handler(event, object())

    assert any(
        "VALIDATION MODE task=buy_candidate" in record.message
        and "stock_code=2914" in record.message
        for record in caplog.records
    )


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
