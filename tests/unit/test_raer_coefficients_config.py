"""Issue #602 PR-E: RAER の係数の専用 loader と出荷 YAML の契約テスト。

固定するもの(設計 #602 issuecomment-6137314302 / サブちゃんの設計レビュー …6137645260)
  (1) 出荷値 = 承認済みの初期値(USER #122 …6114461065【1】A-4)。値を 1 つ変えると golden が落ちる
  (2) fail-closed: 無い・壊れている・未知のキー・型の不正・範囲外・モデル版の不一致は、
      全体が縮退する
      (部分的に効いた係数を返さない・既定値で補完しない・例外を送出しない・警告に値を出さない)
  (3) 識別性: 係数が変われば coefficient_set_id が変わる(識別子は B2 の property。loader は作らない)
  (4) 構造: 『有効化の入口』でない・どこからも import されない・S-13 に触れない・
      時計も I/O も持たない
"""

from __future__ import annotations

import ast
import copy
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.valuation import raer_coefficients_config as cfg_module
from jstock_advisor.domain.valuation.raer import COMBINED_MODEL_VERSION, AxisCode, RiskFlag
from jstock_advisor.domain.valuation.raer_coefficients_config import (
    CONFIG_FILE_NAME,
    RaerCoefficientsBundle,
    load_raer_coefficients,
)

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "jstock_advisor"
_CONFIG_DIR = _ROOT / "config"
_MODULE_PATH = _SRC / "domain" / "valuation" / "raer_coefficients_config.py"

D = Decimal

#: 承認済みの初期値から作った識別子の literal(golden)。値を 1 つ変えるとこのテストが落ちる =
#: 係数の変更は『意図した変更』として、USER の確認・モデル版の更新と一緒に行うことを強制する(A-4)
_APPROVED_COEFFICIENT_SET_ID = "a5ae8a5a937b4e8e-c1"


def _shipped() -> dict[str, Any]:
    return yaml.safe_load((_CONFIG_DIR / CONFIG_FILE_NAME).read_text(encoding="utf-8"))


def _write(tmp_path: Path, content: dict[str, Any] | str) -> Path:
    text = content if isinstance(content, str) else yaml.safe_dump(content, allow_unicode=True)
    (tmp_path / CONFIG_FILE_NAME).write_text(text, encoding="utf-8")
    return tmp_path


def _load(tmp_path: Path, content: dict[str, Any] | str) -> RaerCoefficientsBundle:
    return load_raer_coefficients(_write(tmp_path, content))


def _mutated(path: list[str], value: Any) -> dict[str, Any]:
    content = copy.deepcopy(_shipped())
    node = content
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return content


# =================================================================================================
# (1) 出荷値 = 承認済みの初期値
# =================================================================================================


def test_the_shipped_file_loads_without_falling_back(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=cfg_module.__name__)
    bundle = load_raer_coefficients(_CONFIG_DIR)
    assert bundle.source == "FILE"
    assert bundle.coefficients is not None
    assert bundle.model_version == COMBINED_MODEL_VERSION
    assert [r for r in caplog.records if r.name == cfg_module.__name__] == []


def test_the_shipped_coefficient_set_id_is_the_approved_golden() -> None:
    bundle = load_raer_coefficients(_CONFIG_DIR)
    assert bundle.coefficient_set_id == _APPROVED_COEFFICIENT_SET_ID
    assert bundle.coefficients is not None
    assert bundle.coefficients.coefficient_set_id == _APPROVED_COEFFICIENT_SET_ID


def test_the_shipped_values_are_the_approved_initial_values() -> None:
    coefficients = load_raer_coefficients(_CONFIG_DIR).coefficients
    assert coefficients is not None
    level = ConfidenceLevel
    assert dict(coefficients.fv_confidence_factor) == {
        level.HIGH: D("1.00"),
        level.MEDIUM: D("0.85"),
        level.LOW: D("0.60"),
    }
    assert coefficients.quality_deduction_max == D(4)
    assert coefficients.quality_max_score == D(50)
    assert dict(coefficients.risk_deductions) == {
        RiskFlag.EARNINGS_WITHIN_3_BUSINESS_DAYS: D("3.0"),
        RiskFlag.EARNINGS_WITHIN_7_BUSINESS_DAYS: D("1.5"),
        RiskFlag.CYCLICAL_INDUSTRY: D("1.5"),
        RiskFlag.MAJOR_CUSTOMER_DEPENDENCY: D("1.5"),
        RiskFlag.TEMPORARY_EARNINGS_BOOST_RISK: D("2.0"),
        RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY: D("2.0"),
    }
    assert coefficients.risk_deduction_cap == D(6)
    assert coefficients.trend_deduction_scale == D(3)
    assert coefficients.min_applied_axes == 3
    assert coefficients.required_axes == frozenset({AxisCode.FAIR_VALUE_CONFIDENCE})


