"""Issue #754(#27 U-2): 株主優待registryのcoverageを、候補側・保有側の2軸でINFO記録する。

## 何を固定するのか

```
CANDIDATE_COVERAGE = registry ∩ 候補 / 候補       HOLDINGS_COVERAGE = registry ∩ 保有 / 保有
  ・2軸を分けて記録する(合計%だけでは、候補側が0%へ悪化しても見えにくい)。合計は補助情報
  ・WARNINGは出さない(USER決定: threshold = NONE)。coverageの値は投資判定に使わない
  ・ログには件数と割合だけ(銘柄コード・所有者・holding_idは出さない)
  ・fail-soft(#120): 算出が失敗しても例外を外へ出さない。失敗時は件数不明のERRORを残す(沈黙しない)
  ・読み取り専用: 追加の外部読み込みはregistryのlist_all() 1回のみ。
    write・publish・invoke・LINE送信は無い
  ・2つのhandlerは、読み込み済みの銘柄コードを渡すだけ(watchlist・holdingsの追加の読み込みは無い)
```

## 検査していない範囲

```
・Productionのregistryの件数・実際のcoverageの値(未観測)・追加のregistry Scanが日次経路の実行時間へ
  与える影響の大きさ(件数に依存。ここで固定するのは「list_all() 1回だけ」という構造)
・coverage不足の閾値・通知(threshold = NONE。外部データを採用すると決めた時点で別のUSER判断)
・handlerの他の処理(dispatch等)。coverageの呼び出しが戻り値を変えないことだけ固定する
```

fixture は架空値のみ(銘柄コードは実在しない"1111"系)。Production・AWS へは触れない。
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.services import shareholder_benefit_registry_service as registry_module
from jstock_advisor.services.shareholder_benefit_registry_service import check_registry_coverage

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOGGER_NAME = registry_module.__name__
_EVENT = "event=shareholder_benefit_registry_coverage "
_FAILED_EVENT = "event=shareholder_benefit_registry_coverage_failed"


class _Benefit:
    def __init__(self, stock_code: str) -> None:
        self.stock_code = stock_code


class _Registry:
    """registry の代わり。list_all() だけを持つ(それ以外の属性にアクセスしたら失敗する)。"""

    def __init__(self, codes: list[str]) -> None:
        self._codes = codes
        self.list_all_calls = 0

    def list_all(self) -> list[_Benefit]:
        self.list_all_calls += 1
        return [_Benefit(code) for code in self._codes]

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"registry の {name} にアクセスした(list_all 以外は使わない契約)")


class _FailingRegistry:
    def list_all(self) -> list[_Benefit]:
        raise RuntimeError("scan failed: synthetic")


def _record(
    caplog: pytest.LogCaptureFixture,
    candidates: Any,
    holdings: Any,
    registry: Any,
) -> list[logging.LogRecord]:
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        check_registry_coverage(candidates, holdings, service=registry)
    return [r for r in caplog.records if r.name == _LOGGER_NAME]


def _fields(record: logging.LogRecord) -> dict[str, str]:
    message = record.getMessage()
    assert message.startswith(_EVENT)
    return dict(re.findall(r"(\w+)=(\S+)", message))


# =============================================================================
# 2軸の値
# =============================================================================


def test_two_axes_are_recorded_separately_with_counts_and_percentages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _Registry(["1111", "2222", "3333"])
    records = _record(caplog, ["1111", "2222", "7777", "8888"], ["1111", "3333"], registry)

    assert len(records) == 1 and records[0].levelno == logging.INFO
    fields = _fields(records[0])
    assert fields["candidate_registered_of_total"] == "2/4"
    assert fields["candidate_coverage_pct"] == "50.0"
    assert fields["holdings_registered_of_total"] == "2/2"
    assert fields["holdings_coverage_pct"] == "100.0"
    # 補助情報の合計: 候補 ∪ 保有 = {1111, 2222, 7777, 8888, 3333} の 5 件のうち登録済みは 3 件
    assert fields["total_registered_of_total"] == "3/5"
    assert fields["total_coverage_pct"] == "60.0"
    assert fields["registry_entries"] == "3"


def test_a_candidate_side_collapse_is_visible_even_when_the_total_looks_healthy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ 2軸に分ける理由: 保有側が多いと、合計%は健全に見えるが、候補側は0%でありうる。"""
    registry = _Registry([f"{n}" for n in range(1000, 1010)])
    holdings = [f"{n}" for n in range(1000, 1010)]  # 10件すべて登録済み
    candidates = ["9001", "9002"]  # 2件とも未登録

    fields = _fields(_record(caplog, candidates, holdings, registry)[0])

    assert fields["candidate_coverage_pct"] == "0.0"
    assert fields["holdings_coverage_pct"] == "100.0"
    assert float(fields["total_coverage_pct"]) > 80  # 合計だけ見ると健全に見える


