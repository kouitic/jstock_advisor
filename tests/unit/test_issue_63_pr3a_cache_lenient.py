"""Issue #63 PR-3a(A-U3 前半): cache 5 件を LENIENT + item_id PLAIN で宣言する。

不正レコードは fixture としてのみ作る。**Production への注入は行わない。**
値はすべて架空値であり、実在人物の情報・実際の保有データを含めない。

本 PR の対象は cache だけである。cache の decode 失敗は 1 件 skip しても
次回取得で置き換わるため、collection 全体を止める理由がない。
audit_log(やり直しの経路が無い)は PR-3b で別に扱う。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import BaseModel

from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore
from jstock_advisor.infrastructure.record_failure_policy import (
    ItemIdDisclosure,
    RecordFailurePolicy,
)

# cache の主キーの形（架空値）。銘柄コードは実在しない "0000" を使う。
_BROKEN_KEY = "latest_price:0000:2026-09-07"
_GOOD_KEY = "latest_price:0001:2026-09-07"


class _CacheRow(BaseModel):
    cache_key: str
    payload: int


def _seed(tmp_path: Path) -> Path:
    """正常 1 件 + 不正 1 件（payload が数値でない）を置く。"""
    path = tmp_path / "cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {"cache_key": _GOOD_KEY, "payload": 1},
                {"cache_key": _BROKEN_KEY, "payload": "not-a-number"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _cache_store(tmp_path: Path) -> JsonCollectionStore[_CacheRow]:
    """PR-3a で cache 5 件へ宣言するのと同じ組み合わせ。"""
    return JsonCollectionStore(
        _CacheRow,
        "cache.json",
        "cache_key",
        tmp_path,
        failure_policy=RecordFailurePolicy.LENIENT,
        item_id_disclosure=ItemIdDisclosure.PLAIN,
    )


def test_lenient_returns_the_remaining_cache_entries(tmp_path: Path) -> None:
    """1 件の不正で cache 全体が読めなくなることはない。"""
    _seed(tmp_path)

    rows = _cache_store(tmp_path).list_all()

    assert [r.cache_key for r in rows] == [_GOOD_KEY]


def test_broken_entry_behaves_as_a_cache_miss(tmp_path: Path) -> None:
    """★ 壊れた 1 件は「cache に無い」= miss として扱われる。

    呼び出し側（get_or_fetch）はこれを miss として数え、provider から
    取り直して上書きする。**失敗が蓄積しない**ため、件数を数える基盤
    （#245）を待たずに LENIENT にできる、というのが PR-3a の前提である。
    """
    _seed(tmp_path)
    store = _cache_store(tmp_path)

    assert store.get(_BROKEN_KEY) is None, "壊れた 1 件は miss として見える"
    assert store.get(_GOOD_KEY) is not None, "他の entry は読める"


def test_refetch_overwrites_the_broken_entry(tmp_path: Path) -> None:
    """★ 次回取得で置き換わる（自然に解消する）。"""
    path = _seed(tmp_path)
    store = _cache_store(tmp_path)

    store.upsert(_CacheRow(cache_key=_BROKEN_KEY, payload=42))

    rows = json.loads(path.read_text(encoding="utf-8"))
    by_key = {r["cache_key"]: r for r in rows}
    assert by_key[_BROKEN_KEY]["payload"] == 42, "取り直した値で置き換わること"
    assert _GOOD_KEY in by_key, "他の entry は残ること"


def test_plain_disclosure_shows_the_cache_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """PLAIN を宣言した cache では item_id が平文で出る。

    主キーは "<用途>:<銘柄コード>:<日付>" であり所有者名を含まない
    （#135 Phase A が実測した 6 collection と重ならない）。
    """
    _seed(tmp_path)

    with caplog.at_level(logging.WARNING):
        _cache_store(tmp_path).list_all()

    assert _BROKEN_KEY in caplog.text


def test_undeclared_collection_keeps_strict_and_hash(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 宣言していない collection は STRICT + HASH のまま（回帰の固定）。

    PR-3a は cache 5 件だけを宣言する。既定を変えていないことを固定し、
    「宣言しなければ挙動は変わらない」という PR-2 の前提を守る。
    """
    _seed(tmp_path)
    default_store: JsonCollectionStore[_CacheRow] = JsonCollectionStore(
        _CacheRow, "cache.json", "cache_key", tmp_path
    )

    with pytest.raises(Exception):  # noqa: B017 - pydantic の ValidationError をそのまま通す
        default_store.list_all()

    assert _BROKEN_KEY not in caplog.text, "既定では平文の主キーを出さない"