def test_the_shipped_model_version_equals_the_code_constant() -> None:
    assert _shipped()["model_version"] == COMBINED_MODEL_VERSION


def test_the_shipped_file_has_exactly_the_expected_top_level_keys() -> None:
    assert set(_shipped()) == {
        "version",
        "model_version",
        "fv_confidence_factor",
        "quality",
        "risk_deductions",
        "risk_deduction_cap",
        "trend_deduction_scale",
        "min_applied_axes",
        "required_axes",
    }


def test_the_decimal_values_come_from_the_repr_of_the_float_not_its_binary_expansion() -> None:
    coefficients = load_raer_coefficients(_CONFIG_DIR).coefficients
    assert coefficients is not None
    assert coefficients.fv_confidence_factor[ConfidenceLevel.MEDIUM] == D("0.85")
    assert str(coefficients.risk_deductions[RiskFlag.CYCLICAL_INDUSTRY]) == "1.5"


# =================================================================================================
# (2) fail-closed(全体が縮退する)
# =================================================================================================


def _assert_fallback(bundle: RaerCoefficientsBundle) -> None:
    assert bundle.coefficients is None
    assert bundle.coefficient_set_id is None
    assert bundle.model_version is None
    assert bundle.source == "FALLBACK"


def test_a_missing_file_falls_back(tmp_path: Path) -> None:
    _assert_fallback(load_raer_coefficients(tmp_path))


@pytest.mark.parametrize(
    "text",
    ["", "   \n", "not: [valid", "- a\n- b\n", "just a string", "null"],
    ids=["empty", "blank", "broken", "list", "scalar", "null"],
)
def test_an_unreadable_or_non_mapping_file_falls_back(tmp_path: Path, text: str) -> None:
    _assert_fallback(_load(tmp_path, text))


def test_an_unknown_top_level_key_falls_back(tmp_path: Path) -> None:
    content = _mutated(["mode"], "SHADOW")
    _assert_fallback(_load(tmp_path, content))


def test_an_unknown_nested_key_falls_back(tmp_path: Path) -> None:
    _assert_fallback(_load(tmp_path, _mutated(["quality", "extra"], 1)))


@pytest.mark.parametrize(
    "key",
    [
        "version",
        "model_version",
        "fv_confidence_factor",
        "quality",
        "risk_deductions",
        "risk_deduction_cap",
        "trend_deduction_scale",
        "min_applied_axes",
        "required_axes",
    ],
)
def test_a_missing_top_level_key_falls_back_and_is_never_defaulted(
    tmp_path: Path, key: str
) -> None:
    content = copy.deepcopy(_shipped())
    del content[key]
    _assert_fallback(_load(tmp_path, content))


@pytest.mark.parametrize("key", ["deduction_max", "max_score"])
def test_a_missing_quality_key_falls_back(tmp_path: Path, key: str) -> None:
    content = copy.deepcopy(_shipped())
    del content["quality"][key]
    _assert_fallback(_load(tmp_path, content))


_BAD_NUMBERS = ["x", "1", True, None, [1], {"a": 1}]
_NUMERIC_PATHS = [
    ["fv_confidence_factor", "HIGH"],
    ["fv_confidence_factor", "MEDIUM"],
    ["fv_confidence_factor", "LOW"],
    ["quality", "deduction_max"],
    ["quality", "max_score"],
    ["risk_deductions", "earnings_within_3_business_days"],
    ["risk_deductions", "cyclical_industry"],
    ["risk_deduction_cap"],
    ["trend_deduction_scale"],
]


@pytest.mark.parametrize("path", _NUMERIC_PATHS, ids=lambda p: ".".join(p))
@pytest.mark.parametrize("bad", _BAD_NUMBERS, ids=repr)
def test_a_non_numeric_value_falls_back_for_every_numeric_key(
    tmp_path: Path, path: list[str], bad: Any
) -> None:
    _assert_fallback(_load(tmp_path, _mutated(path, bad)))


@pytest.mark.parametrize("path", _NUMERIC_PATHS, ids=lambda p: ".".join(p))
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=repr)
def test_a_non_finite_value_falls_back_for_every_numeric_key(
    tmp_path: Path, path: list[str], bad: float
) -> None:
    _assert_fallback(_load(tmp_path, _mutated(path, bad)))


@pytest.mark.parametrize(
    "path",
    [
        ["quality", "deduction_max"],
        ["risk_deductions", "small_cap_or_low_liquidity"],
        ["risk_deduction_cap"],
        ["trend_deduction_scale"],
    ],
    ids=lambda p: ".".join(p),
)
def test_a_negative_amount_falls_back(tmp_path: Path, path: list[str]) -> None:
    _assert_fallback(_load(tmp_path, _mutated(path, -0.5)))


