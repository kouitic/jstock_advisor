"""Issue #69 U-1: JPX 2 ファイル(上場銘柄一覧 / JPX400)の vintage 差の記録。

本 Issue の F-J12 は「上場銘柄一覧(45日)と JPX400(90日)が **独立した閾値**で
それぞれ staleness 判定されるだけで、**相互の整合が一度も検査されていない**」
という欠陥である。90日前の構成銘柄リストを当日の上場一覧へ結合しても、外形的に
知る手段が無かった。

#223(O-A)がバッチ監査へ入れた観測値は **listed_issues 側だけ**であり、JPX400 の
promoted / source_date はどこにも残らない。ここを埋めるのが U-1 である。

★ 本単位は **記録と WARNING のみ**で、乖離があっても処理を中断しない(gate に
  しない)。取得の一時的な失敗で候補の自動追加そのものが止まることを避けるため
  (#223 が防いだ状態と同じになる)。許容幅を決めて gate するかどうかは U-3。

★ 実在の銘柄コード・銘柄名は使用しない(source_date は公開日であり銘柄情報では
  ないため、#223 と同じ実測値を境界の根拠として使う)。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.lambda_handlers.watchlist_dispatcher_handler import (
    UNIVERSE_SOURCE_CACHE,
    UNIVERSE_SOURCE_DOWNLOADED,
    _cache_age_days,
    _universe_observation,
)
from jstock_advisor.services.candidate_universe_downloader import DownloadOutcome

_LISTED_SOURCE_DATE = dt.date(2026, 7, 31)


def _jst_0600_run(date: dt.date) -> dt.datetime:
    """JST 06:00 の定期実行時刻を UTC で表す(前日 21:00 UTC)。"""
    return dt.datetime.combine(date, dt.time(), tzinfo=dt.UTC) - dt.timedelta(hours=9)


def _listed(
    source_date: dt.date | None, *, promoted: bool = False
) -> DownloadOutcome:
    return DownloadOutcome(
        source="listed_issues",
        promoted=promoted,
        reason=None if promoted else "HTTP Error 404",
        metadata=None,
        effective_source_date=source_date,
    )


def _jpx400(source_date: dt.date | None, *, promoted: bool = True) -> DownloadOutcome:
    return DownloadOutcome(
        source="jpx400",
        promoted=promoted,
        reason=None if promoted else "HTTP Error 404",
        metadata=None,
        effective_source_date=source_date,
    )


# --- T-1〜T-3: 一致 / 不一致 / 片方欠落 ------------------------------------------------


def test_matching_source_dates_record_a_zero_gap_and_do_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ 2 ファイルの source_date が一致していれば gap は 0 で、WARNING は出ない。

    肯定形で「0 が入る」ことを見る(#254 P-1)。「WARNING が出ない」だけを見ると、
    キー自体が存在しなくても通ってしまう。
    """
    now = _jst_0600_run(dt.date(2026, 9, 7))
    with caplog.at_level(logging.WARNING):
        observed = _universe_observation(
            [_listed(_LISTED_SOURCE_DATE), _jpx400(_LISTED_SOURCE_DATE)], now
        )
    assert observed["universe_vintage_gap_days"] == 0
    assert observed["universe_jpx400_source_date"] == "2026-07-31"
    assert observed["universe_jpx400_promoted"] is True
    assert "vintage mismatch" not in caplog.text
    assert "vintage gap unavailable" not in caplog.text


