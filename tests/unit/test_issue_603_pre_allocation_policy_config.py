"""Issue #603 / PR-E: 購入側の資金配分の方針値(専用 YAML + loader)の契約テスト。

固定するもの:
  - 出荷値は USER が承認した判断 B(#122 …6114810773【3】)のとおり。変えるなら意図した変更として
    この表も一緒に更新する(= 閾値の変更は USER 判断)
  - 出荷の `comparison_key` は RAER 側の定数 RAER_FULL_COMPARISON_KEY(#602 PR-G2 で設定。
    null にすると POLICY_INCOMPLETE = 配分しない)
  - fail-closed: 無い・壊れている・不正(値が 1 つでも)なら全項目を未決定へ縮退し、例外を送出せず、
    警告に値・path を出さない。項目だけの null は、その項目だけが未決定(0・既定値で補完しない)
  - 数値は float の誤差を持ち込まない / bool・文字列・NaN・±inf を拒否 / 比率は (0, 1]
  - config_hash: 値・方針の名前・売買単位が変わると変わり、キー順・数値の表記に依らない
  - 構造: どこからも import されない(dormant)/ 時計・I/O・AWS・services・AppConfig へ触れない /
    有効化の入口(mode / enabled)を持たない
"""

from __future__ import annotations

import ast
import logging
import os
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from jstock_advisor.domain.signals import allocation_policy_config as module
from jstock_advisor.domain.signals.allocation_policy_config import (
    CONFIG_FILE_NAME,
    AllocationPolicyBundle,
    AllocationPolicyConfig,
    load_allocation_policy,
    undecided_policy,
)
from jstock_advisor.domain.signals.portfolio_allocation_types import (
    POLICY_ITEMS,
    AllocationContractError,
    PriceTier,
)
from jstock_advisor.domain.valuation.raer import RiskFlag
from jstock_advisor.domain.valuation.raer_inputs import RAER_FULL_COMPARISON_KEY

_REPO = Path(__file__).resolve().parents[2]
_CONFIG_DIR = _REPO / "config"
_PACKAGE = _REPO / "src" / "jstock_advisor"
_UNIT = 100


def _shipped_data() -> dict[str, Any]:
    loaded = yaml.safe_load((_CONFIG_DIR / CONFIG_FILE_NAME).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write(tmp_path: Path, data: dict[str, Any] | str) -> Path:
    text = data if isinstance(data, str) else yaml.safe_dump(data, allow_unicode=True)
    (tmp_path / CONFIG_FILE_NAME).write_text(text, encoding="utf-8")
    return tmp_path


def _load(tmp_path: Path, data: dict[str, Any] | str) -> AllocationPolicyBundle:
    return load_allocation_policy(_write(tmp_path, data), minimum_trading_unit=_UNIT)


def _with(**changes: Any) -> dict[str, Any]:
    data = _shipped_data()
    data.update(changes)
    return data


def _all_keys(value: Any) -> list[str]:
    """入れ子を含む全てのキー(dict の中の dict・list の中の dict も辿る)。"""
    keys: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            keys.append(str(key))
            keys.extend(_all_keys(inner))
    elif isinstance(value, list):
        for inner in value:
            keys.extend(_all_keys(inner))
    return keys


def _is_fallback(bundle: AllocationPolicyBundle) -> bool:
    return (
        bundle.source == "FALLBACK"
        and bundle.policy_id is None
        and bundle.unknown_price_tier_as is None
        and bundle.policy.min_raer_to_allocate is None
        and bundle.policy.comparison_key is None
        and bundle.policy.tier_quantity_caps is None
        and bundle.constraints.max_single_stock_amount is None
        and bundle.constraints.max_single_stock_ratio is None
        and bundle.constraints.max_sector_ratio is None
        and bundle.constraints.max_high_risk_ratio is None
        and bundle.constraints.high_risk_flags == frozenset()
    )


# --- 出荷値(承認済みの値そのもの) ---------------------------------------------------------


def test_shipped_values_are_the_approved_decision_b() -> None:
    bundle = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT)

    assert bundle.source == "FILE"
    assert bundle.policy_id == "standard-2026-10"
    assert bundle.policy.min_raer_to_allocate == Decimal("12")
    assert bundle.constraints.max_single_stock_ratio == Decimal("0.15")
    assert bundle.constraints.max_sector_ratio == Decimal("0.30")
    assert bundle.constraints.max_high_risk_ratio == Decimal("0.20")
    # 判断 B (a): 金額の上限なしは『大きな有限値』(None = 未決定ではない)
    amount = bundle.constraints.max_single_stock_amount
    assert amount is not None and amount.is_finite() and amount >= Decimal(10) ** 12
    # 判断 B: 『標準的』の high_risk_flags
    assert bundle.constraints.high_risk_flags == frozenset(
        {
            RiskFlag.MAJOR_CUSTOMER_DEPENDENCY.value,
            RiskFlag.TEMPORARY_EARNINGS_BOOST_RISK.value,
            RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY.value,
        }
    )
    # 段階の割合 33% / 67% / 100%
    assert dict(bundle.policy.tier_quantity_caps or {}) == {
        PriceTier.ENTRY: Decimal("0.33"),
        PriceTier.STANDARD: Decimal("0.67"),
        PriceTier.STRONG: Decimal("1.0"),
    }
    # 判断 B (b): 段階が不明のときは ENTRY の割合
    assert bundle.unknown_price_tier_as is PriceTier.ENTRY
    assert bundle.constraints.minimum_trading_unit == _UNIT


