"""資金入替の観測 P の handler・Scan の proxy・ports・mode の設定の契約テスト(Issue #604 P-b4 A1)。

対象: `lambda_handlers/rotation_observation_handler`・
`infrastructure/aws/rotation_observation_scan`・`services/rotation_observation_ports`・`services/rotation_observation_config`・
`config/rotation_observation.yaml`。

設計: #604 P-b4 統合文書(…6188895158)・USER 決定 #604 …6195577714(A1・A2 は追加の確認なしに
実装・テスト・PR してよい。★ 出荷は Scheduler = DISABLED かつ mode = OFF ★。template は A2)。
★ 実際の書き手・実際の AuditService で moto に書き、呼ばれた API 操作を数える ★
(fake の形を信じない)。

  1  mode の設定: 出荷値 OFF・OFF / SHADOW だけ・不正は OFF へ縮退(ACTIVE は存在しない)
  2  Scan の proxy: `scan` だけを通す(reader の proxy とは別)/ 1 表・200 件・ページ・時間の上限・
     部分結果にしない
  3  ports: 表の論理名が実際の書き手と一致 / 営業日は既存の BusinessCalendar
  4  AuditLog の sink: 条件付き PutItem・先勝ち・呼ぶ API は PutItem だけ
  5  handler: OFF は ports を作らない / failed は例外 / 通し(moto)で呼ぶ API 操作の完全一致
  6  構造(AST): 環境変数を読まない・時計は handler の 1 か所・書込・LINE・秘密へ到達しない
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.client import BaseClient
from moto import mock_aws

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.signals.rotation_observation import MAX_HOLDINGS
from jstock_advisor.domain.signals.rotation_observation_record import (
    CONTRACT_VERSION,
    RECORD_KIND,
)
from jstock_advisor.domain.signals.rotation_observation_schedule import (
    DEFAULT_SCHEDULED_TIME,
    latest_safe_run_at,
    scheduled_at,
)
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws import rotation_observation_scan as scan_module
from jstock_advisor.infrastructure.aws.audit_shadow_reader import ReadOnlyViolationError
from jstock_advisor.infrastructure.aws.rotation_observation_scan import (
    ALLOWED_SCAN_OPERATIONS,
    MAX_SCAN_PAGES,
    SCAN_PAGE_LIMIT,
    HoldingIdScanner,
    RotationScanClient,
)
from jstock_advisor.infrastructure.collection_store import resolve_table_name
from jstock_advisor.infrastructure.local_repository.holding_evaluation_record_repository import (
    _TABLE_FILE_NAME as RECORDS_FILE_OF_REPOSITORY,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    PRODUCTION_FILE_NAME as RECOMMENDATIONS_FILE_OF_REPOSITORY,
)
from jstock_advisor.lambda_handlers import rotation_observation_handler as handler_module
from jstock_advisor.services import rotation_observation_ports as ports_module
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.rotation_observation_config import (
    RotationObservationConfig,
    load_rotation_observation_config,
    off_config,
)
from jstock_advisor.services.rotation_observation_ports import (
    AuditObservationSink,
    BusinessCalendarAdapter,
    load_business_calendar,
    remaining_seconds_from,
)
from jstock_advisor.services.rotation_observation_runner import (
    TOTAL_BUDGET_SECONDS,
    WRITE_RESERVE_SECONDS,
    RotationObserverMode,
)
from tests.unit.test_rotation_observation_reader_contract import (
    _END,
    _INSIDE,
    _START,
    _save_recommendation,
    _save_record,
    _utc,
    _write_batch,
)

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_SHIPPED_CONFIG_DIR = _REPO / "config"
_NEW_MODULES = {
    "handler": _PACKAGE / "lambda_handlers" / "rotation_observation_handler.py",
    "scan": _PACKAGE / "infrastructure" / "aws" / "rotation_observation_scan.py",
    "ports": _PACKAGE / "services" / "rotation_observation_ports.py",
    "config": _PACKAGE / "services" / "rotation_observation_config.py",
}

_REGION = "ap-northeast-1"
_JST = dt.timezone(dt.timedelta(hours=9))
_DAY = dt.date(2026, 10, 14)  # 水曜日(営業日)
_NOW = dt.datetime(2026, 10, 14, 0, 30, tzinfo=dt.UTC)  # JST 09:30(最後の回の後)
_EARLY = dt.datetime(2026, 10, 13, 23, 15, tzinfo=dt.UTC)  # JST 08:15(最後の回の前)
_SATURDAY = dt.datetime(2026, 10, 17, 0, 30, tzinfo=dt.UTC)
_SHADOW = RotationObservationConfig(mode=RotationObserverMode.SHADOW)
_ALLOWED_OPERATIONS = {"GetItem", "BatchGetItem", "Query", "Scan", "PutItem"}


# =================================================================================================
# 1 mode の設定
# =================================================================================================


def test_the_shipped_mode_is_off() -> None:
    assert load_rotation_observation_config(_SHIPPED_CONFIG_DIR).mode is RotationObserverMode.OFF
    assert off_config().mode is RotationObserverMode.OFF


def test_the_modes_are_only_off_and_shadow() -> None:
    assert {m.value for m in RotationObserverMode} == {"OFF", "SHADOW"}  # ACTIVE は存在しない
    assert "ACTIVE" not in {m.name for m in RotationObserverMode}


def _config_dir(tmp_path: Path, text: str | None) -> Path:
    if text is not None:
        (tmp_path / "rotation_observation.yaml").write_text(text, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('version: 1\nmode: "SHADOW"\n', RotationObserverMode.SHADOW),
        ('version: 1\nmode: "OFF"\n', RotationObserverMode.OFF),
        ("", RotationObserverMode.OFF),  # 空
        (None, RotationObserverMode.OFF),  # ファイルが無い
        ("mode: [unclosed\n", RotationObserverMode.OFF),  # 壊れた YAML
        ("- a\n- b\n", RotationObserverMode.OFF),  # 辞書でない
        ("mode: OFF\n", RotationObserverMode.OFF),  # 引用符なし = 真偽値 False(検証に失敗 → OFF)
        ("mode: on\n", RotationObserverMode.OFF),  # 真偽値 True(SHADOW にならない)
        ('mode: "ACTIVE"\n', RotationObserverMode.OFF),  # 未知の mode
        ('mode: "shadow"\n', RotationObserverMode.OFF),  # 大文字小文字を丸めない
        ('mode: "SHADOW"\nextra: 1\n', RotationObserverMode.OFF),  # 余計なキー
        ('mode: "SHADOW"\nversion: "x"\n', RotationObserverMode.OFF),  # 型違い
    ],
)
def test_the_loader_never_turns_a_bad_config_into_an_active_one(
    tmp_path: Path, text: str | None, expected: RotationObserverMode
) -> None:
    assert load_rotation_observation_config(_config_dir(tmp_path, text)).mode is expected


def test_the_loader_does_not_raise_and_does_not_log_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    leak_marker = "SENTINEL-PATH-AND-VALUE-777"
    with caplog.at_level("WARNING"):
        config = load_rotation_observation_config(_config_dir(tmp_path, f'mode: "{leak_marker}"\n'))
    assert config.mode is RotationObserverMode.OFF
    assert leak_marker not in caplog.text and str(tmp_path) not in caplog.text
    assert "ValidationError" in caplog.text  # 原因の型だけ


# =================================================================================================
# 2 Scan の proxy
# =================================================================================================


class _FakeScan:
    """Scan だけを持つ fake client。呼び出しの引数を記録する。"""

    def __init__(self, pages: list[dict[str, Any] | Exception]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    def scan(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def _items(*ids: str) -> list[dict[str, Any]]:
    return [{"holding_id": {"S": holding_id}} for holding_id in ids]


def _scanner(fake: _FakeScan, remaining: Callable[[], float] = lambda: 1000.0) -> HoldingIdScanner:
    return HoldingIdScanner(
        RotationScanClient(fake),
        "table-x",
        remaining_seconds=remaining,
        reserve_seconds=WRITE_RESERVE_SECONDS,
    )


def test_the_allowlist_is_exactly_scan() -> None:
    assert frozenset({"scan"}) == ALLOWED_SCAN_OPERATIONS


@pytest.mark.parametrize(
    "operation",
    [
        "get_item",
        "batch_get_item",
        "query",
        "put_item",
        "update_item",
        "delete_item",
        "batch_write_item",
        "transact_write_items",
        "describe_table",
        "list_tables",
        "create_table",
        "delete_table",
        "invoke",
        "get_paginator",
        "meta",
    ],
)
def test_every_operation_other_than_scan_is_refused(operation: str) -> None:
    client = RotationScanClient(_FakeScan([]))

    with pytest.raises(ReadOnlyViolationError):
        getattr(client, operation)


def test_the_scan_request_is_one_table_keys_only_and_bounded() -> None:
    fake = _FakeScan([{"Items": _items("owner-a#0001", "owner-a#0002")}])

    assert _scanner(fake).holding_ids() == ["owner-a#0001", "owner-a#0002"]
    assert fake.calls == [
        {"TableName": "table-x", "ProjectionExpression": "holding_id", "Limit": SCAN_PAGE_LIMIT}
    ]
    assert SCAN_PAGE_LIMIT == MAX_HOLDINGS + 1


def test_the_scan_follows_the_continuation_key_and_joins_the_pages() -> None:
    key = {"holding_id": {"S": "owner-a#0002"}}
    fake = _FakeScan(
        [
            {"Items": _items("owner-a#0001", "owner-a#0002"), "LastEvaluatedKey": key},
            {"Items": _items("owner-a#0003")},
        ]
    )

    assert _scanner(fake).holding_ids() == ["owner-a#0001", "owner-a#0002", "owner-a#0003"]
    assert "ExclusiveStartKey" not in fake.calls[0]
    assert fake.calls[1]["ExclusiveStartKey"] == key


def test_more_than_the_limit_stops_at_limit_plus_one_and_reads_nothing_more() -> None:
    ids = [f"owner-a#{i:04d}" for i in range(MAX_HOLDINGS + 1)]
    fake = _FakeScan([{"Items": _items(*ids), "LastEvaluatedKey": {"holding_id": {"S": "x"}}}])

    result = _scanner(fake).holding_ids()

    assert result is not None and len(result) == MAX_HOLDINGS + 1  # 上限超過の合図
    assert len(fake.calls) == 1  # 続きを読まない


def test_exactly_the_limit_is_not_over_the_limit() -> None:
    ids = [f"owner-a#{i:04d}" for i in range(MAX_HOLDINGS)]
    fake = _FakeScan([{"Items": _items(*ids)}])

    result = _scanner(fake).holding_ids()

    assert result is not None and len(result) == MAX_HOLDINGS


def test_a_scan_that_cannot_finish_within_the_page_limit_is_unreadable() -> None:
    key = {"holding_id": {"S": "k"}}
    pages: list[dict[str, Any] | Exception] = [
        {"Items": _items(f"owner-a#{i}"), "LastEvaluatedKey": key} for i in range(MAX_SCAN_PAGES)
    ]
    fake = _FakeScan(pages)

    assert _scanner(fake).holding_ids() is None  # 部分結果を返さない
    assert len(fake.calls) == MAX_SCAN_PAGES


def test_the_time_budget_is_checked_before_every_page() -> None:
    key = {"holding_id": {"S": "k"}}
    fake = _FakeScan([{"Items": _items("owner-a#1"), "LastEvaluatedKey": key}, {"Items": []}])
    remaining = iter([100.0, float(WRITE_RESERVE_SECONDS)])  # 2 ページ目の前に尽きる

    assert _scanner(fake, lambda: next(remaining)).holding_ids() is None
    assert len(fake.calls) == 1

    fake2 = _FakeScan([{"Items": []}])
    assert _scanner(fake2, lambda: 0.0).holding_ids() is None
    assert fake2.calls == []  # 尽きていれば 1 回も呼ばない


@pytest.mark.parametrize(
    "page",
    [
        {"Items": [{"holding_id": {"N": "1"}}]},  # 型が違う
        {"Items": [{"holding_id": {"S": ""}}]},  # 空
        {"Items": [{}]},  # キーなし
        {"Items": ["not-a-dict"]},
        "not-a-dict",
    ],
)
def test_a_broken_row_makes_the_whole_listing_unreadable(page: Any) -> None:
    fake = _FakeScan([page])

    assert _scanner(fake).holding_ids() is None


def test_a_failing_scan_is_unreadable_and_leaks_nothing(caplog: pytest.LogCaptureFixture) -> None:
    leak_marker = "SENTINEL-HOLDING-ID-31337"
    fake = _FakeScan([RuntimeError(leak_marker)])

    with caplog.at_level("DEBUG"):
        assert _scanner(fake).holding_ids() is None
    assert leak_marker not in caplog.text


@pytest.fixture
def holdings_table(
    monkeypatch: pytest.MonkeyPatch,
    lambda_runtime_env: None,
    create_collection_table: Callable[..., None],
) -> Iterator[str]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        create_collection_table(ports_module.HOLDINGS_FILE, "holding_id")
        yield resolve_table_name(ports_module.HOLDINGS_FILE)


def test_the_scanner_lists_the_ids_of_a_real_table(holdings_table: str) -> None:
    client = boto3.client("dynamodb", region_name=_REGION)
    for number in range(3):
        client.put_item(
            TableName=holdings_table,
            Item={
                "holding_id": {"S": f"owner-a#{number:04d}"},
                "data": {"S": json.dumps({"large": "x" * 50})},
            },
        )
    scanner = HoldingIdScanner(
        RotationScanClient(client),
        holdings_table,
        remaining_seconds=lambda: 1000.0,
        reserve_seconds=WRITE_RESERVE_SECONDS,
    )

    assert sorted(scanner.holding_ids() or []) == [f"owner-a#{n:04d}" for n in range(3)]


# =================================================================================================
# 3 ports
# =================================================================================================


def test_the_table_names_are_the_real_writers_names() -> None:
    assert ports_module.BATCH_RUNS_FILE == batch_tracker._TABLE_FILE_NAME
    assert ports_module.EVALUATION_RECORDS_FILE == RECORDS_FILE_OF_REPOSITORY
    assert ports_module.RECOMMENDATIONS_FILE == RECOMMENDATIONS_FILE_OF_REPOSITORY
    holding_repository = (
        _PACKAGE / "infrastructure" / "local_repository" / "holding_repository.py"
    ).read_text(encoding="utf-8")
    assert f'"{ports_module.HOLDINGS_FILE}"' in holding_repository
    audit_repository = (
        _PACKAGE / "infrastructure" / "local_repository" / "audit_log_repository.py"
    ).read_text(encoding="utf-8")
    assert f'"{ports_module.AUDIT_LOG_FILE}"' in audit_repository


def test_the_decision_type_is_the_record_kind() -> None:
    assert ports_module.DECISION_TYPE == RECORD_KIND == "capital_rotation_observation"


def test_the_business_day_is_the_existing_calendar() -> None:
    calendar = load_business_calendar(_SHIPPED_CONFIG_DIR)
    adapter = BusinessCalendarAdapter(calendar)

    assert adapter.is_business_day(dt.date(2026, 10, 14)) is True  # 水曜日
    assert adapter.is_business_day(dt.date(2026, 10, 17)) is False  # 土曜日
    assert adapter.is_business_day(dt.date(2026, 10, 12)) is False  # スポーツの日(祝日)
    for day in (dt.date(2026, 10, 14), dt.date(2026, 10, 17), dt.date(2026, 10, 12)):
        assert adapter.is_business_day(day) is calendar.is_business_day(day)


def test_an_unreadable_holiday_calendar_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_business_calendar(tmp_path)  # 『全日が営業日』にしない


def test_the_remaining_time_counts_down_from_the_total_budget() -> None:
    now = [100.0]
    remaining = remaining_seconds_from(TOTAL_BUDGET_SECONDS, 100.0, lambda: now[0])

    assert remaining() == TOTAL_BUDGET_SECONDS
    now[0] = 130.0
    assert remaining() == TOTAL_BUDGET_SECONDS - 30


# =================================================================================================
# 4 AuditLog の sink
# =================================================================================================


class _Recorder:
    """botocore の API 呼出(操作名・表名)を記録する。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = BaseClient._make_api_call
        recorder = self

        def wrapper(client: Any, operation_name: str, api_params: Any) -> Any:
            table = api_params.get("TableName") if isinstance(api_params, dict) else None
            recorder.calls.append((operation_name, table))
            return original(client, operation_name, api_params)

        monkeypatch.setattr(BaseClient, "_make_api_call", wrapper)

    def snapshot(self) -> _Recorder:
        """この時点までの呼出の写し(後の確認用の読取を数えない)。"""
        copy = _Recorder()
        copy.calls = list(self.calls)
        return copy

    def operations(self) -> set[str]:
        return {operation for operation, _ in self.calls}

    def tables_of(self, operation: str) -> set[str | None]:
        return {table for op, table in self.calls if op == operation}


