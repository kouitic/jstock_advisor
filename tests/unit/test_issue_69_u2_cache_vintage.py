"""Issue #69 U-2: 財務・配当キャッシュの vintage を銘柄単位で記録する。

`get_or_fetch()` は cache hit の判定で age_hours を計算しているのに、それを
**ログへ出すだけで捨てて**いた。そのため「どの銘柄がどの時点のデータで評価
されたか」を事後に説明できず、受入条件 1 が満たせない状態だった（F-J3）。

★ この単位で守るべき区別（本 Issue の root cause と同型の作り込みを避ける）
  ・「再利用した」古さと「捨てた（取り直した）」古さを混ぜない
  ・「測れなかった（再利用 0 件・初回）」を **0 と同じ値にしない**
  ・価格系キャッシュ（JST 暦日キーあり）を財務の集計へ**混ぜない**
  ・1 銘柄目の値が 2 銘柄目へ**漏れない**（BatchSize は引き上げ可能）

★ 実在の銘柄コード・銘柄名は使用しない（"0000" 等の架空値のみ）。
★ Production への failure injection は行わない。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from pydantic import TypeAdapter

from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.collection_store import build_collection_store
from jstock_advisor.services.screening_data_provider import (
    ScreeningDataResult,
    ScreeningDataStatus,
    _with_cache_vintage,
)
from jstock_advisor.services.watchlist_data_cache import (
    CacheEntry,
    CacheStats,
    CacheVintage,
    _classify_optional,
    get_or_fetch,
)

_NOW = dt.datetime(2026, 9, 9, 7, 0, tzinfo=dt.UTC)
_TTL_HOURS = 168  # 7日
_NEGATIVE_TTL_MINUTES = 15


@pytest.fixture
def repo(tmp_path: Path):
    return build_collection_store(CacheEntry, "test_cache.json", "cache_key", tmp_path)


def _adapter() -> TypeAdapter:
    return TypeAdapter(Decimal | None)


def _seed(repo, cache_key: str, age_hours: float) -> None:
    repo.upsert(
        CacheEntry(
            cache_key=cache_key,
            cached_at=_NOW - dt.timedelta(hours=age_hours),
            payload_json="1",
            quality_status="VALID",
        )
    )


def _fetch(repo, cache_key: str, vintage: CacheVintage | None, stats: CacheStats | None = None):
    return get_or_fetch(
        repo,
        cache_key,
        _TTL_HOURS,
        _NEGATIVE_TTL_MINUTES,
        _NOW,
        lambda: Decimal("2"),
        _adapter(),
        _classify_optional,
        "test",
        stats=stats,
        vintage=vintage,
    )


# --- T-1〜T-4: 3 状態を潰さない -------------------------------------------------------


def test_reused_entry_records_the_age_it_actually_used(repo) -> None:
    """T-1: hit したとき、実際に使った古さが記録されること。"""
    _seed(repo, "financial_summary:0000", 30.0)
    vintage = CacheVintage()
    assert _fetch(repo, "financial_summary:0000", vintage) == Decimal("1")
    assert vintage.reused_count == 1
    assert vintage.refetched_count == 0
    assert vintage.age_hours_max == pytest.approx(30.0)
    assert vintage.age_hours_min == pytest.approx(30.0)


def test_expired_entry_counts_as_refetched_and_its_age_is_not_used(repo) -> None:
    """T-2: ★ **捨てた古さ**を「使った古さ」に入れないこと。

    ここを混ぜると「200 時間前のデータで評価した」という誤った記録になる
    （実際には取り直した新しい値で評価している）。
    """
    _seed(repo, "financial_summary:0000", 200.0)
    vintage = CacheVintage()
    assert _fetch(repo, "financial_summary:0000", vintage) == Decimal("2")
    assert vintage.reused_count == 0
    assert vintage.refetched_count == 1
    assert vintage.age_hours_max is None
    assert vintage.age_hours_min is None


def test_absent_entry_records_no_age_and_counts_as_refetched(repo) -> None:
    """T-3: ★ 初回（キャッシュ不在）で 0 を作らないこと。"""
    vintage = CacheVintage()
    assert _fetch(repo, "financial_summary:0001", vintage) == Decimal("2")
    assert vintage.reused_count == 0
    assert vintage.refetched_count == 1
    assert vintage.age_hours_max is None
    assert vintage.age_hours_min is None


def test_max_and_min_stay_none_while_nothing_was_reused(repo) -> None:
    """T-4: ★ 再利用 0 件のとき max/min は **0 ではなく None**。

    0 を入れると「0 時間前の新しいデータを使った」と読めてしまい、
    本 Issue の root cause（測れなかったものを該当なしと同じ値へ潰す）と
    同型の欠陥になる。
    """
    vintage = CacheVintage()
    _fetch(repo, "financial_summary:0000", vintage)
    _fetch(repo, "dividend_info:0000:3", vintage)
    assert vintage.refetched_count == 2
    assert vintage.age_hours_max is None
    assert vintage.age_hours_min is None


def test_max_and_min_span_multiple_reused_entries(repo) -> None:
    """複数キーを再利用したとき、幅（max−min）が取れること。"""
    _seed(repo, "financial_summary:0000", 12.0)
    _seed(repo, "dividend_info:0000:3", 130.0)
    _seed(repo, "cashflow_decomposition:0000", 60.0)
    vintage = CacheVintage()
    for key in ("financial_summary:0000", "dividend_info:0000:3", "cashflow_decomposition:0000"):
        _fetch(repo, key, vintage)
    assert vintage.reused_count == 3
    assert vintage.age_hours_max == pytest.approx(130.0)
    assert vintage.age_hours_min == pytest.approx(12.0)


# --- T-5: 境界（DoD 1） ---------------------------------------------------------------


def test_age_exactly_at_the_ttl_is_treated_as_reused(repo) -> None:
    """T-5: ★ age == TTL ちょうどは hit 側（現行の `<=` に揃える）。

    記録側だけ `<` にすると、同じ 1 回の処理が「再利用」とも「取り直し」とも
    数えられない/二重に数えられる状態になる。
    """
    _seed(repo, "financial_summary:0000", float(_TTL_HOURS))
    vintage = CacheVintage()
    assert _fetch(repo, "financial_summary:0000", vintage) == Decimal("1")
    assert vintage.reused_count == 1
    assert vintage.refetched_count == 0
    assert vintage.age_hours_max == pytest.approx(float(_TTL_HOURS))


def test_just_past_the_ttl_is_refetched(repo) -> None:
    """境界のもう片側。TTL を 1 分でも超えたら取り直し。"""
    _seed(repo, "financial_summary:0000", _TTL_HOURS + (1 / 60))
    vintage = CacheVintage()
    assert _fetch(repo, "financial_summary:0000", vintage) == Decimal("2")
    assert vintage.reused_count == 0
    assert vintage.refetched_count == 1


# --- T-6: 価格系の除外（D-7） ---------------------------------------------------------


def test_price_cache_is_excluded_by_wiring_not_by_key_matching() -> None:
    """T-6: ★ 価格系は**配線で**除外されていること。

    `build_cached_provider_bundle()` が vintage を渡すのは財務・配当の
    ラッパだけで、market_data のラッパには渡さない。キー接頭辞の照合に
    頼らないので、将来キー名が変わっても混入しない。
    """
    from jstock_advisor.services import watchlist_data_cache as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    market_block = source.split("market_data=_CachingMarketDataProvider(", 1)[1]
    market_block = market_block.split("),", 1)[0]
    assert "vintage=" not in market_block

    for name in (
        "financial_data=_CachingFinancialDataProvider(",
        "dividend_data=_CachingDividendDataProvider(",
    ):
        block = source.split(name, 1)[1].split("),", 1)[0]
        assert "vintage=vintage" in block, name


def test_market_data_provider_has_no_vintage_field() -> None:
    """価格系ラッパは収集器を**持たない**（渡す経路そのものが無い）。"""
    from jstock_advisor.services.watchlist_data_cache import (
        _CachingFinancialDataProvider,
        _CachingMarketDataProvider,
    )

    assert "vintage" not in _CachingMarketDataProvider.__dataclass_fields__
    assert "vintage" in _CachingFinancialDataProvider.__dataclass_fields__


# --- T-7: 銘柄ごとのスコープ（BatchSize > 1 対策） ------------------------------------


def _result(status: ScreeningDataStatus = ScreeningDataStatus.OK) -> ScreeningDataResult:
    return ScreeningDataResult(status=status, input=None, missing_fields=[], error_message=None)


def test_the_second_stock_does_not_inherit_the_first_stocks_vintage(repo) -> None:
    """T-7: ★ 同じ収集器で 2 銘柄を処理しても値が混ざらないこと。

    `CacheStats` は Lambda 呼び出し全体で共有されており、SQS の BatchSize は
    「安定稼働後に引き上げ可能」なパラメータである。収集器をそのまま共有すると
    2 銘柄目の記録に 1 銘柄目が混ざり、銘柄単位の永続化が静かに壊れる。
    """
    _seed(repo, "financial_summary:0000", 100.0)
    vintage = CacheVintage()

    def collect_first(stock_code: str, now: dt.datetime) -> ScreeningDataResult:
        _fetch(repo, "financial_summary:0000", vintage)
        return _result()

    def collect_second(stock_code: str, now: dt.datetime) -> ScreeningDataResult:
        _fetch(repo, "financial_summary:0001", vintage)  # cache 不在
        return _result()

    first = _with_cache_vintage(vintage, collect_first, "0000", _NOW)
    second = _with_cache_vintage(vintage, collect_second, "0001", _NOW)

    assert first.financial_cache_reused_count == 1
    assert first.financial_cache_age_hours_max == pytest.approx(100.0)
    # ★ 2 銘柄目は再利用 0 件なので None。100.0 が漏れていない。
    assert second.financial_cache_reused_count == 0
    assert second.financial_cache_refetched_count == 1
    assert second.financial_cache_age_hours_max is None
    assert second.financial_cache_age_hours_min is None


def test_vintage_is_attached_even_when_the_data_was_unavailable(repo) -> None:
    """T-8: DATA_ERROR / NOT_FOUND の回も、取得を試みた分の集計は残ること。

    「取得できなかった」ことと「キャッシュをどう使ったか」は別の情報である。
    """
    _seed(repo, "financial_summary:0000", 20.0)
    vintage = CacheVintage()

    def collect(stock_code: str, now: dt.datetime) -> ScreeningDataResult:
        _fetch(repo, "financial_summary:0000", vintage)
        return _result(ScreeningDataStatus.DATA_ERROR)

    result = _with_cache_vintage(vintage, collect, "0000", _NOW)
    assert result.status == ScreeningDataStatus.DATA_ERROR
    assert result.financial_cache_reused_count == 1
    assert result.financial_cache_age_hours_max == pytest.approx(20.0)


def test_without_a_collector_the_result_keeps_all_four_fields_none(repo) -> None:
    """収集器を渡さない呼び出し（Dispatcher の候補収集・CLI）では None のまま。"""
    result = _with_cache_vintage(None, lambda code, now: _result(), "0000", _NOW)
    assert result.financial_cache_reused_count is None
    assert result.financial_cache_refetched_count is None
    assert result.financial_cache_age_hours_max is None
    assert result.financial_cache_age_hours_min is None


# --- T-9: 永続化（Decimal 化を含む） --------------------------------------------------


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-northeast-1")
        client.create_table(
            TableName="jstock-watchlist_candidate_progress",
            KeySchema=[
                {"AttributeName": "batch_id", "KeyType": "HASH"},
                {"AttributeName": "stock_code", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "batch_id", "AttributeType": "S"},
                {"AttributeName": "stock_code", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="jstock-batch_runs",
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def _claim(batch_id: str, stock_code: str, owner: str) -> None:
    batch_tracker.create_missing_candidate_progress_rows(batch_id, [stock_code], _NOW, 72)
    assert batch_tracker.claim_candidate_lease(batch_id, stock_code, owner, _NOW, 360)


def _complete(batch_id: str, stock_code: str, owner: str, **kwargs) -> bool:
    return batch_tracker.complete_candidate(
        batch_id,
        stock_code,
        owner,
        terminal_status=batch_tracker.WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=10,
        now=_NOW,
        **kwargs,
    )


def test_vintage_is_persisted_on_the_candidate_row(dynamo) -> None:
    """T-9: 銘柄単位の進捗行へ 4 項目が入ること（float は Decimal 経由）。"""
    _claim("b-u2", "0000", "owner-1")
    assert _complete(
        "b-u2",
        "0000",
        "owner-1",
        financial_cache_reused_count=3,
        financial_cache_refetched_count=2,
        financial_cache_age_hours_max=129.4,
        financial_cache_age_hours_min=32.75,
    )
    rows = batch_tracker.query_all_candidate_progress("b-u2", consistent_read=True)
    assert len(rows) == 1
    row = rows[0]
    assert row.financial_cache_reused_count == 3
    assert row.financial_cache_refetched_count == 2
    assert row.financial_cache_age_hours_max == pytest.approx(129.4)
    assert row.financial_cache_age_hours_min == pytest.approx(32.75)


def test_the_row_keeps_the_fields_none_when_nothing_was_reused(dynamo) -> None:
    """★ 再利用 0 件の回は max/min の**属性自体を書かない**（0 で埋めない）。"""
    _claim("b-u2-none", "0001", "owner-1")
    assert _complete(
        "b-u2-none",
        "0001",
        "owner-1",
        financial_cache_reused_count=0,
        financial_cache_refetched_count=5,
        financial_cache_age_hours_max=None,
        financial_cache_age_hours_min=None,
    )
    row = batch_tracker.query_all_candidate_progress("b-u2-none", consistent_read=True)[0]
    assert row.financial_cache_age_hours_max is None
    assert row.financial_cache_age_hours_min is None
    # ★ 取り直しの件数は残るので「測っていない」わけではないと分かる。
    assert row.financial_cache_refetched_count == 5


def test_rows_written_before_this_change_read_back_as_none(dynamo) -> None:
    """★ 本変更の反映前に書かれた行（属性が無い）を読んでも 0 にしないこと。"""
    _claim("b-u2-old", "0002", "owner-1")
    assert _complete("b-u2-old", "0002", "owner-1")
    row = batch_tracker.query_all_candidate_progress("b-u2-old", consistent_read=True)[0]
    assert row.financial_cache_reused_count is None
    assert row.financial_cache_refetched_count is None
    assert row.financial_cache_age_hours_max is None
    assert row.financial_cache_age_hours_min is None


# --- T-10: 既存の観測値が変わっていないこと -------------------------------------------


def test_existing_hit_miss_counting_is_unchanged(repo) -> None:
    """T-10: ★ 既存の CacheStats（hit/miss）の数え方が変わっていないこと。

    vintage を足したことで既存のログ・集計の意味が動いていないかを見る。
    """
    _seed(repo, "financial_summary:0000", 30.0)
    stats_only = CacheStats()
    _fetch(repo, "financial_summary:0000", None, stats_only)
    _fetch(repo, "financial_summary:0001", None, stats_only)

    stats_with_vintage = CacheStats()
    _fetch(repo, "financial_summary:0000", CacheVintage(), stats_with_vintage)
    _fetch(repo, "financial_summary:0003", CacheVintage(), stats_with_vintage)

    assert (stats_only.hit_count, stats_only.miss_count) == (1, 1)
    assert (stats_with_vintage.hit_count, stats_with_vintage.miss_count) == (1, 1)


def test_get_or_fetch_still_returns_the_value_itself(repo) -> None:
    """★ 戻り値の契約（値そのものを返す）を変えていないこと。

    タプル化（O-B）を採らなかったことの明示。9 箇所ある呼び出し側は無改変。
    """
    _seed(repo, "financial_summary:0000", 10.0)
    assert _fetch(repo, "financial_summary:0000", CacheVintage()) == Decimal("1")
    assert _fetch(repo, "financial_summary:0000", None) == Decimal("1")


def test_reset_clears_every_field() -> None:
    """スコープを切る操作が 4 項目すべてを戻すこと（取りこぼしがないこと）。"""
    vintage = CacheVintage()
    vintage.record_reused(50.0)
    vintage.record_refetched()
    vintage.reset()
    assert vintage.reused_count == 0
    assert vintage.refetched_count == 0
    assert vintage.age_hours_max is None
    assert vintage.age_hours_min is None
