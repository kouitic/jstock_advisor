"""Issue #279(#63 A-U4): notification_log の decode 失敗で「重複送信」も「欠落」も起こさない。

```
問題  notification_log は再送抑止の判定材料である。
      1 件でも decode できないと
        skip すれば   過去の送信実績を見落として **重複送信**
        例外にすれば  通知が **出せなくなる**
      どちらも困る。だから FAIL_SAFE_SUPPRESS(判定不能を立てて呼び出し側へ委ねる)がある。

★ ところが宣言するだけでは足りなかった。
  store の読み取り API はどれも `list[T]` / `T | None` を返し、
  **「判定不能」を呼び出し側へ渡す口が無かった**(TARO Phase A の CENTRAL_FINDING)。
  そこで CollectionStore へ `find_with_outcome()` を **1 つだけ追加**し
  (既存 API は 1 つも変えていない)、repository が NotificationLookup として
  `undecidable` / `skipped` を返せるようにした。
```

```
経路ごとの扱い(#279 の設計)
  再送判定(stock scope / holding scope)  判定不能 -> **送らない**
  claim repair(get 経由)                 fail-closed(例外)を維持 + 呼び出し側で捕捉
  backtest / 集計                        抑止しない。skip 件数を添える
```

```
不正レコードは fixture としてのみ作る。**Production への注入は行わない。**
値はすべて架空値であり、実在の銘柄コード・所有者・保有データを含まない。
銘柄コードは実在しない "0000" を使う。
```
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from jstock_advisor.domain.entities.enums import NotificationType
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
    NotificationLookup,
)
from jstock_advisor.infrastructure.record_failure_policy import (
    ItemIdDisclosure,
    RecordFailurePolicy,
)

_STOCK = "0000"
_NOW = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
_GOOD_ID = "11111111-1111-4111-8111-111111111111"
_BROKEN_ID = "22222222-2222-4222-8222-222222222222"


def _good_row(notification_id: str = _GOOD_ID) -> dict[str, Any]:
    """decode できる架空の通知履歴。"""
    return {
        "notification_id": notification_id,
        "notification_type": NotificationType.SELL_SIGNAL.value,
        "stock_code": _STOCK,
        "content_hash": "hash-0001",
        "sent_at": "2026-09-08T00:00:00+00:00",
        "related_recommendation_id": "rec-0001",
        "owner": None,
        "holding_id": None,
    }


def _broken_row() -> dict[str, Any]:
    """decode できない架空の通知履歴(sent_at が日時として読めない)。"""
    row = _good_row(_BROKEN_ID)
    row["sent_at"] = "not-a-timestamp"
    return row


def _seed(store_dir: Path, rows: list[dict[str, Any]] | None = None) -> Path:
    store_dir.mkdir(parents=True, exist_ok=True)
    path = store_dir / "notification_log.json"
    path.write_text(
        json.dumps(rows if rows is not None else [_good_row(), _broken_row()], ensure_ascii=False),
        encoding="utf-8",
    )
    return path


# =============================================================================
# A) find_with_outcome — policy ごとの戻り値（S-17 への追加そのもの）
# =============================================================================


class _Row(BaseModel):
    row_id: str
    value: int


def _rows_file(tmp_path: Path) -> Path:
    path = tmp_path / "rows.json"
    path.write_text(
        json.dumps(
            [
                {"row_id": "a", "value": 1},
                {"row_id": "b", "value": "not-a-number"},
                {"row_id": "c", "value": 3},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _store(tmp_path: Path, policy: RecordFailurePolicy) -> JsonCollectionStore[_Row]:
    return JsonCollectionStore(
        _Row,
        "rows.json",
        "row_id",
        tmp_path,
        failure_policy=policy,
        item_id_disclosure=ItemIdDisclosure.PLAIN,
    )


def test_find_with_outcome_strict_still_raises(tmp_path: Path) -> None:
    """★ STRICT は従来どおり最初の失敗で例外(既定の挙動を変えていない)。"""
    _rows_file(tmp_path)

    with pytest.raises(Exception):  # noqa: B017 - pydantic の ValidationError をそのまま通す
        _store(tmp_path, RecordFailurePolicy.STRICT).find_with_outcome(lambda _: True)


def test_find_with_outcome_lenient_reports_failures_without_undecidable(tmp_path: Path) -> None:
    """★ LENIENT は skip して続行する。件数は残るが `undecidable` は立たない。

    「読めなかった件数がある」ことと「判断できない」ことは別である。
    """
    _rows_file(tmp_path)

    outcome = _store(tmp_path, RecordFailurePolicy.LENIENT).find_with_outcome(lambda _: True)

    assert [r.row_id for r in outcome.records] == ["a", "c"]
    assert outcome.failure_count == 1
    assert outcome.undecidable is False


def test_find_with_outcome_fail_safe_sets_undecidable(tmp_path: Path) -> None:
    """★ FAIL_SAFE_SUPPRESS では `undecidable` が立つ(本 Issue の核心)。

    これが無いと、呼び出し側は「0 件だった」と「読めなかった」を区別できない。
    """
    _rows_file(tmp_path)

    outcome = _store(tmp_path, RecordFailurePolicy.FAIL_SAFE_SUPPRESS).find_with_outcome(
        lambda _: True
    )

    assert [r.row_id for r in outcome.records] == ["a", "c"]
    assert outcome.failure_count == 1
    assert outcome.undecidable is True


def test_find_with_outcome_applies_the_predicate(tmp_path: Path) -> None:
    """絞り込みは `find()` と同じく decode できたレコードにのみ効く。"""
    _rows_file(tmp_path)

    outcome = _store(tmp_path, RecordFailurePolicy.LENIENT).find_with_outcome(
        lambda r: r.value > 2
    )

    assert [r.row_id for r in outcome.records] == ["c"]
    assert outcome.failure_count == 1, "絞り込みで失敗件数が減らないこと"


def test_find_is_unchanged(tmp_path: Path) -> None:
    """★ 既存の `find()` は挙動も戻り値も変えていない(LOCK_LEVEL_1 の根拠)。"""
    _rows_file(tmp_path)

    rows = _store(tmp_path, RecordFailurePolicy.LENIENT).find(lambda _: True)

    assert [r.row_id for r in rows] == ["a", "c"]
    assert isinstance(rows, list)


def test_undeclared_collection_is_still_strict(tmp_path: Path) -> None:
    """★ 宣言していない collection は既定の STRICT のまま(回帰の固定)。"""
    _rows_file(tmp_path)
    default_store: JsonCollectionStore[_Row] = JsonCollectionStore(
        _Row, "rows.json", "row_id", tmp_path
    )

    with pytest.raises(Exception):  # noqa: B017
        default_store.find_with_outcome(lambda _: True)


def test_missing_file_is_empty_not_undecidable(tmp_path: Path) -> None:
    """★ ファイルが無い(=まだ 1 件も送っていない)は「判定不能」ではない。

    ここを取り違えると、初回実行で通知が 1 件も出なくなる。
    """
    outcome = _store(tmp_path, RecordFailurePolicy.FAIL_SAFE_SUPPRESS).find_with_outcome(
        lambda _: True
    )

    assert outcome.records == []
    assert outcome.undecidable is False
    assert outcome.failure_count == 0


# =============================================================================
# B) repository — NotificationLookup
# =============================================================================


def test_lookup_is_undecidable_when_a_record_is_broken(tmp_path: Path) -> None:
    """★ 再送判定の読み取りで、壊れた 1 件があれば `undecidable` が立つ。"""
    _seed(tmp_path)

    lookup = NotificationLogRepository(store_dir=tmp_path).latest_by_stock_and_type(
        _STOCK, NotificationType.SELL_SIGNAL
    )

    assert lookup.undecidable is True
    assert lookup.skipped == 1
    assert [n.notification_id for n in lookup.records] == [_GOOD_ID]


def test_lookup_is_decidable_when_every_record_decodes(tmp_path: Path) -> None:
    """★ 正常時は従来どおり(挙動が変わらないことの固定)。"""
    _seed(tmp_path, [_good_row()])

    lookup = NotificationLogRepository(store_dir=tmp_path).latest_by_stock_and_type(
        _STOCK, NotificationType.SELL_SIGNAL
    )

    assert lookup.undecidable is False
    assert lookup.skipped == 0
    assert lookup.latest is not None
    assert lookup.latest.notification_id == _GOOD_ID


def test_empty_history_is_not_undecidable(tmp_path: Path) -> None:
    """★ 「1 件も送っていない」と「読めなかった」を混同しないこと。"""
    _seed(tmp_path, [])

    lookup = NotificationLogRepository(store_dir=tmp_path).latest_by_stock_and_type(
        _STOCK, NotificationType.SELL_SIGNAL
    )

    assert lookup.latest is None
    assert lookup.undecidable is False


def test_holding_scope_lookup_reports_undecidable(tmp_path: Path) -> None:
    """holding scope の再送判定も同じであること(#33 の scope 分離を維持)。"""
    _seed(tmp_path)

    lookup = NotificationLogRepository(store_dir=tmp_path).latest_by_holding_and_type(
        "owner-a#0000", NotificationType.SELL_SIGNAL
    )

    assert lookup.undecidable is True


def test_plain_disclosure_shows_the_notification_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """★ PLAIN。notification_id は UUID であり所有者名・銘柄コードを含まない。

    平文で出すことで、隔離されたレコードを運用で特定できる。
    """
    _seed(tmp_path)

    with caplog.at_level(logging.WARNING):
        NotificationLogRepository(store_dir=tmp_path).latest_by_stock_and_type(
            _STOCK, NotificationType.SELL_SIGNAL
        )

    assert _BROKEN_ID in caplog.text
    assert "sha256:" not in caplog.text, "PLAIN ではハッシュ表記にしない"


def test_failure_is_countable_and_summarised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 抑止の材料になった失敗が可視化されること(A-U4-4)。

    1 件ごとの WARNING に加えて、`find_with_outcome()` が `decode_records()` を
    経由するため **走査単位の集計行も出る**(Issue #281 の一部がここで解消する)。
    """
    _seed(tmp_path)

    with caplog.at_level(logging.WARNING):
        NotificationLogRepository(store_dir=tmp_path).latest_by_stock_and_type(
            _STOCK, NotificationType.SELL_SIGNAL
        )

    assert "persistence record decode failed" in caplog.text
    assert "policy=FAIL_SAFE_SUPPRESS" in caplog.text
    assert "persistence decode summary" in caplog.text
    assert "failed=1" in caplog.text


# --- 経路 4 / 5（分析・集計）は抑止しない -------------------------------------


def test_backtest_path_is_not_suppressed_but_reports_skipped(tmp_path: Path) -> None:
    """★ backtest は抑止しない。ただし skip 件数は添える(A-U4-7)。

    ここで抑止すると「壊れた 1 件のせいで分析ができない」状態へ戻る。
    """
    _seed(tmp_path)

    lookup = NotificationLogRepository(store_dir=tmp_path).list_by_recommendation_id("rec-0001")

    assert [n.notification_id for n in lookup.records] == [_GOOD_ID]
    assert lookup.skipped == 1


def test_list_all_signature_is_unchanged(tmp_path: Path) -> None:
    """★ `list_all()` は **戻り値の型を変えていない**(既存の呼び出しを壊さない)。

    skip 件数が要る呼び出し側だけが `list_all_with_outcome()` を使う。
    """
    _seed(tmp_path)
    repo = NotificationLogRepository(store_dir=tmp_path)

    records = repo.list_all()
    outcome = repo.list_all_with_outcome()

    assert isinstance(records, list)
    assert [n.notification_id for n in records] == [_GOOD_ID]
    assert outcome.skipped == 1


# =============================================================================
# C) NotificationLookup 自体の意味論
# =============================================================================


def test_latest_is_none_does_not_mean_not_sent_when_undecidable() -> None:
    """★ `undecidable` のとき `latest is None` は「送っていない」ではない。

    呼び出し側が `.latest` だけを見ると、この 2 つを取り違える。
    型の上で区別できることをここで固定する。
    """
    unknown = NotificationLookup(records=[], undecidable=True, skipped=2)
    never_sent = NotificationLookup(records=[], undecidable=False, skipped=0)

    assert unknown.latest is None
    assert never_sent.latest is None
    assert unknown != never_sent, "同じ latest でも別物として扱えること"


def test_records_are_sorted_by_sent_at() -> None:
    """並び順(sent_at 昇順)を変えていないこと。`latest` は末尾。"""
    older = NotificationLog.model_validate(_good_row("id-old"))
    newer = NotificationLog.model_validate(
        _good_row("id-new") | {"sent_at": "2026-09-09T00:00:00+00:00"}
    )
    from jstock_advisor.infrastructure.record_failure_policy import DecodeOutcome

    lookup = NotificationLookup.from_outcome(
        DecodeOutcome(records=[newer, older], failures=(), undecidable=False)
    )

    assert [n.notification_id for n in lookup.records] == ["id-old", "id-new"]
    assert lookup.latest is newer
