"""Issue #833 E2: `jstock weekly-aggregate` の backend 明示指定(local / dynamodb)の契約テスト。

確認するもの:
* 既定は local。環境変数 `AWS_LAMBDA_FUNCTION_NAME` だけでは Production の表に触れない
  (設定されていれば local を fail-closed で拒否する)。
* dynamodb は (a) --backend dynamodb / (b) --aws-region / (c) --confirm-table(表名の完全一致)の
  どれか 1 つが欠けても、1 件も読まない・書かない。
* write には --execute(verify は --mark-rebuild-required)が別に必要。無ければ書込 API を呼ばない。
* moto(実際の AWS を呼ばない)で、dry-run が read-only・execute が書く・冪等・verify・rebuild。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import boto3
import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import weekly_aggregate
from jstock_advisor.cli.main import app
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvaluationLabel,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.aws.dynamodb_store import DynamoDbCollectionStore
from jstock_advisor.infrastructure.aws.weekly_evaluation_aggregate_dynamodb import (
    DynamoWeeklyEvaluationAggregateStore,
)
from tests.factories import build_recommendation

_REGION = "ap-northeast-1"
_AGG = "jstock-weekly_evaluation_aggregate"
_EVAL = "jstock-evaluation_results"
_REC = "jstock-recommendations"
_NOW = dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.UTC)
_HORIZON = 7
_W38_DAY = dt.date(2026, 9, 16)
_W37_DAY = dt.date(2026, 9, 9)
_RUNNER = CliRunner()


def _args(
    command: str,
    *extra: str,
    backend: str | None = "dynamodb",
    region: str | None = _REGION,
    confirm: str | None = _AGG,
    prefix: str | None = None,
    profile: str | None = None,
) -> list[str]:
    out = ["weekly-aggregate", command]
    if backend is not None:
        out += ["--backend", backend]
    if region is not None:
        out += ["--aws-region", region]
    if confirm is not None:
        out += ["--confirm-table", confirm]
    if prefix is not None:
        out += ["--table-prefix", prefix]
    if profile is not None:
        out += ["--aws-profile", profile]
    return out + list(extra)


def _seed(
    evaluations: DynamoDbCollectionStore[EvaluationResult],
    recommendations: DynamoDbCollectionStore[Recommendation],
) -> None:
    for idx, day in enumerate([_W38_DAY, _W38_DAY, _W37_DAY], start=1):
        recommendations.upsert(
            build_recommendation(
                recommendation_id=f"rec-{idx}",
                stock_code="1234",
                stock_name="test",
                recommended_at=dt.datetime.combine(
                    day - dt.timedelta(days=_HORIZON), dt.time(3, 0), tzinfo=dt.UTC
                ),
                recommendation_type=RecommendationType.BUY,
                price_at_recommendation=Decimal("1000"),
                confidence=ConfidenceLevel.HIGH,
                rule_version="v1",
            )
        )
        evaluations.upsert(
            EvaluationResult(
                evaluation_id=f"ev-{idx}",
                recommendation_id=f"rec-{idx}",
                horizon_calendar_days=_HORIZON,
                evaluated_at=_NOW,
                evaluation_date=day,
                price_at_evaluation=Decimal("1010"),
                price_return_pct=1.0,
                excess_return_pct=0.5,
                evaluation_label=EvaluationLabel.SUCCESS,
                label_evidence="x",
            )
        )


class Aws:
    """moto の 3 表と、表の中身を読むヘルパ。"""

    def __init__(self) -> None:
        self.client = boto3.client("dynamodb", region_name=_REGION)

    def scan(self, table: str) -> list[dict[str, Any]]:
        items = self.client.scan(TableName=table).get("Items", [])
        return sorted(items, key=lambda i: str(sorted(i.items())))

    def aggregate_rows(self) -> list[dict[str, Any]]:
        """集計行(更新時刻 `updated_at` は実行ごとに変わるため比較から除く)。"""
        return [
            {k: v for k, v in i.items() if k != "updated_at"}
            for i in self.scan(_AGG)
            if i["item_key"]["S"].startswith("AGG#")
        ]


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[Aws]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    # `setup_default_session` が書き換える process 全体の既定 session を、テスト後に元へ戻す。
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        for name, key in (("evaluation_id", _EVAL), ("recommendation_id", _REC)):
            client.create_table(
                TableName=key,
                KeySchema=[{"AttributeName": name, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        client.create_table(
            TableName=_AGG,
            KeySchema=[
                {"AttributeName": "review_week", "KeyType": "HASH"},
                {"AttributeName": "item_key", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "review_week", "AttributeType": "S"},
                {"AttributeName": "item_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        _seed(
            DynamoDbCollectionStore(EvaluationResult, _EVAL, "evaluation_id"),
            DynamoDbCollectionStore(Recommendation, _REC, "recommendation_id"),
        )
        # seed 用に作った session を捨て、CLI が自分で作る session だけを検証対象にする。
        monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
        yield Aws()


@pytest.fixture
def forbid_writes(monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[str]]:
    """Aggregate ストアの書込メソッドが呼ばれたら記録する(呼ばれたら失敗させる)。"""
    calls: list[str] = []

    def _trap(name: str) -> Callable[..., Any]:
        def inner(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"write API called: {name}")

        return inner

    for name in (
        "replace_week",
        "set_backfill_complete",
        "mark_rebuild_required",
        "commit_evaluation",
    ):
        monkeypatch.setattr(DynamoWeeklyEvaluationAggregateStore, name, _trap(name))
    return lambda: calls


def _invoke(args: list[str]) -> Any:
    return _RUNNER.invoke(app, args)


# ---- local が既定・Production を暗黙に選ばない ---------------------------------------------


def test_default_backend_is_local_and_never_builds_the_dynamodb_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    built: list[str] = []

    def fake_dynamodb(selection: Any) -> Any:
        built.append("dynamodb")
        raise AssertionError("dynamodb service must not be built by default")

    monkeypatch.setattr(weekly_aggregate, "_dynamodb_service", fake_dynamodb)
    result = _invoke(["weekly-aggregate", "backfill"])  # backend 指定なし
    assert result.exit_code == 0, result.output
    assert "backend=local" in result.output
    assert "mode=DRY_RUN" in result.output
    assert built == []


@pytest.mark.parametrize("command", ["backfill", "verify", "rebuild"])
def test_local_is_rejected_when_lambda_function_name_is_set(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """環境変数 AWS_LAMBDA_FUNCTION_NAME だけで Production の表を読み書きできてしまう経路を塞ぐ。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "pretend")
    touched: list[str] = []
    monkeypatch.setattr(weekly_aggregate, "_service", lambda: touched.append("local"))
    monkeypatch.setattr(weekly_aggregate, "_dynamodb_service", lambda s: touched.append("aws"))
    extra = ["--week", "2026-W38"] if command == "rebuild" else []
    result = _invoke(["weekly-aggregate", command, *extra])
    assert result.exit_code != 0
    assert "AWS_LAMBDA_FUNCTION_NAME" in result.output
    assert touched == []


