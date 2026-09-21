"""Issue #496(#413): watchlist_display_name の INFO が Lambda で出力されるようにする。

`JpxStockNameSource` は、JPX 銘柄名 map を読み込んだときに INFO を 1 行出す
(`JPX stock name map loaded count=N`。件数のみ)。Lambda の root logger は WARNING なので、
module が level を宣言していないとこの INFO は出力されなかった。ここでは次を確認する。

    1 宣言が実際に効く: Lambda の root(WARNING)のもとで、INFO は有効。
    2 INFO が実際に出力される: root が WARNING でも、読み込み成功時に 1 行だけ出る(件数のみ)。
    3 銘柄名・stock_code が出力に現れない: 架空の銘柄名・コードを「実際に読まれる map」に置く。
    4 コンテナ生存期間中は再出力されない(成功キャッシュ。出力量が増えない根拠)。
    5 既存の WARNING は変わっていない(文面・level・件数)。

宣言があること自体は tests/unit/test_issue_413_logger_level_declared.py(#413 の guard)が見る。
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
from pathlib import Path

import pytest

from jstock_advisor.services import watchlist_display_name
from jstock_advisor.services.watchlist_display_name import JpxStockNameSource

_MODULE = watchlist_display_name.__name__
_NOW = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
# 架空の銘柄コード・銘柄名(実在しない値)。出力に現れてはならない。
_FAKE_MAP = {"0001": "架空銘柄アルファ", "0002": "架空銘柄ブラボー"}


def _source(monkeypatch: pytest.MonkeyPatch, loaded: dict[str, str] | None) -> JpxStockNameSource:
    """読み込み結果を差し替えた JpxStockNameSource(実際に読まれる map = loaded)。"""
    monkeypatch.setattr(
        "jstock_advisor.services.watchlist_display_name._load_jpx_stock_name_map",
        lambda: loaded,
    )
    return JpxStockNameSource(negative_cache_ttl_seconds=60, clock=lambda: _NOW)


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _MODULE]


def test_declared_level_takes_effect_under_the_lambda_root_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lambda の root(WARNING)のもとで、この module の logger は INFO が有効(宣言が効いている)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)

    logger = logging.getLogger(_MODULE)
    assert logger.level == logging.INFO  # 明示されている(NOTSET ではない)
    assert logger.isEnabledFor(logging.INFO)


def test_info_is_emitted_when_the_root_logger_is_at_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """root が WARNING でも、map の読み込み成功時に INFO が 1 行出る(件数のみ)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    source = _source(monkeypatch, dict(_FAKE_MAP))

    assert source.get("0001") == "架空銘柄アルファ"

    infos = [r for r in _records(caplog) if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert infos[0].getMessage() == "JPX stock name map loaded count=2"


def test_the_output_contains_neither_stock_names_nor_stock_codes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """出力(文面と引数)に、実際に読まれた map の銘柄名・stock_code が現れない。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    source = _source(monkeypatch, dict(_FAKE_MAP))

    source.get("0001")
    source.is_known("0002")

    records = _records(caplog)
    assert records  # 検査が空振りしない(INFO が実際に出ている)
    for record in records:
        rendered = record.getMessage() + repr(record.args)
        for code, name in _FAKE_MAP.items():
            assert code not in rendered
            assert name not in rendered


def test_the_info_is_not_repeated_while_the_success_cache_is_kept(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """成功キャッシュがある間は再読み込みしない = INFO も再出力されない(出力量が増えない根拠)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    source = _source(monkeypatch, dict(_FAKE_MAP))

    for _ in range(5):
        source.get("0001")
        source.is_known("0002")

    infos = [r for r in _records(caplog) if r.levelno == logging.INFO]
    assert len(infos) == 1


def test_no_info_is_emitted_when_the_load_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """読み込みに失敗したときは INFO を出さない(失敗の WARNING は loader 側の責務)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    source = _source(monkeypatch, None)

    assert source.get("0001") is None

    assert [r for r in _records(caplog) if r.levelno == logging.INFO] == []


def test_the_existing_warnings_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """既存の WARNING(loader の 2 経路)は、文面・level が従来どおり。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)

    class _NoCache:
        def read_current(self, source: str) -> None:
            del source

    class _Broken:
        def read_current(self, source: str) -> None:
            del source
            raise RuntimeError("boom")

    monkeypatch.setattr(watchlist_display_name, "CandidateUniverseCacheIO", _NoCache)
    assert watchlist_display_name._load_jpx_stock_name_map() is None
    monkeypatch.setattr(watchlist_display_name, "CandidateUniverseCacheIO", _Broken)
    assert watchlist_display_name._load_jpx_stock_name_map() is None

    warnings = [r for r in _records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    first, second = (w.getMessage() for w in warnings)
    assert "JPX stock name map load failed: no cached listed_issues data" in first
    assert "JPX stock name map load failed error_type=RuntimeError" in second


def test_the_module_has_exactly_one_info_and_five_warning_call_sites() -> None:
    """検査の網羅: INFO は 1 か所・WARNING は 5 か所(増減したら赤くなり、記述の更新が要る合図)。"""
    tree = ast.parse(Path(watchlist_display_name.__file__).read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
            and node.func.attr in {"info", "warning"}
        ):
            counts[node.func.attr] = counts.get(node.func.attr, 0) + 1

    assert counts == {"info": 1, "warning": 5}