def test_mismatched_source_dates_are_recorded_and_warned_without_stopping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ 本 Issue の中心。乖離が記録に残り、WARNING が出て、**処理は続く**。"""
    now = _jst_0600_run(dt.date(2026, 9, 7))
    with caplog.at_level(logging.WARNING):
        observed = _universe_observation(
            [_listed(_LISTED_SOURCE_DATE), _jpx400(dt.date(2026, 5, 12))], now
        )
    assert observed["universe_vintage_gap_days"] == 80
    assert observed["universe_source_date"] == "2026-07-31"
    assert observed["universe_jpx400_source_date"] == "2026-05-12"
    assert "candidate universe vintage mismatch" in caplog.text
    assert "gap_days=80" in caplog.text
    # ★ 例外を送出しない = 候補の自動追加を止めない(D-1 の管理者判断)。
    assert observed["universe_source"] == UNIVERSE_SOURCE_CACHE


def test_the_gap_is_symmetric_when_jpx400_is_the_newer_file() -> None:
    """どちらが新しくても同じ大きさとして記録される(絶対値)。"""
    now = _jst_0600_run(dt.date(2026, 9, 7))
    forward = _universe_observation(
        [_listed(dt.date(2026, 9, 1)), _jpx400(dt.date(2026, 8, 22))], now
    )
    backward = _universe_observation(
        [_listed(dt.date(2026, 8, 22)), _jpx400(dt.date(2026, 9, 1))], now
    )
    assert forward["universe_vintage_gap_days"] == 10
    assert backward["universe_vintage_gap_days"] == 10


def test_missing_jpx400_source_date_records_none_not_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ 片方が欠落したとき gap は **0 ではなく None**。

    0 を入れると「一致している」と読めてしまい、本 Issue の root cause
    (評価できなかったものを該当しないものと同じ値へ潰す)を作り直すことになる。
    """
    now = _jst_0600_run(dt.date(2026, 9, 7))
    with caplog.at_level(logging.WARNING):
        observed = _universe_observation([_listed(_LISTED_SOURCE_DATE), _jpx400(None)], now)
    assert observed["universe_vintage_gap_days"] is None
    assert observed["universe_jpx400_source_date"] is None
    assert observed["universe_jpx400_cache_age_days"] is None
    # ★ 取得自体は行われたことが promoted で分かる(「そもそも対象外」ではない)。
    assert observed["universe_jpx400_promoted"] is True
    assert "candidate universe vintage gap unavailable" in caplog.text


def test_missing_listed_source_date_records_none_not_zero() -> None:
    now = _jst_0600_run(dt.date(2026, 9, 7))
    observed = _universe_observation([_listed(None), _jpx400(_LISTED_SOURCE_DATE)], now)
    assert observed["universe_vintage_gap_days"] is None
    assert observed["universe_source_date"] is None
    assert observed["universe_cache_age_days"] is None
    # ★ JPX400 側は測れているので、そちらは残る(片側の欠落で両方を捨てない)。
    assert observed["universe_jpx400_source_date"] == "2026-07-31"


def test_absent_jpx400_outcome_is_distinguishable_from_an_unknown_date() -> None:
    """★ 「そもそも JPX400 の outcome が無い」と「取得したが日付が不明」の区別。

    前者は promoted も None、後者は promoted に True/False が入る。
    値の形で区別できることを固定する(どちらも None に潰さない)。
    """
    now = _jst_0600_run(dt.date(2026, 9, 7))
    absent = _universe_observation([_listed(_LISTED_SOURCE_DATE)], now)
    unknown = _universe_observation([_listed(_LISTED_SOURCE_DATE), _jpx400(None)], now)
    assert absent["universe_jpx400_promoted"] is None
    assert unknown["universe_jpx400_promoted"] is True
    assert absent["universe_vintage_gap_days"] is None
    assert unknown["universe_vintage_gap_days"] is None


def test_jpx400_download_failure_is_recorded_as_not_promoted() -> None:
    """DoD 5(失敗の可視性): JPX400 の取得失敗が観測値に現れること。"""
    now = _jst_0600_run(dt.date(2026, 9, 7))
    observed = _universe_observation(
        [_listed(_LISTED_SOURCE_DATE, promoted=True), _jpx400(_LISTED_SOURCE_DATE, promoted=False)],
        now,
    )
    assert observed["universe_promoted"] is True
    assert observed["universe_source"] == UNIVERSE_SOURCE_DOWNLOADED
    # ★ listed が成功していても JPX400 の失敗は別に見える。
    assert observed["universe_jpx400_promoted"] is False


# --- T-7〜T-9: 境界・単調性・単位(DoD 1/2/4) ------------------------------------------


def test_a_one_day_gap_is_recorded_as_one_and_warns() -> None:
    """DoD 1(境界の連続性): 0 と 1 の境界で値が飛ばない・落ちない。"""
    now = _jst_0600_run(dt.date(2026, 9, 7))
    assert (
        _universe_observation(
            [_listed(dt.date(2026, 9, 1)), _jpx400(dt.date(2026, 8, 31))], now
        )["universe_vintage_gap_days"]
        == 1
    )