def test_an_empty_axis_is_not_applicable_and_a_missing_axis_is_not_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _Registry(["1111"])

    empty = _fields(_record(caplog, [], ["1111"], registry)[0])
    caplog.clear()
    missing = _fields(_record(caplog, None, ["1111"], registry)[0])

    assert empty["candidate_registered_of_total"] == "0/0"
    assert empty["candidate_coverage_pct"] == "n/a"  # 0% と区別する
    assert missing["candidate_registered_of_total"] == "not_recorded"
    assert missing["candidate_coverage_pct"] == "not_recorded"
    assert missing["holdings_coverage_pct"] == "100.0"
    assert missing["total_registered_of_total"] == "1/1"  # 渡された軸だけの合計


def test_both_axes_missing_records_no_total(caplog: pytest.LogCaptureFixture) -> None:
    fields = _fields(_record(caplog, None, None, _Registry(["1111"]))[0])

    assert fields["total_registered_of_total"] == "not_recorded"
    assert fields["total_coverage_pct"] == "not_recorded"


def test_an_empty_registry_is_zero_percent_not_an_error(caplog: pytest.LogCaptureFixture) -> None:
    records = _record(caplog, ["1111"], ["2222"], _Registry([]))

    assert [r.levelno for r in records] == [logging.INFO]
    fields = _fields(records[0])
    assert fields["candidate_coverage_pct"] == "0.0"
    assert fields["holdings_coverage_pct"] == "0.0"
    assert fields["registry_entries"] == "0"


def test_duplicate_codes_are_counted_once(caplog: pytest.LogCaptureFixture) -> None:
    """同じ銘柄が重複して渡されても(複数ownerの保有等)、銘柄コード単位で1回と数える。"""
    fields = _fields(
        _record(caplog, ["1111", "1111", "2222"], ["1111", "1111", "1111"], _Registry(["1111"]))[0]
    )

    assert fields["candidate_registered_of_total"] == "1/2"
    assert fields["holdings_registered_of_total"] == "1/1"


# =============================================================================
# WARNING なし・識別子を出さない
# =============================================================================


@pytest.mark.parametrize(
    ("candidates", "holdings", "registry_codes"),
    [
        (["1111"], ["2222"], []),  # 0% / 0%
        ([], [], ["1111"]),  # 分母 0
        (None, None, []),
        (["1111", "2222"], ["1111"], ["1111", "2222"]),  # 100%
    ],
    ids=["zero_percent", "empty_denominators", "nothing_given", "full"],
)
def test_it_never_warns_regardless_of_the_coverage(
    caplog: pytest.LogCaptureFixture,
    candidates: Any,
    holdings: Any,
    registry_codes: list[str],
) -> None:
    """threshold = NONE: coverage がどれだけ低くても WARNING / ERROR を出さない。"""
    records = _record(caplog, candidates, holdings, _Registry(registry_codes))

    assert [r.levelno for r in records] == [logging.INFO]


