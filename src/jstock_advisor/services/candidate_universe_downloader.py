"""候補ユニバース(東証上場銘柄一覧・JPX400構成銘柄)のDownloader(候補ユニバース本格対応)。

`urllib.request`(stdlib、既存infrastructure/edinet/client.pyと同じ流儀)でJPX/Nikkeiの
公開ファイルを取得し、providers/candidate_universe/jpx_impl.pyのパース関数で解析・
検証したうえで、検証成功時のみキャッシュ(S3またはローカル)へ昇格する。

**検証してから昇格する**(validate-before-promote): ダウンロードした生バイト列は
メモリ上でパース・検証を終えるまでS3/ローカルキャッシュへは一切書き込まない。
これにより「取得したファイルが壊れていた/想定外のフォーマットだった場合でも、
既存の`current/`は無傷のまま残り、次にProviderが読むときは前回成功時のキャッシュが
そのまま使われる」という安全性を、明示的なS3 staging/プレフィックスを設けなくても
実現できる(検証はダウンロード直後のインメモリ処理であり、S3から読み戻す必要が
無いため)。`current/`昇格と同時に`archive/<source>/<日付>/`へも複製し、世代履歴を残す。

取得・検証に失敗した場合は例外を送出せず、呼び出し側(Dispatcher)がログ+AuditLog
記録のうえで既存キャッシュのまま処理を継続できるようにする(戻り値のDownloadOutcomeで
成否を返す)。
"""

from __future__ import annotations

import datetime as dt
import logging
import urllib.request
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel

from jstock_advisor.infrastructure.collection_store import (
    resolve_candidate_universe_bucket,
    resolve_candidate_universe_local_cache_dir,
    running_on_lambda,
)
from jstock_advisor.providers.candidate_universe.jpx_impl import (
    parse_jpx400_weight_csv,
    parse_listed_issues_xls,
)

logger = logging.getLogger(__name__)

_CURRENT_PREFIX = "current"
_ARCHIVE_PREFIX = "archive"
_USER_AGENT = "jstock-advisor/1.0 (+watchlist candidate universe downloader)"
_FETCH_TIMEOUT_SECONDS = 60

# 検証しきい値(実データで確認したおおよその件数に安全マージンを持たせた値)。
# listed_issues: プライム+スタンダード合計約3,122件(2026-07時点)。
# jpx400: 構成銘柄は常に約400件。
_MIN_RESPONSE_BYTES = {"listed_issues": 100_000, "jpx400": 5_000}
_ROW_COUNT_BOUNDS = {"listed_issues": (2500, 4000), "jpx400": (300, 450)}
_MAX_INVALID_CODE_RATE = 0.01
_MAX_SELECTED_COUNT_CHANGE_RATE = 0.10
# 運用ハードニング7節。
_MAX_DUPLICATE_RATE = 0.01
_MAX_UNKNOWN_SEGMENT_RATE = 0.01
_HTML_SNIFF_BYTES = 512
_HTML_MARKERS = (b"<html", b"<!doctype html", b"<body")


class CacheMetadata(BaseModel):
    """8節: source_date(元データの公開日)を鮮度判定の主基準とし、取得・検証・
    昇格の各時刻を別々に保持する(S3のLastModifiedだけでは、配信元が同じ内容の
    ファイルを更新せず公開し続けている場合に古さを検知できないため)。
    """

    source_date: dt.date | None
    downloaded_at: dt.datetime
    validated_at: dt.datetime
    promoted_at: dt.datetime
    raw_row_count: int
    selected_count: int
    invalid_code_count: int


@dataclass(frozen=True)
class DownloadOutcome:
    source: str
    promoted: bool
    reason: str | None  # promoted=Falseの場合の失敗理由
    metadata: CacheMetadata | None


