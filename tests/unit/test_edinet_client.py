"""EdinetClientの取得結果型(Issue #53 Phase B1)。

「取得できて0件」と「取得に失敗した」を型で区別できること、および
書類一覧を必要な書類種別・項目だけへ射影することを検証する。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import urllib.error
from typing import Any

import pytest

from jstock_advisor.infrastructure.edinet import client as client_module
from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.edinet.types import (
    EdinetFailureReason,
    EdinetFetchStatus,
)

_DATE = dt.date(2026, 8, 31)


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, behaviour: Any) -> None:
    def fake_urlopen(url: str, timeout: int = 0) -> Any:
        if isinstance(behaviour, Exception):
            raise behaviour
        return _FakeResponse(behaviour)

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)


def _documents_body(results: list[dict[str, Any]]) -> bytes:
    return json.dumps({"results": results}).encode("utf-8")


def _raw_entry(**overrides: Any) -> dict[str, Any]:
    entry = {
        "secCode": "29140",
        "docID": "DOC1",
        "docTypeCode": "180",
        "submitDateTime": "2026-08-31 15:45",
        "periodEnd": "2026-03-31",
        "filerName": "サンプル株式会社",
    }
    entry.update(overrides)
    return entry


# --- 成功系 -----------------------------------------------------------------


def test_documents_found_returns_success_with_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, _documents_body([_raw_entry()]))

    result = EdinetClient(api_key="k").list_documents(_DATE)

    assert result.status is EdinetFetchStatus.SUCCESS_WITH_DOCUMENTS
    assert result.succeeded is True
    assert result.failure_reason is None
    assert [e.doc_id for e in result.entries] == ["DOC1"]
    assert result.entries[0].submit_date_time == "2026-08-31 15:45"
    assert result.entries[0].filer_name == "サンプル株式会社"


def test_empty_results_is_success_empty_not_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, _documents_body([]))

    result = EdinetClient(api_key="k").list_documents(_DATE)

    assert result.status is EdinetFetchStatus.SUCCESS_EMPTY
    assert result.succeeded is True
    assert result.entries == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"docTypeCode": "350"},  # 大量保有報告書等、本システムが参照しない種別
        {"secCode": None},  # 証券コードを持たない提出者
        {"docID": None},
    ],
)
def test_entries_are_projected_to_needed_documents_only(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    _patch_urlopen(monkeypatch, _documents_body([_raw_entry(**overrides)]))

    result = EdinetClient(api_key="k").list_documents(_DATE)

    assert result.status is EdinetFetchStatus.SUCCESS_EMPTY
    assert result.entries == []


@pytest.mark.parametrize("doc_type", ["120", "130", "160", "170", "180", "190"])
def test_all_consumed_doc_type_codes_are_kept(
    monkeypatch: pytest.MonkeyPatch, doc_type: str
) -> None:
    _patch_urlopen(monkeypatch, _documents_body([_raw_entry(docTypeCode=doc_type)]))

    result = EdinetClient(api_key="k").list_documents(_DATE)

    assert [e.doc_type_code for e in result.entries] == [doc_type]


# --- 失敗系(0件へ潰さない) ------------------------------------------------


def test_not_configured_is_failure_without_http_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("APIキー未設定ではHTTP呼び出しを行わない")

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fail)

    result = EdinetClient(api_key=None).list_documents(_DATE)

    assert result.status is EdinetFetchStatus.FETCH_FAILED
    assert result.failure_reason is EdinetFailureReason.NOT_CONFIGURED


@pytest.mark.parametrize(
    ("error", "expected_reason"),
    [
        (TimeoutError(), EdinetFailureReason.TIMEOUT),
        (urllib.error.URLError("boom"), EdinetFailureReason.HTTP_ERROR),
        (OSError("socket"), EdinetFailureReason.OTHER),
    ],
)
def test_transport_errors_are_reported_as_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected_reason: EdinetFailureReason
) -> None:
    _patch_urlopen(monkeypatch, error)

    result = EdinetClient(api_key="k").list_documents(_DATE)

    assert result.status is EdinetFetchStatus.FETCH_FAILED
    assert result.failure_reason is expected_reason
    assert result.entries == []


def test_invalid_json_is_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, b"not json")

    result = EdinetClient(api_key="k").list_documents(_DATE)

    assert result.status is EdinetFetchStatus.FETCH_FAILED
    assert result.failure_reason is EdinetFailureReason.PARSE_ERROR


def test_missing_results_key_is_parse_error_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """resultsが欠落した応答を「開示0件」として扱わないこと。"""
    _patch_urlopen(monkeypatch, json.dumps({"metadata": {}}).encode("utf-8"))

    result = EdinetClient(api_key="k").list_documents(_DATE)

    assert result.status is EdinetFetchStatus.FETCH_FAILED
    assert result.failure_reason is EdinetFailureReason.PARSE_ERROR


# --- ZIPダウンロード --------------------------------------------------------


def test_download_success_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, b"zip-bytes")

    result = EdinetClient(api_key="k").download_document_zip("DOC1")

    assert result.succeeded is True
    assert result.payload == b"zip-bytes"


@pytest.mark.parametrize(
    ("error", "expected_reason"),
    [
        (TimeoutError(), EdinetFailureReason.TIMEOUT),
        (urllib.error.URLError("boom"), EdinetFailureReason.DOWNLOAD_ERROR),
    ],
)
def test_download_failure_is_not_flattened_to_none(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected_reason: EdinetFailureReason
) -> None:
    _patch_urlopen(monkeypatch, error)

    result = EdinetClient(api_key="k").download_document_zip("DOC1")

    assert result.status is EdinetFetchStatus.FETCH_FAILED
    assert result.failure_reason is expected_reason
    assert result.payload is None


def test_download_empty_payload_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, b"")

    result = EdinetClient(api_key="k").download_document_zip("DOC1")

    assert result.status is EdinetFetchStatus.FETCH_FAILED
    assert result.failure_reason is EdinetFailureReason.DOWNLOAD_ERROR


def test_download_not_configured_is_failure() -> None:
    result = EdinetClient(api_key=None).download_document_zip("DOC1")

    assert result.status is EdinetFetchStatus.FETCH_FAILED
    assert result.failure_reason is EdinetFailureReason.NOT_CONFIGURED
