"""Issue #495(#413): watchlist_data_cache の logger level を WARNING で明示する(意図した静音)。

`get_or_fetch()` の INFO は cache の hit / miss(期限切れ)/ miss(不在)の 3 か所で、候補ごとに
最大 9 行になり高頻度である。当初設計(#413 issuecomment-5738498397 §2)は INFO を有効化せず、
**`setLevel(logging.WARNING)` と理由で明示する**とした(MANAGER の判断: #495 は通常運用の logger 設計
= WARNING 維持・常時 INFO 化しない。#5 の calibration 用の高粒度な計測は別責務)。
ここでは次を確認する。

    1 宣言が実際に効く: Lambda の root logger の既定(WARNING)のもとで、INFO は無効・WARNING は有効。
    2 意図した静音: INFO の 3 経路(hit / miss〔期限切れ〕/ miss〔不在〕)を実際に通しても、
      root logger を INFO にしても INFO は出力されない(module の宣言が効いている)。
    3 挙動が変わっていない: hit / miss の判定・返り値・stats の集計は、静音にしても同じ。
      (集計は handler 側の CacheStats ログで取得できる、という理由の根拠。handler 側の集計ログが
      INFO で出続けることも、実際のコードで確認する。)

宣言があること自体は tests/unit/test_issue_413_logger_level_declared.py(#413 の guard)が見る。
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from jstock_advisor.infrastructure.collection_store import build_collection_store
from jstock_advisor.lambda_handlers import watchlist_worker_handler
from jstock_advisor.services import watchlist_data_cache
from jstock_advisor.services.watchlist_data_cache import (
    CacheEntry,
    CacheStats,
    _classify_optional,
    get_or_fetch,
)

_MODULE = watchlist_data_cache.__name__
_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_TTL_HOURS = 24
_NEGATIVE_TTL_MINUTES = 15
_KEY = "price:0000:2026-08-01"  # 架空の cache key(銘柄コードは架空値)
_ADAPTER: TypeAdapter[Decimal | None] = TypeAdapter(Decimal | None)


@pytest.fixture
def repo(tmp_path: Path):
    return build_collection_store(CacheEntry, "test_cache.json", "cache_key", tmp_path)


def _call(repo, now: dt.datetime, stats: CacheStats, fetched: list[str]) -> Decimal | None:
    def fetch() -> Decimal | None:
        fetched.append("fetch")
        return Decimal("100")

    return get_or_fetch(
        repo,
        _KEY,
        _TTL_HOURS,
        _NEGATIVE_TTL_MINUTES,
        now,
        fetch,
        _ADAPTER,
        _classify_optional,
        "price",
        stats=stats,
    )


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _MODULE]


def test_declared_level_takes_effect_under_the_lambda_root_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lambda の root(WARNING)のもとで、INFO は無効・WARNING は有効(宣言が効いている)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)

    logger = logging.getLogger(_MODULE)
    assert logger.level == logging.WARNING  # 明示されている(NOTSET ではない)
    assert not logger.isEnabledFor(logging.INFO)
    assert logger.isEnabledFor(logging.WARNING)


def test_the_three_info_paths_are_silent_even_when_the_root_logger_is_at_info(
    repo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """意図した静音: 不在 → 新鮮な hit → 期限切れ の 3 経路を通しても INFO は出ない。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.INFO)
    stats = CacheStats()
    fetched: list[str] = []

    with caplog.at_level(logging.INFO):  # root と handler を INFO にする(module は変えない)
        _call(repo, _NOW, stats, fetched)  # miss(absent)
        _call(repo, _NOW + dt.timedelta(hours=1), stats, fetched)  # hit
        _call(repo, _NOW + dt.timedelta(hours=_TTL_HOURS + 1), stats, fetched)  # miss(expired)

    assert _records(caplog) == []  # INFO は出ない(この経路に WARNING は無い)


def test_behavior_is_unchanged_by_the_silence(repo) -> None:
    """判定・返り値・stats は静音と無関係(不在 = miss、期限内 = hit、期限切れ = miss)。"""
    stats = CacheStats()
    fetched: list[str] = []

    first = _call(repo, _NOW, stats, fetched)
    second = _call(repo, _NOW + dt.timedelta(hours=1), stats, fetched)
    third = _call(repo, _NOW + dt.timedelta(hours=_TTL_HOURS + 1), stats, fetched)

    assert first == second == third == Decimal("100")
    assert fetched == ["fetch", "fetch"]  # 期限内の 2 回目は取り直さない
    assert (stats.hit_count, stats.miss_count) == (1, 2)


def test_every_info_call_site_in_the_module_is_reached_by_the_paths_above() -> None:
    """検査の網羅: この module の INFO は 3 か所(経路も 3 つ)。INFO が増えたら赤くなる。"""
    tree = ast.parse(Path(watchlist_data_cache.__file__).read_text(encoding="utf-8"))
    infos = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "info"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "logger"
    ]
    assert len(infos) == 3


def test_the_aggregate_is_still_logged_by_the_handler_at_info() -> None:
    """理由の根拠: 集計は handler 側の CacheStats ログで出ている(候補ごとの詳細は要らない)。

    handler の logger が INFO を宣言していて、"watchlist worker cache stats hit=… miss=…" を
    INFO で出す(この PR は handler を変更していない)。
    """
    path = Path(watchlist_worker_handler.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))

    declares_info = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "setLevel"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "logger"
        and isinstance(n.args[0], ast.Attribute)
        and n.args[0].attr == "INFO"
        for n in ast.walk(tree)
    )
    aggregate_infos = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "info"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and isinstance(n.args[0].value, str)
        and n.args[0].value.startswith("watchlist worker cache stats hit=")
    ]

    assert declares_info
    assert len(aggregate_infos) == 1
