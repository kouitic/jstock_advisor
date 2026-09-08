"""Issue #280(#63 A-U5 / F-F6): sent_at に naive と aware が混ざっても並べ替えが落ちない。

```
問題  notification_log の読み取りは `sent_at` で並べ替える。
      timezone を持つ値(aware)と持たない値(naive)が混ざると Python は比較できず
      `TypeError` を送出し、**通知履歴の読み取りが止まる**(再送判定ができなくなる)。

★ 同じファイルには正規化関数 `_sent_at_as_utc()` が既にあった。
  ただし通っていたのは**保存用のソートキー生成**だけで、
  **読み取り側の sorted() は通っていなかった**。
  = 規則は決まっているのに、片側へ適用されていなかった。
```

```
★ naive が実在しうる経路(実測)
  書き込み側は常に `dt.datetime.now(dt.UTC)` を渡すため正規形は aware UTC。
  一方 `NotificationLog.sent_at` には `require_timezone_aware()` の検証が無く、
  **offset を持たない ISO 文字列は naive のまま復元される**
  (pydantic: "2026-09-08T23:30:00" -> tzinfo=None)。
  したがって旧データ・移行データが naive を持てば、そのまま読み込まれる。
```

```
★ 本テストは**保存値を変えない**ことも固定する(受入条件 A-U5-3)。
  正規化するのは比較キーだけであり、移行・backfill は行わない。
★ 値はすべて架空値。銘柄コードは実在しない "0000" を使う。
  所有者名・保有数量・取得単価は 1 つも含まない。
★ Production への注入は行わない。fixture を tmp_path へ書くだけである。
```
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import NotificationType
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
    _sent_at_as_utc,
)

_STOCK = "0000"
_HOLDING = "holding-0001"
_RECOMMENDATION = "rec-0001"

#: naive で保存された旧レコード。UTC とみなすと 2026-09-08T23:30Z。
#: ★ JST では **2026-09-09 08:30** であり、UTC 暦日と JST 暦日が食い違う値を
#:   意図的に選んでいる(C-BS: 実際に分岐する状態)。
_NAIVE_RAW = "2026-09-08T23:30:00"
_NAIVE_AS_UTC = dt.datetime(2026, 9, 8, 23, 30, tzinfo=dt.UTC)

#: aware で保存された新しいレコード。naive の 30 分前。
_AWARE_EARLIER_RAW = "2026-09-08T23:00:00+00:00"
_AWARE_EARLIER = dt.datetime(2026, 9, 8, 23, 0, tzinfo=dt.UTC)

#: aware で保存された、naive より後のレコード。
_AWARE_LATER_RAW = "2026-09-09T00:10:00+00:00"
_AWARE_LATER = dt.datetime(2026, 9, 9, 0, 10, tzinfo=dt.UTC)

_ID_AWARE_EARLIER = "11111111-1111-4111-8111-111111111111"
_ID_NAIVE = "22222222-2222-4222-8222-222222222222"
_ID_AWARE_LATER = "33333333-3333-4333-8333-333333333333"


def _row(notification_id: str, sent_at_raw: str) -> dict[str, Any]:
    """架空の通知履歴 1 件。`sent_at` の**文字列表現**を呼び出し側が決める。"""
    return {
        "notification_id": notification_id,
        "notification_type": NotificationType.SELL_SIGNAL.value,
        "stock_code": _STOCK,
        "content_hash": "hash-0001",
        "sent_at": sent_at_raw,
        "related_recommendation_id": _RECOMMENDATION,
        "owner": None,
        "holding_id": _HOLDING,
    }


def _seed(store_dir: Path, rows: list[dict[str, Any]]) -> Path:
    """★ 保存の順番を**わざと時系列とずらす**。

    並べ替えが効いていることと、入力順に依存していないことを分けて確かめるため。
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    path = store_dir / "notification_log.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


_MIXED_ROWS = [
    _row(_ID_NAIVE, _NAIVE_RAW),
    _row(_ID_AWARE_LATER, _AWARE_LATER_RAW),
    _row(_ID_AWARE_EARLIER, _AWARE_EARLIER_RAW),
]


# =============================================================================
# A) 混在しても落ちない / 順序が正しい（本 Issue の目的）
# =============================================================================


def test_mixed_naive_and_aware_does_not_raise_and_orders_by_utc_instant(
    tmp_path: Path,
) -> None:
    """A-U5-1: 混在でも例外にならず、**UTC の瞬間**で昇順に並ぶこと。"""
    _seed(tmp_path, _MIXED_ROWS)
    repo = NotificationLogRepository(store_dir=tmp_path)

    lookup = repo.list_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL)

    assert [n.notification_id for n in lookup.records] == [
        _ID_AWARE_EARLIER,  # 23:00Z
        _ID_NAIVE,  # 23:30(naive) = 23:30Z
        _ID_AWARE_LATER,  # 翌 00:10Z
    ]
    assert lookup.undecidable is False
    assert lookup.skipped == 0


