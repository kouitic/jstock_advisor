"""既知の実在人物情報の再混入防止スキャン(scripts/scan_for_pii.py)のテスト。

CLAUDE.md「実在人物の個人情報をGit管理対象へ含めない」ルールのCI側の
歯止め。denylist方式であり全てのPIIを検出できるわけではないことに注意
(CLAUDE.md本文・scan_for_pii.pyのdocstring参照)。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from scan_for_pii import _KNOWN_PII_HASHES, _hash, scan  # noqa: E402


def test_current_repository_has_no_known_pii() -> None:
    """本リポジトリの現行Git管理ファイルに既知のPIIが存在しないこと
    (再混入防止の実運用テスト)。"""
    assert scan(_REPO_ROOT) == []


def test_scan_detects_reintroduced_known_pii(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """denylist中のハッシュに一致する文字列を含むファイルをGit管理下に
    置いた場合、scan()がそのファイルパスを検出すること(検出ロジック自体の
    健全性テスト)。denylistに実際に含まれる実在人物名の平文はこのテストにも
    書かない(ハッシュ経由でのみ検証する)。
    """
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # denylist中のkoichi/kazuho/ryosuke/maruoは既にハッシュが判明している
    # 既知の実在人物名のローマ字表記。ここでは"maruo"のハッシュを使い、
    # 該当文字列を平文で含む新規ファイルを検出できることを確認する。
    target_hash = next(h for h in _KNOWN_PII_HASHES if h == _hash("maruo"))
    pii_file = tmp_path / "leaked.txt"
    pii_file.write_text("owner = maruo\n", encoding="utf-8")
    subprocess.run(["git", "add", "leaked.txt"], cwd=tmp_path, check=True)

    violations = scan(tmp_path)

    assert violations == ["leaked.txt"]
    assert _hash("maruo") == target_hash


def test_scan_ignores_unrelated_content(tmp_path: Path) -> None:
    """denylistに一致しない通常のテキストは検出されないこと(誤検知しない
    ことの確認)。"""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    clean_file = tmp_path / "clean.txt"
    clean_file.write_text("owner = 所有者A\nemail is not personal here\n", encoding="utf-8")
    subprocess.run(["git", "add", "clean.txt"], cwd=tmp_path, check=True)

    assert scan(tmp_path) == []