def test_shipped_comparison_key_is_the_raer_full_key_so_the_policy_is_complete() -> None:
    """#602 PR-G2: 比較の鍵は RAER 側の定数(唯一の算出元)と一致する。ずれれば CI が落ちる。"""
    bundle = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT)

    assert bundle.policy.comparison_key == RAER_FULL_COMPARISON_KEY
    assert bundle.missing() == ()
    assert bundle.policy.is_complete(bundle.constraints) is True


def test_a_null_comparison_key_still_means_nothing_is_allocated(tmp_path: Path) -> None:
    """鍵を null にすると『未決定 = 配分しない』へ戻る(fail-closed は維持)。"""
    bundle = _load(tmp_path, _with(comparison_key=None))

    assert bundle.policy.comparison_key is None
    assert bundle.missing() == ("comparison_key",)
    assert bundle.policy.is_complete(bundle.constraints) is False


def test_the_raer_full_key_is_the_four_implemented_axes_and_depends_on_axis_four() -> None:
    # 軸 4(DATA_QUALITY)は未実装。実装すると適用軸の集合が変わり、この鍵も変わる
    assert RAER_FULL_COMPARISON_KEY.endswith(
        "|BUSINESS_RISK,COMPANY_QUALITY,EARNINGS_TREND,FAIR_VALUE_CONFIDENCE"
    )
    assert "DATA_QUALITY" not in RAER_FULL_COMPARISON_KEY


def test_the_shipped_file_loads_without_falling_back(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=module.__name__)

    bundle = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT)

    assert bundle.source == "FILE"
    assert [r for r in caplog.records if r.name == module.__name__] == []


def test_with_a_comparison_key_the_shipped_policy_is_complete(tmp_path: Path) -> None:
    bundle = _load(tmp_path, _with(comparison_key="raer-model|axes"))

    assert bundle.source == "FILE"
    assert bundle.missing() == ()
    assert bundle.policy.is_complete(bundle.constraints) is True


def test_missing_items_use_the_vocabulary_of_the_policy_types() -> None:
    assert set(undecided_policy(minimum_trading_unit=_UNIT).missing()) <= set(POLICY_ITEMS)


# --- 数値の読み方 ----------------------------------------------------------------------------


def test_ratios_are_exact_decimals_not_float_artifacts(tmp_path: Path) -> None:
    bundle = _load(tmp_path, _with(max_single_stock_ratio=0.15, max_sector_ratio=0.3))

    assert bundle.constraints.max_single_stock_ratio == Decimal("0.15")
    assert str(bundle.constraints.max_single_stock_ratio) == "0.15"
    assert bundle.constraints.max_sector_ratio == Decimal("0.3")


