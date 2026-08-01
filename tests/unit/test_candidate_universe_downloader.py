"""候補ユニバース本格対応のDownloader検証ロジック・ローカルキャッシュI/Oのテスト。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from jstock_advisor.services.candidate_universe_downloader import (
    CacheMetadata,
    CandidateUniverseCacheIO,
    _validate,
)

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


# --- _validate: 検証しきい値 ------------------------------------------------------


def test_validate_rejects_response_smaller_than_minimum_bytes() -> None:
    reason = _validate(
        "listed_issues",
        data=b"x" * 10,
        raw_row_count=3000,
        invalid_code_count=0,
        selected_count=3000,
        source_date=dt.date(2026, 7, 1),
        previous_selected_count=None,
    )
    assert reason is not None
    assert "バイト" in reason


def test_validate_rejects_missing_source_date() -> None:
    reason = _validate(
        "listed_issues",
        data=b"x" * 200_000,
        raw_row_count=3000,
        invalid_code_count=0,
        selected_count=3000,
        source_date=None,
        previous_selected_count=None,
    )
    assert reason is not None
    assert "ソース日付" in reason


def test_validate_rejects_row_count_outside_bounds() -> None:
    reason = _validate(
        "jpx400",
        data=b"x" * 10_000,
        raw_row_count=10,  # jpx400の想定範囲(300〜450)を大きく下回る
        invalid_code_count=0,
        selected_count=10,
        source_date=dt.date(2026, 7, 1),
        previous_selected_count=None,
    )
    assert reason is not None
    assert "行数" in reason


def test_validate_rejects_high_invalid_code_rate() -> None:
    reason = _validate(
        "listed_issues",
        data=b"x" * 200_000,
        raw_row_count=3000,
        invalid_code_count=100,  # 3.3% > 1%上限
        selected_count=2900,
        source_date=dt.date(2026, 7, 1),
        previous_selected_count=None,
    )
    assert reason is not None
    assert "不正コード率" in reason


def test_validate_rejects_large_change_rate_from_previous() -> None:
    reason = _validate(
        "listed_issues",
        data=b"x" * 200_000,
        raw_row_count=3000,
        invalid_code_count=0,
        selected_count=2000,  # 前回3000件から33%減少(>10%上限)
        source_date=dt.date(2026, 7, 1),
        previous_selected_count=3000,
    )
    assert reason is not None
    assert "変化率" in reason


def test_validate_passes_within_all_thresholds() -> None:
    reason = _validate(
        "listed_issues",
        data=b"x" * 200_000,
        raw_row_count=3100,
        invalid_code_count=5,
        selected_count=3095,
        source_date=dt.date(2026, 7, 1),
        previous_selected_count=3080,
    )
    assert reason is None


def test_validate_skips_change_rate_check_when_no_previous_data() -> None:
    reason = _validate(
        "listed_issues",
        data=b"x" * 200_000,
        raw_row_count=3100,
        invalid_code_count=0,
        selected_count=3100,
        source_date=dt.date(2026, 7, 1),
        previous_selected_count=None,
    )
    assert reason is None


# --- CandidateUniverseCacheIO: ローカルバックエンド ------------------------------


@pytest.fixture
def local_cache_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CandidateUniverseCacheIO:
    monkeypatch.setattr(
        "jstock_advisor.services.candidate_universe_downloader.running_on_lambda", lambda: False
    )
    monkeypatch.setattr(
        "jstock_advisor.services.candidate_universe_downloader."
        "resolve_candidate_universe_local_cache_dir",
        lambda: tmp_path,
    )
    return CandidateUniverseCacheIO()


def test_read_current_returns_none_when_no_cache_exists(
    local_cache_io: CandidateUniverseCacheIO,
) -> None:
    assert local_cache_io.read_current("listed_issues") is None


def test_promote_then_read_current_round_trips(local_cache_io: CandidateUniverseCacheIO) -> None:
    metadata = CacheMetadata(
        source_date=dt.date(2026, 7, 1),
        downloaded_at=_NOW,
        validated_at=_NOW,
        promoted_at=_NOW,
        raw_row_count=3100,
        selected_count=3095,
        invalid_code_count=5,
    )
    local_cache_io.promote("listed_issues", b"raw-xls-bytes", metadata)

    result = local_cache_io.read_current("listed_issues")
    assert result is not None
    data, read_metadata = result
    assert data == b"raw-xls-bytes"
    assert read_metadata.source_date == dt.date(2026, 7, 1)
    assert read_metadata.selected_count == 3095


def test_promote_writes_archive_copy(
    local_cache_io: CandidateUniverseCacheIO, tmp_path: Path
) -> None:
    metadata = CacheMetadata(
        source_date=dt.date(2026, 7, 1),
        downloaded_at=_NOW,
        validated_at=_NOW,
        promoted_at=_NOW,
        raw_row_count=100,
        selected_count=100,
        invalid_code_count=0,
    )
    local_cache_io.promote("jpx400", b"csv-bytes", metadata)

    archive_dir = tmp_path / "archive" / "jpx400" / _NOW.strftime("%Y%m%d")
    assert (archive_dir / "data").read_bytes() == b"csv-bytes"


def test_promote_overwrites_current_on_repeated_calls(
    local_cache_io: CandidateUniverseCacheIO,
) -> None:
    metadata = CacheMetadata(
        source_date=dt.date(2026, 7, 1),
        downloaded_at=_NOW,
        validated_at=_NOW,
        promoted_at=_NOW,
        raw_row_count=1,
        selected_count=1,
        invalid_code_count=0,
    )
    local_cache_io.promote("jpx400", b"first", metadata)
    local_cache_io.promote("jpx400", b"second", metadata)

    result = local_cache_io.read_current("jpx400")
    assert result is not None
    assert result[0] == b"second"
