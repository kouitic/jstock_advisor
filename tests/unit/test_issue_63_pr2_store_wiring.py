"""Issue #63 PR-2(A-U1b / A-U2): store の decode 経路を失敗ポリシー機構へ接続する。

不正レコードは fixture としてのみ作る。**Production への注入は行わない。**
値はすべて架空値であり、実在人物の情報・実際の保有データを含めない。
主キーの形は `owner-a#0000` のように「所有者を含みうる形」を再現するが、
所有者名・銘柄コードのいずれも実在の値ではない。
"""

from __future__ import annotations

import hashlib
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

# 所有者を含みうる主キーの形（架空値）。Issue #135 が実測した
# `<所有者>#<銘柄コード>` の構造だけを再現する。
_BROKEN_ID = "owner-a#0000"
_GOOD_ID = "owner-a#0001"


class _Item(BaseModel):
    item_id: str
    amount: int


def _hashed(item_id: str) -> str:
    return "sha256:" + hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:8]


def _write_raw(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _store(
    tmp_path: Path,
    policy: RecordFailurePolicy = RecordFailurePolicy.STRICT,
    disclosure: ItemIdDisclosure = ItemIdDisclosure.HASH,
) -> JsonCollectionStore[_Item]:
    return JsonCollectionStore(
        _Item,
        "items.json",
        "item_id",
        tmp_path,
        failure_policy=policy,
        item_id_disclosure=disclosure,
    )


def _seed_one_broken(tmp_path: Path) -> Path:
    """正常 1 件 + 不正 1 件（amount が数値でない）を置く。"""
    path = tmp_path / "items.json"
    _write_raw(
        path,
        [
            {"item_id": _GOOD_ID, "amount": 1},
            {"item_id": _BROKEN_ID, "amount": "not-a-number"},
        ],
    )
    return path


# --- 既定 STRICT で挙動が変わらないこと（本 PR の中核の不変条件） -------------


def test_strict_still_raises_on_the_first_broken_record(tmp_path: Path) -> None:
    """既定(STRICT)では 1 件の不正で例外が送出される。**現行の挙動と同じ。**

    本 PR は機構への差し替えであり、既定の挙動は変えない(H-19)。
    holdings / purchase_lots / transactions / recommendations のように
    欠損が集計を静かに歪めるコレクションは STRICT のまま残す(H-14)。
    """
    _seed_one_broken(tmp_path)
    store = _store(tmp_path)

    with pytest.raises(Exception):  # noqa: B017 - pydantic の ValidationError をそのまま通す
        store.list_all()


def test_strict_raises_the_original_exception_type(tmp_path: Path) -> None:
    """STRICT が送出するのは **元の例外**であり、包み直していない。

    ValidationError を捕捉している呼び出し元の挙動を変えないための固定。
    """
    from pydantic import ValidationError

    _seed_one_broken(tmp_path)
    store = _store(tmp_path)

    with pytest.raises(ValidationError):
        store.list_all()


def test_lenient_returns_the_remaining_records(tmp_path: Path) -> None:
    """LENIENT なら 1 件の不正で全滅しない。"""
    _seed_one_broken(tmp_path)
    store = _store(tmp_path, policy=RecordFailurePolicy.LENIENT)

    items = store.list_all()

    assert [i.item_id for i in items] == [_GOOD_ID]


# --- A-U2: 書き込みが不正レコードを黙って消さないこと（T-14） -----------------


def test_writing_keeps_the_broken_record_as_raw(tmp_path: Path) -> None:
    """★ 不正レコードがある状態で upsert しても、**不正レコードは消えない。**

    `_write_all()` は検証済みモデルから再直列化するため、素朴に skip すると
    次の書き込みで不正レコードが黙って消える。A-U2 の目的は
    「壊れたレコードを消せるようにする(自己修復)」であって
    「意図せず消える」ことではない。
    """
    path = _seed_one_broken(tmp_path)
    store = _store(tmp_path, policy=RecordFailurePolicy.LENIENT)

    store.upsert(_Item(item_id="owner-a#0002", amount=2))

    rows = json.loads(path.read_text(encoding="utf-8"))
    by_id = {r["item_id"]: r for r in rows}
    assert _BROKEN_ID in by_id, "不正レコードがファイルに残っていること"
    assert by_id[_BROKEN_ID]["amount"] == "not-a-number", "raw のまま保持されていること"
    assert "owner-a#0002" in by_id, "新規レコードは書き込まれていること"


def test_delete_removes_the_broken_record(tmp_path: Path) -> None:
    """★ 明示的な delete(id) だけが不正レコードを消せる（自己修復）。

    従来は 16 メソッドすべてが全件検証を経由するため delete すら通らず、
    壊れたレコードを消して復旧する手段が無かった。
    """
    path = _seed_one_broken(tmp_path)
    store = _store(tmp_path, policy=RecordFailurePolicy.LENIENT)

    assert store.delete(_BROKEN_ID) is True

    rows = json.loads(path.read_text(encoding="utf-8"))
    assert [r["item_id"] for r in rows] == [_GOOD_ID]


def test_apply_batch_keeps_untouched_broken_records(tmp_path: Path) -> None:
    """apply_batch でも、対象にしていない不正レコードは残る。"""
    path = _seed_one_broken(tmp_path)
    store = _store(tmp_path, policy=RecordFailurePolicy.LENIENT)

    store.apply_batch(delete_ids=[_GOOD_ID], puts=[_Item(item_id="owner-a#0003", amount=3)])

    rows = json.loads(path.read_text(encoding="utf-8"))
    ids = {r["item_id"] for r in rows}
    assert _BROKEN_ID in ids
    assert _GOOD_ID not in ids
    assert "owner-a#0003" in ids


def test_insert_if_absent_does_not_overwrite_a_broken_record(tmp_path: Path) -> None:
    """デコードできない既存レコードを「無い」とみなして上書きしない。

    上書きすると、壊れたレコードの内容が復元不能になる。
    """
    _seed_one_broken(tmp_path)
    store = _store(tmp_path, policy=RecordFailurePolicy.LENIENT)

    inserted = store.insert_if_absent(_Item(item_id=_BROKEN_ID, amount=99))

    assert inserted is False


# --- item_id の開示レベル（T-15 / Issue #135 E-4 を開かないこと） -------------


def test_item_id_is_hashed_by_default_in_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """★ 既定では主キーの平文をログへ出さない。

    主キーは `<所有者>#<銘柄コード>` の形を取りうる(Issue #135 の実測で
    6 collection)。平文を出すと、本機構の接続がそのまま
    Production ログへの個人識別情報の出力経路になる。
    """
    _seed_one_broken(tmp_path)
    store = _store(tmp_path, policy=RecordFailurePolicy.LENIENT)

    with caplog.at_level(logging.WARNING):
        store.list_all()

    text = caplog.text
    assert _BROKEN_ID not in text, "平文の主キーがログへ出ていないこと"
    assert "owner-a" not in text, "所有者を表す部分文字列も出ていないこと"
    assert _hashed(_BROKEN_ID) in text, "所在はハッシュとして残ること"


def test_plain_disclosure_is_opt_in(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """PLAIN は明示指定したときだけ有効になる。

    個人識別情報を含まないと実測できた collection(audit_id / stock_code /
    cache_key 等)にのみ宣言する。
    """
    _seed_one_broken(tmp_path)
    store = _store(
        tmp_path,
        policy=RecordFailurePolicy.LENIENT,
        disclosure=ItemIdDisclosure.PLAIN,
    )

    with caplog.at_level(logging.WARNING):
        store.list_all()

    assert _BROKEN_ID in caplog.text


def test_hash_is_deterministic_so_the_record_can_be_located(tmp_path: Path) -> None:
    """ハッシュは決定的であり、候補キーとの突き合わせで所在を特定できる。

    是正可能性を失っていないことの固定。
    """
    from jstock_advisor.infrastructure.record_failure_policy import _disclose_item_id

    first = _disclose_item_id(_BROKEN_ID, ItemIdDisclosure.HASH)
    second = _disclose_item_id(_BROKEN_ID, ItemIdDisclosure.HASH)

    assert first == second == _hashed(_BROKEN_ID)
    assert first != _disclose_item_id(_GOOD_ID, ItemIdDisclosure.HASH)
