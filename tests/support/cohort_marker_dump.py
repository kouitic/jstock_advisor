"""pytest plugin: 収集後の item の『module → cohort marker 名の集合』を JSON へ書く(Issue #966)。

`tests/unit/test_issue_277_cohort_markers.py` が子プロセスの pytest へ
`-p tests.support.cohort_marker_dump --cohort-dump <path>` で渡す。
親の実行には読み込まれない(明示した子プロセスだけ)。

## 何のためか

cohort ごとに `-m <marker>` の子プロセスを 20 回起動していた検証を、次の 3 回にまとめる(#966)。

  子 1  全 cohort の marker の OR で 1 回収集し、この plugin の出力で
        『module ごとの marker の集合』を 20 cohort 分まとめて等号で比べる
        (不足・混入・二重付与の検出)
  子 2・3  代表の通常 cohort・SOLO cohort を `-m <単一 marker>` で選ぶ
        (単一の marker の `-m` が効くこと)

## 守ること

* `pytest_collection_finish` で書く = ★ `-m` の deselect の後 ★ の item を見る
  (deselect された item は `session.items` から外れる)。conftest の自動付与が deselect より後に
  走ると、marker は付くのに選べず、item は 1 件も残らない → 出力は空になり、呼び出し側の等号で
  落ちる(空振りの緑にならない)
* 時計・環境変数・乱数を使わない。出力先は `--cohort-dump` で渡された 1 か所だけ
* marker の名前は `cohort_` で始まるものだけを書く(他の marker を含めない)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_MARKER_PREFIX = "cohort_"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--cohort-dump",
        action="store",
        default=None,
        help="収集後の module -> cohort marker 名の集合を、この path へ JSON で書く(#966)",
    )


def collect_cohort_markers(items: list[pytest.Item]) -> dict[str, list[str]]:
    """item の列から、repository root からの相対 path -> 昇順の cohort marker 名 を作る。"""
    found: dict[str, set[str]] = {}
    for item in items:
        path = getattr(item, "path", None)
        if path is None:  # pragma: no cover - 古い pytest との互換
            continue
        try:
            relative = Path(path).resolve().relative_to(_REPO_ROOT).as_posix()
        except ValueError:  # pragma: no cover - repo 外の item は対象外
            continue
        names = found.setdefault(relative, set())
        names.update(m.name for m in item.iter_markers() if m.name.startswith(_MARKER_PREFIX))
    return {module: sorted(names) for module, names in sorted(found.items())}


def pytest_collection_finish(session: pytest.Session) -> None:
    target = session.config.getoption("--cohort-dump")
    if not target:
        return
    payload = collect_cohort_markers(list(session.items))
    Path(target).write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
