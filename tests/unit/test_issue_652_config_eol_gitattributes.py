"""Issue #652(#338 Child C): config/の改行コードを.gitattributesで固定する契約のテスト。

背景: infra/template.yamlのConfigLayerは`ContentUri: ../config/`でソースツリーを直接
zipする。作業ツリーのバイト列がcore.autocrlfに依存すると、同じcommitから異なるzip
(= ConfigLayerの版上がり)ができる。`.gitattributes`の`config/** text=auto eol=lf`は、
**gitがtextと判定したconfig/配下のファイルだけ**を、環境に依らずLFに固定する。

root causeの直接の固定(SAME_COMMIT -> SAME_CONFIG_WORKTREE_BYTES):
同一commitを`core.autocrlf=true`と`false`の2環境でcheckout(clone)し、config/配下の
作業ツリーのバイト列が一致することを、隔離repoで直接assertする。

反証: 修正前(`.gitattributes`が無い状態)は、同じ2環境でバイト列が食い違う
(trueはCRLF、falseはLF)。修正後はどちらもLFで一致する。隔離repoテストは、実際の
`.gitattributes`をそのままcommitに含めた場合と、含めない場合(= 修正前相当)を並べて
検証し、後者で食い違うことを示す(テストが修正の有無を区別できる証拠)。

Issue #802(USER承認 = #122 issuecomment-5979839836): 対象を`src/`と`infra/layer/`へ
広げた(`sam build`が作業ツリーの`src/`と`infra/layer/`を取り込むため)。範囲の契約は
`config/`・`src/`・`infra/layer/`の3つで、それ以外のpath(docs/・tests/・
infra/template.yaml等)は属性を持たない。git indexの内容は変わらない(対象は既に全件
LFまたは改行なし)。#802のテストは同じ構造(隔離repo・autocrlf true / false・
`.gitattributes`を外した反証)で`src/`と`infra/layer/`を固定する。
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

_BINARY_PAYLOAD = b"\x00\x01binary\r\nbytes\r\n"
_SYNTHETIC_BINARY = "config/synthetic_binary.bin"
_SYNTHETIC_SRC_BINARY = "src/jstock_advisor/synthetic_binary.bin"
_SYNTHETIC_LAYER_BINARY = "infra/layer/synthetic_binary.bin"
# #802で固定する対象(.gitattributesの範囲の契約)。
_PINNED_ROOTS = ("config", "src", "infra/layer")


def _git_bytes(cwd: Path, *args: str, autocrlf: str | None = "false") -> bytes:
    """gitの出力をバイト列のまま返す(universal newlineによるCRLF→LF変換を避ける)。

    autocrlfがNoneの場合は`core.autocrlf`を注入しない(cloneで`-c core.autocrlf=...`を
    明示する呼び出し用)。
    """
    assert _GIT is not None
    config = ["-c", "core.safecrlf=false"]
    if autocrlf is not None:
        config += ["-c", f"core.autocrlf={autocrlf}"]
    result = subprocess.run(  # noqa: S603 - 固定のgit引数のみ(外部入力なし)
        [_GIT, *config, *args],
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


def test_src_and_infra_layer_files_are_pinned_to_lf() -> None:
    """#802: src/ と infra/layer/ のファイルも text=auto / eol=lf になる。"""
    for path in (
        "src/jstock_advisor/__init__.py",
        "src/jstock_advisor/cli/migrate.py",
        "infra/layer/requirements.in",
        "infra/layer/requirements.txt",
        "infra/layer/build-requirements.txt",
    ):
        attrs = _check_attr(path)
        assert attrs["text"] == "auto", path
        assert attrs["eol"] == "lf", path


