"""Issue #530(#71 F-C13残り): watchlist_service.pyのread-modify-write upsertへ
楽観ロックを追加したことの回帰テスト(N1・N2)。

既存の`CollectionStore.replace_if_raw_matches()`/`insert_if_absent()`(Issue #17で
確立済みのCAS primitive)を再利用しており、新しい機構は作っていない。競合の
シミュレーションは、repositoryの読み取りメソッドをmonkeypatchし、「このメソッドが
読み取った直後に、別実行が先に書き込みを確定させる」という順序を再現する
手法による(実際に2プロセスを起動する必要はない)。
"""

from __future__ import annotations

import pytest

from jstock_advisor.services.watchlist_service import WatchlistService
from jstock_advisor.services.write_plan import ConcurrentUpdateError

# --- N1: 同時update(既存item)→ stale writerの失敗 -----------------------------


def test_n1_add_item_detects_concurrent_modification(
    watchlist_service: WatchlistService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ add_item()が既存itemを読んだ直後に別実行(writer B)が先に更新を確定
    させた場合、writer A(add_item()の呼び出し元)はConcurrentUpdateErrorで
    明示的に失敗する(silent overwriteしない)。"""
    watchlist_service.add_item("7203", patch={"memo": "初期メモ"})
    repository = watchlist_service._repository  # noqa: SLF001
    original_get_raw_data = repository.get_raw_data

    def racy_get_raw_data(stock_code: str) -> str | None:
        raw = original_get_raw_data(stock_code)
        current = repository.get(stock_code)
        assert current is not None
        repository.replace_if_raw_matches(
            stock_code, raw, current.model_copy(update={"memo": "Bが割り込んで更新"})
        )
        return raw

    monkeypatch.setattr(repository, "get_raw_data", racy_get_raw_data)

    with pytest.raises(ConcurrentUpdateError):
        watchlist_service.add_item("7203", patch={"memo": "Aが更新しようとした値"})

    # ★ N7: Bの更新が上書きされずに残っていること(明示失敗であり、
    # silent overwriteではない)。
    item = watchlist_service.get_item("7203")
    assert item is not None
    assert item.memo == "Bが割り込んで更新"


def test_n1_update_item_detects_concurrent_modification(
    watchlist_service: WatchlistService, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_service.add_item("7203", patch={"memo": "初期メモ"})
    repository = watchlist_service._repository  # noqa: SLF001
    original_get_raw_data = repository.get_raw_data

    def racy_get_raw_data(stock_code: str) -> str | None:
        raw = original_get_raw_data(stock_code)
        current = repository.get(stock_code)
        assert current is not None
        repository.replace_if_raw_matches(
            stock_code, raw, current.model_copy(update={"memo": "Bが割り込んで更新"})
        )
        return raw

    monkeypatch.setattr(repository, "get_raw_data", racy_get_raw_data)

    with pytest.raises(ConcurrentUpdateError):
        watchlist_service.update_item("7203", memo="Aが更新しようとした値")

    item = watchlist_service.get_item("7203")
    assert item is not None
    assert item.memo == "Bが割り込んで更新"


def test_n1_concurrent_new_item_registration_is_detected(
    watchlist_service: WatchlistService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新規登録同士の競合(同一stock_codeを2つの実行がほぼ同時にadd_itemする)。
    `insert_if_absent()`の`attribute_not_exists`条件で検出する。"""
    repository = watchlist_service._repository  # noqa: SLF001
    original_get = repository.get
    injected = False

    def racy_get(stock_code: str):  # type: ignore[no-untyped-def]
        nonlocal injected
        existing = original_get(stock_code)
        if existing is None and not injected:
            injected = True
            monkeypatch.setattr(repository, "get", original_get)  # 一度きりの割り込み
            watchlist_service.add_item(stock_code, patch={"memo": "Bが先に登録"})
        return existing

    monkeypatch.setattr(repository, "get", racy_get)

    with pytest.raises(ConcurrentUpdateError):
        watchlist_service.add_item("7203", patch={"memo": "Aが登録しようとした"})

    item = watchlist_service.get_item("7203")
    assert item is not None
    assert item.memo == "Bが先に登録"


def test_n1_success_path_is_unaffected_when_no_conflict(
    watchlist_service: WatchlistService,
) -> None:
    """★ 反証: 競合が無い通常のupdate_itemは、これまでどおり成功する
    (楽観ロックの追加が正常系を壊していないことを固定する)。"""
    watchlist_service.add_item("7203", patch={"memo": "初期メモ"})

    result = watchlist_service.update_item("7203", memo="通常の更新")

    assert result.memo == "通常の更新"
    item = watchlist_service.get_item("7203")
    assert item is not None
    assert item.memo == "通常の更新"


# --- F3(サブちゃんレビュー): get()とget_raw_data()の間の並行削除 ----------------


def test_f3_add_item_raises_value_error_when_concurrently_deleted(
    watchlist_service: WatchlistService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`existing is not None`確認後、`get_raw_data()`呼び出しまでの間に別実行が
    このitemを削除すると、existing_raw is Noneに実際に到達しうる。
    assertではなく明示的なValueErrorとし、AssertionErrorとして生tracebackを
    見せない(サブちゃんレビューF3)。"""
    watchlist_service.add_item("7203", patch={"memo": "初期メモ"})
    repository = watchlist_service._repository  # noqa: SLF001
    original_get_raw_data = repository.get_raw_data

    def racy_get_raw_data(stock_code: str) -> str | None:
        repository.delete(stock_code)  # get()確認後、get_raw_data()前の並行削除
        return original_get_raw_data(stock_code)

    monkeypatch.setattr(repository, "get_raw_data", racy_get_raw_data)

    with pytest.raises(ValueError):
        watchlist_service.add_item("7203", patch={"memo": "更新しようとした値"})


def test_f3_update_item_raises_value_error_when_concurrently_deleted(
    watchlist_service: WatchlistService, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchlist_service.add_item("7203", patch={"memo": "初期メモ"})
    repository = watchlist_service._repository  # noqa: SLF001
    original_get_raw_data = repository.get_raw_data

    def racy_get_raw_data(stock_code: str) -> str | None:
        repository.delete(stock_code)
        return original_get_raw_data(stock_code)

    monkeypatch.setattr(repository, "get_raw_data", racy_get_raw_data)

    with pytest.raises(ValueError):
        watchlist_service.update_item("7203", memo="更新しようとした値")


# --- N2: build_add_item_plan()作成後に別update → commit時に競合検出 -----------


def test_n2_plan_for_existing_item_then_concurrent_update_is_detected_at_apply(
    watchlist_service: WatchlistService,
) -> None:
    """build_add_item_plan()(propose)構築後、実際の書き込み(confirm。利用者の
    LINE操作を挟むため秒~分単位の間隔が空きうる)までの間に別実行が更新すると、
    計画のexpected_dataではもう一致しない(#502のような「計画構築時点のraw」を
    保持する設計であることを直接固定する)。
    """
    watchlist_service.add_item("7203", patch={"memo": "初期メモ"})

    plan = watchlist_service.build_add_item_plan("7203", patch={"memo": "会話で入力した値"})

    # 別実行(例えばCLI編集)がconfirm前に割り込む。
    watchlist_service.update_item("7203", memo="別経路での更新")

    # 元の計画(古いexpected_data)を、confirm相当の適用として試みると失敗する。
    repository = watchlist_service._repository  # noqa: SLF001
    assert repository.replace_if_raw_matches("7203", plan.expected_data, plan.model) is False
    item = watchlist_service.get_item("7203")
    assert item is not None
    assert item.memo == "別経路での更新"  # 割り込んだ更新は上書きされていない


def test_n2_plan_for_new_item_then_concurrent_registration_is_detected_at_apply(
    watchlist_service: WatchlistService,
) -> None:
    plan = watchlist_service.build_add_item_plan("7203", patch={"memo": "会話で入力した値"})
    assert plan.expected_data is None

    # 別実行が先にconfirmを終える(同一銘柄の新規登録)。
    watchlist_service.add_item("7203", patch={"memo": "別経路で先に登録済み"})

    repository = watchlist_service._repository  # noqa: SLF001
    assert repository.insert_if_absent(plan.model) is False
    item = watchlist_service.get_item("7203")
    assert item is not None
    assert item.memo == "別経路で先に登録済み"


def test_n2_plan_applies_successfully_when_no_conflict(
    watchlist_service: WatchlistService,
) -> None:
    """★ 反証: 競合が無ければ、計画はそのまま正常に適用できる。"""
    watchlist_service.add_item("7203", patch={"memo": "初期メモ"})
    plan = watchlist_service.build_add_item_plan("7203", patch={"memo": "会話で入力した値"})

    repository = watchlist_service._repository  # noqa: SLF001
    assert repository.replace_if_raw_matches("7203", plan.expected_data, plan.model) is True
    item = watchlist_service.get_item("7203")
    assert item is not None
    assert item.memo == "会話で入力した値"