def test_the_log_contains_no_stock_code_or_identifier(caplog: pytest.LogCaptureFixture) -> None:
    records = _record(caplog, ["1234", "5678"], ["4321"], _Registry(["1234", "9999"]))

    text = " ".join(r.getMessage() for r in records)
    for code in ("1234", "5678", "4321", "9999"):
        assert code not in text
    assert "owner" not in text and "holding_id" not in text


# =============================================================================
# fail-soft(3パターン)
# =============================================================================


def test_a_failing_registry_read_does_not_raise_and_leaves_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    records = _record(caplog, ["1111"], ["2222"], _FailingRegistry())

    assert [r.levelno for r in records] == [logging.ERROR]
    message = records[0].getMessage()
    assert _FAILED_EVENT in message and "error_type=RuntimeError" in message
    assert "synthetic" not in message and "1111" not in message  # 例外の内容・識別子は出さない
    assert not [r for r in records if r.getMessage().startswith(_EVENT)]  # 通常の記録は出ない


@pytest.mark.parametrize(
    ("candidates", "holdings"),
    [(123, ["1111"]), (["1111"], 3.5), ([["unhashable"]], None), (None, [{"a": 1}])],
    ids=["candidates_not_iterable", "holdings_not_iterable", "unhashable_candidate", "unhashable"],
)
def test_invalid_inputs_do_not_raise_and_leave_an_error(
    caplog: pytest.LogCaptureFixture, candidates: Any, holdings: Any
) -> None:
    records = _record(caplog, candidates, holdings, _Registry(["1111"]))

    assert [r.levelno for r in records] == [logging.ERROR]
    assert _FAILED_EVENT in records[0].getMessage()