def test_attribute_scope_is_limited_to_config_src_and_infra_layer() -> None:
    """範囲は config/・src/・infra/layer/ のみ。他のpathの改行コード扱いは変更しない
    (必要最小限)。#802でsrc/・infra/layer/を加えたが、無関係なpathへは広げない。
    """
    for path in (
        "docs/development_workflow.md",
        "tests/unit/test_issue_652_config_eol_gitattributes.py",
        "infra/template.yaml",
        "infra/layer_other/requirements.txt",
        "infra/samconfig.toml",
        "scripts/scan_for_pii.py",
        ".github/workflows/ci.yml",
        "src_other/module.py",
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


def test_tracked_src_and_infra_layer_files_have_no_crlf_in_the_index() -> None:
    """#802: commitされるsrc/・infra/layer/の内容(index)にCRLF・mixedが無い(LFまたは
    改行なし)。したがって`eol=lf`の追加でgit管理される内容は変わらない。
    """
    for root in ("src", "infra/layer"):
        out = _git(_REPO_ROOT, "ls-files", "--eol", "--", root)
        lines = [line for line in out.splitlines() if line.strip()]
        assert lines, f"{root}配下にgit管理ファイルが無い"
        offenders = [line for line in lines if not line.startswith(("i/lf", "i/none"))]
        assert offenders == [], root


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
    assert _git_bytes(repo, "cat-file", "-p", ":config/sample.yaml") == b"key: 1\nother: 2\n"


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


@pytest.mark.parametrize(
    "path",
    ["src/jstock_advisor/sample.py", "infra/layer/sample.txt"],
)
def test_crlf_src_and_infra_layer_files_are_normalized_to_lf(tmp_path: Path, path: str) -> None:
    """#802: core.autocrlf=falseの環境でCRLFのsrc/・infra/layer/のファイルを`git add`しても、
    実際の`.gitattributes`によりindexへはLFで入る。
    """
    repo = _init_repo(tmp_path, with_gitattributes=True)
    target = repo / path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"a = 1\r\nb = 2\r\n")

    _git(repo, "add", path)

    assert _index_eol(repo, path) == "i/lf"
    assert _git_bytes(repo, "cat-file", "-p", f":{path}") == b"a = 1\nb = 2\n"


@pytest.mark.parametrize(
    "path",
    ["src/jstock_advisor/sample.py", "infra/layer/sample.txt"],
)
def test_crlf_src_and_infra_layer_files_stay_crlf_without_gitattributes_counterexample(
    tmp_path: Path, path: str
) -> None:
    """反証(#802): `.gitattributes`が無い(= 変更前)と、同じ操作でCRLFのままindexへ入る。
    上のテストはこの差で、変更の有無を区別できる(変更前の実装では失敗する)。
    """
    repo = _init_repo(tmp_path, with_gitattributes=False)
    target = repo / path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"a = 1\r\nb = 2\r\n")

    _git(repo, "add", path)

    assert _index_eol(repo, path) == "i/crlf"


@pytest.mark.parametrize(
    "path",
    [
        "docs/sample.md",
        "tests/sample.py",
        "infra/template.yaml",
        "infra/layer_other/sample.txt",
        "scripts/sample.py",
    ],
)
def test_files_outside_the_pinned_roots_are_not_normalized(tmp_path: Path, path: str) -> None:
    """範囲外(#802): docs/・tests/・infra/template.yaml等のCRLFファイルは、この変更でも
    正規化されない(範囲の契約が無関係なpathへ広がっていない)。
    """
    repo = _init_repo(tmp_path, with_gitattributes=True)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"a\r\nb\r\n")

    _git(repo, "add", path)

    assert _index_eol(repo, path) == "i/crlf"


def test_adding_gitattributes_does_not_change_the_index_content(tmp_path: Path) -> None:
    """git indexの内容は変わらない(#802の確認3): LFでcommit済みの内容へ、実際の
    `.gitattributes`を後から加えて`git add --renormalize`しても、indexに差分が出ない。
    """
    repo = _init_repo(tmp_path, with_gitattributes=False)
    files = {
        "config/sample.yaml": b"key: 1\nother: 2\n",
        "src/jstock_advisor/sample.py": b"a = 1\nb = 2\n",
        "infra/layer/sample.txt": b"pkg==1.0\n",
        "src/jstock_advisor/no_newline.py": b"x = 1",
    }
    for name, data in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git(repo, "add", "-A")
    before = _git(repo, "ls-files", "--stage")

    shutil.copyfile(_GITATTRIBUTES, repo / ".gitattributes")
    _git(repo, "add", "--renormalize", "config", "src", "infra/layer")

    after = _git(repo, "ls-files", "--stage")
    assert after == before
    for name, data in files.items():
        assert _git_bytes(repo, "cat-file", "-p", f":{name}") == data, name