def test_latest_is_the_newest_instant_even_when_it_is_the_naive_record(
    tmp_path: Path,
) -> None:
    """★ naive のレコードが**最新**の場合でも `latest` が正しいこと。

    再送判定は `latest` だけを見る。順序が壊れると
    「まだ送っていない」と誤読して**重複送信**になる。
    """
    _seed(tmp_path, [_row(_ID_AWARE_EARLIER, _AWARE_EARLIER_RAW), _row(_ID_NAIVE, _NAIVE_RAW)])
    repo = NotificationLogRepository(store_dir=tmp_path)

    latest = repo.latest_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL).latest

    assert latest is not None
    assert latest.notification_id == _ID_NAIVE


def test_the_unnormalized_key_would_still_raise_on_this_fixture(tmp_path: Path) -> None:
    """★ 修正前の壊れ方を**直接**固定する。

    正規化を外した比較(= 修正前の `key=lambda n: n.sent_at`)は、この同じ
    fixture で今も `TypeError` になる。これが成り立たなくなったら、
    fixture が naive / aware の混在を表さなくなったということであり、
    上のテストは**何も守っていない**ことになる。
    """
    _seed(tmp_path, _MIXED_ROWS)
    repo = NotificationLogRepository(store_dir=tmp_path)
    records = repo.list_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL).records

    with pytest.raises(TypeError):
        sorted(records, key=lambda n: n.sent_at)


# =============================================================================
# B) 混在していない場合は従来どおり（回帰）
# =============================================================================


def test_all_aware_records_are_unchanged(tmp_path: Path) -> None:
    """純 aware（= 現在の正規形）の並びが変わらないこと。"""
    _seed(
        tmp_path,
        [_row(_ID_AWARE_LATER, _AWARE_LATER_RAW), _row(_ID_AWARE_EARLIER, _AWARE_EARLIER_RAW)],
    )
    repo = NotificationLogRepository(store_dir=tmp_path)

    lookup = repo.list_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL)

    assert [n.notification_id for n in lookup.records] == [_ID_AWARE_EARLIER, _ID_AWARE_LATER]
    assert [n.sent_at for n in lookup.records] == [_AWARE_EARLIER, _AWARE_LATER]


def test_all_naive_records_are_unchanged(tmp_path: Path) -> None:
    """純 naive でも従来どおり並ぶこと（naive 同士は元から比較できる）。"""
    _seed(
        tmp_path,
        [
            _row(_ID_NAIVE, "2026-09-08T23:30:00"),
            _row(_ID_AWARE_EARLIER, "2026-09-08T23:00:00"),
        ],
    )
    repo = NotificationLogRepository(store_dir=tmp_path)

    lookup = repo.list_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL)

    assert [n.notification_id for n in lookup.records] == [_ID_AWARE_EARLIER, _ID_NAIVE]


# =============================================================================
# C) 境界 — 同一 instant を指す naive と aware
# =============================================================================


def test_naive_and_aware_pointing_at_the_same_instant_do_not_raise(tmp_path: Path) -> None:
    """境界: **同時刻**（naive を UTC とみなすと完全に一致）でも例外にならないこと。

    ★ 同値のときの前後関係は保証しない（Python の sort は安定であり、
      入力順が保たれる）。**どちらが先か**を固定すると、店側の走査順が
      変わっただけでテストが落ちるためである。
      ここで固定するのは「落ちないこと」と「1 件も落とさないこと」である。
    """
    _seed(
        tmp_path,
        [
            _row(_ID_NAIVE, "2026-09-08T23:30:00"),
            _row(_ID_AWARE_LATER, "2026-09-08T23:30:00+00:00"),
        ],
    )
    repo = NotificationLogRepository(store_dir=tmp_path)

    lookup = repo.list_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL)

    assert {n.notification_id for n in lookup.records} == {_ID_NAIVE, _ID_AWARE_LATER}
    assert len(lookup.records) == 2


# =============================================================================
# D) C-BS（§3.5 T3）— 実際に分岐する状態を固定 clock で確かめる
# =============================================================================


def test_c_bs_utc_jst_day_boundary_flips_the_resend_branch(tmp_path: Path) -> None:
    """★ C-BS: 並べ替えを誤ると**再送するかどうかの分岐が反転する**ことを固定する。

    下流(line_notification_service)の分岐は
        `evaluation_date_jst(latest.sent_at) == evaluation_date_jst(now)`
    である。同じなら「今日はもう送った」として抑止し、違えば送る。

    ★ 固定 clock は **UTC 暦日と JST 暦日が食い違う瞬間**を選んでいる。
      §3.5.4 の C-BS が求める「実際に分岐する状態」がここである
      (全境界の網羅は T1 の C-BM であり、本 PR の対象ではない)。

        now      2026-09-09T00:05Z  = JST 09-09 09:05
        aware A  2026-09-08T14:00Z  = JST 09-08 23:00   -> 別の日 -> **送る**
        naive B  2026-09-08T23:30   = 23:30Z = JST 09-09 08:30 -> 同じ日 -> **送らない**

    正しい latest は B(後の instant)。並べ替えが壊れて A を latest と誤ると、
    抑止すべき場面で**送ってしまう**。
    """
    now = dt.datetime(2026, 9, 9, 0, 5, tzinfo=dt.UTC)
    _seed(
        tmp_path,
        [
            _row(_ID_NAIVE, "2026-09-08T23:30:00"),
            _row(_ID_AWARE_EARLIER, "2026-09-08T14:00:00+00:00"),
        ],
    )
    repo = NotificationLogRepository(store_dir=tmp_path)

    records = repo.list_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL).records
    latest = records[-1]
    earliest = records[0]

    assert latest.notification_id == _ID_NAIVE
    assert earliest.notification_id == _ID_AWARE_EARLIER

    # 正しい latest では「今日はもう送った」側へ分岐する
    assert evaluation_date_jst(_sent_at_as_utc(latest.sent_at)) == evaluation_date_jst(now)
    # 取り違えた場合は「まだ送っていない」側へ**反転する**
    assert evaluation_date_jst(_sent_at_as_utc(earliest.sent_at)) != evaluation_date_jst(now)