def test_a_failure_while_writing_the_info_log_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ログ出力自体の失敗(handler の不具合等)でも、例外を外へ出さず、ERROR を残そうとする。"""
    real_info = registry_module.logger.info

    def _boom(*args: Any, **kwargs: Any) -> None:
        if args and "shareholder_benefit_registry_coverage" in str(args[0]):
            raise OSError("log sink failed")
        real_info(*args, **kwargs)

    monkeypatch.setattr(registry_module.logger, "info", _boom)

    records = _record(caplog, ["1111"], ["2222"], _Registry(["1111"]))

    assert [r.levelno for r in records] == [logging.ERROR]
    assert "error_type=OSError" in records[0].getMessage()


def test_the_call_returns_none_in_every_case() -> None:
    assert check_registry_coverage(["1111"], ["2222"], service=_Registry(["1111"])) is None  # type: ignore[func-returns-value]
    assert check_registry_coverage(None, None, service=_FailingRegistry()) is None  # type: ignore[arg-type,func-returns-value]


# =============================================================================
# 読み取り専用・追加の外部読み込みは list_all() 1回のみ
# =============================================================================


def test_only_list_all_is_called_exactly_once_on_the_registry() -> None:
    registry = _Registry(["1111"])

    check_registry_coverage(["1111"], ["2222"], service=registry)  # type: ignore[arg-type]

    assert registry.list_all_calls == 1  # list_all 以外の属性は __getattr__ が拒否する


def _function_source_nodes() -> ast.FunctionDef:
    tree = ast.parse(Path(registry_module.__file__).read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "check_registry_coverage"
    )


def test_the_function_references_no_write_publish_invoke_or_other_reads() -> None:
    """★ call graph(この関数の本体が参照する名前)に、write・publish・invoke・LINE 送信・
    他の repository / service の読み込みが無い(CLAUDE.md §3: 名前だけを根拠に read-only としない)。

    見ているのは、この関数の本体が直接参照する名前に限る。`list_all()` の先の実装
    (読み取り専用であることは #120 のテストが固定する)は別の検査の責務である。
    """
    names = {
        node.id for node in ast.walk(_function_source_nodes()) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(_function_source_nodes()) if isinstance(node, ast.Attribute)
    }

    forbidden = {
        "save",
        "put",
        "put_item",
        "update",
        "delete",
        "register",
        "publish",
        "publish_incident_envelope",
        "invoke",
        "dispatch_async",
        "push_message",
        "PortfolioService",
        "WatchlistService",
        "list_holdings",
        "list_items",
        "boto3",
    }
    assert names & forbidden == set()
    assert "list_all" in names


def test_the_coverage_function_does_not_change_the_health_check() -> None:
    """既存の check_registry_health() の署名・呼び出し契約は変えていない(#675 の通知を含む)。"""
    import inspect

    parameters = list(inspect.signature(registry_module.check_registry_health).parameters)
    assert parameters == ["min_expected_entries", "service", "now"]


# =============================================================================
# 2つの handler: 読み込み済みの銘柄コードを渡すだけ
# =============================================================================


@pytest.mark.parametrize(
    ("handler_file", "expected_keywords"),
    [
        ("buy_candidates_handler.py", {"candidate_codes", "holding_codes"}),
        ("holdings_watchlist_handler.py", {"candidate_codes", "holding_codes"}),
    ],
)
def test_each_handler_calls_the_coverage_check_exactly_once_with_keywords(
    handler_file: str, expected_keywords: set[str]
) -> None:
    tree = ast.parse(
        (_REPO_ROOT / "src" / "jstock_advisor" / "lambda_handlers" / handler_file).read_text(
            encoding="utf-8"
        )
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "check_registry_coverage"
    ]

    assert len(calls) == 1
    assert {kw.arg for kw in calls[0].keywords} == expected_keywords
    assert calls[0].args == []


def test_the_holdings_handler_does_not_record_the_candidate_side() -> None:
    """保有監視バッチは候補(watchlist)を読み込まない(WatchlistTable の権限が無い可能性があるため、
    候補側は買い候補バッチでのみ記録する)。candidate_codes に None を渡している。"""
    tree = ast.parse(
        (
            _REPO_ROOT
            / "src"
            / "jstock_advisor"
            / "lambda_handlers"
            / "holdings_watchlist_handler.py"
        ).read_text(encoding="utf-8")
    )
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "check_registry_coverage"
    )
    candidate = next(kw.value for kw in call.keywords if kw.arg == "candidate_codes")

    assert isinstance(candidate, ast.Constant) and candidate.value is None


# =============================================================================
# ★ 実際の handler 呼び出し経路(本物の handler.handler を通す)
# =============================================================================

_SCHEDULED_EVENT = {"scheduled_time": "2026-07-28T23:00:00Z"}  # JST 2026-07-29(水)08:00 = 営業日


def _holding(stock_code: str) -> Any:
    from decimal import Decimal

    from jstock_advisor.domain.entities.enums import AccountType
    from jstock_advisor.domain.entities.holding import Holding
    from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id

    now = dt.datetime(2026, 7, 29, 7, 0, tzinfo=dt.UTC)
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=now,
        updated_at=now,
    )


def _drive_buy_candidates(
    monkeypatch: pytest.MonkeyPatch,
    watchlist_codes: list[str],
    holding_codes: list[str],
    *,
    batch_started: bool = True,
    registry: Any = None,
) -> tuple[Any, list[dict[str, Any]], list[Any]]:
    from jstock_advisor.lambda_handlers import buy_candidates_handler as module
    from tests.unit import test_issue_558_batch_id_idempotency as helpers

    helpers._patch_buy_candidates_common(monkeypatch)
    items = [helpers._watchlist_item(code) for code in watchlist_codes]
    holdings = [_holding(code) for code in holding_codes]
    monkeypatch.setattr(module.WatchlistService, "list_items", lambda self: items)
    monkeypatch.setattr(module.PortfolioService, "list_holdings", lambda self: holdings)
    monkeypatch.setattr(module, "check_registry_health", lambda *a, **k: None)
    monkeypatch.setattr(module, "start_batch", lambda *a, **k: batch_started)
    dispatched: list[Any] = []
    monkeypatch.setattr(module, "dispatch_async", lambda name, payload: dispatched.append(payload))
    coverage_calls: list[dict[str, Any]] = []
    if registry is not None:
        # 実際の check_registry_coverage を通す(registry だけを差し替える)
        monkeypatch.setattr(registry_module, "ShareholderBenefitRegistryService", lambda: registry)
    else:
        monkeypatch.setattr(
            module, "check_registry_coverage", lambda **kwargs: coverage_calls.append(kwargs)
        )
    result = module.handler(_SCHEDULED_EVENT, helpers._FakeBuyContext())
    return result, coverage_calls, dispatched