def test_binary_file_under_config_is_not_converted(tmp_path: Path) -> None:
    """USER判断「binaryをtext化しない」: text=autoにより、gitがbinaryと判定した
    config/配下のファイル(NUL文字を含む)は改行変換されない。
    """
    repo = _init_repo(tmp_path, with_gitattributes=True)
    (repo / "config" / "blob.bin").write_bytes(_BINARY_PAYLOAD)

    _git(repo, "add", "config/blob.bin")

    assert _git_bytes(repo, "cat-file", "-p", ":config/blob.bin") == _BINARY_PAYLOAD


# --- root cause: SAME_COMMIT -> SAME_CONFIG_WORKTREE_BYTES(core.autocrlf=true / false) ---


def _tracked_blobs(roots: tuple[str, ...], *, limit: int | None = None) -> dict[str, bytes]:
    """本repoのroots配下のgit管理ファイルを、index(commitされる内容)のバイト列で返す。

    limitを指定すると、root ごとに先頭からlimit件だけを返す(反証テストの実行時間を抑える)。
    """
    blobs: dict[str, bytes] = {}
    for root in roots:
        names = [n for n in _git(_REPO_ROOT, "ls-files", "-z", "--", root).split("\0") if n]
        found = {name: _git_bytes(_REPO_ROOT, "show", f":{name}") for name in names[:limit]}
        assert found, f"{root}配下にgit管理ファイルが無い"
        blobs.update(found)
    return blobs


def _tracked_config_blobs() -> dict[str, bytes]:
    """本repoのconfig/配下のgit管理ファイルを、index(commitされる内容)のバイト列で返す。"""
    return _tracked_blobs(("config",))


def _make_source_repo(
    tmp_path: Path,
    *,
    with_gitattributes: bool,
    roots: tuple[str, ...] = ("config",),
    binaries: tuple[str, ...] = (_SYNTHETIC_BINARY,),
    limit: int | None = None,
) -> Path:
    """本repoのroots(index内容)+合成のbinaryを1 commitにまとめた隔離repoを作る。"""
    repo = tmp_path / ("src_with" if with_gitattributes else "src_without")
    repo.mkdir()
    _git(repo, "init", "-q")
    if with_gitattributes:
        shutil.copyfile(_GITATTRIBUTES, repo / ".gitattributes")
    blobs = dict(_tracked_blobs(roots, limit=limit))
    for binary in binaries:
        blobs[binary] = _BINARY_PAYLOAD
    for name, data in blobs.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "seed",
    )
    return repo


def _checkout_config_bytes(
    src: Path,
    dst: Path,
    *,
    autocrlf: str,
    roots: tuple[str, ...] = ("config",),
) -> dict[str, bytes]:
    """同一commitを指定のcore.autocrlfでcloneし、roots配下の作業ツリーのバイト列を返す。"""
    _git_bytes(
        dst.parent,
        "clone",
        "-q",
        "--no-hardlinks",
        "-c",
        f"core.autocrlf={autocrlf}",
        str(src),
        str(dst),
        autocrlf=None,
    )
    return {
        path.relative_to(dst).as_posix(): path.read_bytes()
        for root in roots
        for path in sorted((dst / root).rglob("*"))
        if path.is_file()
    }


def test_same_commit_gives_same_config_worktree_bytes_for_autocrlf_true_and_false(
    tmp_path: Path,
) -> None:
    """root causeの直接の固定: 同一commitを core.autocrlf=true / false の2環境で
    checkoutしても、config/配下の作業ツリーのバイト列は完全に一致する
    (= ConfigLayerの入力が環境に依らない)。
    """
    src = _make_source_repo(tmp_path, with_gitattributes=True)

    on = _checkout_config_bytes(src, tmp_path / "clone_autocrlf_true", autocrlf="true")
    off = _checkout_config_bytes(src, tmp_path / "clone_autocrlf_false", autocrlf="false")

    assert on == off
    assert set(on) == set(_tracked_config_blobs()) | {_SYNTHETIC_BINARY}
    # textファイルは、commitされた内容(LF)そのままである(CRLFへ変換されていない)。
    for name, data in _tracked_config_blobs().items():
        assert on[name] == data, name
        assert b"\r\n" not in on[name], name
    # binaryは、どちらの環境でも1byteも変換されない(text=auto)。
    assert on[_SYNTHETIC_BINARY] == _BINARY_PAYLOAD


