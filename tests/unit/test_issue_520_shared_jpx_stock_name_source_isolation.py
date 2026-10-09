"""Issue #520: JPX銘柄名の共有ソースが、テスト間で持ち越されないこと。

`watchlist_display_name._shared_jpx_stock_name_source`(モジュール変数)は、`get_shared_jpx_
stock_name_source()`が`global`代入で書く。unit testのautouse隔離(`tests/unit/conftest.py`の
`_isolated_shared_jpx_stock_name_source`)が無いと、先行テストが作ったインスタンスが後続テストへ
残る(#148と同型。単独では通り、先行テストの後だと落ちる順序依存)。

## 順序を固定した2テスト

`test_a_leaves_a_shared_instance_behind` → `test_b_starts_without_a_shared_instance` は
**この順で実行される前提の対**である(pytestは同一ファイル内を定義順に実行する。順序を入れ替える
plugin は導入していない)。a が共有インスタンスを作って終わり、b は最初に未生成であることを見る。
autouse fixture が無い(または何もしない)と、b が赤になる。
**2つの順序を入れ替えない・間に挟まない。**
"""

from __future__ import annotations

from jstock_advisor.services import watchlist_display_name
from tests.unit.conftest import reset_shared_jpx_stock_name_source


def test_a_leaves_a_shared_instance_behind() -> None:
    """先行テスト役: 共有インスタンスを作ったまま終わる(後続へ漏れる状況を作る)。"""
    instance = watchlist_display_name.get_shared_jpx_stock_name_source(60)

    assert instance is not None
    assert watchlist_display_name._shared_jpx_stock_name_source is instance


def test_b_starts_without_a_shared_instance() -> None:
    """後続テスト役: 先行テストの共有インスタンスが残っていない(隔離 fixture が reset している)。"""
    assert watchlist_display_name._shared_jpx_stock_name_source is None, (
        "先行テストの共有JPX銘柄名ソースが残っている(autouse隔離が効いていない)"
    )


def test_reset_helper_discards_the_shared_instance() -> None:
    """helper の単体: 作成 → reset → 未生成。次の取得は新しい実体を返す。"""
    first = watchlist_display_name.get_shared_jpx_stock_name_source(60)
    assert watchlist_display_name.get_shared_jpx_stock_name_source(60) is first

    reset_shared_jpx_stock_name_source()

    assert watchlist_display_name._shared_jpx_stock_name_source is None
    second = watchlist_display_name.get_shared_jpx_stock_name_source(60)
    assert second is not first
