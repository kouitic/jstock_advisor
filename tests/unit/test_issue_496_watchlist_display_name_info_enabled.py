"""Issue #496(#413): watchlist_display_name の INFO が Lambda で出力されるようにする。

`JpxStockNameSource` は、JPX 銘柄名 map を読み込んだときに INFO を 1 行出す
(`JPX stock name map loaded count=N`。件数のみ)。Lambda の root logger は WARNING なので、
module が level を宣言していないとこの INFO は出力されなかった。ここでは次を確認する。

    1 宣言が実際に効く: Lambda の root(WARNING)のもとで、INFO は有効。
    2 INFO が実際に出力される: root が WARNING でも、読み込み成功時に 1 行だけ出る(件数のみ)。
    3 銘柄名・stock_code が出力に現れない: 架空の銘柄名・コードを「実際に読まれる map」に置く。
    4 コンテナ生存期間中は再出力されない。根拠は 2 層:
      (a)インスタンス内の成功キャッシュ(再 load しない)
      (b)module 共有(共有アクセサと標準の構築方法が、呼び出しをまたいで同じ実体を返す)
      (b)が壊れる(呼び出しごとに新規構築する)と、INFO は呼び出しごとに 1 行出る(レビュー指摘)。
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


def _reset_shared_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """共有インスタンスを未生成に戻す(テスト間の独立。テスト後に元の値へ戻る)。"""
    monkeypatch.setattr(watchlist_display_name, "_shared_jpx_stock_name_source", None)


def test_the_shared_accessor_returns_the_same_instance_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) module 共有: 呼び出しをまたいで同じ実体を返す(壊すと、共有をやめた変異が赤になる)。"""
    _reset_shared_source(monkeypatch)

    first = watchlist_display_name.get_shared_jpx_stock_name_source(60)
    second = watchlist_display_name.get_shared_jpx_stock_name_source(60)

    assert first is second


def test_the_standard_builder_uses_the_shared_source_and_loads_only_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """標準の構築方法を複数回呼んでも、同じ source を使い、読み込みも INFO も 1 回だけ。

    finalizer・会話・CLI が使う `build_stock_display_name_resolver` 経由で確認する
    (呼び出しごとに新規構築すると、読み込みと INFO が呼び出しの数だけ増える)。
    """
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    _reset_shared_source(monkeypatch)
    loads: list[None] = []

    def _loader() -> dict[str, str] | None:
        loads.append(None)
        return dict(_FAKE_MAP)

    monkeypatch.setattr(watchlist_display_name, "_load_jpx_stock_name_map", _loader)
    # 標準の構築方法が作るローカルの repository は、この検査の対象外(実ファイルに触れさせない)。
    monkeypatch.setattr(watchlist_display_name, "StockNameOverrideRepository", lambda: object())
    monkeypatch.setattr(watchlist_display_name, "WatchlistRepository", lambda: object())

    resolvers = [watchlist_display_name.build_stock_display_name_resolver(60) for _ in range(3)]
    for resolver in resolvers:
        assert resolver._jpx_name_source.get("0001") == "架空銘柄アルファ"

    assert len({id(r._jpx_name_source) for r in resolvers}) == 1  # 同じ実体
    assert len(loads) == 1
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
