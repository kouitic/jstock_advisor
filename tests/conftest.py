"""全テスト共通のfixture(Issue #229)。

## 何を解決するか

`json_store.DEFAULT_STORE_DIR` はリポジトリ直下の `data/local_store` を
ハードコードしており、環境変数などの差し替え口が無い。そのため
`store_dir` を明示しないテストは**利用者の実データストアへ直接書き込む**。

その結果、`data/local_store/audit_log.json` は実測で 91.8MB / 29,579件まで育ち、
1件のupsertに約5.5秒(全読み→パース→全再直列化→全書き出し)かかるようになった。
`tests/unit/test_watchlist_finalize_integration.py` が「hangする」と見えていたのは
デッドロックではなく、この蓄積による極端な低速化である(空の一時ディレクトリでの
A/Bでは20 testsが24.47秒で完走した)。

**OS依存でも順序依存でもない。** 長く使っている作業コピーであれば同じことが起きる。
CIで再現しないのは `data/local_store/*.json` が `.gitignore` 対象で、
毎回まっさらな作業ディレクトリから始まるためである。

## なぜ tests/ 直下に置くか

`tests/unit/conftest.py` ではなく `tests/` 直下に置く。
`tests/integration/` が既に存在するため(現時点ではテスト0件)、
将来そちらへテストが増えたときに同じ問題を繰り返さないようにする。

## なぜ src 側に差し替え口を作らないか

`json_store.py` は共通部品カタログの **S-17 永続化ストア層**であり、
変更すると lock 対象が全領域へ広がる。本fixtureはテスト実行時にのみ効き、
srcを1行も変更しない。

## 既存の個別回避策は残す

`_NoopAuditService` / `_FakeTradeCooldownService` /
`tests/unit/conftest.py` の `csv_import_ledger` は、同じ問題を個別に
回避してきたものだが**削除しない**。監査を書かないことでテストの関心を絞る
という別の役割も持っており、同時に消すと本fixtureの効果と副作用を
切り分けられなくなる。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from jstock_advisor.infrastructure.local_repository import json_store


@pytest.fixture(autouse=True)
def _isolated_default_store_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """`json_store.DEFAULT_STORE_DIR` をテストごとの一時ディレクトリへ向ける。

    function scopeとする。session scopeにすると1回の実行で1つのストアを
    共有することになり、**本Issueと同じ蓄積の問題を小規模に再現する**。

    `JsonCollectionStore.__init__` は `store_dir or DEFAULT_STORE_DIR` を
    **構築時に解決**するため、本fixtureはストアが構築される前に差し替わって
    いる必要がある。srcにはimport時にストアを構築するmoduleが存在しない
    ことを確認済みであり(実測0件)、autouseのfunction scopeで足りる。

    `store_dir` を明示しているテスト(実測89ファイル)と、自前で
    `DEFAULT_STORE_DIR` をmonkeypatchしているテスト(実測2ファイル)には
    影響しない。後者は各テスト内のmonkeypatchが本fixtureより後に効き、
    どちらも一時ディレクトリを向く。
    """
    store_dir = tmp_path / "local_store"
    monkeypatch.setattr(json_store, "DEFAULT_STORE_DIR", store_dir)
    yield store_dir