def test_a_non_positive_or_inexact_max_score_falls_back(tmp_path: Path) -> None:
    for bad in (0, -50, 3, 7):  # 0 以下、または Q / 満点が有限小数で割り切れない値
        _assert_fallback(_load(tmp_path, _mutated(["quality", "max_score"], bad)))


@pytest.mark.parametrize(
    "factors",
    [
        {"HIGH": 0.8, "MEDIUM": 0.85, "LOW": 0.6},  # HIGH < MEDIUM
        {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.6},  # MEDIUM < LOW
        {"HIGH": 1.1, "MEDIUM": 0.85, "LOW": 0.6},  # 1 を超える
        {"HIGH": 1.0, "MEDIUM": 0.85, "LOW": 0.0},  # 0(割引で上値余地が消える)
        {"HIGH": 1.0, "MEDIUM": 0.85},  # LOW が無い
        {"HIGH": 1.0, "MEDIUM": 0.85, "LOW": 0.6, "EXTREME": 0.5},  # 未知の信頼度
    ],
    ids=["high-lt-medium", "medium-lt-low", "over-one", "zero", "missing-low", "unknown"],
)
def test_an_invalid_confidence_factor_table_falls_back(
    tmp_path: Path, factors: dict[str, float]
) -> None:
    _assert_fallback(_load(tmp_path, _mutated(["fv_confidence_factor"], factors)))


def test_a_missing_or_unknown_risk_word_falls_back_and_is_never_filled_with_zero(
    tmp_path: Path,
) -> None:
    for word in [flag.value for flag in RiskFlag]:
        content = copy.deepcopy(_shipped())
        del content["risk_deductions"][word]
        _assert_fallback(_load(tmp_path, content))
    content = copy.deepcopy(_shipped())
    content["risk_deductions"]["brand_new_risk"] = 1.0
    _assert_fallback(_load(tmp_path, content))


@pytest.mark.parametrize("bad", [0, 6, -1, True, "3", 3.5, None], ids=repr)
def test_an_invalid_min_applied_axes_falls_back(tmp_path: Path, bad: Any) -> None:
    _assert_fallback(_load(tmp_path, _mutated(["min_applied_axes"], bad)))


@pytest.mark.parametrize("bad", [["NO_SUCH_AXIS"], "FAIR_VALUE_CONFIDENCE", [1], None])
def test_an_invalid_required_axes_falls_back(tmp_path: Path, bad: Any) -> None:
    _assert_fallback(_load(tmp_path, _mutated(["required_axes"], bad)))


@pytest.mark.parametrize("bad", [2, 0, "1", True, None])
def test_an_unsupported_file_version_falls_back(tmp_path: Path, bad: Any) -> None:
    _assert_fallback(_load(tmp_path, _mutated(["version"], bad)))


@pytest.mark.parametrize(
    "bad", ["raer-combined-v2", "", "raer-combined-v1-provisional-axes-y ", None, 1]
)
def test_a_model_version_mismatch_falls_back(tmp_path: Path, bad: Any) -> None:
    # 係数だけが黙って変わらないよう、モデル版が一致しない YAML は読まない(A-4)
    _assert_fallback(_load(tmp_path, _mutated(["model_version"], bad)))


def test_one_invalid_item_makes_the_whole_configuration_fall_back(tmp_path: Path) -> None:
    # 他の項目がすべて正しくても、1 項目の不正で全体が縮退する(部分的に効いた係数を返さない)
    bundle = _load(tmp_path, _mutated(["risk_deductions", "cyclical_industry"], "x"))
    _assert_fallback(bundle)
    assert bundle.coefficients is None  # 『cyclical だけ 0』のような補完された係数は返らない


def test_the_loader_never_raises_for_any_of_the_invalid_inputs(tmp_path: Path) -> None:
    for text in ["", "{", "a: b", "\x00", "version: 1\n" * 3, "!!python/object:os.system {}"]:
        _assert_fallback(_load(tmp_path, text))


def test_the_warning_names_only_the_exception_type_never_values_or_paths(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=cfg_module.__name__)
    secret = "super-secret-value-123"
    _assert_fallback(_load(tmp_path, _mutated(["risk_deductions", "cyclical_industry"], secret)))
    messages = " ".join(r.getMessage() for r in caplog.records if r.name == cfg_module.__name__)
    assert messages  # 縮退は警告される
    assert secret not in messages
    assert str(tmp_path) not in messages
    assert "falling back" in messages


# =================================================================================================
# (3) 識別性(識別子は B2 の property。loader は作らない)
# =================================================================================================