def test_an_integer_amount_stays_an_exact_integer(tmp_path: Path) -> None:
    bundle = _load(tmp_path, _with(max_single_stock_amount=1000000000000000))

    assert bundle.constraints.max_single_stock_amount == Decimal(10) ** 15


def test_the_ratio_boundaries_are_zero_exclusive_and_one_inclusive(tmp_path: Path) -> None:
    assert _load(tmp_path, _with(max_sector_ratio=1)).source == "FILE"
    assert _load(tmp_path, _with(max_sector_ratio=0.0000001)).source == "FILE"
    assert _is_fallback(_load(tmp_path, _with(max_sector_ratio=0)))
    assert _is_fallback(_load(tmp_path, _with(max_sector_ratio=1.0000001)))


def test_min_raer_may_be_any_finite_number(tmp_path: Path) -> None:
    assert _load(tmp_path, _with(min_raer_to_allocate=0)).policy.min_raer_to_allocate == 0
    assert _load(tmp_path, _with(min_raer_to_allocate=-3.5)).source == "FILE"


# --- fail-closed(無い・壊れている・不正 → 全項目を未決定へ。例外を出さない) -------------


def test_a_missing_file_is_all_undecided(tmp_path: Path) -> None:
    assert _is_fallback(load_allocation_policy(tmp_path, minimum_trading_unit=_UNIT))


@pytest.mark.parametrize(
    "text",
    ["", "- a\n- b\n", "not: [valid", "just a string", "null\n", "version: 1\nunknown_key: 1\n"],
    ids=["empty", "list", "broken", "scalar", "null", "unknown_key"],
)
def test_a_broken_or_unrecognized_file_is_all_undecided(tmp_path: Path, text: str) -> None:
    assert _is_fallback(_load(tmp_path, text))


_INVALID: dict[str, list[Any]] = {
    "version": [2, 0, "1", True, 1.0, None],
    "policy_id": ["", "   ", 5, ["a"]],
    "comparison_key": ["", " ", 5, True],
    "min_raer_to_allocate": ["12", True, float("nan"), float("inf"), -float("inf"), [12]],
    "max_single_stock_amount": [0, -1, "100", True, float("nan"), float("inf")],
    "max_single_stock_ratio": [0, -0.1, 1.5, "0.15", True, float("nan"), float("inf")],
    "max_sector_ratio": [0, -0.1, 1.5, "0.3", True, float("nan"), float("inf")],
    "max_high_risk_ratio": [0, -0.1, 1.5, "0.2", True, float("nan"), float("inf")],
    "high_risk_flags": [
        "major_customer_dependency",
        ["not_a_risk_flag"],
        ["major_customer_dependency", "major_customer_dependency"],
        [1],
        {"major_customer_dependency": True},
        ["MAJOR_CUSTOMER_DEPENDENCY"],
    ],
    "tier_quantity_caps": [
        {"ENTRY": 0.33, "STANDARD": 0.67},
        {"ENTRY": 0.33, "STANDARD": 0.67, "STRONG": 1.0, "EXTRA": 1.0},
        {"ENTRY": 0, "STANDARD": 0.67, "STRONG": 1.0},
        {"ENTRY": 0.33, "STANDARD": 0.67, "STRONG": 1.5},
        {"ENTRY": "0.33", "STANDARD": 0.67, "STRONG": 1.0},
        {"entry": 0.33, "standard": 0.67, "strong": 1.0},
        {"ENTRY": float("nan"), "STANDARD": 0.67, "STRONG": 1.0},
        [0.33, 0.67, 1.0],
    ],
    "unknown_price_tier_as": ["entry", "UNKNOWN", 1, True, ["ENTRY"]],
}


