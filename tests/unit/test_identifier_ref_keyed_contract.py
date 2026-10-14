"""Issue #925 段階 2: 鍵つきの owner の符号 kv1 と鍵の型の契約テスト。

固定するもの:
  - kv1 = HMAC-SHA256(鍵, b"owner:" + 正規化した owner)の先頭 24 桁 + 鍵の版(`kv1.{版}:{桁}`)。
    独立な計算(hmac モジュール・RFC 4231 のテストベクトル)と一致
  - 鍵・鍵の版・owner が変われば符号が変わる / 表記ゆれは同じ符号 / 平文を含まない
  - KeyMaterial: 短い鍵・非 bytes・版の範囲外を拒否。★ repr・str・format・pickle・copy・
    dataclass 変換・例外のどれにも鍵が出ない(sentinel の鍵で走査)★
  - resolve_owner_ref: ★ 鍵が取れなければ符号を作らない(kv0・固定の符号へ倒さない)★。provider が
    例外・不正な型を返しても KeyUnavailable(ERROR)(例外を伝播させない)
  - parse_ref: kv0 と kv1.N の両方を受理(S4)。kv1 の鍵の版は 1〜999・先頭 0 なし
  - 書込側の境界: require_keyed が kv0・壊れた符号・平文を拒否。新しい記録の書き手(builder)は
    kv0 の owner_ref を import しない。owner_ref(kv0)を import する src は一覧の中だけ
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import hmac
import io
import pickle
import re
from pathlib import Path

import pytest

from jstock_advisor.domain.entities import identifier_ref as module
from jstock_advisor.domain.entities.identifier_ref import (
    KEYED_VERSION,
    KNOWN_VERSIONS,
    IdentifierRefError,
    KeyedOwnerRef,
    KeyedReferenceRequiredError,
    KeyMaterial,
    KeyUnavailable,
    KeyUnavailableReason,
    keyed_owner_ref,
    owner_ref,
    parse_keyed_ref,
    parse_ref,
    require_keyed,
    resolve_owner_ref,
)

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "domain" / "entities" / "identifier_ref.py"
_KV1 = re.compile(r"^kv1\.[1-9][0-9]{0,2}:[0-9a-f]{24}$")

#: テスト用の鍵(秘密ではない。sentinel として全方向の漏出を走査する)
_SENTINEL_KEY = b"sentinel-key-material-0123456789abcdef"
_SENTINEL_OWNER = "sentinel-owner-zz"


def _key(version: int = 1, key: bytes = _SENTINEL_KEY) -> KeyMaterial:
    return KeyMaterial(key_version=version, key=key)


# --- 形式と独立な計算 -----------------------------------------------------------------------


def test_the_keyed_reference_has_the_versioned_format() -> None:
    assert KEYED_VERSION == "kv1"
    assert _KV1.match(keyed_owner_ref("owner-a", _key(7)))
    assert keyed_owner_ref("owner-a", _key(7)).startswith("kv1.7:")


def test_the_digest_is_the_first_24_hex_of_an_independent_hmac_sha256() -> None:
    """独立な定義: HMAC-SHA256(鍵, 'owner:' + owner)の先頭 24 桁。実装から式を写さない。"""
    expected = hmac.new(_SENTINEL_KEY, b"owner:owner-a", hashlib.sha256).hexdigest()[:24]
    assert keyed_owner_ref("owner-a", _key(3)) == f"kv1.3:{expected}"


def test_the_hmac_matches_the_rfc_4231_test_case_through_the_same_primitive() -> None:
    """RFC 4231 の Test Case 2(鍵 'Jefe')で HMAC-SHA256 の部品そのものを確認する。

    KeyMaterial は 32 バイト未満の鍵を拒否するので、同じ構成(hmac.new(鍵, msg, sha256))を
    4231 の鍵で確かめ、kv1 が使う _hmac が標準の HMAC-SHA256 と同じ出力であることを、
    長い鍵(Test Case 6 = 131 バイトの 0xaa)で直接確かめる。
    """
    standard = hmac.new(b"Jefe", b"what do ya want for nothing?", hashlib.sha256).hexdigest()
    assert standard == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    key = b"\xaa" * 131
    message = b"Test Using Larger Than Block-Size Key - Hash Key First"
    vector = "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"
    assert _key(1, key)._hmac(message) == vector


def test_it_is_deterministic_and_differs_by_owner_key_and_key_version() -> None:
    base = keyed_owner_ref("owner-a", _key(1))
    assert keyed_owner_ref("owner-a", _key(1)) == base
    assert keyed_owner_ref("owner-b", _key(1)) != base
    other_key = _key(1, b"another-key-material-0123456789abcdef")
    assert keyed_owner_ref("owner-a", other_key) != base
    # 鍵の版は符号に入る(同じ鍵でも版が違えば別の文字列 = reader が版で分けられる)
    assert keyed_owner_ref("owner-a", _key(2)) != base
    assert keyed_owner_ref("owner-a", _key(2)).split(":")[1] == base.split(":")[1]


def test_notation_variants_of_the_same_owner_give_the_same_keyed_reference() -> None:
    base = keyed_owner_ref("owner a", _key())
    assert keyed_owner_ref("　owner　a　", _key()) == base
    assert keyed_owner_ref("owner   a", _key()) == base
    assert keyed_owner_ref("ｏｗｎｅｒ ａ", _key()) == base


def test_the_keyed_reference_is_not_the_unkeyed_one_and_hides_the_plaintext() -> None:
    keyed = keyed_owner_ref(_SENTINEL_OWNER, _key())
    assert _SENTINEL_OWNER not in keyed
    assert keyed.split(":")[1] != owner_ref(_SENTINEL_OWNER).split(":")[1]


@pytest.mark.parametrize("value", ["", "   ", "\t\n", "　"])
def test_an_empty_owner_is_rejected(value: str) -> None:
    with pytest.raises(IdentifierRefError):
        keyed_owner_ref(value, _key())


@pytest.mark.parametrize("value", [None, 5, b"x", ["a"]])
def test_a_non_str_owner_is_rejected(value: object) -> None:
    with pytest.raises(IdentifierRefError):
        keyed_owner_ref(value, _key())  # type: ignore[arg-type]


@pytest.mark.parametrize("key", [None, b"k" * 40, "k" * 40, object()])
def test_a_key_that_is_not_a_key_material_is_rejected(key: object) -> None:
    with pytest.raises(IdentifierRefError):
        keyed_owner_ref("owner-a", key)  # type: ignore[arg-type]


# --- KeyMaterial: 検証と漏出の防止 ------------------------------------------------------------


@pytest.mark.parametrize("version", [0, -1, 1000, 1.0, "1", None, True])
def test_a_key_version_outside_1_to_999_is_rejected(version: object) -> None:
    with pytest.raises(IdentifierRefError):
        KeyMaterial(key_version=version, key=_SENTINEL_KEY)  # type: ignore[arg-type]


@pytest.mark.parametrize("version", [1, 2, 999])
def test_the_key_version_bounds_are_accepted(version: int) -> None:
    assert KeyMaterial(key_version=version, key=_SENTINEL_KEY).key_version == version


@pytest.mark.parametrize("key", [b"", b"k" * 31, "k" * 40, bytearray(b"k" * 40), None, 5])
def test_a_short_or_non_bytes_key_is_rejected(key: object) -> None:
    with pytest.raises(IdentifierRefError):
        KeyMaterial(key_version=1, key=key)  # type: ignore[arg-type]


def test_a_key_of_the_minimum_length_is_accepted() -> None:
    assert KeyMaterial(key_version=1, key=b"k" * 32).key_version == 1


def _leak_forms(material: KeyMaterial) -> list[str]:
    """鍵を文字として取り出しうる全ての経路の出力。"""
    forms = [repr(material), str(material), f"{material}", f"{material!r}"]
    forms.append(format(material, ">40"))
    forms.append(f"{[material]}")  # 入れ子の repr
    forms.append(f"{ {'k': material} }")
    return forms


def _contains_key(text: str) -> bool:
    key = _SENTINEL_KEY
    candidates = {
        key.decode(),
        key.hex(),
        key.hex().upper(),
        __import__("base64").b64encode(key).decode(),
        __import__("base64").urlsafe_b64encode(key).decode(),
        repr(key),
        str(list(key)),
    }
    return any(candidate in text for candidate in candidates)


def test_no_text_form_of_the_key_material_contains_the_key() -> None:
    forms = _leak_forms(_key(5))
    assert all(not _contains_key(form) for form in forms)
    assert repr(_key(5)) == "KeyMaterial(key_version=5)"


def test_the_key_material_cannot_be_pickled_copied_or_converted() -> None:
    material = _key()
    for action in (
        lambda: pickle.dumps(material),
        lambda: pickle.dumps(material, protocol=0),
        lambda: pickle.dumps(material, protocol=pickle.HIGHEST_PROTOCOL),
        lambda: copy.copy(material),
        lambda: copy.deepcopy(material),
        lambda: dataclasses.asdict(material),  # type: ignore[call-overload]
        lambda: dataclasses.astuple(material),  # type: ignore[call-overload]
        lambda: vars(material),
        lambda: material.__getstate__(),
    ):
        with pytest.raises((TypeError, AttributeError)):
            action()


def test_the_key_material_does_not_survive_a_pickler_that_ignores_the_methods() -> None:
    buffer = io.BytesIO()
    with pytest.raises(TypeError):
        pickle.Pickler(buffer, protocol=2).dump(_key())
    assert not _contains_key(buffer.getvalue().decode("latin-1"))


def test_the_key_material_is_immutable_and_has_no_accessor_for_the_key() -> None:
    material = _key()
    with pytest.raises(AttributeError):
        material.key_version = 9  # type: ignore[misc]
    with pytest.raises(AttributeError):
        material._key = b"x" * 40
    with pytest.raises(AttributeError):
        del material._key
    public = {name for name in dir(material) if not name.startswith("_")}
    assert public == {"key_version"}
    assert not hasattr(material, "__dict__")


def test_the_protection_scope_is_stated_and_excludes_direct_attribute_access() -> None:
    """USER の SHOULD: 防げるもの(表示・直列化・複製・例外)と防げないもの(内部属性の直接参照)。

    下の 2 つの assert は『防げない』ことの明記である(この型を使っても、同じプロセスのコードは鍵を
    読める)。鍵の境界は『鍵を持つ role を Lambda の 2 つに限る』ことで守る(型では守らない)。
    """
    material = _key()

    # 防げないもの(明記): 内部属性への直接アクセス・内部のメソッドの呼び出し
    assert object.__getattribute__(material, "_key") == _SENTINEL_KEY
    assert material._hmac(b"m") == hmac.new(_SENTINEL_KEY, b"m", hashlib.sha256).hexdigest()
    # 防げるもの: 通常の経路(上の別のテストが全方向を走査)
    assert not _contains_key(repr(material) + str(material) + f"{material}")
    doc = KeyMaterial.__doc__ or ""
    assert "内部属性への直接アクセス" in doc
    assert "防止できない" in doc


def test_the_key_material_compares_by_key_and_version_and_hashes_by_version_only() -> None:
    assert _key(1) == _key(1)
    assert _key(1) != _key(2)
    assert _key(1) != _key(1, b"another-key-material-0123456789abcdef")
    assert _key(1) != object()
    assert hash(_key(1)) == hash(_key(1, b"another-key-material-0123456789abcdef"))


def test_a_construction_error_never_contains_the_key_or_the_owner() -> None:
    with pytest.raises(IdentifierRefError) as short:
        KeyMaterial(key_version=1, key=b"short-sentinel")
    assert "short-sentinel" not in str(short.value)
    with pytest.raises(IdentifierRefError) as empty:
        keyed_owner_ref("   ", _key())
    assert _SENTINEL_KEY.decode() not in str(empty.value)


# --- KeyUnavailable / resolve_owner_ref(fail-closed)-------------------------------------------


def test_the_reasons_are_the_agreed_vocabulary() -> None:
    assert {r.value for r in KeyUnavailableReason} == {
        "NOT_CONFIGURED",
        "ACCESS_DENIED",
        "NOT_FOUND",
        "TIMEOUT",
        "MALFORMED",
        "ERROR",
    }


def test_a_key_unavailable_holds_only_a_reason() -> None:
    assert [f.name for f in dataclasses.fields(KeyUnavailable)] == ["reason"]
    with pytest.raises(IdentifierRefError):
        KeyUnavailable("TIMEOUT")  # type: ignore[arg-type]


class _Provider:
    def __init__(self, result: object = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def get(self) -> object:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def test_resolve_returns_the_keyed_reference_when_the_key_is_available() -> None:
    provider = _Provider(_key(4))
    assert resolve_owner_ref("owner-a", provider) == keyed_owner_ref("owner-a", _key(4))  # type: ignore[arg-type]
    assert provider.calls == 1


@pytest.mark.parametrize("reason", list(KeyUnavailableReason))
def test_resolve_passes_every_unavailable_reason_through_and_never_makes_a_reference(
    reason: KeyUnavailableReason,
) -> None:
    result = resolve_owner_ref("owner-a", _Provider(KeyUnavailable(reason)))  # type: ignore[arg-type]

    assert result == KeyUnavailable(reason)
    assert not isinstance(result, str)  # ★ 符号(kv0・固定の符号を含む)を返さない ★


@pytest.mark.parametrize(
    "error", [RuntimeError("boom"), OSError("net"), ValueError("v"), KeyError("k"), TimeoutError()]
)
def test_a_provider_that_raises_is_folded_into_error_without_propagating(
    error: Exception,
) -> None:
    result = resolve_owner_ref("owner-a", _Provider(error=error))  # type: ignore[arg-type]

    assert result == KeyUnavailable(KeyUnavailableReason.ERROR)


@pytest.mark.parametrize("bad", [None, "kv1.1:" + "a" * 24, b"k" * 40, {"key": 1}, 5, object()])
def test_a_provider_that_returns_something_else_is_an_error(bad: object) -> None:
    result = resolve_owner_ref("owner-a", _Provider(bad))  # type: ignore[arg-type]

    assert result == KeyUnavailable(KeyUnavailableReason.ERROR)


def test_an_invalid_owner_is_a_caller_error_not_a_key_failure() -> None:
    provider = _Provider(_key())
    with pytest.raises(IdentifierRefError):
        resolve_owner_ref("  ", provider)  # type: ignore[arg-type]
    assert provider.calls == 0  # 鍵を取りに行かない


def test_the_failure_path_never_reaches_the_unkeyed_function() -> None:
    """resolve_owner_ref の本体は kv0 の owner_ref() を呼ばない(AST)。倒し先が存在しない。"""
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "resolve_owner_ref"
    )
    called = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "owner_ref" not in called
    assert "require_persistable" not in called


# --- parse_ref(S4)---------------------------------------------------------------------------


def test_parse_ref_accepts_both_versions() -> None:
    assert {"kv0", "kv1"} == KNOWN_VERSIONS
    assert parse_ref(owner_ref("owner-a")) == ("kv0", owner_ref("owner-a").split(":")[1])
    keyed = keyed_owner_ref("owner-a", _key(12))
    assert parse_ref(keyed) == ("kv1", keyed.split(":")[1])
    assert parse_keyed_ref(keyed) == (12, keyed.split(":")[1])


@pytest.mark.parametrize("version", [1, 9, 10, 99, 100, 999])
def test_parse_keyed_ref_reads_the_key_version(version: int) -> None:
    assert parse_keyed_ref(f"kv1.{version}:" + "a" * 24)[0] == version


@pytest.mark.parametrize(
    "value",
    [
        "kv1:" + "a" * 24,
        "kv1.:" + "a" * 24,
        "kv1.0:" + "a" * 24,
        "kv1.00:" + "a" * 24,
        "kv1.01:" + "a" * 24,
        "kv1.1000:" + "a" * 24,
        "kv1.-1:" + "a" * 24,
        "kv1.1.2:" + "a" * 24,
        "kv1.1:" + "a" * 23,
        "kv1.1:" + "a" * 25,
        "kv1.1:" + "A" * 24,
        "kv1.1:" + "g" * 24,
        "kv0.1:" + "a" * 24,
        "kv2.1:" + "a" * 24,
        "kv10.1:" + "a" * 24,
        " kv1.1:" + "a" * 24,
        "kv1.1:" + "a" * 24 + "\n",
        "KV1.1:" + "a" * 24,
        None,
        5,
    ],
)
def test_parse_ref_rejects_malformed_or_out_of_range_keyed_references(value: object) -> None:
    with pytest.raises(IdentifierRefError):
        parse_ref(value)  # type: ignore[arg-type]


def test_parse_keyed_ref_refuses_an_unkeyed_reference() -> None:
    with pytest.raises(KeyedReferenceRequiredError):
        parse_keyed_ref(owner_ref("owner-a"))


# --- 書込側の境界 ----------------------------------------------------------------------------


def test_require_keyed_accepts_only_a_well_formed_keyed_reference() -> None:
    keyed = keyed_owner_ref("owner-a", _key())
    assert require_keyed(keyed) == keyed


@pytest.mark.parametrize(
    "value",
    [
        owner_ref("owner-a"),  # kv0
        "owner-a",  # 平文
        "",
        "kv1:" + "a" * 24,
        "sha256:abcd1234",
        None,
        5,
    ],
)
def test_require_keyed_refuses_everything_else_without_echoing_it(value: object) -> None:
    with pytest.raises(KeyedReferenceRequiredError) as caught:
        require_keyed(value)  # type: ignore[arg-type]

    assert isinstance(caught.value, IdentifierRefError)
    assert not isinstance(value, str) or not value or value not in str(caught.value)


def test_the_keyed_reference_type_is_a_subtype_of_the_unkeyed_type() -> None:
    assert KeyedOwnerRef.__supertype__ is module.OwnerRef  # type: ignore[attr-defined]


def test_the_functions_return_the_new_types_by_annotation() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    returns = {
        node.name: ast.unparse(node.returns)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.returns is not None
    }
    assert returns["keyed_owner_ref"] == "KeyedOwnerRef"
    assert returns["require_keyed"] == "KeyedOwnerRef"
    assert returns["resolve_owner_ref"] == "KeyedOwnerRef | KeyUnavailable"
    assert returns["owner_ref"] == "OwnerRef"


#: kv0 の owner_ref()(鍵なし)・require_persistable を使ってよい src = ★ 空 ★(#603 PR-F2-2b)。
#: 新規の記録の符号は、観測のループが `resolve_owner_ref` で作る kv1 だけ(例外なし)。
#: 一覧に 1 つでも足す変更は『実在の owner を kv0 で永続できる経路』を作る(USER の承認の記録が要る)
_UNKEYED_REFERENCE_USERS: set[str] = set()


def _is_identifier_ref_module(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "identifier_ref"
    return isinstance(node, ast.Attribute) and node.attr == "identifier_ref"


def _names_from_identifier_ref(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (
            node.module == "jstock_advisor.domain.entities.identifier_ref"
        ):
            names |= {alias.name for alias in node.names}
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {"owner_ref", "require_persistable"}
            and _is_identifier_ref_module(node.value)
        ):
            names.add(node.attr)  # `identifier_ref.xxx` の形だけ(フィールド名は除く)
    return names


def test_only_the_listed_modules_use_the_unkeyed_reference_function() -> None:
    users = {
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if path != _MODULE_PATH
        and {"owner_ref", "require_persistable"} & _names_from_identifier_ref(path)
    }
    assert users == _UNKEYED_REFERENCE_USERS


def test_the_record_builders_do_not_import_the_unkeyed_function() -> None:
    record = _PACKAGE / "domain" / "signals" / "allocation_shadow_record.py"
    names = _names_from_identifier_ref(record)
    assert not names & {"owner_ref", "require_persistable"}
    assert "require_keyed" in names


def test_the_module_stays_free_of_io_and_logging() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not imported & {"logging", "os", "boto3", "botocore", "json", "pickle", "copy"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"print", "open", "input"}


# --- 配線後の境界(#603 PR-F2-2b): 符号は resolve_owner_ref を経由した kv1 だけ ----------------
# (旧い『owners を渡す呼び出しが 1 つも無い』tripwire は、kv0 の前提 = CURRENT_VERSION に依存して
#  自動では外れなかった。配線後は、前提に依存しない形 = 作り手と呼び出しの集合の完全一致で固定する)

_ENTRY_KEYWORDS = {
    "batch_id",
    "now",
    "execution_context",
    "audit_service",
    "remaining_time_ms",
    "shadow_config",
    "wiring_factory",
    "monotonic",
}


def _calls_named(names: set[str]) -> dict[str, set[str]]:
    """src 全体で、指定した名前の関数の呼び出しを持つ file(identifier_ref 自身を除く)。"""
    found: dict[str, set[str]] = {}
    for path in _PACKAGE.rglob("*.py"):
        if path == _MODULE_PATH:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                called = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                if called in names:
                    found.setdefault(path.relative_to(_PACKAGE).as_posix(), set()).add(called)
    return found


def test_owner_references_are_created_only_by_the_loop_through_the_keyed_resolver() -> None:
    found = _calls_named(
        {"keyed_owner_ref", "resolve_owner_ref", "owner_ref", "require_persistable"}
    )

    # ★ 符号を作る呼び出しは、観測のループの `resolve_owner_ref` だけ(kv0・直接の kv1 の生成は 0)★
    assert found == {"services/allocation_shadow_runner.py": {"resolve_owner_ref"}}


def test_no_src_call_site_passes_owners_or_a_compute_to_the_shadow_entry() -> None:
    """入口に owner の一覧・計算の差し込み先を渡す呼び出しが無い(渡せる引数も無い)。"""
    sites = []
    for path in _PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "observe_allocation_shadow"
            ):
                sites.append((path.relative_to(_PACKAGE).as_posix(), node))
    assert [file for file, _ in sites] == ["lambda_handlers/buy_candidates_handler.py"]
    for _, node in sites:
        keywords = {keyword.arg for keyword in node.keywords}
        assert keywords <= _ENTRY_KEYWORDS, sorted(keywords - _ENTRY_KEYWORDS)
        assert not keywords & {"owners", "compute"}