@pytest.mark.parametrize("days", [0, 1, 2, 30, 45, 90])
def test_the_gap_is_monotonic_in_the_distance_between_the_two_files(days: int) -> None:
    """DoD 2(単調性): 離れた日数がそのまま gap になる。"""
    now = _jst_0600_run(dt.date(2026, 9, 7))
    listed_date = dt.date(2026, 9, 1)
    observed = _universe_observation(
        [_listed(listed_date), _jpx400(listed_date - dt.timedelta(days=days))], now
    )
    assert observed["universe_vintage_gap_days"] == days


def test_gap_days_and_cache_age_days_use_different_bases() -> None:
    """★ DoD 4(単位・スケール): 同じ「日」でも基準が違うことを固定する。

    universe_cache_age_days = **現在時刻**から source_date までの経過日数
    universe_vintage_gap_days = **2 つの source_date どうし**の差
    取り違えると「片方が古い」と「両方が古い」を混同する。
    """
    now = _jst_0600_run(dt.date(2026, 9, 7))
    observed = _universe_observation(
        [_listed(dt.date(2026, 9, 1)), _jpx400(dt.date(2026, 8, 31))], now
    )
    # 現在時刻からの経過は 2 ファイルで別々の値になる。
    assert observed["universe_cache_age_days"] != observed["universe_jpx400_cache_age_days"]
    # 一方 gap は 1 日で、どちらの cache_age とも一致しない。
    assert observed["universe_vintage_gap_days"] == 1