@pytest.mark.parametrize(
    ("key", "value"),
    [(key, value) for key, values in _INVALID.items() for value in values],
    ids=lambda v: repr(v)[:30] if not isinstance(v, str) or len(v) < 30 else v[:30],
)
def test_one_invalid_value_makes_every_item_undecided(tmp_path: Path, key: str, value: Any) -> None:
    """部分的に効いた設定を作らない: 1 項目が不正なら、決定済みの他の項目も使わない。"""
    bundle = _load(tmp_path, _with(**{key: value}))

    assert _is_fallback(bundle)
    assert bundle.missing() != ()


def test_a_file_without_a_version_is_not_read_as_version_1(tmp_path: Path) -> None:
    """version は必須。無いファイルを v1 と読まない(未知の様式を暗黙に受け入れない)。"""
    data = _shipped_data()
    del data["version"]

    assert _is_fallback(_load(tmp_path, data))


def test_the_invalid_table_covers_every_key_of_the_shipped_file() -> None:
    assert set(_INVALID) == set(_shipped_data())


def test_a_fallback_never_raises_and_warns_with_the_type_name_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=module.__name__)
    bad_value = "12345.6789-marker"

    bundle = _load(tmp_path, _with(max_sector_ratio=bad_value))

    assert _is_fallback(bundle)
    records = [r for r in caplog.records if r.name == module.__name__]
    assert [r.levelno for r in records] == [logging.WARNING]
    message = records[0].getMessage()
    assert bad_value not in message
    assert str(tmp_path) not in message and CONFIG_FILE_NAME not in message
    assert "ValidationError" in message


def test_a_missing_file_warns_without_the_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=module.__name__)

    load_allocation_policy(tmp_path, minimum_trading_unit=_UNIT)

    message = " ".join(r.getMessage() for r in caplog.records if r.name == module.__name__)
    assert "FileNotFoundError" in message
    assert str(tmp_path) not in message


# --- 項目だけの null は、その項目だけが未決定(0・既定値で補完しない) ------------------------

_NULLABLE = (
    "policy_id",
    "comparison_key",
    "min_raer_to_allocate",
    "max_single_stock_amount",
    "max_single_stock_ratio",
    "max_sector_ratio",
    "max_high_risk_ratio",
    "high_risk_flags",
    "tier_quantity_caps",
    "unknown_price_tier_as",
)


@pytest.mark.parametrize("key", _NULLABLE)
def test_a_single_null_item_is_undecided_and_the_rest_are_kept(tmp_path: Path, key: str) -> None:
    shipped = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT)

    bundle = _load(tmp_path, _with(**{key: None}))

    assert bundle.source == "FILE"
    # 出荷の各項目は決定済み。どの項目を null にしても値が消えるので hash が変わる
    assert bundle.config_hash != shipped.config_hash
    expected_missing = {
        "min_raer_to_allocate": "min_raer_to_allocate",
        "max_single_stock_amount": "max_single_stock_amount",
        "max_single_stock_ratio": "max_single_stock_ratio",
        "max_sector_ratio": "max_sector_ratio",
        "max_high_risk_ratio": "max_high_risk_ratio",
        "tier_quantity_caps": "tier_quantity_caps",
        "high_risk_flags": "high_risk_flags",
    }
    missing = set(bundle.missing()) - {"comparison_key"}
    if key in expected_missing:
        # high_risk_flags が None のときは『上限を決めたのに何を high-risk と数えるか空』= 未決定
        assert missing == {expected_missing[key]}
    else:
        assert missing == set()
    # 0・既定値で補完していない
    still = {
        "min_raer_to_allocate": bundle.policy.min_raer_to_allocate,
        "max_single_stock_amount": bundle.constraints.max_single_stock_amount,
        "max_single_stock_ratio": bundle.constraints.max_single_stock_ratio,
        "max_sector_ratio": bundle.constraints.max_sector_ratio,
        "max_high_risk_ratio": bundle.constraints.max_high_risk_ratio,
        "tier_quantity_caps": bundle.policy.tier_quantity_caps,
    }
    if key in still:
        assert still[key] is None
    # 他の決定済みの値は保たれる
    for other, value in still.items():
        if other != key:
            assert value is not None


