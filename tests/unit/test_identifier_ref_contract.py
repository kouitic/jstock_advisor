"""Issue #925 段階 0 / #603 F2-0: 新しい AuditLog の記録の owner の符号の契約テスト。

固定するもの:
  - 形式 `kv0:` + 24 桁の 16 進(決定的・表記ゆれは同じ符号・用途ラベルつき)
  - 空・空白だけ・非 str は拒否(log_ref のように素通しにしない)。例外のメッセージに値を含めない
  - parse_ref は実装済みの版(kv0)だけを受け、未知の版(kv1 を含む)・壊れた形式を拒否
    (reader が版で分岐する。kv1 は段階 2 の PR で足す)
  - 記録を書く module(新しい記録の書き手)は log_ref を直接使わない(AST。一覧との差分も検出)
  - 型: 新しい記録の builder は OwnerRef を受ける(平文の str を型で拒否)
  - holding_ref は作らない(使う記録が無い。必要になった PR で足す)
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

import pytest

from jstock_advisor.domain.entities import identifier_ref as module
from jstock_advisor.domain.entities.identifier_ref import (
    CURRENT_VERSION,
    KNOWN_VERSIONS,
    IdentifierRefError,
    KeyedReferenceRequiredError,
    OwnerRef,
    owner_ref,
    parse_ref,
    require_persistable,
)
from jstock_advisor.domain.entities.owner import log_ref

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "domain" / "entities" / "identifier_ref.py"
_FORMAT = re.compile(r"^kv0:[0-9a-f]{24}$")


def test_the_reference_has_the_versioned_fixed_length_format() -> None:
    assert CURRENT_VERSION == "kv0"
    assert _FORMAT.match(owner_ref("owner-a"))


def test_the_reference_is_deterministic_and_distinct_per_input() -> None:
    assert owner_ref("owner-a") == owner_ref("owner-a")
    assert owner_ref("owner-a") != owner_ref("owner-b")


def test_the_digest_is_the_first_24_hex_of_the_labelled_sha256() -> None:
    """独立な定義: SHA-256('owner:' + 正規化した owner) の先頭 24 桁。形式を実装から写さない。"""
    expected = hashlib.sha256(b"owner:owner-a").hexdigest()[:24]
    assert owner_ref("owner-a") == f"kv0:{expected}"


def test_the_purpose_label_is_part_of_the_digest() -> None:
    """用途ラベルが入る(別の用途の符号を将来足しても、同じ入力が同じ符号にならない)。"""
    assert owner_ref("same-text") != f"kv0:{hashlib.sha256(b'same-text').hexdigest()[:24]}"


def test_notation_variants_of_the_same_owner_give_the_same_reference() -> None:
    """全角・半角・前後の空白・連続空白(owner.py の正規化と同じ規則)。"""
    base = owner_ref("owner a")
    assert owner_ref("　owner　a　") == base
    assert owner_ref("owner   a") == base
    assert owner_ref("ｏｗｎｅｒ ａ") == base


@pytest.mark.parametrize("value", ["", "   ", "\t\n", "　"])
def test_an_empty_owner_is_rejected_not_encoded(value: str) -> None:
    with pytest.raises(IdentifierRefError):
        owner_ref(value)


@pytest.mark.parametrize("value", [None, 5, b"x", ["a"]])
def test_a_non_str_owner_is_rejected(value: object) -> None:
    with pytest.raises(IdentifierRefError):
        owner_ref(value)  # type: ignore[arg-type]


def test_an_error_never_echoes_the_value() -> None:
    sentinel = "sentinel-owner-zz#9999"
    for attempt in (lambda: parse_ref(sentinel), lambda: owner_ref("")):
        with pytest.raises(IdentifierRefError) as caught:
            attempt()
        assert sentinel not in str(caught.value)
        assert not any(ch.isdigit() for ch in str(caught.value))


def test_the_reference_does_not_contain_the_plaintext() -> None:
    plain = "sentinel-owner-zz"
    assert plain not in owner_ref(plain)


def test_parse_ref_splits_the_implemented_version() -> None:
    assert parse_ref(owner_ref("owner-a")) == ("kv0", owner_ref("owner-a").split(":")[1])
    assert {"kv0", "kv1"} == KNOWN_VERSIONS


@pytest.mark.parametrize(
    "value",
    [
        "",
        "kv0:",
        "kv0:" + "a" * 23,
        "kv0:" + "a" * 25,
        "kv0:" + "A" * 24,
        "kv0:" + "g" * 24,
        "kv1:" + "a" * 24,  # kv1 は鍵の版が要る
        "kv1.0:" + "a" * 24,
        "kv1.1000:" + "a" * 24,
        "kv1.01:" + "a" * 24,
        "kv0.1:" + "a" * 24,  # kv0 は鍵の版を持たない
        "kv2.1:" + "a" * 24,
        "kv9:" + "a" * 24,
        "sha256:abcd1234",
        "xx0:" + "a" * 24,
        " kv0:" + "a" * 24,
        None,
        5,
    ],
)
def test_parse_ref_rejects_unimplemented_versions_and_malformed_references(
    value: object,
) -> None:
    with pytest.raises(IdentifierRefError):
        parse_ref(value)  # type: ignore[arg-type]


def test_the_error_is_built_in_exactly_one_place() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "IdentifierRefError"
    ]
    assert len(constructions) == 1


def test_log_ref_is_unchanged_and_is_a_different_thing() -> None:
    """log_ref(ログ・例外用の 8 桁)は変更しない。記録の識別子ではない。"""
    assert log_ref("owner-a").startswith("sha256:")
    assert len(log_ref("owner-a")) == len("sha256:") + 8
    assert log_ref("") == ""
    assert module.owner_ref("owner-a") != log_ref("owner-a")


def test_no_holding_reference_is_provided_until_a_record_needs_it() -> None:
    """使う記録が無い関数を先に作らない(必要になった PR で足す)。"""
    assert not hasattr(module, "holding_ref")
    assert not hasattr(module, "HoldingRef")


# --- 新しい記録の書き手は log_ref を直接使わない(NEW_RECORD_IDENTIFIER_RULE の 3)------------

#: AuditLog へ『新しい記録』を書く module。log_ref を使わず owner_ref を使う
_NEW_RECORD_WRITERS = {
    "services/allocation_shadow_service.py",
    "domain/signals/allocation_shadow_record.py",
    "services/allocation_shadow_compute.py",  # #603 PR-F2-2a(計算の組み立て)
    "services/allocation_shadow_runner.py",  # #603 PR-F2-2b(観測のループ。記録を書く)
    "lambda_handlers/allocation_shadow_wiring.py",  # #603 PR-F2-2b(配線。ループを組み立てる)
}


#: 名前に allocation_shadow を持つ『読み手』の module(#605 読取専用 CLI)。AuditLog へは書かない
#: (_NEW_RECORD_WRITERS には入れない。その集合は『書く module』の定義)。
#: 書込の経路が無いことは test_allocation_shadow_reader_contract(GetItem だけの proxy)と
#: test_allocation_cli_contract(AST)が固定する。ここへ足すのは『読むだけ』と示せるものに限る
_NEW_RECORD_READERS = {
    "domain/notification/allocation_shadow_cli_view.py",
    "domain/notification/allocation_shadow_cli_renderer.py",
    "infrastructure/aws/allocation_shadow_reader.py",
}


#: 名前に allocation_shadow を持つ『鍵の provider』の module(#925 kv1)。書き手でも読み手でもない。
#: AuditLog へは書かず、記録も読まない(Secrets Manager の鍵を読むだけ)。
#: (_NEW_RECORD_WRITERS・_NEW_RECORD_READERS には入れない。それぞれ『書く』『読む』module の定義)。
#: 記録の層へ触れないことは下の test_the_key_providers_touch_no_record_layer が固定する
_NEW_RECORD_KEY_PROVIDERS = {
    "infrastructure/aws/allocation_shadow_key_provider.py",
}


def _imports_or_calls_log_ref(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "log_ref" for a in node.names):
            return True
        if isinstance(node, ast.Name) and node.id == "log_ref":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "log_ref":
            return True
    return False


_ALL_SHADOW_MODULES = _NEW_RECORD_WRITERS | _NEW_RECORD_READERS | _NEW_RECORD_KEY_PROVIDERS


def test_the_new_record_writers_readers_and_key_providers_do_not_use_log_ref() -> None:
    """読み手・鍵の provider も log_ref(owner の平文へ解く経路)を使わない。網は 3 集合の和集合。"""
    offenders = []
    for relative in sorted(_ALL_SHADOW_MODULES):
        path = _PACKAGE / relative
        if path.exists() and _imports_or_calls_log_ref(path):
            offenders.append(relative)
    assert offenders == []


def test_every_allocation_shadow_module_is_in_one_of_the_lists() -> None:
    """名前に allocation_shadow を持つ src は、書き手・読取専用・鍵の provider のどれかへ入れる。"""
    named = {
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if "allocation_shadow" in path.name and path.name != "allocation_shadow_config.py"
    }
    assert named <= _ALL_SHADOW_MODULES, sorted(named - _ALL_SHADOW_MODULES)
    # 3 集合は互いに素(1 つの module が書き手かつ読み手、などの曖昧さを作らない)
    assert not (_NEW_RECORD_WRITERS & _NEW_RECORD_READERS)
    assert not (_NEW_RECORD_WRITERS & _NEW_RECORD_KEY_PROVIDERS)
    assert not (_NEW_RECORD_READERS & _NEW_RECORD_KEY_PROVIDERS)


def test_the_key_providers_touch_no_record_layer() -> None:
    """鍵の provider は、AuditLog・repository・DynamoDB・記録の module を import しない。"""
    forbidden = ("audit", "repository", "dynamodb", "allocation_shadow_record", "owner_ref")
    for relative in sorted(_NEW_RECORD_KEY_PROVIDERS):
        path = _PACKAGE / relative
        assert path.exists(), relative  # 空振り防止(一覧に載せた module が実在する)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
        offenders = sorted(m for m in imported if any(word in m.lower() for word in forbidden))
        assert offenders == [], (relative, offenders)


def test_the_loop_creates_owner_references_only_through_the_keyed_resolver() -> None:
    """新規の記録の符号は鍵つき(kv1)だけ: ループは `resolve_owner_ref` を使い、kv0 を使わない。"""
    runner = (_PACKAGE / "services" / "allocation_shadow_runner.py").read_text(encoding="utf-8")
    service = (_PACKAGE / "services" / "allocation_shadow_service.py").read_text(encoding="utf-8")

    assert "resolve_owner_ref(" in runner
    assert "owner_ref(" not in service.replace("resolve_owner_ref(", "")  # kv0 の関数を呼ばない


# --- 型: 平文の str を渡せない -------------------------------------------------------------


def test_the_reference_type_is_a_new_type_of_str() -> None:
    assert OwnerRef.__supertype__ is str  # type: ignore[attr-defined]


def test_the_function_returns_the_new_type_by_annotation() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    returns = {
        node.name: ast.unparse(node.returns)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.returns is not None
    }
    assert returns["owner_ref"] == "OwnerRef"


# --- 構造(純粋)-------------------------------------------------------------------------


def test_the_module_is_pure() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported <= {
        "__future__",
        "dataclasses",
        "enum",
        "hashlib",
        "hmac",
        "re",
        "typing",
        "jstock_advisor.domain.entities.owner",
    }
    forbidden_calls = {"now", "today", "open", "getenv", "environ"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_calls


# --- kv0 の境界(USER 決定 #925 …6142405238 の 4)-----------------------------------------


def test_only_the_default_owner_may_be_persisted_with_the_unkeyed_reference() -> None:
    from jstock_advisor.domain.entities.owner import DEFAULT_OWNER

    require_persistable(DEFAULT_OWNER)
    require_persistable(f"  {DEFAULT_OWNER}  ")  # 正規化後に同じ owner


@pytest.mark.parametrize("owner", ["owner-b", "owner-a", "", "  ", "本人2", "他人"])
def test_any_other_owner_is_refused_without_echoing_it(owner: str) -> None:
    with pytest.raises(KeyedReferenceRequiredError) as caught:
        require_persistable(owner)

    assert owner.strip() == "" or owner not in str(caught.value)
    assert isinstance(caught.value, IdentifierRefError)


@pytest.mark.parametrize("value", [None, 5, b"x", ["a"]])
def test_a_non_str_owner_is_refused_too(value: object) -> None:
    with pytest.raises(KeyedReferenceRequiredError):
        require_persistable(value)  # type: ignore[arg-type]


def test_the_refusal_error_is_built_without_a_value() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "KeyedReferenceRequiredError"
    ]
    assert len(calls) == 1
    assert all(isinstance(arg, ast.Constant) for arg in calls[0].args)
