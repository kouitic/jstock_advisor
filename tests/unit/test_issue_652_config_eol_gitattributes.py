"""Issue #652(#338 Child C): config/の改行コードを.gitattributesで固定する契約のテスト。

背景: infra/template.yamlのConfigLayerは`ContentUri: ../config/`でソースツリーを直接
zipする。作業ツリーのバイト列がcore.autocrlfに依存すると、同じcommitから異なるzip
(= ConfigLayerの版上がり)ができる。`.gitattributes`の`config/** text=auto eol=lf`は、
**gitがtextと判定したconfig/配下のファイルだけ**を、環境に依らずLFに固定する。

反証: 修正前(`.gitattributes`が無い状態)は、core.autocrlf=falseの環境でCRLFのファイルを
`git add`するとCRLFのままindexへ入る。修正後は同じ操作でLFへ正規化される。
下の隔離repoテストは、実際の`.gitattributes`をそのままコピーした場合と、コピーしない
場合(= 修正前相当)を並べて検証し、後者でCRLFが残ることを示す(テストが修正の有無を
区別できる証拠)。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GITATTRIBUTES = _REPO_ROOT / ".gitattributes"

_GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(_GIT is None, reason="git command is required")


def _git_bytes(cwd: Path, *args: str) -> bytes:
    """gitの出力をバイト列のまま返す(universal newlineによるCRLF→LF変換を避ける)。"""
    assert _GIT is not None
    result = subprocess.run(  # noqa: S603 - 固定のgit引数のみ(外部入力なし)
        [_GIT, "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )
    return result.stdout


def _git(cwd: Path, *args: str) -> str:
    return _git_bytes(cwd, *args).decode("utf-8")


def _check_attr(path: str) -> dict[str, str]:
    out = _git(_REPO_ROOT, "check-attr", "text", "eol", "--", path)
    attrs: dict[str, str] = {}
    for line in out.splitlines():
        _, name, value = line.rsplit(": ", 2)
        attrs[name] = value
    return attrs


def _index_eol(repo: Path, path: str) -> str:
    """`git ls-files --eol`のindex側の改行表現(i/lf, i/crlf, i/mixed, i/-text)を返す。"""
    out = _git(repo, "ls-files", "--eol", "--", path)
    return out.split()[0]


def _init_repo(tmp_path: Path, *, with_gitattributes: bool) -> Path:
    repo = tmp_path / ("with" if with_gitattributes else "without")
    repo.mkdir()
    _git(repo, "init", "-q")
    if with_gitattributes:
        shutil.copyfile(_GITATTRIBUTES, repo / ".gitattributes")
    (repo / "config").mkdir()
    (repo / "other").mkdir()
    return repo


def test_gitattributes_exists_at_repo_root() -> None:
    assert _GITATTRIBUTES.is_file()


def test_config_files_are_pinned_to_lf() -> None:
    """config/配下のファイルは text=auto / eol=lf になる(環境のautocrlfに依らない)。"""
    attrs = _check_attr("config/profit_taking_rules.yaml")

    assert attrs["text"] == "auto"
    assert attrs["eol"] == "lf"


def test_attribute_scope_is_limited_to_config() -> None:
    """範囲は config/ のみ。他のpathの改行コード扱いは変更しない(必要最小限)。"""
    for path in (
        "src/jstock_advisor/__init__.py",
        "docs/development_workflow.md",
        "tests/unit/test_issue_652_config_eol_gitattributes.py",
        "infra/template.yaml",
    ):
        attrs = _check_attr(path)
        assert attrs["eol"] == "unspecified", path
        assert attrs["text"] == "unspecified", path


def test_tracked_config_files_are_lf_in_the_index() -> None:
    """commitされるconfig/の内容(index)は全件LF。CRLFが混入していない。"""
    out = _git(_REPO_ROOT, "ls-files", "--eol", "--", "config")
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines, "config/配下にgit管理ファイルが無い"
    offenders = [line for line in lines if not line.startswith("i/lf")]
    assert offenders == []


def test_crlf_config_file_is_normalized_to_lf_with_the_repo_gitattributes(
    tmp_path: Path,
) -> None:
    """core.autocrlf=falseの環境(= 改行を変換しない環境)でCRLFのconfigを`git add`しても、
    実際の`.gitattributes`によりindexへはLFで入る。
    """
    repo = _init_repo(tmp_path, with_gitattributes=True)
    (repo / "config" / "sample.yaml").write_bytes(b"key: 1\r\nother: 2\r\n")

    _git(repo, "add", "config/sample.yaml")

    assert _index_eol(repo, "config/sample.yaml") == "i/lf"
    assert _git(repo, "cat-file", "-p", ":config/sample.yaml") == "key: 1\nother: 2\n"


def test_crlf_config_file_stays_crlf_without_gitattributes_counterexample(
    tmp_path: Path,
) -> None:
    """反証: `.gitattributes`が無い(= 修正前)と、同じ操作でCRLFのままindexへ入る。
    上のテストはこの差で、修正の有無を区別できる(修正前の実装では失敗する)。
    """
    repo = _init_repo(tmp_path, with_gitattributes=False)
    (repo / "config" / "sample.yaml").write_bytes(b"key: 1\r\nother: 2\r\n")

    _git(repo, "add", "config/sample.yaml")

    assert _index_eol(repo, "config/sample.yaml") == "i/crlf"


def test_files_outside_config_are_not_normalized(tmp_path: Path) -> None:
    """範囲外: config/以外のCRLFファイルは、この変更で正規化されない。"""
    repo = _init_repo(tmp_path, with_gitattributes=True)
    (repo / "other" / "sample.txt").write_bytes(b"a\r\nb\r\n")

    _git(repo, "add", "other/sample.txt")

    assert _index_eol(repo, "other/sample.txt") == "i/crlf"


def test_binary_file_under_config_is_not_converted(tmp_path: Path) -> None:
    """USER判断「binaryをtext化しない」: text=autoにより、gitがbinaryと判定した
    config/配下のファイル(NUL文字を含む)は改行変換されない。
    """
    repo = _init_repo(tmp_path, with_gitattributes=True)
    payload = b"\x00\x01binary\r\nbytes\r\n"
    (repo / "config" / "blob.bin").write_bytes(payload)

    _git(repo, "add", "config/blob.bin")

    assert _git_bytes(repo, "cat-file", "-p", ":config/blob.bin") == payload