class CandidateUniverseCacheIO:
    """S3(Lambda環境)またはローカルファイル(それ以外)で候補ユニバースキャッシュを
    読み書きする。running_on_lambda()による環境判定はinfrastructure/collection_store.py
    のrunning_on_lambda()/resolve_table_name()と同じパターン。
    """

    def __init__(self) -> None:
        self._on_lambda = running_on_lambda()
        if self._on_lambda:
            import boto3

            self._bucket = resolve_candidate_universe_bucket()
            self._s3 = boto3.client("s3")
        else:
            self._local_dir = resolve_candidate_universe_local_cache_dir()
            self._local_dir.mkdir(parents=True, exist_ok=True)

    def read_current(self, source: str) -> tuple[bytes, CacheMetadata] | None:
        if self._on_lambda:
            try:
                data_obj = self._s3.get_object(
                    Bucket=self._bucket, Key=f"{_CURRENT_PREFIX}/{source}/data"
                )
                meta_obj = self._s3.get_object(
                    Bucket=self._bucket, Key=f"{_CURRENT_PREFIX}/{source}/metadata.json"
                )
            except self._s3.exceptions.NoSuchKey:
                return None
            return data_obj["Body"].read(), CacheMetadata.model_validate_json(
                meta_obj["Body"].read()
            )

        data_path = self._local_dir / source / "data"
        meta_path = self._local_dir / source / "metadata.json"
        if not data_path.exists() or not meta_path.exists():
            return None
        return data_path.read_bytes(), CacheMetadata.model_validate_json(
            meta_path.read_text(encoding="utf-8")
        )

    def promote(self, source: str, data: bytes, metadata: CacheMetadata) -> None:
        metadata_json = metadata.model_dump_json().encode("utf-8")
        archive_date = metadata.downloaded_at.strftime("%Y%m%d")

        if self._on_lambda:
            self._s3.put_object(
                Bucket=self._bucket, Key=f"{_CURRENT_PREFIX}/{source}/data", Body=data
            )
            self._s3.put_object(
                Bucket=self._bucket,
                Key=f"{_CURRENT_PREFIX}/{source}/metadata.json",
                Body=metadata_json,
            )
            self._s3.put_object(
                Bucket=self._bucket,
                Key=f"{_ARCHIVE_PREFIX}/{source}/{archive_date}/data",
                Body=data,
            )
            self._s3.put_object(
                Bucket=self._bucket,
                Key=f"{_ARCHIVE_PREFIX}/{source}/{archive_date}/metadata.json",
                Body=metadata_json,
            )
            return

        current_dir = self._local_dir / source
        current_dir.mkdir(parents=True, exist_ok=True)
        (current_dir / "data").write_bytes(data)
        (current_dir / "metadata.json").write_bytes(metadata_json)
        archive_dir = self._local_dir / _ARCHIVE_PREFIX / source / archive_date
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "data").write_bytes(data)
        (archive_dir / "metadata.json").write_bytes(metadata_json)


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
        return cast(bytes, response.read())


def _looks_like_html(data: bytes) -> bool:
    """運用ハードニング7節: プロキシ/認証エラー等でHTMLエラーページが返された
    ケースを検知する(先頭バイトのみを緩く走査、xlrd/csvパース失敗より前に弾く)。
    """
    head = data[:_HTML_SNIFF_BYTES].lower()
    return any(marker in head for marker in _HTML_MARKERS)


def _validate(
    source: str,
    data: bytes,
    raw_row_count: int,
    invalid_code_count: int,
    duplicate_count: int,
    unknown_market_segment_count: int | None,
    selected_count: int,
    source_date: dt.date | None,
    previous_selected_count: int | None,
    now: dt.datetime,
) -> str | None:
    """検証失敗時は理由文字列、成功時はNoneを返す。"""
    if _looks_like_html(data):
        return "レスポンスがHTMLエラーページと判定されました"
    min_bytes = _MIN_RESPONSE_BYTES[source]
    if len(data) < min_bytes:
        return f"レスポンスサイズが小さすぎます: {len(data)}バイト(最小{min_bytes}バイト)"
    if source_date is None:
        return "ソース日付を取得できませんでした"
    if source_date > now.date():
        return f"ソース日付が未来です: {source_date}"
    low, high = _ROW_COUNT_BOUNDS[source]
    if not (low <= raw_row_count <= high):
        return f"行数が想定範囲外です: {raw_row_count}件(想定{low}〜{high}件)"
    if raw_row_count > 0 and invalid_code_count / raw_row_count > _MAX_INVALID_CODE_RATE:
        rate = invalid_code_count / raw_row_count
        return f"不正コード率が高すぎます: {rate:.1%}(上限{_MAX_INVALID_CODE_RATE:.0%})"
    if raw_row_count > 0 and duplicate_count / raw_row_count > _MAX_DUPLICATE_RATE:
        rate = duplicate_count / raw_row_count
        return f"重複率が高すぎます: {rate:.1%}(上限{_MAX_DUPLICATE_RATE:.0%})"
    if (
        unknown_market_segment_count is not None
        and raw_row_count > 0
        and unknown_market_segment_count / raw_row_count > _MAX_UNKNOWN_SEGMENT_RATE
    ):
        rate = unknown_market_segment_count / raw_row_count
        return f"未知の市場区分率が高すぎます: {rate:.1%}(上限{_MAX_UNKNOWN_SEGMENT_RATE:.0%})"
    if previous_selected_count is not None and previous_selected_count > 0:
        change_rate = abs(selected_count - previous_selected_count) / previous_selected_count
        if change_rate > _MAX_SELECTED_COUNT_CHANGE_RATE:
            return (
                f"前回からの件数変化率が大きすぎます: {previous_selected_count}件→"
                f"{selected_count}件({change_rate:.1%})"
            )
    return None