def test_same_commit_gives_different_worktree_bytes_without_gitattributes_counterexample(
    tmp_path: Path,
) -> None:
    """反証: `.gitattributes`が無い(= 修正前)と、同一commitでも core.autocrlf の違いで
    config/の作業ツリーのバイト列が食い違う(trueはCRLF、falseはLF)。
    上のテストはこの差で、修正の有無を区別できる(修正前の実装では失敗する)。
    """
    src = _make_source_repo(tmp_path, with_gitattributes=False)

    on = _checkout_config_bytes(src, tmp_path / "clone_autocrlf_true", autocrlf="true")
    off = _checkout_config_bytes(src, tmp_path / "clone_autocrlf_false", autocrlf="false")

    assert on != off
    # falseはcommitされた内容(LF)のまま、trueはtextファイルがCRLFになる。
    yaml_names = [name for name in on if name.endswith(".yaml")]
    assert yaml_names
    assert all(b"\r\n" not in off[name] for name in yaml_names)
    assert any(b"\r\n" in on[name] for name in yaml_names)


# --- #802 root cause: SAME_COMMIT -> SAME_WORKTREE_BYTES(src/・infra/layer/) ---


def test_same_commit_gives_same_pinned_roots_worktree_bytes_for_autocrlf_true_and_false(
    tmp_path: Path,
) -> None:
    """#802の確認1: 同一commitを core.autocrlf=true / false の2環境でcheckoutしても、
    config/・src/・infra/layer/配下の作業ツリーのバイト列は完全に一致する
    (= sam buildの入力が環境に依らない)。
    """
    binaries = (_SYNTHETIC_BINARY, _SYNTHETIC_SRC_BINARY, _SYNTHETIC_LAYER_BINARY)
    src = _make_source_repo(
        tmp_path, with_gitattributes=True, roots=_PINNED_ROOTS, binaries=binaries
    )

    on = _checkout_config_bytes(
        src, tmp_path / "clone_autocrlf_true", autocrlf="true", roots=_PINNED_ROOTS
    )
    off = _checkout_config_bytes(
        src, tmp_path / "clone_autocrlf_false", autocrlf="false", roots=_PINNED_ROOTS
    )

    tracked = _tracked_blobs(_PINNED_ROOTS)
    assert on == off
    assert set(on) == set(tracked) | set(binaries)
    # src/・infra/layer/の対象ファイルが含まれている(空の比較にならない)。
    assert any(name.startswith("src/") for name in tracked)
    assert any(name.startswith("infra/layer/") for name in tracked)
    # textファイルは、commitされた内容(LFまたは改行なし)そのままである(CRLFへ変換されていない)。
    for name, data in tracked.items():
        assert on[name] == data, name
        assert b"\r\n" not in on[name], name
    # binaryは、どちらの環境でも1byteも変換されない(text=auto)。
    for binary in binaries:
        assert on[binary] == _BINARY_PAYLOAD, binary


def test_same_commit_gives_different_pinned_roots_bytes_without_gitattributes_counterexample(
    tmp_path: Path,
) -> None:
    """反証(#802の確認2): `.gitattributes`が無い(= 変更前)と、同一commitでも
    core.autocrlf の違いで src/・infra/layer/ の作業ツリーのバイト列が食い違う
    (trueはCRLF、falseはLF)。上のテストはこの差で、変更の有無を区別できる。
    """
    roots = ("src", "infra/layer")
    # 食い違いの再現には全件は要らない(実行時間を抑えるため、rootごとに先頭20件)。
    src = _make_source_repo(tmp_path, with_gitattributes=False, roots=roots, binaries=(), limit=20)

    on = _checkout_config_bytes(src, tmp_path / "clone_autocrlf_true", autocrlf="true", roots=roots)
    off = _checkout_config_bytes(
        src, tmp_path / "clone_autocrlf_false", autocrlf="false", roots=roots
    )

    assert on != off
    # falseはcommitされた内容(LF)のまま、trueはtextファイルがCRLFになる。
    for prefix, suffix in (("src/", ".py"), ("infra/layer/", ".txt")):
        names = [n for n in on if n.startswith(prefix) and n.endswith(suffix)]
        assert names, prefix
        assert all(b"\r\n" not in off[n] for n in names), prefix
        assert any(b"\r\n" in on[n] for n in names), prefix