@pytest.fixture
def aws_tables(
    monkeypatch: pytest.MonkeyPatch,
    lambda_runtime_env: None,
    create_collection_table: Callable[..., None],
) -> Iterator[None]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        create_collection_table(ports_module.BATCH_RUNS_FILE, "batch_id")
        create_collection_table(ports_module.HOLDINGS_FILE, "holding_id")
        create_collection_table(ports_module.RECOMMENDATIONS_FILE, "recommendation_id")
        create_collection_table(ports_module.AUDIT_LOG_FILE, "audit_id")
        boto3.client("dynamodb", region_name=_REGION).create_table(
            TableName=resolve_table_name(ports_module.EVALUATION_RECORDS_FILE),
            KeySchema=[{"AttributeName": "holding_evaluation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "holding_evaluation_id", "AttributeType": "S"},
                {"AttributeName": "holding_id", "AttributeType": "S"},
                {"AttributeName": "evaluated_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "holding_id-index",
                    "KeySchema": [
                        {"AttributeName": "holding_id", "KeyType": "HASH"},
                        {"AttributeName": "evaluated_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _audit_item(audit_id: str) -> dict[str, Any] | None:
    table = boto3.resource("dynamodb", region_name=_REGION).Table(
        resolve_table_name(ports_module.AUDIT_LOG_FILE)
    )
    return table.get_item(Key={"audit_id": audit_id}).get("Item")


def test_the_sink_writes_one_conditional_put_and_the_first_one_wins(
    aws_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    recorder.install(monkeypatch)
    sink = AuditObservationSink(AuditService(), lambda: _NOW)
    audit_id = f"{RECORD_KIND}:2026-10-14"

    assert sink.record_if_absent(audit_id, {"schema_version": 1, "marker": "first"}) is True
    assert sink.record_if_absent(audit_id, {"schema_version": 1, "marker": "second"}) is False

    seen = recorder.snapshot()  # 以降の確認用の読取を数えない
    entry = AuditLogEntry.model_validate_json(_audit_item(audit_id)["data"])  # type: ignore[index]
    assert entry.output_values["marker"] == "first"  # 先勝ち
    assert entry.decision_type == "capital_rotation_observation"
    assert entry.rule_version == CONTRACT_VERSION
    assert entry.stock_code is None and entry.timestamp == _NOW
    # ★ 呼んだ API は PutItem だけ(AuditLog の表のみ)★
    assert seen.operations() == {"PutItem"}
    assert seen.tables_of("PutItem") == {resolve_table_name(ports_module.AUDIT_LOG_FILE)}


# =================================================================================================
# 5 handler
# =================================================================================================


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float | dt.datetime]:
    state: dict[str, float | dt.datetime] = {"now": _NOW, "mono": 1000.0, "step": 0.0}

    def monotonic() -> float:
        state["mono"] = float(state["mono"]) + float(state["step"])  # type: ignore[arg-type]
        return float(state["mono"])

    monkeypatch.setattr(handler_module, "_utc_now", lambda: state["now"])
    monkeypatch.setattr(handler_module, "_monotonic", monotonic)
    return state


def _use_mode(monkeypatch: pytest.MonkeyPatch, config: RotationObservationConfig) -> None:
    monkeypatch.setattr(handler_module, "load_rotation_observation_config", lambda: config)


def test_off_builds_no_port_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    _use_mode(monkeypatch, off_config())

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("OFF must not build any port")

    monkeypatch.setattr(handler_module, "_build_observer", boom)
    monkeypatch.setattr(handler_module, "AuditService", boom)
    monkeypatch.setattr(handler_module, "load_business_calendar", boom)

    assert handler_module.handler({}, None) == {"kind": "NOT_RUN_OFF"}


def test_the_shipped_config_runs_nothing(monkeypatch: pytest.MonkeyPatch, clock: Any) -> None:
    """出荷の設定(OFF)のまま handler を呼んでも、ports を作らない。"""
    monkeypatch.setattr(
        handler_module,
        "load_rotation_observation_config",
        lambda: load_rotation_observation_config(_SHIPPED_CONFIG_DIR),
    )
    monkeypatch.setattr(
        handler_module,
        "_build_observer",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not build")),
    )

    assert handler_module.handler({}, None)["kind"] == "NOT_RUN_OFF"


@pytest.mark.parametrize("key", ["execution_mode", "notification_mode"])
def test_a_manual_execution_mode_is_rejected_loudly(
    key: str, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    _use_mode(monkeypatch, off_config())

    with pytest.raises(Exception, match="rotation observation"):
        handler_module.handler({key: "VALIDATION"}, None)


def test_a_failed_run_fails_the_lambda_with_the_kind_only(
    monkeypatch: pytest.MonkeyPatch, clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    _use_mode(monkeypatch, _SHADOW)
    leak_marker = "SENTINEL-WRITE-ERROR-9"

    class _Sink:
        def record_if_absent(self, *_args: Any) -> bool:
            raise RuntimeError(leak_marker)

    class _Batch:
        def get(self, _batch_id: str) -> Any:
            return None

    from jstock_advisor.services.rotation_observation_runner import (
        RotationObserverPorts,
        make_rotation_observer,
    )

    class _Calendar:
        def is_business_day(self, _day: dt.date) -> bool:
            return True

    class _Holdings:
        def inputs_for(self, *_a: Any, **_k: Any) -> Any:
            return None

    observer = make_rotation_observer(
        RotationObserverPorts(
            _Batch(),
            _Holdings(),
            _Sink(),
            _Calendar(),
            lambda: 1000.0,  # type: ignore[arg-type]
        ),
        last_run_time=handler_module.LAST_RUN_TIME,
    )
    monkeypatch.setattr(handler_module, "_build_observer", lambda _started: observer)

    with (
        caplog.at_level("DEBUG"),
        pytest.raises(handler_module.RotationObservationFailedError) as raised,
    ):
        handler_module.handler({}, None)

    assert str(raised.value) == "WRITE_FAILED"
    assert leak_marker not in caplog.text and leak_marker not in repr(raised.value)


def _prepare_day(complete: bool = True, holdings: int = 2) -> None:
    client = boto3.client("dynamodb", region_name=_REGION)
    for number in range(holdings):
        holding_id = f"owner-a#{number:04d}"
        client.put_item(
            TableName=resolve_table_name(ports_module.HOLDINGS_FILE),
            Item={"holding_id": {"S": holding_id}, "data": {"S": "{}"}},
        )
        if complete:
            recommendation = _save_recommendation(
                f"{number + 1:04d}",
                RecommendationType.HOLD if number % 2 == 0 else RecommendationType.SELL,
            )
            _save_record(
                holding_id,
                _utc(_INSIDE),
                authoritative_recommendation_id=recommendation.recommendation_id,
            )
    if complete:
        _write_batch(_batch_id(), _START, _END)


def _batch_id() -> str:
    from jstock_advisor.domain.signals.rotation_observation_schedule import holdings_batch_id

    return holdings_batch_id(_DAY)


def test_the_whole_run_against_real_writers_uses_only_the_approved_operations(
    aws_tables: None, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    _prepare_day()
    _use_mode(monkeypatch, _SHADOW)
    recorder = _Recorder()
    recorder.install(monkeypatch)
    audit_table = resolve_table_name(ports_module.AUDIT_LOG_FILE)

    first = handler_module.handler({}, None)
    second = handler_module.handler({}, None)

    seen = recorder.snapshot()  # 以降の確認用の読取を数えない
    assert first == {"kind": "WRITTEN", "day_status": "OBSERVED"}
    assert second == {"kind": "ALREADY_PRESENT", "day_status": "OBSERVED"}  # 先勝ち・重複しない
    stored = _audit_item(f"{RECORD_KIND}:2026-10-14")
    assert stored is not None
    values = AuditLogEntry.model_validate_json(stored["data"]).output_values
    assert values["day_status"] == "OBSERVED" and values["holdings_total"] == 2
    assert values["unreadable"] == {"record_read_failed": 0, "recommendation_read_failed": 0}
    # ★ 呼んだ API 操作の完全一致(読取 4 種 + AuditLog への PutItem だけ)★
    assert seen.operations() == _ALLOWED_OPERATIONS
    assert seen.operations() <= _ALLOWED_OPERATIONS
    assert seen.tables_of("PutItem") == {audit_table}  # 書込は AuditLog だけ
    assert seen.tables_of("Scan") == {resolve_table_name(ports_module.HOLDINGS_FILE)}
    assert seen.tables_of("Query") == {resolve_table_name(ports_module.EVALUATION_RECORDS_FILE)}
    assert seen.tables_of("GetItem") == {resolve_table_name(ports_module.BATCH_RUNS_FILE)}
    assert not seen.operations() & {
        "UpdateItem",
        "DeleteItem",
        "BatchWriteItem",
        "TransactWriteItems",
        "DescribeTable",
        "ListTables",
    }


def test_a_non_business_day_reads_and_writes_nothing(
    aws_tables: None, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    _prepare_day()
    _use_mode(monkeypatch, _SHADOW)
    clock["now"] = _SATURDAY
    recorder = _Recorder()
    recorder.install(monkeypatch)

    assert handler_module.handler({}, None) == {
        "kind": "NOT_RUN_NOT_BUSINESS_DAY",
        "day_status": None,
    }
    assert recorder.calls == []  # 平日の休場日に gap を作らない・AWS へ 1 回も触れない


def test_an_unfinished_batch_before_the_last_run_is_deferred_and_writes_nothing(
    aws_tables: None, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    _prepare_day(complete=False)
    _use_mode(monkeypatch, _SHADOW)
    clock["now"] = _EARLY  # 08:15 JST(最後の回の前)
    recorder = _Recorder()
    recorder.install(monkeypatch)

    result = handler_module.handler({}, None)

    assert result["kind"] == "DEFERRED"
    assert "PutItem" not in recorder.operations()  # 一時的な状態が日を固定しない
    assert _audit_item(f"{RECORD_KIND}:2026-10-14") is None


def test_an_exhausted_time_budget_at_the_last_run_writes_a_gap(
    aws_tables: None, monkeypatch: pytest.MonkeyPatch, clock: Any
) -> None:
    _prepare_day()
    _use_mode(monkeypatch, _SHADOW)
    clock["step"] = float(TOTAL_BUDGET_SECONDS)  # 総予算が最初の確認で尽きる
    recorder = _Recorder()
    recorder.install(monkeypatch)

    result = handler_module.handler({}, None)

    assert result["kind"] == "GAP_WRITTEN"
    assert recorder.operations() == {"PutItem"}  # 読めない日。部分結果にせず gap だけ書く
    assert _audit_item(f"{RECORD_KIND}_gap:2026-10-14") is not None


def test_the_last_run_is_inside_the_batch_run_ttl_and_matches_the_final_schedule() -> None:
    last = handler_module.LAST_RUN_TIME
    assert last == dt.time(9, 0, 0)
    assert scheduled_at(_DAY, last) <= latest_safe_run_at(_DAY, DEFAULT_SCHEDULED_TIME)


def test_the_run_fits_the_lambda_timeout_with_margin() -> None:
    """総予算 50 秒 ≦ Timeout 60 秒の 0.9(A2 の template の Timeout と照合する前提)。"""
    assert TOTAL_BUDGET_SECONDS <= 60 * 0.9


# =================================================================================================
# 6 構造(AST)
# =================================================================================================


def _tree(name: str) -> ast.Module:
    return ast.parse(_NEW_MODULES[name].read_text(encoding="utf-8"))


def _called(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def _names(tree: ast.Module) -> set[str]:
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }


@pytest.mark.parametrize("name", sorted(_NEW_MODULES))
def test_no_new_module_reads_the_environment(name: str) -> None:
    assert not _names(_tree(name)) & {"environ", "getenv"}


@pytest.mark.parametrize("name", ["scan", "ports", "config"])
def test_only_the_handler_reads_a_clock(name: str) -> None:
    """時計は注入された関数だけを呼ぶ(`now()` 等の呼出も time の import も持たない)。"""
    tree = _tree(name)
    attribute_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not attribute_calls & {"now", "utcnow", "today", "monotonic", "time", "sleep"}
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not imported & {"time", "datetime.datetime"}


def test_the_handler_reads_the_clock_in_exactly_two_functions() -> None:
    tree = _tree("handler")
    holders = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        and any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr in {"now", "monotonic"}
            for c in ast.walk(fn)
        )
    }
    assert holders == {"_utc_now", "_monotonic"}


def test_the_scan_module_calls_no_write_and_no_other_read() -> None:
    called = _called(_tree("scan"))
    assert "scan" in called
    assert not called & {
        "put_item",
        "update_item",
        "delete_item",
        "batch_write_item",
        "transact_write_items",
        "get_item",
        "batch_get_item",
        "query",
        "describe_table",
        "invoke",
    }


def test_the_ports_module_has_no_direct_write_call_except_through_the_audit_service() -> None:
    called = _called(_tree("ports"))
    assert "record_if_absent" in called
    assert not called & {"put_item", "update_item", "delete_item", "record", "save", "insert"}


def _handlers_of(tree: ast.Module) -> int:
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "Exception"
    )


def test_the_broad_except_is_confined_to_the_two_swallowing_modules() -> None:
    assert _handlers_of(_tree("scan")) == 1  # 失敗は『読めない』へ
    assert _handlers_of(_tree("config")) == 1  # 不正な設定は OFF へ
    assert _handlers_of(_tree("ports")) == 0
    assert _handlers_of(_tree("handler")) == 0  # 例外は握りつぶさず Lambda を失敗させる


def test_the_new_modules_never_reach_notification_or_secret_code() -> None:
    from tests.unit.test_issue_606_production_connection_gate import _module_files, _reachable

    known = _module_files()
    closure = _reachable({"jstock_advisor.lambda_handlers.rotation_observation_handler"}, known)
    forbidden = (
        "jstock_advisor.infrastructure.line",
        "jstock_advisor.providers",
        "jstock_advisor.services.line_notification_service",
        "jstock_advisor.infrastructure.aws.secrets",
    )
    assert not [m for m in closure if m.startswith(forbidden)]
    assert not [m for m in closure if "line" in m.rsplit(".", 1)[-1].split("_")]
    assert not [m for m in closure if any(w in m for w in ("edinet", "secret", "github"))]
    # 空振りでない: 実際に AuditLog・reader・Scan の proxy へは到達している
    assert "jstock_advisor.services.audit_service" in closure
    assert "jstock_advisor.infrastructure.aws.rotation_observation_scan" in closure
    services = sorted(m for m in closure if m.startswith("jstock_advisor.services."))
    assert services == [
        "jstock_advisor.services.audit_service",
        "jstock_advisor.services.rotation_observation_config",
        "jstock_advisor.services.rotation_observation_inputs",
        "jstock_advisor.services.rotation_observation_ports",
        "jstock_advisor.services.rotation_observation_runner",
    ]


def test_the_module_names_and_limits_are_unchanged() -> None:
    assert scan_module.MAX_SCAN_PAGES == 3
    assert MAX_HOLDINGS == 200