def test_an_absent_key_is_undecided_like_null(tmp_path: Path) -> None:
    data = _shipped_data()
    del data["max_sector_ratio"]

    bundle = _load(tmp_path, data)

    assert bundle.source == "FILE"
    assert bundle.constraints.max_sector_ratio is None
    assert "max_sector_ratio" in bundle.missing()


# --- config_hash(どの閾値で計算したかを後から検証できる) ---------------------------------


def test_the_hash_is_a_sha256_hex_digest() -> None:
    digest = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT).config_hash

    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_the_same_values_give_the_same_hash_regardless_of_key_order_and_number_spelling(
    tmp_path: Path,
) -> None:
    baseline = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT).config_hash
    data = _shipped_data()
    reordered = dict(reversed(list(data.items())))
    reordered["max_single_stock_ratio"] = 0.150
    reordered["max_sector_ratio"] = 3e-1
    reordered["max_single_stock_amount"] = 1e15
    reordered["tier_quantity_caps"] = {"STRONG": 1, "ENTRY": 0.330, "STANDARD": 0.67}
    reordered["high_risk_flags"] = sorted(data["high_risk_flags"], reverse=True)

    assert _load(tmp_path, reordered).config_hash == baseline


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("policy_id", "other-name"),
        ("comparison_key", "k"),
        ("min_raer_to_allocate", 13),
        ("max_single_stock_amount", 999999999999999),
        ("max_single_stock_ratio", 0.16),
        ("max_sector_ratio", 0.31),
        ("max_high_risk_ratio", 0.21),
        ("high_risk_flags", ["major_customer_dependency"]),
        ("tier_quantity_caps", {"ENTRY": 0.34, "STANDARD": 0.67, "STRONG": 1.0}),
        ("tier_quantity_caps", {"ENTRY": 0.33, "STANDARD": 0.68, "STRONG": 1.0}),
        ("tier_quantity_caps", {"ENTRY": 0.33, "STANDARD": 0.67, "STRONG": 0.9}),
        ("unknown_price_tier_as", "STANDARD"),
    ],
    ids=lambda v: str(v)[:24],
)
def test_changing_any_value_changes_the_hash(tmp_path: Path, key: str, value: Any) -> None:
    baseline = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT).config_hash

    assert _load(tmp_path, _with(**{key: value})).config_hash != baseline


def test_the_config_version_is_part_of_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """将来 v2 を足したとき、旧版と新版の記録が同じ hash にならない(version は hash の入力)。"""
    bundle = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT)

    def digest() -> str:
        return module._hash(
            policy_id=bundle.policy_id,
            constraints=bundle.constraints,
            policy=bundle.policy,
            unknown_price_tier_as=bundle.unknown_price_tier_as,
        )

    assert digest() == bundle.config_hash
    monkeypatch.setattr(module, "SUPPORTED_VERSION", 2)

    assert digest() != bundle.config_hash


def test_the_trading_unit_is_part_of_the_hash_and_the_constraints() -> None:
    a = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=100)
    b = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=1)

    assert a.config_hash != b.config_hash
    assert (a.constraints.minimum_trading_unit, b.constraints.minimum_trading_unit) == (100, 1)


def test_the_fallback_hash_differs_from_the_shipped_hash() -> None:
    shipped = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT)

    assert undecided_policy(minimum_trading_unit=_UNIT).config_hash != shipped.config_hash


def test_the_same_input_gives_the_same_output() -> None:
    first = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT)
    second = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT)

    assert first == second


# --- 呼び出し側の誤りは握り潰さない -----------------------------------------------------------


@pytest.mark.parametrize("unit", [0, -1, True, "100", 100.0, None])
def test_a_bad_trading_unit_is_the_callers_error_and_is_not_swallowed(unit: Any) -> None:
    with pytest.raises(AllocationContractError):
        load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=unit)
    with pytest.raises(AllocationContractError):
        undecided_policy(minimum_trading_unit=unit)