def test_dynamodb_only_options_are_rejected_in_local_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    touched: list[str] = []
    monkeypatch.setattr(weekly_aggregate, "_service", lambda: touched.append("local"))
    for opt, value in (
        ("--aws-region", _REGION),
        ("--confirm-table", _AGG),
        ("--aws-profile", "p"),
        ("--table-prefix", "x"),
    ):
        result = _invoke(["weekly-aggregate", "backfill", opt, value])
        assert result.exit_code != 0, opt
    assert touched == []


def test_unknown_backend_value_is_rejected_not_treated_as_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []
    monkeypatch.setattr(weekly_aggregate, "_service", lambda: touched.append("local"))
    result = _invoke(["weekly-aggregate", "backfill", "--backend", "prod"])
    assert result.exit_code != 0
    assert touched == []


# ---- dynamodb は 3 条件が揃わなければ 1 件も読まない・書かない -------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"backend": None},  # (a) 明示なし = local。dynamodb のオプションが残るので拒否
        {"region": None},  # (b)
        {"confirm": None},  # (c)
        {"confirm": "jstock-evaluation_results"},  # (c) 別の表名
        {"confirm": "jstock-weekly_evaluation_aggregate "},  # (c) 完全一致でない
        {"confirm": "JSTOCK-WEEKLY_EVALUATION_AGGREGATE"},
        {"prefix": "other"},  # prefix を変えたのに confirm が jstock- のまま
    ],
)
@pytest.mark.parametrize("command", ["backfill", "verify", "rebuild"])
def test_dynamodb_requires_all_three_conditions_and_touches_nothing(
    command: str, overrides: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    touched: list[str] = []
    monkeypatch.setattr(weekly_aggregate, "_service", lambda: touched.append("local"))
    monkeypatch.setattr(weekly_aggregate, "_dynamodb_service", lambda s: touched.append("aws"))
    extra = ["--week", "2026-W38"] if command == "rebuild" else []
    result = _invoke(_args(command, *extra, **overrides))
    assert result.exit_code != 0, result.output
    assert touched == []


def test_confirm_table_error_does_not_echo_the_expected_table_name() -> None:
    result = _invoke(_args("backfill", confirm="something-else"))
    assert result.exit_code != 0
    assert _AGG not in result.output


def test_missing_confirm_table_is_reported_differently_from_a_mismatch() -> None:
    missing = _invoke(_args("backfill", confirm=None))
    mismatch = _invoke(_args("backfill", confirm="something-else"))
    assert missing.exit_code != 0 and mismatch.exit_code != 0
    assert "入力が必要" in missing.output
    assert "一致しません" in mismatch.output
    assert "入力が必要" not in mismatch.output


def test_table_prefix_changes_the_expected_confirm_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def fake(selection: Any) -> Any:
        seen.append(selection)
        raise RuntimeError("stop after selection")

    monkeypatch.setattr(weekly_aggregate, "_dynamodb_service", fake)
    _invoke(_args("backfill", prefix="stg", confirm="stg-weekly_evaluation_aggregate"))
    assert [s.aggregate_table for s in seen] == ["stg-weekly_evaluation_aggregate"]
    assert seen[0].evaluation_table == "stg-evaluation_results"
    assert seen[0].recommendation_table == "stg-recommendations"


def test_aws_profile_is_applied_through_the_default_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(boto3, "setup_default_session", lambda **kw: calls.append(kw))
    monkeypatch.setattr(
        weekly_aggregate, "DynamoDbCollectionStore", lambda *a, **k: (_ for _ in ()).throw(KeyError)
    )
    _invoke(_args("backfill", profile="observer-x"))
    assert calls == [{"profile_name": "observer-x", "region_name": _REGION}]


# ---- moto: dry-run は read-only ---------------------------------------------------------


def test_dynamodb_dry_run_backfill_is_read_only(
    aws: Aws, forbid_writes: Callable[[], list[str]]
) -> None:
    before = {t: aws.scan(t) for t in (_AGG, _EVAL, _REC)}
    result = _invoke(_args("backfill"))
    assert result.exit_code == 0, result.output
    out = result.output
    assert "backend=dynamodb" in out
    assert f"aws_region={_REGION}" in out
    assert f"aggregate_table={_AGG}" in out
    assert "writes=NO(read-only)" in out
    assert "mode=DRY_RUN" in out
    assert "scanned_evaluations=3" in out
    assert "matched_evaluations=3" in out
    assert "weeks=2 (2026-W37 .. 2026-W38)" in out
    assert forbid_writes() == []
    assert {t: aws.scan(t) for t in (_AGG, _EVAL, _REC)} == before


def test_dynamodb_verify_without_mark_is_read_only(
    aws: Aws, forbid_writes: Callable[[], list[str]]
) -> None:
    before = aws.scan(_AGG)
    result = _invoke(_args("verify"))
    # Aggregate が空なので不一致(終了コード 1)になるが、書込 API は呼ばない。
    assert result.exit_code == 1, result.output
    assert "writes=NO(read-only)" in result.output
    assert forbid_writes() == []
    assert aws.scan(_AGG) == before


def test_dynamodb_rebuild_without_execute_is_read_only(
    aws: Aws, forbid_writes: Callable[[], list[str]]
) -> None:
    result = _invoke(_args("rebuild", "--week", "2026-W38"))
    assert result.exit_code == 0, result.output
    assert "mode=DRY_RUN" in result.output
    assert "2026-W38: aggregate_rows=1" in result.output
    assert forbid_writes() == []
    assert aws.scan(_AGG) == []


# ---- moto: execute は期待どおり書く -------------------------------------------------------


def test_dynamodb_execute_backfill_writes_expected_items_and_is_idempotent(aws: Aws) -> None:
    result = _invoke(_args("backfill", "--execute"))
    assert result.exit_code == 0, result.output
    assert "writes=YES" in result.output
    assert "mode=EXECUTE" in result.output
    rows = aws.aggregate_rows()
    assert len(rows) == 2  # 週 2 つ × BUY × v1
    by_week = {r["review_week"]["S"]: r for r in rows}
    assert set(by_week) == {"2026-W37", "2026-W38"}
    control = [i for i in aws.scan(_AGG) if i["review_week"]["S"] == "#CONTROL"]
    assert len(control) == 1 and control[0]["status"]["S"] == "COMPLETE"
    snapshot = aws.scan(_AGG)
    # 再実行しても二重加算しない(Put による上書き)。
    again = _invoke(_args("backfill", "--execute"))
    assert again.exit_code == 0, again.output
    assert [(i["review_week"], i["item_key"]) for i in aws.scan(_AGG)] == [
        (i["review_week"], i["item_key"]) for i in snapshot
    ]
    assert aws.aggregate_rows() == rows
    # raw の評価は一切変更しない。
    assert len(aws.scan(_EVAL)) == 3


def test_dynamodb_verify_after_backfill_is_consistent(aws: Aws) -> None:
    assert _invoke(_args("backfill", "--execute")).exit_code == 0
    result = _invoke(_args("verify"))
    assert result.exit_code == 0, result.output
    assert "consistent=True" in result.output


def test_dynamodb_verify_mismatch_is_reported_and_marks_only_with_flag(aws: Aws) -> None:
    assert _invoke(_args("backfill", "--execute")).exit_code == 0
    victim = next(r for r in aws.aggregate_rows() if r["review_week"]["S"] == "2026-W38")
    aws.client.delete_item(
        TableName=_AGG,
        Key={"review_week": victim["review_week"], "item_key": victim["item_key"]},
    )
    before = aws.scan(_AGG)
    plain = _invoke(_args("verify", "--week", "2026-W38"))
    assert plain.exit_code == 1, plain.output
    assert "MISMATCH 2026-W38" in plain.output
    assert aws.scan(_AGG) == before  # --mark-rebuild-required が無ければ書かない
    marked = _invoke(_args("verify", "--week", "2026-W38", "--mark-rebuild-required"))
    assert marked.exit_code == 1, marked.output
    assert "writes=YES" in marked.output
    assert "marked_rebuild_required=2026-W38" in marked.output
    assert aws.scan(_AGG) != before


def test_dynamodb_rebuild_execute_restores_a_week(aws: Aws) -> None:
    assert _invoke(_args("backfill", "--execute")).exit_code == 0
    expected = aws.aggregate_rows()
    victim = next(r for r in expected if r["review_week"]["S"] == "2026-W38")
    aws.client.delete_item(
        TableName=_AGG,
        Key={"review_week": victim["review_week"], "item_key": victim["item_key"]},
    )
    result = _invoke(_args("rebuild", "--week", "2026-W38", "--execute"))
    assert result.exit_code == 0, result.output
    assert aws.aggregate_rows() == expected


def test_dynamodb_backend_does_not_depend_on_the_lambda_environment_variable(aws: Aws) -> None:
    """dynamodb は AWS_LAMBDA_FUNCTION_NAME が無くても動き、実行後に環境へ残さない。"""
    import os

    assert "AWS_LAMBDA_FUNCTION_NAME" not in os.environ
    assert _invoke(_args("backfill", "--execute")).exit_code == 0
    assert "AWS_LAMBDA_FUNCTION_NAME" not in os.environ