def test_c_bs_naive_is_read_as_utc_not_as_local_time(tmp_path: Path) -> None:
    """★ A-U5-4: naive を **UTC** とみなす既定が読み取り経路でも効いていること。

    ローカル TZ で暗黙解釈すると、実行環境（開発機 = JST / Lambda = UTC）で
    並びが変わる。**環境によって結果が変わらない**ことを固定する。
    """
    _seed(
        tmp_path,
        [
            # naive 23:30 を JST とみなすと 14:30Z となり、aware 15:00Z より前になる。
            # UTC とみなせば 23:30Z であり **後ろ**になる。並びで区別できる。
            _row(_ID_NAIVE, "2026-09-08T23:30:00"),
            _row(_ID_AWARE_EARLIER, "2026-09-08T15:00:00+00:00"),
        ],
    )
    repo = NotificationLogRepository(store_dir=tmp_path)

    lookup = repo.list_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL)

    assert [n.notification_id for n in lookup.records] == [_ID_AWARE_EARLIER, _ID_NAIVE]


# =============================================================================
# E) 保存値と正規化規則（受入条件 A-U5-2 / A-U5-3）
# =============================================================================


def test_stored_values_are_not_rewritten(tmp_path: Path) -> None:
    """A-U5-3: 読み取りは**保存値を書き換えない**（移行・backfill を伴わない）。"""
    path = _seed(tmp_path, _MIXED_ROWS)
    before = path.read_text(encoding="utf-8")
    repo = NotificationLogRepository(store_dir=tmp_path)

    lookup = repo.list_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL)

    # ファイルの内容が 1 byte も変わっていない
    assert path.read_text(encoding="utf-8") == before
    # 返ってくる record の sent_at も読み込んだままの形（naive は naive のまま）
    naive_record = next(n for n in lookup.records if n.notification_id == _ID_NAIVE)
    assert naive_record.sent_at.tzinfo is None
    assert naive_record.sent_at == dt.datetime(2026, 9, 8, 23, 30)


def test_normalization_is_the_existing_shared_helper(tmp_path: Path) -> None:
    """A-U5-2: 読み取り側に**別の規則を作っていない**こと。

    保存側のソートキー生成が使う関数と同一であることを、値で突き合わせる
    （`_sent_at_as_utc` を通した結果が並べ替えの基準になっている）。
    """
    _seed(tmp_path, _MIXED_ROWS)
    repo = NotificationLogRepository(store_dir=tmp_path)

    records = repo.list_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL).records
    keys = [_sent_at_as_utc(n.sent_at) for n in records]

    assert keys == sorted(keys)
    assert keys == [_AWARE_EARLIER, _NAIVE_AS_UTC, _AWARE_LATER]


# =============================================================================
# F) すべての読み取り経路が同じ正規化を通ること
# =============================================================================


def test_every_lookup_path_survives_mixed_tzinfo(tmp_path: Path) -> None:
    """★ 経路ごとに直すのではなく、**1 か所で直っている**ことの確認。

    #279 が並べ替えを `NotificationLookup.from_outcome` へ集約したため、
    5 つの読み取り経路はすべてそこを通る。1 つでも別経路で並べていれば、
    ここで `TypeError` になる。
    """
    _seed(tmp_path, _MIXED_ROWS)
    repo = NotificationLogRepository(store_dir=tmp_path)

    lookups = [
        repo.list_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL),
        repo.latest_by_stock_and_type(_STOCK, NotificationType.SELL_SIGNAL),
        repo.list_by_holding_and_type(_HOLDING, NotificationType.SELL_SIGNAL),
        repo.latest_by_holding_and_type(_HOLDING, NotificationType.SELL_SIGNAL),
        repo.list_by_recommendation_id(_RECOMMENDATION),
        repo.list_all_with_outcome(),
    ]

    for lookup in lookups:
        assert [n.notification_id for n in lookup.records] == [
            _ID_AWARE_EARLIER,
            _ID_NAIVE,
            _ID_AWARE_LATER,
        ]
        assert lookup.latest is not None
        assert lookup.latest.notification_id == _ID_AWARE_LATER
