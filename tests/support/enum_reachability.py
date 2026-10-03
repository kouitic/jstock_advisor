"""enum member の「名前による生成箇所」を AST で数える(Issue #274)。

`tests/unit/test_enum_reachability_guard.py` が使う。**registry の再生成と guard の検証が
同じ規則を使う**ために、規則を本モジュールの1か所だけに定義する(registry と guard の規則が
離れると、guard が黙って古くなる)。

## 規則(機械的。人手の判定を含まない)

```
対象     src/jstock_advisor/ 配下の全 .py の `class X(Enum | StrEnum | IntEnum | IntFlag | Flag)` の
         member(アンダースコア始まりを除く)。enum class は (ファイルパス, クラス名) で区別する
         (同じクラス名が複数のファイルにある: 実測で ShadowMode が 2 か所・CsvRowStatus が 4 か所)
参照     `Enum.MEMBER`(または `module.Enum.MEMBER`)の属性参照を 1 件ずつ、親ノードの文脈で分類する
           GEN      生成(値を作る・渡す): 代入の右辺 / return / Call の引数・kwarg / dict の値 /
                    既定値 / IfExp の分岐 / yield
           CONSUME  消費(値を読む): Compare の項 / dict の key / Subscript の添字 /
                    match の pattern / 集合定数(ALL_CAPS への代入)の要素 / `in` の右辺
           VALUE    `.value` / `.name` の属性アクセス
           OTHER    上のどれでもない
UNGENERATED_BY_NAME   src で GEN が 0 件の member(= 名前で生成している箇所が src に無い)
動的な構築 / iteration  `Enum(<値>)`・for / comprehension / list() 等による iteration がある class
```

## この規則の「視界外」(★ 「到達不能」の根拠にしてはならない)

```
・`Enum(value)` の構築・iteration がある class(DYNAMIC_CLASS)の member は、
  名前で生成されていなくても永続データや入力文字列から構築されて到達しうる
・pydantic の field 検証による構築(永続データの読み込み)は AST では数えられない
・getattr / dict の key 経由・文字列からの構築・fixture / parametrize 経由の参照は数えない
・同名の enum class(複数ファイル)の参照の帰属は、同じファイル内の定義・from-import で解決する。
  解決できない参照は「曖昧」として全候補へ帰属させ(= 生成と数える側へ倒す)、件数を報告する
```
"""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

GEN = "GEN"
CONSUME = "CONSUME"
VALUE = "VALUE"
OTHER = "OTHER"

ENUM_BASES = frozenset({"Enum", "StrEnum", "IntEnum", "IntFlag", "Flag"})
_ITERATING_CALLS = frozenset({"list", "set", "tuple", "frozenset", "sorted"})


@dataclass(frozen=True)
class EnumDef:
    """enum class の定義1件。ファイルパス(src 相対)とクラス名で区別する。"""

    path: str
    name: str
    members: tuple[str, ...]