def test_the_hash_does_not_depend_on_the_process_hash_seed() -> None:
    """frozenset の並びは PYTHONHASHSEED で変わる。hash は並びに依らず、別のプロセスでも同じ値。"""
    code = (
        "from pathlib import Path;"
        "from jstock_advisor.domain.signals.allocation_policy_config import load_allocation_policy;"
        f"print(load_allocation_policy(Path({str(_CONFIG_DIR)!r}), minimum_trading_unit={_UNIT})"
        ".config_hash)"
    )
    digests = set()
    for seed in ("1", "2", "3", "12345"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(_PACKAGE.parent)},
        )
        digests.add(result.stdout.strip())
    assert len(digests) == 1
    assert digests == {load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT).config_hash}


# --- 不変性 ----------------------------------------------------------------------------------


def test_the_parsed_config_model_is_frozen() -> None:
    model = AllocationPolicyConfig.model_validate(_shipped_data())

    with pytest.raises(ValidationError):
        model.max_sector_ratio = Decimal("0.99")


def test_the_bundle_and_its_caps_are_read_only() -> None:
    bundle = load_allocation_policy(_CONFIG_DIR, minimum_trading_unit=_UNIT)

    with pytest.raises(AttributeError):
        bundle.config_hash = "x"  # type: ignore[misc]
    caps = bundle.policy.tier_quantity_caps
    assert caps is not None
    with pytest.raises(TypeError):
        caps[PriceTier.ENTRY] = Decimal(1)  # type: ignore[index]


# --- 構造(dormant・純粋・有効化の入口でない) ---------------------------------------------


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


_MODULE_PATH = _PACKAGE / "domain" / "signals" / "allocation_policy_config.py"


def _imported_modules(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_nothing_in_src_imports_the_policy_loader() -> None:
    """dormant: 本 module はどこからも import されない(Production へ接続しない)。"""
    importers = sorted(
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if path != _MODULE_PATH
        and any("allocation_policy_config" in name for name in _imported_modules(_tree(path)))
    )
    assert importers == []


def test_the_loader_imports_only_pure_dependencies() -> None:
    imported = _imported_modules(_tree(_MODULE_PATH))

    forbidden = ("boto3", "botocore", "datetime", "time", "os", "requests", "yfinance")
    assert [m for m in imported if m.split(".")[0] in forbidden] == []
    assert [
        m
        for m in imported
        if m.startswith("jstock_advisor")
        and not m.startswith(
            (
                "jstock_advisor.config.loader",
                "jstock_advisor.domain.signals.portfolio_allocation_types",
                "jstock_advisor.domain.valuation.raer",
            )
        )
    ] == []


def test_only_the_yaml_reader_is_taken_from_the_app_config_loader() -> None:
    """S-13(AppConfig)へは触れない: config.loader から取るのは設定の置き場と YAML 読みだけ。"""
    tree = _tree(_MODULE_PATH)
    taken = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "jstock_advisor.config.loader"
        for alias in node.names
    }
    assert taken == {"DEFAULT_CONFIG_DIR", "_load_yaml"}
    assert "jstock_advisor.config.models" not in _imported_modules(tree)


def test_the_policy_file_is_not_an_activation_switch() -> None:
    """出荷 YAML は mode / enabled の類のキーを持たない(閾値だけ)。有効化は shadow 側の別承認。"""
    keys = {key.lower() for key in _all_keys(_shipped_data())}

    assert not keys & {"on", "off"}
    assert not [
        key for key in keys if any(word in key for word in ("mode", "enable", "active", "shadow"))
    ]
    assert not [key for key in keys if any(word in key for word in ("dry_run", "switch", "toggle"))]
    imported = _imported_modules(_tree(_MODULE_PATH))
    assert not [m for m in imported if "allocation_shadow" in m]
    assert "AllocationShadowMode" not in _MODULE_PATH.read_text(encoding="utf-8")


def test_the_loader_is_free_of_clocks_and_io_other_than_reading_the_yaml() -> None:
    tree = _tree(_MODULE_PATH)
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    assert not called & {"now", "today", "utcnow", "open", "read_text", "getenv", "environ"}
