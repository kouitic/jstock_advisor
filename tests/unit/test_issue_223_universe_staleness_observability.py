"""Issue #223: 候補ユニバースの鮮度上限(U2)と、取得結果の観測
(U3 logger / U5 batch audit のキー)の検証。

★ 2026-09-10: PR-1a の暫定延長(1080 -> 2160)を **1080 へ戻した**。
  PR-2(.xlsx対応)が W3 として Production へ反映され、2026-09-10 06:00 JST の
  候補取得で promoted=true を実測したためである(Issue #236 の観測記録)。
  下の境界テストは上限そのものの振る舞いを固定するもので、戻しの前後で不変である。

中心は **「2026-09-15 の停止が起きないこと」と「その代わりに新しい停止日が
いつになるか」を同時に固定すること**である。上限を延ばす変更は、延ばしたことを
忘れると「上場廃止銘柄が最大90日残る」状態を恒久化してしまうため、新しい境界日を
テストで明示しておく(config/watchlist_screening_rules.yaml のコメントに書いた
戻し条件と対になる)。

実データ・実在の銘柄名は使用しない(source_date のみ本番実測値 2026-07-31 を
境界の根拠として使う。これは公開日であり銘柄情報ではない)。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.config.loader import load_config
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.interfaces.candidate_universe import CandidateUniverseError
from jstock_advisor.lambda_handlers.watchlist_dispatcher_handler import (
    UNIVERSE_SOURCE_CACHE,
    UNIVERSE_SOURCE_DOWNLOADED,
    _universe_observation,
)
from jstock_advisor.providers.candidate_universe.jpx_impl import (
    JpxCandidateUniverseProvider,
)
from jstock_advisor.services import candidate_universe_downloader
from jstock_advisor.services.candidate_universe_downloader import (
    CacheMetadata,
    CandidateUniverseCacheIO,
    DownloadOutcome,
    _with_effective_source_date,
)

# 本番で最後に取得できた上場銘柄一覧の公開日(#223 Phase A の実測)。
_PRODUCTION_SOURCE_DATE = dt.date(2026, 7, 31)

_OLD_MAX_STALE_HOURS = 1080  # 45日(変更前)
_NEW_MAX_STALE_HOURS = 2160  # 90日(暫定延長後)


def _provider(now: dt.datetime, max_stale_hours: int) -> JpxCandidateUniverseProvider:
    return JpxCandidateUniverseProvider(
        target_market_segments=None,
        listed_issues_max_stale_hours=max_stale_hours,
        jpx400_max_stale_hours=max_stale_hours,
        now=now,
    )


def _jst_0600_run(date: dt.date) -> dt.datetime:
    """JST 06:00 の定期実行時刻を UTC で表す(前日 21:00 UTC)。"""
    return dt.datetime.combine(date, dt.time(21, 0), tzinfo=dt.UTC) - dt.timedelta(days=1)


# --- U2: 設定値と staleness の境界 --------------------------------------------------


def test_shipped_config_reverted_listed_issues_max_stale_hours() -> None:
    """出荷される config が 1080(45日)へ **戻されている** こと(2026-09-10)。

    2160(90日)は PR-1a による**暫定**延長であり、PR-2(.xlsx対応)が Production へ
    反映され取得成功(promoted=true)を実測した時点で戻す約束だった。
    ★ 戻し忘れると「上場廃止銘柄が最大90日候補に残る」状態が既定になるため、
      戻ったことをテストで固定する(延長時に 2160 を固定していたのと対になる)。

    jpx400 側は元から 2160 であり、延長でも戻しでも **触っていない**ことを併せて
    固定する(「両方を一律に緩めた/締めた」のではない)。
    """
    cu = load_config().watchlist_screening.candidate_universe
    assert cu.listed_issues_max_stale_hours == _OLD_MAX_STALE_HOURS
    assert cu.jpx400_max_stale_hours == _NEW_MAX_STALE_HOURS


def test_config_comment_records_that_the_revert_was_done_and_why() -> None:
    """戻したことと、その根拠が config に書かれていること。

    値だけを戻して経緯を消すと、次に同じ障害が起きたときに
    「なぜ一度 90 日へ延ばしたのか」「何を確認して戻したのか」が失われる。
    ★ 延長時は「戻し条件」を、戻した後は「戻した根拠(観測記録)」を残す。
    """
    text = Path("config/watchlist_screening_rules.yaml").read_text(encoding="utf-8")
    assert "listed_issues_max_stale_hours: 1080" in text
    assert "2160" in text  # 暫定延長していた経緯が残っていること
    assert "promoted=true" in text  # 戻した根拠(実測した事象)
    assert "#236" in text  # 観測記録の所在
    assert "#223" in text


def test_2026_09_15_run_stops_before_the_change_and_survives_after() -> None:
    """★ 本 PR の目的そのもの。

    同一時刻・同一 source_date に対して、上限 1080 では停止し 2160 では停止しない
    ことを 1 つのテストで示す。上限の変更以外の要因では説明できない。
    """
    now = _jst_0600_run(dt.date(2026, 9, 15))

    with pytest.raises(CandidateUniverseError):
        _provider(now, _OLD_MAX_STALE_HOURS)._check_staleness(
            "東証上場銘柄一覧", _PRODUCTION_SOURCE_DATE, _OLD_MAX_STALE_HOURS
        )

    _provider(now, _NEW_MAX_STALE_HOURS)._check_staleness(
        "東証上場銘柄一覧", _PRODUCTION_SOURCE_DATE, _NEW_MAX_STALE_HOURS
    )


def test_new_hard_stop_is_2026_10_30_jst() -> None:
    """延長後の新しい停止日を固定する。

    source_date 2026-07-31 00:00 UTC + 2160h = 2026-10-29 00:00 UTC。
    定期実行は JST 06:00(= 前日 21:00 UTC)のため、
      10-29(木) 06:00 JST -> 2157h  継続
      10-30(金) 06:00 JST -> 2181h  停止
    となる。PR-2(.xlsx 対応)はこの日までに反映されている必要がある。
    """
    last_ok = _jst_0600_run(dt.date(2026, 10, 29))
    first_stop = _jst_0600_run(dt.date(2026, 10, 30))

    _provider(last_ok, _NEW_MAX_STALE_HOURS)._check_staleness(
        "東証上場銘柄一覧", _PRODUCTION_SOURCE_DATE, _NEW_MAX_STALE_HOURS
    )
    with pytest.raises(CandidateUniverseError):
        _provider(first_stop, _NEW_MAX_STALE_HOURS)._check_staleness(
            "東証上場銘柄一覧", _PRODUCTION_SOURCE_DATE, _NEW_MAX_STALE_HOURS
        )


def test_extension_does_not_disable_the_staleness_gate() -> None:
    """延長は「上限を外した」のではないこと(十分に古ければ従来どおり停止する)。"""
    now = _jst_0600_run(dt.date(2027, 1, 5))
    with pytest.raises(CandidateUniverseError):
        _provider(now, _NEW_MAX_STALE_HOURS)._check_staleness(
            "東証上場銘柄一覧", _PRODUCTION_SOURCE_DATE, _NEW_MAX_STALE_HOURS
        )


# --- U3: logger のレベル -------------------------------------------------------------


def test_downloader_logger_emits_info() -> None:
    """取得成功(promoted)の記録が INFO で出ること。

    Lambda の既定レベルは WARNING であり、setLevel が無いと成功時の
    source_date が CloudWatch へ一度も出ない(= 成功と失敗を区別できない)。
    レベル値そのものではなく「INFO が有効か」を検査する。
    """
    assert candidate_universe_downloader.logger.isEnabledFor(logging.INFO)


# --- U5: effective_source_date（失敗した回でも現況が分かる） -------------------------


@pytest.fixture
def local_cache_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CandidateUniverseCacheIO:
    monkeypatch.setattr(candidate_universe_downloader, "running_on_lambda", lambda: False)
    monkeypatch.setattr(
        candidate_universe_downloader,
        "resolve_candidate_universe_local_cache_dir",
        lambda: tmp_path,
    )
    return CandidateUniverseCacheIO()


def _metadata(source_date: dt.date) -> CacheMetadata:
    at = dt.datetime(2026, 7, 31, 21, 0, tzinfo=dt.UTC)
    return CacheMetadata(
        source_date=source_date,
        downloaded_at=at,
        validated_at=at,
        promoted_at=at,
        raw_row_count=3000,
        selected_count=3000,
        invalid_code_count=0,
    )


def test_effective_source_date_uses_the_newly_promoted_value(
    local_cache_io: CandidateUniverseCacheIO,
) -> None:
    outcome = DownloadOutcome(
        source="listed_issues",
        promoted=True,
        reason=None,
        metadata=_metadata(dt.date(2026, 9, 30)),
    )
    assert _with_effective_source_date(local_cache_io, outcome).effective_source_date == dt.date(
        2026, 9, 30
    )


def test_effective_source_date_falls_back_to_the_cache_on_failure(
    local_cache_io: CandidateUniverseCacheIO,
) -> None:
    """★ 404 が続いている本番の状態そのもの。

    取得に失敗した回でも「いまどの日付のデータで動いているか」が取れなければ、
    鮮度上限まであとどれだけかを外から知ることができない。
    """
    local_cache_io.promote("listed_issues", b"x" * 1000, _metadata(_PRODUCTION_SOURCE_DATE))
    outcome = DownloadOutcome(
        source="listed_issues", promoted=False, reason="HTTP Error 404", metadata=None
    )
    resolved = _with_effective_source_date(local_cache_io, outcome)
    assert resolved.effective_source_date == _PRODUCTION_SOURCE_DATE
    assert resolved.promoted is False
    assert resolved.reason == "HTTP Error 404"


def test_effective_source_date_is_none_when_no_cache_exists(
    local_cache_io: CandidateUniverseCacheIO,
) -> None:
    outcome = DownloadOutcome(
        source="listed_issues", promoted=False, reason="HTTP Error 404", metadata=None
    )
    assert _with_effective_source_date(local_cache_io, outcome).effective_source_date is None


def test_cache_metadata_read_failure_does_not_break_the_run(
    local_cache_io: CandidateUniverseCacheIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """観測値の取得失敗が処理を止めないこと(観測のために本処理を落とさない)。"""

    def _boom(_source: str) -> None:
        raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(local_cache_io, "read_current", _boom)
    outcome = DownloadOutcome(
        source="listed_issues", promoted=False, reason="HTTP Error 404", metadata=None
    )
    assert _with_effective_source_date(local_cache_io, outcome).effective_source_date is None


# --- U5: batch audit へ載せる 4 つのキー ---------------------------------------------


def test_observation_reports_cache_when_download_failed() -> None:
    now = _jst_0600_run(dt.date(2026, 9, 7))
    outcomes = [
        DownloadOutcome(
            source="listed_issues",
            promoted=False,
            reason="HTTP Error 404",
            metadata=None,
            effective_source_date=_PRODUCTION_SOURCE_DATE,
        ),
        DownloadOutcome(source="jpx400", promoted=True, reason=None, metadata=None),
    ]
    observed = _universe_observation(outcomes, now)
    assert observed["universe_source"] == UNIVERSE_SOURCE_CACHE
    assert observed["universe_promoted"] is False
    assert observed["universe_source_date"] == "2026-07-31"
    assert observed["universe_cache_age_days"] == 37


def test_observation_reports_downloaded_on_success() -> None:
    now = _jst_0600_run(dt.date(2026, 9, 7))
    outcomes = [
        DownloadOutcome(
            source="listed_issues",
            promoted=True,
            reason=None,
            metadata=_metadata(dt.date(2026, 9, 6)),
            effective_source_date=dt.date(2026, 9, 6),
        )
    ]
    observed = _universe_observation(outcomes, now)
    assert observed["universe_source"] == UNIVERSE_SOURCE_DOWNLOADED
    assert observed["universe_promoted"] is True
    assert observed["universe_source_date"] == "2026-09-06"
    assert observed["universe_cache_age_days"] == 0


def test_observation_cache_age_days_uses_the_same_basis_as_the_staleness_gate() -> None:
    """★ cache_age_days が staleness 判定と別の数え方になっていないこと。

    ここがずれると「監査では上限に余裕があるように見えるのに実際は停止する」
    という、観測が判断を誤らせる状態になる。
    """
    now = _jst_0600_run(dt.date(2026, 10, 29))  # 新上限に達する直前の実行
    outcomes = [
        DownloadOutcome(
            source="listed_issues",
            promoted=False,
            reason="HTTP Error 404",
            metadata=None,
            effective_source_date=_PRODUCTION_SOURCE_DATE,
        )
    ]
    age_hours = (
        now - dt.datetime.combine(_PRODUCTION_SOURCE_DATE, dt.time(), tzinfo=dt.UTC)
    ).total_seconds() / 3600
    assert _universe_observation(outcomes, now)["universe_cache_age_days"] == int(age_hours // 24)


def test_observation_tolerates_missing_source_date() -> None:
    now = _jst_0600_run(dt.date(2026, 9, 7))
    outcomes = [
        DownloadOutcome(source="listed_issues", promoted=False, reason="boom", metadata=None)
    ]
    observed = _universe_observation(outcomes, now)
    assert observed["universe_source_date"] is None
    assert observed["universe_cache_age_days"] is None
    assert observed["universe_source"] == UNIVERSE_SOURCE_CACHE


def test_observation_is_empty_without_a_listed_issues_outcome() -> None:
    """provider!="jpx" 等で Downloader を走らせない場合は 4 キーを足さない。

    空の dict を返すことで、set_watchlist_batch_total 側の既定値(None)が
    そのまま残る(「取得した結果 None だった」と「そもそも取得していない」を
    ここで作り分けない)。
    """
    now = _jst_0600_run(dt.date(2026, 9, 7))
    assert _universe_observation([], now) == {}
    assert (
        _universe_observation(
            [DownloadOutcome(source="jpx400", promoted=True, reason=None, metadata=None)], now
        )
        == {}
    )


# --- U5: BatchRunsTable への永続化と、finalize 監査への到達 ---------------------------


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


def test_observation_is_persisted_to_the_batch_row(dynamo) -> None:
    """dispatch 時点の観測値が BatchRunsTable へ入ること。

    finalize の監査ログはこの行を読むため、ここに入らなければ監査へも出ない。
    """
    now = _jst_0600_run(dt.date(2026, 9, 7))
    outcomes = [
        DownloadOutcome(
            source="listed_issues",
            promoted=False,
            reason="HTTP Error 404",
            metadata=None,
            effective_source_date=_PRODUCTION_SOURCE_DATE,
        )
    ]
    batch_tracker.set_watchlist_batch_total(
        "batch-223", 300, 72, now, **_universe_observation(outcomes, now)
    )
    item = batch_tracker.get_watchlist_batch("batch-223")
    assert item is not None
    assert item["universe_source"] == UNIVERSE_SOURCE_CACHE
    assert item["universe_promoted"] is False
    assert item["universe_source_date"] == "2026-07-31"
    assert int(item["universe_cache_age_days"]) == 37


def test_batch_row_keeps_the_keys_absent_when_the_downloader_did_not_run(dynamo) -> None:
    """Downloader を走らせない経路(maintenance / provider!="jpx")では None のまま。

    「取得したが不明だった」と「そもそも取得していない」を取り違えないため、
    観測していない回に値を作らない。
    """
    now = _jst_0600_run(dt.date(2026, 9, 7))
    batch_tracker.set_watchlist_batch_total("batch-maint", 5, 72, now)
    item = batch_tracker.get_watchlist_batch("batch-maint")
    assert item is not None
    assert item["universe_source"] is None
    assert item["universe_promoted"] is None
    assert item["universe_source_date"] is None
    assert item["universe_cache_age_days"] is None


def test_finalize_batch_audit_carries_the_four_keys() -> None:
    """finalize 時点の監査ログの output_values へ 4 キーが載ること。

    dispatcher が record_batch_audit を呼ぶのは中止・skip の経路だけであり、
    **正常に完了した回の batch audit を書くのは finalizer** である。したがって
    ここが繋がっていないと「成功した日も含めて観測できる」という受入条件を
    満たさない。
    """
    source = Path("src/jstock_advisor/services/watchlist_batch_finalizer.py").read_text(
        encoding="utf-8"
    )
    block = source.split('if not batch_item.get("finalize_batch_audit_recorded"):', 1)[1]
    block = block.split("mark_batch_audit_recorded", 1)[0]
    for key in (
        "universe_source",
        "universe_promoted",
        "universe_source_date",
        "universe_cache_age_days",
    ):
        assert f'"{key}": batch_item.get("{key}")' in block, key