def test_changing_any_one_coefficient_changes_the_set_id(tmp_path: Path) -> None:
    baseline = load_raer_coefficients(_CONFIG_DIR).coefficient_set_id
    changes = [
        (["fv_confidence_factor", "MEDIUM"], 0.84),
        (["quality", "deduction_max"], 5),
        (["risk_deductions", "small_cap_or_low_liquidity"], 2.1),
        (["risk_deduction_cap"], 7),
        (["trend_deduction_scale"], 4),
        (["min_applied_axes"], 4),
    ]
    for path, value in changes:
        bundle = _load(tmp_path, _mutated(path, value))
        assert bundle.coefficients is not None, path
        assert bundle.coefficient_set_id != baseline, path


def test_changing_the_required_axes_changes_the_set_id(tmp_path: Path) -> None:
    baseline = load_raer_coefficients(_CONFIG_DIR).coefficient_set_id
    bundle = _load(
        tmp_path, _mutated(["required_axes"], ["FAIR_VALUE_CONFIDENCE", "COMPANY_QUALITY"])
    )
    assert bundle.coefficients is not None
    assert bundle.coefficients.required_axes == frozenset(
        {AxisCode.FAIR_VALUE_CONFIDENCE, AxisCode.COMPANY_QUALITY}
    )
    assert bundle.coefficient_set_id != baseline


def test_the_same_values_give_the_same_id_regardless_of_notation_and_key_order(
    tmp_path: Path,
) -> None:
    content = copy.deepcopy(_shipped())
    reordered = {key: content[key] for key in reversed(list(content))}
    reordered["risk_deductions"] = dict(reversed(list(content["risk_deductions"].items())))
    reordered["fv_confidence_factor"]["MEDIUM"] = 0.850  # 0.85 と同じ値の別表記
    reordered["trend_deduction_scale"] = 3.0  # 3 と同じ値の別表記
    bundle = _load(tmp_path, reordered)
    assert bundle.coefficient_set_id == _APPROVED_COEFFICIENT_SET_ID


def test_the_loader_returns_the_identifier_computed_by_the_composition_module() -> None:
    # 識別子の算出は 1 か所(B2)。loader は独自の hash を作らない
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "hashlib" not in {n.name for n in ast.walk(tree) if isinstance(n, ast.alias)}
    assert "sha256" not in imported
    text = _MODULE_PATH.read_text(encoding="utf-8")
    assert "hashlib" not in text
    assert "sha256" not in text


# =================================================================================================
# (4) 構造: 入口でない・dormant・S-13 に触れない
# =================================================================================================

_ENTRANCE_KEYS = {"mode", "enabled", "active", "on", "off", "enable", "switch", "flag"}


def _all_keys(node: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(str(key).lower())
            keys |= _all_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _all_keys(item)
    return keys


def test_the_shipped_file_has_no_activation_entrance_key() -> None:
    assert _all_keys(_shipped()) & _ENTRANCE_KEYS == set()


def test_the_module_does_not_reference_any_mode_of_the_other_shadow_switches() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    modules = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    for forbidden in ("AllocationShadowMode", "RuntimeConfigMode"):
        assert forbidden not in used | imported, forbidden
    assert not [m for m in modules if "allocation_shadow_config" in m]


def test_the_module_only_uses_the_two_allowed_names_from_the_config_loader() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    from_loader = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "jstock_advisor.config.loader"
    ]
    assert len(from_loader) == 1
    assert {alias.name for alias in from_loader[0].names} == {"DEFAULT_CONFIG_DIR", "_load_yaml"}
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "jstock_advisor.config.models" not in modules  # S-13(AppConfig)に触れない


def test_the_module_has_no_service_infrastructure_clock_or_network_dependency() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
    forbidden = ("services", "infrastructure", "lambda_handlers", "boto3", "requests", "datetime")
    assert not [n for n in names if any(f in n for f in forbidden)], names
    assert "time" not in names


def test_nothing_in_src_imports_the_coefficients_loader_yet() -> None:
    importers = [
        path.relative_to(_SRC).as_posix()
        for path in sorted(_SRC.rglob("*.py"))
        if path != _MODULE_PATH and "raer_coefficients_config" in path.read_text(encoding="utf-8")
    ]
    assert importers == []  # dormant。配線は #603 PR-F 系(別の承認の後)


def test_the_logger_level_is_declared() -> None:
    assert cfg_module.logger.level == logging.INFO


def test_the_module_docstring_says_it_is_neither_an_entrance_nor_undecided() -> None:
    doc = ast.get_docstring(ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))) or ""
    assert "有効化の入口" in doc
    assert "未決" in doc
    assert "dormant" in doc
