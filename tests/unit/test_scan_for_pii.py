"""既知の実在人物情報の再混入防止スキャン(scripts/scan_for_pii.py)のテスト。

CLAUDE.md「実在人物の個人情報をGit管理対象へ含めない」ルールのCI側の
歯止め。denylist方式であり全てのPIIを検出できるわけではないことに注意
(CLAUDE.md本文・scan_for_pii.pyのdocstring参照)。

検出ロジック自体を検証するテストでは、本番denylist(実在人物名のハッシュ)
を再利用せず、完全に架空の合言葉("カナリア"トークン)を使った専用の
denylistを注入する(scan()のknown_hashes引数)。本ファイル自身もCIの
pii-scan対象であり、実在人物名の平文はもちろん、そのローマ字表記等も
一切書かない(本ファイルさえ書けば通ってしまう自己参照的な抜け穴を
作らないため)。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from scan_for_pii import _hash, scan  # noqa: E402

# 検出ロジックのテスト専用の架空トークン。実在の人物・組織とは無関係。
_CANARY_TOKEN = "zzcanarypiitoken"
_CANARY_HASHES = frozenset({_hash(_CANARY_TOKEN)})


def test_current_repository_has_no_known_pii() -> None:
    """本リポジトリの現行Git管理ファイルに既知のPII(本番denylist)が
    存在しないこと(再混入防止の実運用テスト)。"""
    assert scan(_REPO_ROOT) == []


def test_scan_detects_reintroduced_known_pii(tmp_path: Path) -> None:
    """denylist中のハッシュに一致する文字列を含むファイルをGit管理下に
    置いた場合、scan()がそのファイルパスを検出すること(検出ロジック自体の
    健全性テスト)。架空のカナリアトークン専用denylistを注入し、実在人物名
    は一切使わない。
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    pii_file = tmp_path / "leaked.txt"
    pii_file.write_text(f"owner = {_CANARY_TOKEN}\n", encoding="utf-8")
    subprocess.run(["git", "add", "leaked.txt"], cwd=tmp_path, check=True)

    violations = scan(tmp_path, known_hashes=_CANARY_HASHES)

    assert violations == ["leaked.txt"]


def test_scan_ignores_unrelated_content(tmp_path: Path) -> None:
    """denylistに一致しない通常のテキストは検出されないこと(誤検知しない
    ことの確認)。カナリアdenylistで注入しても、無関係な内容は一致しない。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    clean_file = tmp_path / "clean.txt"
    clean_file.write_text("owner = 所有者A\nnothing sensitive here\n", encoding="utf-8")
    subprocess.run(["git", "add", "clean.txt"], cwd=tmp_path, check=True)

    assert scan(tmp_path, known_hashes=_CANARY_HASHES) == []