@dataclass
class ScanResult:
    """あるディレクトリ(src または tests)を走査した結果。"""

    refs: dict[tuple[EnumDef, str], Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    dynamic: dict[EnumDef, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    #: 同名の複数 class のどれへの参照か解決できなかった参照の件数
    ambiguous_refs: int = 0


def member_id(enum: EnumDef, member: str, *, duplicate_names: frozenset[str]) -> str:
    """registry の識別子。クラス名が一意なら `Class.MEMBER`、重複なら `Class[path].MEMBER`。"""
    if enum.name in duplicate_names:
        return f"{enum.name}[{enum.path}].{member}"
    return f"{enum.name}.{member}"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _base_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def collect_enums_from_tree(tree: ast.Module, path: str) -> list[EnumDef]:
    found: list[EnumDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not ({_base_name(b) for b in node.bases} & ENUM_BASES):
            continue
        members: list[str] = []
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                members.extend(
                    t.id
                    for t in stmt.targets
                    if isinstance(t, ast.Name) and not t.id.startswith("_")
                )
            elif (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.value is not None
                and not stmt.target.id.startswith("_")
            ):
                members.append(stmt.target.id)
        found.append(EnumDef(path, node.name, tuple(members)))
    return found


def collect_enums(src_root: Path) -> list[EnumDef]:
    enums: list[EnumDef] = []
    for path in sorted(src_root.rglob("*.py")):
        enums.extend(collect_enums_from_tree(_parse(path), path.relative_to(src_root).as_posix()))
    return enums


def _annotate_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node  # type: ignore[attr-defined]


def classify(node: ast.AST) -> str:
    """enum member への参照1件を、親ノードの文脈で GEN / CONSUME / VALUE / OTHER に分ける。"""
    cur = node
    while True:
        parent = getattr(cur, "_parent", None)
        if parent is None:
            return OTHER
        if (
            isinstance(parent, ast.Attribute)
            and parent.value is cur
            and parent.attr
            in (
                "value",
                "name",
            )
        ):
            return VALUE
        if isinstance(parent, ast.Compare):
            return CONSUME
        if isinstance(parent, ast.Dict):
            return CONSUME if cur in parent.keys else GEN
        if isinstance(parent, ast.Subscript) and parent.slice is cur:
            return CONSUME
        if isinstance(parent, (ast.MatchValue, ast.MatchSingleton)):
            return CONSUME
        if isinstance(parent, (ast.Set, ast.Tuple, ast.List)):
            grandparent = getattr(parent, "_parent", None)
            if isinstance(grandparent, ast.Compare):
                return CONSUME
            outer: ast.AST = parent
            up = getattr(outer, "_parent", None)
            while isinstance(up, ast.Call) and getattr(up.func, "id", None) in (
                "frozenset",
                "set",
                "tuple",
                "list",
            ):
                outer, up = up, getattr(up, "_parent", None)
            if isinstance(up, (ast.Assign, ast.AnnAssign)):
                targets = up.targets if isinstance(up, ast.Assign) else [up.target]
                if any(isinstance(t, ast.Name) and t.id.lstrip("_").isupper() for t in targets):
                    return CONSUME
            cur = parent
            continue
        if isinstance(parent, ast.keyword):
            return GEN
        if isinstance(parent, (ast.IfExp, ast.BoolOp, ast.Starred, ast.Await)):
            cur = parent
            continue
        if isinstance(parent, ast.Call):
            return GEN
        if isinstance(
            parent,
            (ast.Return, ast.Assign, ast.AnnAssign, ast.Yield, ast.YieldFrom, ast.AugAssign),
        ):
            return GEN
        if isinstance(parent, ast.arguments):
            return GEN
        if isinstance(parent, ast.Expr):
            return OTHER
        cur = parent


def _import_map(tree: ast.Module, file_module: str) -> dict[str, str]:
    """`from X import Name [as Alias]` の (ローカル名 -> 取り込み元 module のドット表記)。"""
    mapping: dict[str, str] = {}
    package_parts = file_module.split(".")[:-1]
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = package_parts[: len(package_parts) - (node.level - 1)]
            module = ".".join([*base, *(node.module.split(".") if node.module else [])])
        else:
            module = node.module or ""
        for alias in node.names:
            mapping[alias.asname or alias.name] = module
    return mapping


def _module_of(path: str) -> str:
    """src 相対パス -> `jstock_advisor.x.y` のドット表記。"""
    return "jstock_advisor." + path.removesuffix(".py").replace("/", ".")


def scan(
    root: Path,
    enums: list[EnumDef],
    *,
    module_prefix: str,
    same_file_resolution: bool,
    skip_parts: frozenset[str] = frozenset({"__pycache__", ".aws-sam"}),
) -> ScanResult:
    """`root` 配下の全 .py の enum member 参照と動的な構築を数える。

    module_prefix          相対 import を解決するための、root 直下の module のドット表記の接頭辞
                           (src は `jstock_advisor.`、tests は `tests.`)
    same_file_resolution   同名の複数 class の参照を、同じファイル内の定義へ帰属させる(src のみ)
    """
    by_name: dict[str, list[EnumDef]] = defaultdict(list)
    for enum in enums:
        by_name[enum.name].append(enum)

    result = ScanResult()
    for path in sorted(root.rglob("*.py")):
        if skip_parts & set(path.parts):
            continue
        tree = _parse(path)
        _annotate_parents(tree)
        rel = path.relative_to(root).as_posix()
        imports = _import_map(tree, module_prefix + rel.removesuffix(".py").replace("/", "."))
        scan_tree(tree, rel, by_name, imports, result, same_file_resolution=same_file_resolution)
    return result


def _resolve(
    name: str,
    member: str | None,
    by_name: dict[str, list[EnumDef]],
    imports: dict[str, str],
    rel_path: str,
    same_file_resolution: bool,
    result: ScanResult,
) -> list[EnumDef]:
    candidates = [e for e in by_name.get(name, []) if member is None or member in e.members]
    if len(candidates) <= 1:
        return candidates
    if same_file_resolution:
        same_file = [e for e in candidates if e.path == rel_path]
        if same_file:
            return same_file
    module = imports.get(name)
    if module:
        imported = [e for e in candidates if _module_of(e.path) == module]
        if imported:
            return imported
    result.ambiguous_refs += 1
    return candidates


def scan_tree(
    tree: ast.Module,
    rel_path: str,
    by_name: dict[str, list[EnumDef]],
    imports: dict[str, str],
    result: ScanResult,
    *,
    same_file_resolution: bool,
) -> None:
    for node in ast.walk(tree):
        # Enum.MEMBER / module.Enum.MEMBER
        if isinstance(node, ast.Attribute):
            value = node.value
            owner = value.id if isinstance(value, ast.Name) else _attr_name(value)
            if owner in by_name:
                for enum in _resolve(
                    owner, node.attr, by_name, imports, rel_path, same_file_resolution, result
                ):
                    if node.attr in enum.members:
                        result.refs[(enum, node.attr)][classify(node)] += 1
        # 動的な構築 / iteration
        if isinstance(node, ast.Call):
            func_name = _call_name(node.func)
            if func_name in by_name and node.args:
                for enum in _resolve(
                    func_name, None, by_name, imports, rel_path, same_file_resolution, result
                ):
                    result.dynamic[enum]["construct"] += 1
            if func_name in _ITERATING_CALLS and node.args:
                arg = node.args[0]
                arg_name = arg.id if isinstance(arg, ast.Name) else None
                if arg_name in by_name:
                    for enum in _resolve(
                        arg_name, None, by_name, imports, rel_path, same_file_resolution, result
                    ):
                        result.dynamic[enum]["iterate"] += 1
        iterated: ast.expr | None = None
        if isinstance(node, (ast.For, ast.comprehension)):
            iterated = node.iter
        if isinstance(iterated, ast.Name) and iterated.id in by_name:
            for enum in _resolve(
                iterated.id, None, by_name, imports, rel_path, same_file_resolution, result
            ):
                result.dynamic[enum]["iterate"] += 1


def _attr_name(node: ast.expr) -> str | None:
    return node.attr if isinstance(node, ast.Attribute) else None


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def duplicate_class_names(enums: list[EnumDef]) -> frozenset[str]:
    counts = Counter(e.name for e in enums)
    return frozenset(name for name, n in counts.items() if n > 1)