def _download_listed_issues(
    cache_io: CandidateUniverseCacheIO,
    url: str,
    target_market_segments: set[str] | None,
    now: dt.datetime,
) -> DownloadOutcome:
    source = "listed_issues"
    try:
        data = _fetch(url)
    except Exception as exc:  # noqa: BLE001 - 取得失敗は既存キャッシュ継続のため例外化しない
        logger.exception("candidate universe download failed source=%s", source)
        return DownloadOutcome(source=source, promoted=False, reason=str(exc), metadata=None)

    try:
        parsed = parse_listed_issues_xls(data, target_market_segments)
    except Exception as exc:  # noqa: BLE001 - パース失敗も既存キャッシュ継続のため例外化しない
        logger.exception("candidate universe parse failed source=%s", source)
        return DownloadOutcome(source=source, promoted=False, reason=str(exc), metadata=None)

    previous = cache_io.read_current(source)
    previous_selected = previous[1].selected_count if previous is not None else None

    reason = _validate(
        source,
        data,
        parsed.raw_row_count,
        parsed.invalid_code_count,
        parsed.duplicate_count,
        parsed.unknown_market_segment_count,
        len(parsed.items),
        parsed.source_date,
        previous_selected,
        now,
    )
    if reason is not None:
        logger.error("candidate universe validation failed source=%s reason=%s", source, reason)
        return DownloadOutcome(source=source, promoted=False, reason=reason, metadata=None)

    validated_at = dt.datetime.now(dt.UTC)
    metadata = CacheMetadata(
        source_date=parsed.source_date,
        downloaded_at=now,
        validated_at=validated_at,
        promoted_at=dt.datetime.now(dt.UTC),
        raw_row_count=parsed.raw_row_count,
        selected_count=len(parsed.items),
        invalid_code_count=parsed.invalid_code_count,
    )
    cache_io.promote(source, data, metadata)
    logger.info(
        "candidate universe promoted source=%s selected_count=%d source_date=%s",
        source,
        len(parsed.items),
        parsed.source_date,
    )
    return DownloadOutcome(source=source, promoted=True, reason=None, metadata=metadata)


def _download_jpx400(
    cache_io: CandidateUniverseCacheIO, url: str, now: dt.datetime
) -> DownloadOutcome:
    source = "jpx400"
    try:
        data = _fetch(url)
    except Exception as exc:  # noqa: BLE001
        logger.exception("candidate universe download failed source=%s", source)
        return DownloadOutcome(source=source, promoted=False, reason=str(exc), metadata=None)

    try:
        parsed = parse_jpx400_weight_csv(data)
    except Exception as exc:  # noqa: BLE001
        logger.exception("candidate universe parse failed source=%s", source)
        return DownloadOutcome(source=source, promoted=False, reason=str(exc), metadata=None)

    previous = cache_io.read_current(source)
    previous_selected = previous[1].selected_count if previous is not None else None

    reason = _validate(
        source,
        data,
        parsed.raw_row_count,
        parsed.invalid_code_count,
        parsed.duplicate_count,
        None,  # jpx400には市場区分列が無いため未知区分チェックの対象外
        len(parsed.member_codes),
        parsed.source_date,
        previous_selected,
        now,
    )
    if reason is not None:
        logger.error("candidate universe validation failed source=%s reason=%s", source, reason)
        return DownloadOutcome(source=source, promoted=False, reason=reason, metadata=None)

    metadata = CacheMetadata(
        source_date=parsed.source_date,
        downloaded_at=now,
        validated_at=dt.datetime.now(dt.UTC),
        promoted_at=dt.datetime.now(dt.UTC),
        raw_row_count=parsed.raw_row_count,
        selected_count=len(parsed.member_codes),
        invalid_code_count=parsed.invalid_code_count,
    )
    cache_io.promote(source, data, metadata)
    logger.info(
        "candidate universe promoted source=%s selected_count=%d source_date=%s",
        source,
        len(parsed.member_codes),
        parsed.source_date,
    )
    return DownloadOutcome(source=source, promoted=True, reason=None, metadata=metadata)


def refresh_candidate_universe_cache(
    jpx_listed_issues_url: str,
    jpx_400_weight_url: str,
    target_market_segments: list[str] | None,
    now: dt.datetime,
) -> list[DownloadOutcome]:
    """東証上場銘柄一覧・JPX400構成銘柄の両方を取得・検証・(成功時のみ)昇格する。

    Dispatcherの通常起動時、および`jstock candidate-universe refresh`(ローカル
    リハーサル用)の両方から呼ぶ(6節: 初回キャッシュ作成フローの統一)。
    """
    cache_io = CandidateUniverseCacheIO()
    segments = set(target_market_segments) if target_market_segments is not None else None
    return [
        _download_listed_issues(cache_io, jpx_listed_issues_url, segments, now),
        _download_jpx400(cache_io, jpx_400_weight_url, now),
    ]
