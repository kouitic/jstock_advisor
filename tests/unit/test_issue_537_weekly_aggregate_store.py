"""Issue #537: 週次評価集計(WeeklyEvaluationAggregate)の永続化の契約テスト。

同じ契約テストを、ローカル実装と DynamoDB 実装(moto)の**両方**へ流す。
確認するもの: 増分加算 / 冪等(二重加算しない)/ 遅延評価は evaluation_date の週へ /
marker(REVIEW_RECOMPUTE_PENDING)と rebuild(AGGREGATE_REBUILD_REQUIRED)が別状態 /
再生成中に新しい評価が届いたときに marker が残る / 指定週だけの置き換え(条件付き)/
集計の更新に失敗したら EvaluationResult も保存されない(Q-2)/ Scan を行わない。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.enums import EvaluationLabel, RecommendationType
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.weekly_evaluation_aggregate import (
    WeeklyEvaluationAggregate,
    aggregate_item_key,
    review_week_label,
)
from jstock_advisor.infrastructure.aws.weekly_evaluation_aggregate_dynamodb import (
    DynamoWeeklyEvaluationAggregateStore,
)
from jstock_advisor.infrastructure.weekly_evaluation_aggregate_store import (
    LocalWeeklyEvaluationAggregateStore,
    WeeklyEvaluationAggregateStore,
)

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.UTC)
_BUY = RecommendationType.BUY
_RV = "buy-v1"
# 2026-09-14(月)〜09-20(日)= 2026-W38 / 2026-09-07(月)〜= 2026-W37
_W38 = dt.date(2026, 9, 16)
_W37 = dt.date(2026, 9, 9)
_WEEK38 = "2026-W38"
_WEEK37 = "2026-W37"


def _eval(
    idx: int,
    evaluation_date: dt.date = _W38,
    label: EvaluationLabel = EvaluationLabel.SUCCESS,
    price_return_pct: float = 1.5,
    excess_return_pct: float | None = 0.5,
) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=f"ev-{idx}",
        recommendation_id=f"rec-{idx}",
        horizon_calendar_days=7,
        evaluated_at=_NOW,
        evaluation_date=evaluation_date,
        price_at_evaluation=Decimal("100"),
        price_return_pct=price_return_pct,
        excess_return_pct=excess_return_pct,
        evaluation_label=label,
        label_evidence="test",
    )


@dataclass
class Harness:
    store: WeeklyEvaluationAggregateStore
    saved_ids: Callable[[], set[str]]
    kind: str


@pytest.fixture(params=["local", "dynamodb"])
def harness(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    if request.param == "local":
        saved: set[str] = set()

        def insert(evaluation: EvaluationResult) -> bool:
            if evaluation.evaluation_id in saved:
                return False
            saved.add(evaluation.evaluation_id)
            return True

        yield Harness(LocalWeeklyEvaluationAggregateStore(insert), lambda: set(saved), "local")
        return
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName="jstock-evaluation_results",
            KeySchema=[{"AttributeName": "evaluation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "evaluation_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="jstock-weekly_evaluation_aggregate",
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
        store = DynamoWeeklyEvaluationAggregateStore(
            "jstock-weekly_evaluation_aggregate", "jstock-evaluation_results", client
        )

        def saved_ids() -> set[str]:
            items = client.scan(TableName="jstock-evaluation_results").get("Items", [])
            return {item["evaluation_id"]["S"] for item in items}

        yield Harness(store, saved_ids, "dynamodb")


def _commit(h: Harness, evaluation: EvaluationResult, rv: str = _RV) -> bool:
    return h.store.commit_evaluation(evaluation, _BUY, rv, _NOW)


def _only_row(h: Harness, week: str = _WEEK38) -> WeeklyEvaluationAggregate:
    rows = h.store.query_week(week)
    assert len(rows) == 1
    return rows[0]


# --- 増分加算 ---------------------------------------------------------------


def test_incremental_commit_accumulates_counts_and_sums(harness: Harness) -> None:
    assert _commit(harness, _eval(1, label=EvaluationLabel.SUCCESS, price_return_pct=2.0))
    assert _commit(harness, _eval(2, label=EvaluationLabel.INCONCLUSIVE, price_return_pct=-1.0))
    assert _commit(
        harness, _eval(3, label=EvaluationLabel.EARLY, price_return_pct=0.5, excess_return_pct=None)
    )

    row = _only_row(harness)
    assert row.sample_count == 3
    assert row.conclusive_count == 2  # INCONCLUSIVE は分母から除外
    assert row.success_count == 1
    assert row.price_return_count == 3
    assert row.price_return_sum == Decimal("1.5")
    assert row.excess_return_count == 2  # None は除く
    assert row.excess_return_sum == Decimal("1.0")
    assert row.label_counts == {"SUCCESS": 1, "INCONCLUSIVE": 1, "EARLY": 1}
    assert row.recommendation_type is _BUY
    assert row.rule_version == _RV
    assert harness.saved_ids() == {"ev-1", "ev-2", "ev-3"}


def test_rows_are_separated_by_recommendation_type_and_rule_version(harness: Harness) -> None:
    assert harness.store.commit_evaluation(_eval(1), _BUY, "v1", _NOW)
    assert harness.store.commit_evaluation(_eval(2), _BUY, "v2", _NOW)
    assert harness.store.commit_evaluation(_eval(3), RecommendationType.SELL, "v1", _NOW)

    keys = {row.item_key for row in harness.store.query_week(_WEEK38)}
    assert keys == {
        aggregate_item_key(_BUY, "v1"),
        aggregate_item_key(_BUY, "v2"),
        aggregate_item_key(RecommendationType.SELL, "v1"),
    }


# --- 冪等(二重加算しない) --------------------------------------------------


def test_reprocessing_the_same_evaluation_does_not_double_count(harness: Harness) -> None:
    evaluation = _eval(1)
    assert _commit(harness, evaluation) is True
    assert _commit(harness, evaluation) is False
    assert _commit(harness, evaluation) is False

    row = _only_row(harness)
    assert row.sample_count == 1
    assert row.price_return_sum == Decimal("1.5")
    # marker も 1 回分だけ(再処理で進まない)
    assert harness.store.get_state(_WEEK38).mark_seq == 1


# --- 遅延評価 -----------------------------------------------------------------


def test_late_arrival_goes_to_the_week_of_evaluation_date(harness: Harness) -> None:
    """evaluated_at(処理した日時)ではなく evaluation_date の週へ加算する。"""
    assert review_week_label(_W37) == _WEEK37
    assert _commit(harness, _eval(1, evaluation_date=_W37))  # evaluated_at は W38 の日付

    assert harness.store.query_week(_WEEK38) == []
    assert _only_row(harness, _WEEK37).sample_count == 1
    assert harness.store.list_pending_weeks() == [_WEEK37]


# --- marker: REVIEW_RECOMPUTE_PENDING ----------------------------------------


def test_marker_is_recorded_atomically_with_the_aggregate(harness: Harness) -> None:
    assert harness.store.list_pending_weeks() == []
    assert _commit(harness, _eval(1, evaluation_date=_W38))
    assert _commit(harness, _eval(2, evaluation_date=_W37))

    assert harness.store.list_pending_weeks() == [_WEEK37, _WEEK38]
    assert harness.store.get_state(_WEEK38).recompute_pending


def test_finish_recompute_clears_the_marker(harness: Harness) -> None:
    assert _commit(harness, _eval(1))
    state = harness.store.get_state(_WEEK38)

    assert harness.store.finish_recompute(_WEEK38, state.mark_seq) is True

    assert harness.store.list_pending_weeks() == []
    assert not harness.store.get_state(_WEEK38).recompute_pending


def test_new_evaluation_during_recompute_keeps_the_marker(harness: Harness) -> None:
    """★ 再生成中(mark_seq を読んでから完了を記録するまで)に新しい評価が届いたら、marker を残す。"""
    assert _commit(harness, _eval(1))
    seen = harness.store.get_state(_WEEK38).mark_seq
    assert _commit(harness, _eval(2))  # 再生成の間に届いた評価

    assert harness.store.finish_recompute(_WEEK38, seen) is False

    assert harness.store.list_pending_weeks() == [_WEEK38]
    assert harness.store.get_state(_WEEK38).recompute_pending


# --- rebuild: AGGREGATE_REBUILD_REQUIRED(marker とは別状態) -------------------


def test_rebuild_required_is_a_separate_state_from_recompute_pending(harness: Harness) -> None:
    assert _commit(harness, _eval(1))
    harness.store.finish_recompute(_WEEK38, harness.store.get_state(_WEEK38).mark_seq)
    assert harness.store.list_pending_weeks() == []

    harness.store.mark_rebuild_required(_WEEK38, "MANUAL_REBUILD_REQUEST", _NOW)

    state = harness.store.get_state(_WEEK38)
    assert state.rebuild_required and state.rebuild_reason == "MANUAL_REBUILD_REQUEST"
    assert not state.recompute_pending  # Metrics の再生成待ちとは別
    assert harness.store.list_rebuild_weeks() == [_WEEK38]
    assert harness.store.list_pending_weeks() == []


def test_unknown_rebuild_reason_is_rejected(harness: Harness) -> None:
    with pytest.raises(ValueError):
        harness.store.mark_rebuild_required(_WEEK38, "SOMETHING_ELSE", _NOW)


def _replacement_row(count: int, week: str = _WEEK38) -> WeeklyEvaluationAggregate:
    return WeeklyEvaluationAggregate(
        review_week=week,
        recommendation_type=_BUY,
        rule_version=_RV,
        sample_count=count,
        conclusive_count=count,
        success_count=count,
        price_return_sum=Decimal(count),
        price_return_count=count,
        label_counts={"SUCCESS": count},
        updated_at=_NOW,
    )


def test_replace_week_overwrites_the_rows_and_resolves_the_rebuild_state(harness: Harness) -> None:
    assert harness.store.commit_evaluation(_eval(1), _BUY, "stale-v", _NOW)  # raw に無い古い行
    assert _commit(harness, _eval(2))
    harness.store.mark_rebuild_required(_WEEK38, "AGGREGATION_FAILURE", _NOW)
    expected = harness.store.get_state(_WEEK38).mark_seq

    assert harness.store.replace_week(_WEEK38, [_replacement_row(7)], expected, _NOW) is True

    row = _only_row(harness)  # 古い行は消えている
    assert row.sample_count == 7  # ADD ではなく SET(冪等)
    state = harness.store.get_state(_WEEK38)
    assert not state.rebuild_required and state.rebuild_reason is None
    assert harness.store.list_rebuild_weeks() == []
    assert state.recompute_pending  # Metrics の再生成を要求する
    assert _WEEK38 in harness.store.list_pending_weeks()


def test_replace_week_is_idempotent(harness: Harness) -> None:
    assert harness.store.replace_week(_WEEK37, [_replacement_row(3, _WEEK37)], None, _NOW)
    seq = harness.store.get_state(_WEEK37).mark_seq
    assert harness.store.replace_week(_WEEK37, [_replacement_row(3, _WEEK37)], seq, _NOW)

    assert _only_row(harness, _WEEK37).sample_count == 3


def test_replace_week_is_refused_when_a_new_evaluation_arrived_after_the_raw_read(
    harness: Harness,
) -> None:
    """rebuild の元にした raw を読んだ後に届いた評価を、上書きで消さない。"""
    assert _commit(harness, _eval(1))
    stale_seq = harness.store.get_state(_WEEK38).mark_seq
    assert _commit(harness, _eval(2))  # raw を読んだ後に届いた

    assert harness.store.replace_week(_WEEK38, [_replacement_row(1)], stale_seq, _NOW) is False

    assert _only_row(harness).sample_count == 2  # 何も変わっていない


def test_replace_week_for_a_week_without_state_requires_expected_none(harness: Harness) -> None:
    assert harness.store.replace_week(_WEEK37, [_replacement_row(1, _WEEK37)], 5, _NOW) is False
    assert harness.store.query_week(_WEEK37) == []
    assert harness.store.replace_week(_WEEK37, [_replacement_row(1, _WEEK37)], None, _NOW) is True


# --- backfill の状態 ----------------------------------------------------------


def test_backfill_status_is_incomplete_until_marked_complete(harness: Harness) -> None:
    assert harness.store.get_backfill_status().complete is False

    harness.store.set_backfill_complete(_NOW, week_count=3, row_count=5)

    status = harness.store.get_backfill_status()
    assert status.complete and status.week_count == 3 and status.row_count == 5


# --- Q-2: 集計の更新に失敗したら、EvaluationResult も保存されない -----------------


def test_non_finite_value_is_rejected_and_the_evaluation_is_not_saved(harness: Harness) -> None:
    bad = _eval(1, price_return_pct=float("nan"))

    with pytest.raises(ValueError):
        _commit(harness, bad)

    assert harness.saved_ids() == set()
    assert harness.store.query_week(_WEEK38) == []


def test_dynamodb_aggregate_failure_rolls_back_the_evaluation_save(
    harness: Harness,
) -> None:
    """★ Aggregate 側の書き込みが失敗する(テーブルが無い)と、EvaluationResult も保存されない。"""
    if harness.kind != "dynamodb":
        pytest.skip("Transaction の rollback は DynamoDB 実装の性質")
    store = harness.store
    assert isinstance(store, DynamoWeeklyEvaluationAggregateStore)
    broken = DynamoWeeklyEvaluationAggregateStore(
        "jstock-no_such_aggregate_table", "jstock-evaluation_results", store._client
    )

    with pytest.raises(ClientError):
        broken.commit_evaluation(_eval(1), _BUY, _RV, _NOW)

    assert harness.saved_ids() == set()  # 評価は保存されていない = 翌日の日次実行で再試行される


def test_dynamodb_reads_never_scan(harness: Harness) -> None:
    """AC13: 読み取りは Query / GetItem だけで、Aggregate 全件の Scan を行わない。"""
    if harness.kind != "dynamodb":
        pytest.skip("DynamoDB 実装の性質")
    store = harness.store
    assert isinstance(store, DynamoWeeklyEvaluationAggregateStore)

    class _NoScan:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            if name in {"scan"}:
                raise AssertionError("Scan は行わない")
            return getattr(self._inner, name)

    store._client = _NoScan(store._client)
    assert _commit(harness, _eval(1))
    store.query_week(_WEEK38)
    store.get_state(_WEEK38)
    store.list_pending_weeks()
    store.list_rebuild_weeks()
    store.get_backfill_status()
    store.finish_recompute(_WEEK38, store.get_state(_WEEK38).mark_seq)


def test_replace_week_without_recompute_request_does_not_touch_the_marker(harness: Harness) -> None:
    """backfill(request_recompute=False)は、過去の Metrics の再生成を要求しない。"""
    assert harness.store.replace_week(
        _WEEK37, [_replacement_row(4, _WEEK37)], None, _NOW, request_recompute=False
    )

    assert _only_row(harness, _WEEK37).sample_count == 4
    assert harness.store.list_pending_weeks() == []
    assert not harness.store.get_state(_WEEK37).recompute_pending
    # backfill の後でも、状態が無い週と同じく、次の評価は加算できる
    assert harness.store.commit_evaluation(_eval(9, evaluation_date=_W37), _BUY, _RV, _NOW)
    assert _only_row(harness, _WEEK37).sample_count == 5
    assert harness.store.list_pending_weeks() == [_WEEK37]


# --- F1(レビュー指摘): Transaction 取消理由の分類を、テストで固定する ---------------------


def _canceled_error(reason_codes: list[str | None]) -> ClientError:
    """`TransactionCanceledException` を、`CancellationReasons` を指定して組み立てる。

    `tests/unit/test_conversation_commit.py::_canceled_error` と同じ形(項目の並び順は
    Transaction の項目順)。`None` は AWS の規約どおり文字列 `"None"` として渡す。
    """
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
            "CancellationReasons": [{"Code": code or "None"} for code in reason_codes],
        },
        "TransactWriteItems",
    )


def test_is_first_item_conditional_failure_true_only_for_the_evaluation_result_itself() -> None:
    """★ Transaction の 4 項目 = [EvaluationResult の Put, Aggregate の Update,
    週の状態の Update, PENDING 一覧の Update]。「既存として扱ってよい(False を返す)」のは、
    **先頭(EvaluationResult)だけ**が条件不成立で、他の項目は全て無関係(`None`)の場合のみ。
    """
    from jstock_advisor.infrastructure.aws.weekly_evaluation_aggregate_dynamodb import (
        _is_first_item_conditional_failure,
    )

    # (i) 正しい「既存の評価」パターン: 先頭だけ条件不成立。
    assert _is_first_item_conditional_failure(
        _canceled_error(["ConditionalCheckFailed", None, None, None])
    )


def test_is_first_item_conditional_failure_false_when_a_later_item_conflicts() -> None:
    """★ Aggregate 側・週の状態側が競合した場合(先頭は成功=`None`)は、**既存として扱わない**
    (raise させる)。これを誤って True にすると、実際には何も保存されていない評価が
    `CompletedHorizonIndex` へ記録され、その評価が恒久的に欠落しうる(レビュー指摘 F1)。
    """
    from jstock_advisor.infrastructure.aws.weekly_evaluation_aggregate_dynamodb import (
        _is_first_item_conditional_failure,
    )

    assert not _is_first_item_conditional_failure(
        _canceled_error([None, "TransactionConflict", None, None])
    )
    assert not _is_first_item_conditional_failure(
        _canceled_error([None, None, "TransactionConflict", None])
    )
    assert not _is_first_item_conditional_failure(
        _canceled_error([None, None, None, "TransactionConflict"])
    )


def test_is_first_item_conditional_failure_false_when_ambiguous() -> None:
    """先頭が条件不成立**かつ**他の項目も競合している場合(通常は起こらない組合せ)は、
    fail-closed(False = 既存として扱わず raise する)側へ倒す。"""
    from jstock_advisor.infrastructure.aws.weekly_evaluation_aggregate_dynamodb import (
        _is_first_item_conditional_failure,
    )

    assert not _is_first_item_conditional_failure(
        _canceled_error(["ConditionalCheckFailed", "TransactionConflict", None, None])
    )


def test_is_first_item_conditional_failure_false_for_non_canceled_exceptions() -> None:
    from jstock_advisor.infrastructure.aws.weekly_evaluation_aggregate_dynamodb import (
        _is_first_item_conditional_failure,
    )

    other = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}},
        "TransactWriteItems",
    )
    assert not _is_first_item_conditional_failure(other)


class _RaisingClient:
    """`transact_write_items` を呼ぶと、指定した ClientError を毎回送出するテスト用 client。"""

    def __init__(self, error: ClientError) -> None:
        self._error = error
        self.call_count = 0

    def transact_write_items(self, **kwargs: Any) -> Any:
        self.call_count += 1
        raise self._error


def test_commit_evaluation_raises_and_does_not_return_false_on_a_non_duplicate_conflict() -> None:
    """★ commit_evaluation() レベルの固定(F1)。EvaluationResult 自体は成功([None])だが
    Aggregate 側が競合した場合、`False`(= 呼び出し側が既存評価として `CompletedHorizonIndex`
    へ記録し、二度と保存を試みなくなる)を返してはならない。必ず例外を送出する。
    """
    store = DynamoWeeklyEvaluationAggregateStore(
        "jstock-weekly_evaluation_aggregate", "jstock-evaluation_results", None
    )
    client = _RaisingClient(_canceled_error([None, "TransactionConflict", None, None]))
    store._client = client  # type: ignore[assignment]

    with pytest.raises(ClientError):
        store.commit_evaluation(_eval(1), _BUY, _RV, _NOW)

    # リトライ対象(TransactionConflict)のため、最大試行回数まで呼ばれたうえで最終的に送出する。
    assert client.call_count > 1


def test_commit_evaluation_returns_false_only_for_the_genuine_duplicate_pattern() -> None:
    """対照: 先頭だけが条件不成立の場合は、期待どおり `False`(重複。呼び出し側は正しく
    「既に保存済み」として扱ってよい)を返す。"""
    store = DynamoWeeklyEvaluationAggregateStore(
        "jstock-weekly_evaluation_aggregate", "jstock-evaluation_results", None
    )
    client = _RaisingClient(_canceled_error(["ConditionalCheckFailed", None, None, None]))
    store._client = client  # type: ignore[assignment]

    assert store.commit_evaluation(_eval(1), _BUY, _RV, _NOW) is False
    assert client.call_count == 1  # 条件不成立はリトライ対象ではない


# --- F4(レビュー指摘): ローカルの既定パスは data/local_store 配下(gitignore・隔離の対象) ------


def test_default_local_path_is_under_the_shared_local_store_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`build_weekly_evaluation_aggregate_store()` の既定パスは、他の全リポジトリと同じ
    `json_store.DEFAULT_STORE_DIR` 配下でなければならない。この属性は
    `tests/conftest.py` の autouse fixture(Issue #229)がテストごとに一時ディレクトリへ
    monkeypatch するため、束縛(import 時の値コピー)ではなく **都度の属性参照**が必須である
    (束縛すると、この monkeypatch の効果が及ばず、リポジトリ直下へ実ファイルを作ってしまう)。
    """
    from jstock_advisor.infrastructure.local_repository import json_store
    from jstock_advisor.infrastructure.weekly_evaluation_aggregate_store import (
        build_weekly_evaluation_aggregate_store,
    )

    isolated = tmp_path / "isolated_local_store"
    monkeypatch.setattr(json_store, "DEFAULT_STORE_DIR", isolated)

    store = build_weekly_evaluation_aggregate_store()

    assert isinstance(store, LocalWeeklyEvaluationAggregateStore)
    assert store._path == isolated / "weekly_evaluation_aggregate.json"
    assert not (
        Path(__file__).resolve().parents[2] / "data" / "weekly_evaluation_aggregate.json"
    ).exists()
