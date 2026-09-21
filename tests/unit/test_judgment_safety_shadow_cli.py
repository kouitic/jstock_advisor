"""Issue #458(#160 PR-4): shadow監査記録の集計CLI(`jstock judgment-safety-shadow report`)と、
read-only保証(write / save / update / delete / invoke / 通知へ到達しないことの固定)。

不変条件を固定する:
  * `--source`の既定はlocal(Productionを既定で読まない)。dynamodbのときは、read-onlyであること・
    対象テーブルを標準エラーへ明示する。
  * `--describe-only`はscanしない。`--metrics-only`はshadowの集計を出さない。
  * 終了コード: 0 = 正常(0件でも0)/ 2 = 引数不正 / 3 = 読み取り失敗。
  * 新規3モジュールは、write系の名前・invoke・通知・監査の書き込みサービスを参照しない(AST)。
  * local経路は`AuditLogRepository`の読み取りだけを使い、save / upsert / deleteを呼ばない。

★ 銘柄コードは実在しない0000系のみ。Productionへは一切アクセスしない(fake clientのみ)。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import judgment_safety_shadow as cli_module
from jstock_advisor.cli.main import app as main_app
from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.infrastructure.aws.audit_shadow_reader import ReadOnlyDynamoClient

_runner = CliRunner()
_ROOT = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_NEW_MODULES = (
    _ROOT / "infrastructure" / "aws" / "audit_shadow_reader.py",
    _ROOT / "services" / "judgment_safety_shadow_report.py",
    _ROOT / "cli" / "judgment_safety_shadow.py",
)


def _data(audit_id: str, decision_type: str = "judgment_safety_shadow") -> str:
    return json.dumps(
        {
            "audit_id": audit_id,
            "timestamp": "2026-09-24T00:00:00+00:00",
            "stock_code": "0000",
            "decision_type": decision_type,
            "input_values": {
                "schema_version": 1,
                "engine": "BUY_CANDIDATES",
                "buy_action": "BUY",
            },
            "calculation_formulas": {},
            "output_values": {
                "findings": [
                    {
                        "condition_id": "G2",
                        "reason_code": "STALE_FINANCIALS",
                        "would_suppress": True,
                    }
                ],
                "not_evaluated": ["G1"],
                "strong": True,
            },
            "data_sources": [],
            "rule_version": "v1",
        }
    )


class _FakeClient:
    def __init__(self, *, fail_scan: bool = False) -> None:
        self.scan_calls = 0
        self.describe_calls = 0
        self._fail_scan = fail_scan

    def scan(self, **_kwargs: Any) -> dict[str, Any]:
        self.scan_calls += 1
        if self._fail_scan:
            raise RuntimeError("接続失敗(架空)")
        return {
            "Items": [
                {"audit_id": {"S": "1"}, "data": {"S": _data("1")}},
                {"audit_id": {"S": "2"}, "data": {"S": _data("2", "buy_signal")}},
            ],
            "ScannedCount": 2,
            "Count": 2,
            "ConsumedCapacity": {"CapacityUnits": 1.0},
        }

    def describe_table(self, **_kwargs: Any) -> dict[str, Any]:
        self.describe_calls += 1
        return {"Table": {"ItemCount": 80_000, "TableSizeBytes": 155_000_000}}

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"許可外の操作が呼ばれた: {name}")


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()
    monkeypatch.setattr(cli_module, "build_read_only_client", lambda: ReadOnlyDynamoClient(fake))
    return fake


def _entry(audit_id: str = "1") -> AuditLogEntry:
    return AuditLogEntry.model_validate_json(_data(audit_id))


def test_default_source_is_local_and_never_builds_an_aws_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _must_not_build() -> None:
        raise AssertionError("既定(local)でAWSのclientを作った")

    monkeypatch.setattr(cli_module, "build_read_only_client", _must_not_build)

    class _Repo:
        def list_by_decision_type(self, decision_type: str) -> list[AuditLogEntry]:
            assert decision_type == "judgment_safety_shadow"
            return [_entry()]

    monkeypatch.setattr(cli_module, "AuditLogRepository", _Repo)

    result = _runner.invoke(main_app, ["judgment-safety-shadow", "report", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["SHADOW_RESULT_METRICS"]["shadow_record_count"] == 1
    assert payload["STORAGE_READ_METRICS"]["shadow_matched"] == 1


def test_local_source_only_calls_read_methods_of_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Repo:
        def list_by_decision_type(self, decision_type: str) -> list[AuditLogEntry]:
            return []

        def save(self, *_a: object, **_k: object) -> None:
            raise AssertionError("saveが呼ばれた")

        def upsert(self, *_a: object, **_k: object) -> None:
            raise AssertionError("upsertが呼ばれた")

        def delete(self, *_a: object, **_k: object) -> None:
            raise AssertionError("deleteが呼ばれた")

    monkeypatch.setattr(cli_module, "AuditLogRepository", _Repo)

    result = _runner.invoke(main_app, ["judgment-safety-shadow", "report"])

    assert result.exit_code == 0
    assert "shadow_record_count: 0" in result.output  # 0件でも正常終了し、0件を表示する
    assert "問題なし" in result.output


def test_dynamodb_source_announces_read_only_and_the_target_table(
    fake_client: _FakeClient,
) -> None:
    result = _runner.invoke(
        main_app, ["judgment-safety-shadow", "report", "--source", "dynamodb", "--json"]
    )

    assert result.exit_code == 0
    assert "read-only" in result.output
    assert "jstock-audit_log" in result.output
    assert "書き込みは行いません" in result.output


def test_dynamodb_report_json_schema(fake_client: _FakeClient) -> None:
    result = _runner.invoke(
        main_app, ["judgment-safety-shadow", "report", "--source", "dynamodb", "--json"]
    )

    body = result.output[result.output.index("{") :]
    payload = json.loads(body)
    shadow = payload["SHADOW_RESULT_METRICS"]
    storage = payload["STORAGE_READ_METRICS"]
    assert shadow["shadow_record_count"] == 1
    assert shadow["condition_counts"]["G2"]["findings"] == 1
    assert shadow["not_evaluated_counts"]["G1"] == 1
    assert storage["audit_total_examined"] == 2
    assert storage["shadow_matched"] == 1
    assert storage["consumed_capacity"] == {"total_rru": 1.0}
    assert storage["table_metrics"]["item_count"] == 80_000
    assert storage["baseline"]["records"] == 78_700
    assert fake_client.scan_calls == 1


def test_describe_only_never_scans(fake_client: _FakeClient) -> None:
    result = _runner.invoke(
        main_app,
        ["judgment-safety-shadow", "report", "--source", "dynamodb", "--describe-only", "--json"],
    )

    assert result.exit_code == 0
    assert fake_client.scan_calls == 0
    assert fake_client.describe_calls == 1
    payload = json.loads(result.output[result.output.index("{") :])
    assert "SHADOW_RESULT_METRICS" not in payload
    assert "consumed_capacity" not in payload["STORAGE_READ_METRICS"]


def test_metrics_only_omits_the_shadow_result_section(fake_client: _FakeClient) -> None:
    result = _runner.invoke(
        main_app,
        ["judgment-safety-shadow", "report", "--source", "dynamodb", "--metrics-only", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output[result.output.index("{") :])
    assert "SHADOW_RESULT_METRICS" not in payload
    assert payload["STORAGE_READ_METRICS"]["consumed_capacity"] == {"total_rru": 1.0}
    assert fake_client.scan_calls == 1  # metrics-onlyでもscanは行う


def test_describe_only_with_local_source_is_an_argument_error() -> None:
    result = _runner.invoke(main_app, ["judgment-safety-shadow", "report", "--describe-only"])

    assert result.exit_code == 2


def test_invalid_date_is_an_argument_error() -> None:
    result = _runner.invoke(main_app, ["judgment-safety-shadow", "report", "--from", "2026/09/24"])

    assert result.exit_code == 2


def test_read_failure_exits_with_code_3_and_reports_only_the_error_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(fail_scan=True)
    monkeypatch.setattr(cli_module, "build_read_only_client", lambda: ReadOnlyDynamoClient(fake))

    result = _runner.invoke(main_app, ["judgment-safety-shadow", "report", "--source", "dynamodb"])

    assert result.exit_code == 3
    assert "RuntimeError" in result.output
    assert "接続失敗" not in result.output  # 例外メッセージ本体は出さない


def test_full_dynamodb_run_touches_only_scan_and_describe_table(fake_client: _FakeClient) -> None:
    """scan / describe_table以外の全メソッドが呼ばれたら失敗するclientで、CLI全体を実行する。"""
    result = _runner.invoke(main_app, ["judgment-safety-shadow", "report", "--source", "dynamodb"])

    assert result.exit_code == 0
    assert fake_client.scan_calls == 1 and fake_client.describe_calls == 1


_FORBIDDEN_NAMES = frozenset(
    {
        "save",
        "upsert",
        "delete",
        "insert_if_absent",
        "record_if_absent",
        "put_item",
        "update_item",
        "delete_item",
        "batch_write_item",
        "transact_write_items",
        "invoke",
        "send_message",
        "publish",
        "put_object",
        "AuditService",
        "DynamoDbCollectionStore",
        "LineClient",
        "LineNotificationService",
    }
)


def _referenced_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[-1] for alias in node.names)
    return names


@pytest.mark.parametrize("path", _NEW_MODULES, ids=lambda p: p.name)
def test_new_modules_do_not_reference_write_or_side_effect_names(path: Path) -> None:
    assert path.exists()

    assert _referenced_names(path) & _FORBIDDEN_NAMES == set()


def test_only_the_reader_may_touch_the_dynamo_client_and_only_via_the_allowlist() -> None:
    """`boto3`をimportするのはreaderだけ。service / cliはclientへ直接触れない。"""
    for path in _NEW_MODULES[1:]:
        assert "boto3" not in _referenced_names(path)