def test_buy_candidates_handler_passes_the_loaded_codes_by_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watchlist のみ / 保有のみ / 両方 の銘柄が、候補側・保有側へ正しく振り分けて渡される
    (両方に登録された銘柄は、2つの軸の両方に数える)。"""
    result, calls, _ = _drive_buy_candidates(monkeypatch, ["1111", "2222"], ["2222", "3333"])

    assert result == {"dispatched": 3}
    assert len(calls) == 1
    assert sorted(calls[0]["candidate_codes"]) == ["1111", "2222"]
    assert sorted(calls[0]["holding_codes"]) == ["2222", "3333"]


def test_buy_candidates_handler_result_is_the_same_with_and_without_the_coverage_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判定・dispatch は変わらない: coverage を差し替えても、実際の coverage を通しても同じ結果。"""
    stubbed, _, stubbed_dispatch = _drive_buy_candidates(monkeypatch, ["1111"], ["3333"])
    real, _, real_dispatch = _drive_buy_candidates(
        monkeypatch, ["1111"], ["3333"], registry=_Registry(["1111"])
    )

    assert stubbed == real == {"dispatched": 2}
    assert len(stubbed_dispatch) == len(real_dispatch) == 2


def test_buy_candidates_handler_does_not_record_coverage_for_a_duplicate_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 batch の2回目の開始(Scheduler retry 等)では、記録も dispatch もしない。"""
    result, calls, dispatched = _drive_buy_candidates(
        monkeypatch, ["1111"], ["3333"], batch_started=False
    )

    assert result == {"dispatched": 0, "skipped": "duplicate_batch_start"}
    assert calls == [] and dispatched == []


def test_buy_candidates_handler_survives_a_failing_registry_and_still_dispatches(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """★ fail-soft を handler の経路で確認: registry の読み込みが失敗しても dispatch は完了する。"""
    with caplog.at_level(logging.INFO):
        result, _, dispatched = _drive_buy_candidates(
            monkeypatch, ["1111"], ["3333"], registry=_FailingRegistry()
        )

    assert result == {"dispatched": 2}
    assert len(dispatched) == 2
    assert any(_FAILED_EVENT in r.getMessage() for r in caplog.records)


def test_holdings_handler_passes_only_the_holding_side(monkeypatch: pytest.MonkeyPatch) -> None:
    from jstock_advisor.lambda_handlers import holdings_watchlist_handler as module
    from tests.unit import test_issue_558_batch_id_idempotency as helpers

    helpers._patch_holdings_watchlist_common(monkeypatch)
    holdings = [_holding("2222"), _holding("2222"), _holding("3333")]  # 同じ銘柄を複数 owner が保有
    monkeypatch.setattr(module.PortfolioService, "list_holdings", lambda self: holdings)
    monkeypatch.setattr(module, "check_registry_health", lambda *a, **k: None)
    monkeypatch.setattr(module, "start_batch", lambda *a, **k: True)
    monkeypatch.setattr(module, "dispatch_async", lambda name, payload: None)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "check_registry_coverage", lambda **kwargs: calls.append(kwargs))

    result = module.handler(_SCHEDULED_EVENT, helpers._FakeHoldingsContext())

    assert len(calls) == 1
    assert calls[0]["candidate_codes"] is None
    assert sorted(set(calls[0]["holding_codes"])) == ["2222", "3333"]
    assert "dispatched_holdings" in result