def test_jpx400_cache_age_uses_the_same_basis_as_the_staleness_gate() -> None:
    """★ JPX400 の経過日数が staleness 判定と別の数え方になっていないこと。

    ここがずれると「監査では余裕があるように見えるのに実際は停止する」という、
    観測が判断を誤らせる状態になる(#223 が listed 側で固定したのと同じ不変条件)。
    """
    now = _jst_0600_run(dt.date(2026, 10, 29))
    source_date = dt.date(2026, 7, 31)
    age_hours = (
        now - dt.datetime.combine(source_date, dt.time(), tzinfo=dt.UTC)
    ).total_seconds() / 3600
    observed = _universe_observation([_listed(source_date), _jpx400(source_date)], now)
    assert observed["universe_jpx400_cache_age_days"] == int(age_hours // 24)


def test_cache_age_days_helper_returns_none_for_an_unknown_source_date() -> None:
    """DoD 3(定常でない1回目): 初回でキャッシュが無い場合に 0 を作らない。"""
    now = _jst_0600_run(dt.date(2026, 9, 7))
    assert _cache_age_days(None, now) is None
    assert _cache_age_days(dt.date(2026, 9, 6), now) == 0


def test_cache_age_days_can_be_negative_on_the_publication_day_known_limitation() -> None:
    """★ 既知の制約: JST 公表日を 00:00 UTC とみなすため、公表当日は -1 になりうる。

    JST 06:00 の定期実行は UTC では前日 21:00 であり、その日に公表された
    source_date(当日 00:00 UTC とみなす)より **9 時間前**になる。
    本 Issue は vintage の記録が主題であり、この日付 semantics 自体は
    **変更しない**(Issue #66 の Scope 16C へ移送済み)。

    ★ 望ましい挙動として固定しているのではなく、**現状の基準を明示して
      引き継ぐため**のテストである。#66 で 16C を直す際にここが落ちる。
    """
    now = _jst_0600_run(dt.date(2026, 9, 7))
    assert _cache_age_days(dt.date(2026, 9, 7), now) == -1


# --- T-13: 既存の listed_issues 観測が変わっていないこと -------------------------------


def test_existing_listed_issues_observation_is_unchanged() -> None:
    """★ #223 が入れた 4 キーの値が 1 つも変わっていないこと(退行防止)。

    JPX400 の outcome を足しても listed 側の値は同じでなければならない。
    """
    now = _jst_0600_run(dt.date(2026, 9, 7))
    without_jpx400 = _universe_observation([_listed(_LISTED_SOURCE_DATE)], now)
    with_jpx400 = _universe_observation(
        [_listed(_LISTED_SOURCE_DATE), _jpx400(dt.date(2026, 5, 12))], now
    )
    for key in (
        "universe_source",
        "universe_promoted",
        "universe_source_date",
        "universe_cache_age_days",
    ):
        assert without_jpx400[key] == with_jpx400[key], key
    assert with_jpx400["universe_source"] == UNIVERSE_SOURCE_CACHE
    assert with_jpx400["universe_source_date"] == "2026-07-31"
    assert with_jpx400["universe_cache_age_days"] == 37


def test_observation_is_still_empty_without_a_listed_issues_outcome() -> None:
    """provider!="jpx" 等の経路では、JPX400 だけがあってもキーを足さない。

    ★ #223 の既存の契約(空 dict を返し set_watchlist_batch_total の既定値 None を
    残す)を、JPX400 対応で崩していないことを見る。
    """
    now = _jst_0600_run(dt.date(2026, 9, 7))
    assert _universe_observation([], now) == {}
    assert _universe_observation([_jpx400(dt.date(2026, 9, 1))], now) == {}


# --- T-15〜T-17: 永続化と finalize 監査への到達 ----------------------------------------


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-northeast-1")
        client.create_table(
            TableName="jstock-batch_runs",
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def test_the_jpx400_observation_is_persisted_to_the_batch_row(dynamo) -> None:
    """dispatch 時点の観測値が BatchRunsTable へ入ること。

    finalize の監査ログはこの行を読むため、ここに入らなければ監査へも出ない。
    """
    now = _jst_0600_run(dt.date(2026, 9, 7))
    observation = _universe_observation(
        [_listed(_LISTED_SOURCE_DATE), _jpx400(dt.date(2026, 5, 12))], now
    )
    batch_tracker.set_watchlist_batch_total("batch-69-u1", 300, 72, now, **observation)
    item = batch_tracker.get_watchlist_batch("batch-69-u1")
    assert item is not None
    assert item["universe_jpx400_promoted"] is True
    assert item["universe_jpx400_source_date"] == "2026-05-12"
    assert int(item["universe_jpx400_cache_age_days"]) == 117
    assert int(item["universe_vintage_gap_days"]) == 80


def test_the_batch_row_keeps_the_new_keys_none_when_the_downloader_did_not_run(
    dynamo,
) -> None:
    """Downloader を走らせない経路(maintenance / provider!="jpx")では None のまま。"""
    now = _jst_0600_run(dt.date(2026, 9, 7))
    batch_tracker.set_watchlist_batch_total("batch-69-maint", 5, 72, now)
    item = batch_tracker.get_watchlist_batch("batch-69-maint")
    assert item is not None
    assert item["universe_jpx400_promoted"] is None
    assert item["universe_jpx400_source_date"] is None
    assert item["universe_jpx400_cache_age_days"] is None
    assert item["universe_vintage_gap_days"] is None


def test_a_none_gap_is_persisted_as_none_not_zero(dynamo) -> None:
    """★ 「測れなかった」が保存の時点で 0 に化けないこと。"""
    now = _jst_0600_run(dt.date(2026, 9, 7))
    observation = _universe_observation([_listed(_LISTED_SOURCE_DATE), _jpx400(None)], now)
    batch_tracker.set_watchlist_batch_total("batch-69-unknown", 300, 72, now, **observation)
    item = batch_tracker.get_watchlist_batch("batch-69-unknown")
    assert item is not None
    assert item["universe_vintage_gap_days"] is None
    # ★ 同じ回に listed 側は測れているので、そちらは 0 ではなく実測値が入る。
    assert int(item["universe_cache_age_days"]) == 37


def test_finalize_batch_audit_carries_the_jpx400_keys() -> None:
    """★ finalize 時点の監査ログの output_values へ新しい 4 キーが載ること。

    #223 が同じ形の guard を残しており、その docstring が理由を書いている:
    「dispatcher が record_batch_audit を呼ぶのは中止・skip の経路だけであり、
      **正常に完了した回の batch audit を書くのは finalizer** である。したがって
      ここが繋がっていないと『成功した日も含めて観測できる』という受入条件を
      満たさない」。BatchRunsTable へ入れるだけでは監査から追えないため、
    #223 と同じ位置に同じ形で足したことを固定する。
    """
    source = Path("src/jstock_advisor/services/watchlist_batch_finalizer.py").read_text(
        encoding="utf-8"
    )
    block = source.split('if not batch_item.get("finalize_batch_audit_recorded"):', 1)[1]
    block = block.split("mark_batch_audit_recorded", 1)[0]
    for key in (
        "universe_jpx400_promoted",
        "universe_jpx400_source_date",
        "universe_jpx400_cache_age_days",
        "universe_vintage_gap_days",
    ):
        assert f'"{key}": batch_item.get(' in block, key
