"""Issue #263: ``_trimmed_mean`` の実態の文書化・検知・記録(★ 算出式・anchor は変えない)。

背景:
    ``compute_valuation_anchor()`` は、ばらつき中 / 信頼度 MEDIUM のとき
    ``min(weighted_median, trimmed_mean)`` を採る。``_trimmed_mean`` は
    ``trim_count = int(n * 0.1)``
    のため、方式数 n が 10 未満では 1 件も trim せず **単純平均と同一**になる(本番の方式数は最大 6。
    Issue 本文の実測 = 集約が走った 4,101 件すべてで trim_count = 0)。
    方式が増えて n が 10 に達すると、
    設定変更もなしに anchor の算出規則が黙って変わる。

方式 O-3(MANAGER 承認 = #263 issuecomment-5583602418 / 具体化 = issuecomment-5956880947)。
本ファイルが固定するもの(いずれも挙動を変えない):

    G-3  n <= 9 では単純平均と一致 / n = 10 で trim が効く(Issue 本文の再現値)。既存の #260 のテスト
         (``test_issue_260_anchor_monotone_clamp.py`` の ``test_trimmed_mean_*``)は
         「一致する / 一致しない」
         だけを見ていた。本ファイルは、Issue 本文の具体値(240.8 / 103.5 / 172.9)と、境界の算術
         (trim が効く最小の n)を固定する
    G-1a 方式の種類の数が、trim が効き始める n 未満であること
    (★ 設計は「単一の定義から導く」としたが、
         方式名を 1 つにまとめた定義は無い
         〔METHOD_PRINCIPLES は標準 5 方式のみで industry を含まない〕。
         src の ``FairValueMethodResult(... method="...")`` の構築を AST で全件走査して導く。
         数は直書きしない。
         限界: 1 つの ``methods_used`` に同じ方式名が複数入る構築は、
         この数の上限を超えうる〔現状はそうならない〕)
    G-1b n が trim の効く最小値以上のとき、WARNING が 1 行出る(件数のみ)。平常時(n < 10)は出ない。
         anchor の値は WARNING の有無で変わらない
    T5   compute_valuation_anchor の出力が、固定の入力集合
    (distribution 2 種 × n = 2〜6 × 信頼度 2 × band 4)で、
         ★ 変更前(origin/main 962d5e5)の出力と完全に一致する(挙動不変の evidence。LOCK_LEVEL_1 の
         EXISTING_CONSUMER_BEHAVIOR_UNCHANGED)

fixture は架空値のみ。
"""

from __future__ import annotations

import ast
import inspect
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.valuation import valuation_methods
from jstock_advisor.domain.valuation.valuation_methods import (
    _min_values_to_trim,
    _trimmed_mean,
    compute_valuation_anchor,
    determine_dispersion_band,
)

_LOGGER_NAME = valuation_methods.__name__
_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_DISPERSION = load_config().buy_decision.valuation_dispersion
_NAMES = ("target_yield", "per", "pbr", "industry", "historical_range", "dcf")


def _dec(values: list[str]) -> list[Decimal]:
    return [Decimal(v) for v in values]


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / len(values)


def _range(values: list[str], names: tuple[str, ...] | None = None) -> FairValueRange:
    names = names or _NAMES
    return FairValueRange(
        bear=None,
        neutral=None,
        bull=None,
        overall_confidence=ConfidenceLevel.MEDIUM,
        methods_used=[
            FairValueMethodResult(
                method=names[i],
                fair_value=Decimal(v),
                confidence=ConfidenceLevel.MEDIUM,
                applicable=True,
            )
            for i, v in enumerate(values)
        ],
        methods_excluded=[],
        usable_for_trading_judgment=True,
    )


# --- 境界の算術 ----------------------------------------------------------------------------


def test_default_trim_fraction_is_unchanged() -> None:
    """★ 既定の trim 割合は 0.1 のまま(定数化しても値を変えない)。"""
    assert inspect.signature(_trimmed_mean).parameters["trim_fraction"].default == 0.1
    assert _min_values_to_trim() == 10


# --- docstring の注記 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "fragment",
    [
        "本番の方式数",
        "trimせず単純平均と同一",
        "Issue #263",
        "Issue #263を参照",
    ],
)
def test_trimmed_mean_docstring_keeps_the_issue_263_note(fragment: str) -> None:
    """★ ``_trimmed_mean`` の docstring の注記を固定する。

    固定するのは、本番の方式数では trim されず単純平均と同一であることと、Issue #263 への参照
    (PR #804 の SHOULD-1。注記を消しても CI が落ちなかった)。

    文書の文言そのものの固定であり、算出式・戻り値は見ない(挙動は他のテストが固定する)。
    要点を 3 つの断片に分けるのは、どの要点が失われたかを失敗の表示で分かるようにするため。
    """
    doc = inspect.getdoc(_trimmed_mean)
    assert doc is not None
    assert fragment in doc


@pytest.mark.parametrize("fraction", [0.05, 0.1, 0.2, 0.25])
def test_min_values_to_trim_is_exactly_where_trim_count_becomes_one(fraction: float) -> None:
    """★ _min_values_to_trim() は、int(n * 割合) >= 1 になる最小の n と一致する(境界の算術)。"""
    expected_threshold = next(n for n in range(1, 1000) if int(n * fraction) >= 1)
    assert _min_values_to_trim(fraction) == expected_threshold
    for n in range(1, 200):
        assert (int(n * fraction) >= 1) == (n >= _min_values_to_trim(fraction))


# --- G-3: 実態の固定(Issue 本文の再現値)-----------------------------------------------------


def test_issue_reproduction_n5_trims_nothing_and_equals_the_simple_mean(
    caplog: pytest.LogCaptureFixture,
) -> None:
    values = _dec(["1", "100", "101", "102", "900"])
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = _trimmed_mean(values)

    assert result == Decimal("240.8")
    assert result == _mean(values)
    assert caplog.records == []


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 9])
def test_below_ten_values_equal_the_simple_mean_and_do_not_warn(
    n: int, caplog: pytest.LogCaptureFixture
) -> None:
    values = [Decimal("1"), *(Decimal(str(100 + i)) for i in range(n - 2)), Decimal("900")]
    assert len(values) == n
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = _trimmed_mean(values)

    assert result == _mean(values)
    assert caplog.records == []


def test_issue_reproduction_n10_trims_and_differs_from_the_simple_mean(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ n = 10 で初めて trim が効く(本文の再現値: 103.5 と、単純平均 172.9)。"""
    values = _dec(["1", "100", "101", "102", "103", "104", "105", "106", "107", "900"])
    assert len(values) == _min_values_to_trim()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = _trimmed_mean(values)

    assert result == Decimal("103.5")
    assert _mean(values) == Decimal("172.9")
    assert result != _mean(values)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "n=10" in warnings[0].getMessage()
    assert "trim_count=1" in warnings[0].getMessage()


# --- G-1a: 方式の種類の数が、trim の効き始める n 未満 -----------------------------------------


def _method_names_constructed_in_src() -> set[str]:
    """src の ``FairValueMethodResult(... method="...")`` の構築から方式名を全件集める。"""
    names: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "FairValueMethodResult"
            ):
                for kw in node.keywords:
                    if (
                        kw.arg == "method"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                    ):
                        names.add(kw.value.value)
    return names


def test_number_of_valuation_methods_stays_below_the_trim_threshold() -> None:
    """★ 方式の種類の数 < trim が効き始める n。方式を足して上限に近づくと、このテストが赤くなる。

    赤くなったら「anchor の算出規則が黙って変わる」ことへの気づきの合図である(#263)。
    対処は MANAGER / USER の判断(trimmed_mean の扱いの決定)で、このテストを緩めて通さない。
    """
    names = _method_names_constructed_in_src()

    assert names, "方式名が 1 つも見つからない(走査の前提が崩れている)"
    assert len(names) < _min_values_to_trim(), (
        f"方式の種類が {len(names)} 件になり、"
        f"trim が効き始める n = {_min_values_to_trim()} に近づいた。"
        "valuation の anchor の算出規則が黙って変わりうる(#263)"
    )


# --- G-1b: 実行時の検知(WARNING)と、anchor が不変であること ----------------------------------

_TEN_NAMES = tuple(f"method_{i}" for i in range(10))
_TEN_VALUES = ["1", "100", "101", "102", "103", "104", "105", "106", "107", "900"]


@pytest.mark.parametrize("band", ["MEDIUM", "HIGH"])
def test_anchor_warns_once_when_trimming_happens_and_value_is_unchanged(
    band: str, caplog: pytest.LogCaptureFixture
) -> None:
    fair_value_range = _range(_TEN_VALUES, _TEN_NAMES)
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        with_warning = compute_valuation_anchor(
            fair_value_range,
            ConfidenceLevel.MEDIUM,
            band,  # type: ignore[arg-type]
        )
    warnings = [
        r for r in caplog.records if r.name == _LOGGER_NAME and r.levelno == logging.WARNING
    ]

    # ばらつき大・中のいずれも、trimmed_mean を 1 回だけ計算する。
    assert len(warnings) == 1
    # WARNING を出さない(ログを無効にした)場合と、anchor は同じ。
    logging.disable(logging.CRITICAL)
    try:
        without_warning = compute_valuation_anchor(
            fair_value_range,
            ConfidenceLevel.MEDIUM,
            band,  # type: ignore[arg-type]
        )
    finally:
        logging.disable(logging.NOTSET)
    assert with_warning == without_warning


def test_anchor_does_not_warn_for_realistic_method_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ 平常時(本番の方式数 = 最大 6)は WARNING が 0 件。"""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        for n in range(2, 7):
            compute_valuation_anchor(_range(_TEN_VALUES[:n]), ConfidenceLevel.MEDIUM, "HIGH")
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []


def test_warning_carries_only_counts_not_identifiers(caplog: pytest.LogCaptureFixture) -> None:
    """★ WARNING に銘柄コード・方式名・値を出さない(件数だけ)。"""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _trimmed_mean(_dec(_TEN_VALUES))

    message = "\n".join(r.getMessage() for r in caplog.records)
    assert message
    assert "900" not in message and "method_" not in message


# --- T5: 挙動不変(変更前の出力との完全一致)------------------------------------------------------

_SETS: dict[str, dict[int, list[str]]] = {
    "left": {
        2: ["800", "1500"],
        3: ["100", "1150", "1200"],
        4: ["100", "1150", "1200", "1210"],
        5: ["100", "1150", "1200", "1210", "1220"],
        6: ["100", "500", "1150", "1200", "1210", "1220"],
    },
    "right": {
        2: ["800", "1500"],
        3: ["600", "1150", "1900"],
        4: ["600", "1150", "1200", "2400"],
        5: ["600", "1150", "1200", "1210", "2600"],
        6: ["500", "900", "1150", "1200", "1210", "2600"],
    },
}
_RATIOS: dict[str, float | None] = {"LOW": 1.20, "MEDIUM": 1.59, "HIGH": 1.61, "NONE": None}

# ★ origin/main 962d5e5(変更前)で、同じ入力に対して実測した anchor。
# 算出式を変えると、このテストが赤くなる。
_GOLDEN: dict[tuple[str, int, str, str], str] = {
    ("left", 2, "MEDIUM", "LOW"): "800",
    ("left", 2, "MEDIUM", "MEDIUM"): "800",
    ("left", 2, "MEDIUM", "HIGH"): "800",
    ("left", 2, "MEDIUM", "NONE"): "800",
    ("left", 2, "HIGH", "LOW"): "800",
    ("left", 2, "HIGH", "MEDIUM"): "800",
    ("left", 2, "HIGH", "HIGH"): "800",
    ("left", 2, "HIGH", "NONE"): "800",
    ("left", 3, "MEDIUM", "LOW"): "816.6666666666666666666666667",
    ("left", 3, "MEDIUM", "MEDIUM"): "816.6666666666666666666666667",
    ("left", 3, "MEDIUM", "HIGH"): "816.6666666666666666666666667",
    ("left", 3, "MEDIUM", "NONE"): "816.6666666666666666666666667",
    ("left", 3, "HIGH", "LOW"): "1150",
    ("left", 3, "HIGH", "MEDIUM"): "816.6666666666666666666666667",
    ("left", 3, "HIGH", "HIGH"): "816.6666666666666666666666667",
    ("left", 3, "HIGH", "NONE"): "1150",
    ("left", 4, "MEDIUM", "LOW"): "915",
    ("left", 4, "MEDIUM", "MEDIUM"): "915",
    ("left", 4, "MEDIUM", "HIGH"): "915",
    ("left", 4, "MEDIUM", "NONE"): "915",
    ("left", 4, "HIGH", "LOW"): "1150",
    ("left", 4, "HIGH", "MEDIUM"): "915",
    ("left", 4, "HIGH", "HIGH"): "915",
    ("left", 4, "HIGH", "NONE"): "1150",
    ("left", 5, "MEDIUM", "LOW"): "976",
    ("left", 5, "MEDIUM", "MEDIUM"): "976",
    ("left", 5, "MEDIUM", "HIGH"): "976",
    ("left", 5, "MEDIUM", "NONE"): "976",
    ("left", 5, "HIGH", "LOW"): "1200",
    ("left", 5, "HIGH", "MEDIUM"): "976",
    ("left", 5, "HIGH", "HIGH"): "976",
    ("left", 5, "HIGH", "NONE"): "1200",
    ("left", 6, "MEDIUM", "LOW"): "896.6666666666666666666666667",
    ("left", 6, "MEDIUM", "MEDIUM"): "896.6666666666666666666666667",
    ("left", 6, "MEDIUM", "HIGH"): "896.6666666666666666666666667",
    ("left", 6, "MEDIUM", "NONE"): "896.6666666666666666666666667",
    ("left", 6, "HIGH", "LOW"): "1150",
    ("left", 6, "HIGH", "MEDIUM"): "896.6666666666666666666666667",
    ("left", 6, "HIGH", "HIGH"): "896.6666666666666666666666667",
    ("left", 6, "HIGH", "NONE"): "1150",
    ("right", 2, "MEDIUM", "LOW"): "800",
    ("right", 2, "MEDIUM", "MEDIUM"): "800",
    ("right", 2, "MEDIUM", "HIGH"): "800",
    ("right", 2, "MEDIUM", "NONE"): "800",
    ("right", 2, "HIGH", "LOW"): "800",
    ("right", 2, "HIGH", "MEDIUM"): "800",
    ("right", 2, "HIGH", "HIGH"): "800",
    ("right", 2, "HIGH", "NONE"): "800",
    ("right", 3, "MEDIUM", "LOW"): "1150",
    ("right", 3, "MEDIUM", "MEDIUM"): "1150",
    ("right", 3, "MEDIUM", "HIGH"): "1040.0",
    ("right", 3, "MEDIUM", "NONE"): "1150",
    ("right", 3, "HIGH", "LOW"): "1150",
    ("right", 3, "HIGH", "MEDIUM"): "1150",
    ("right", 3, "HIGH", "HIGH"): "1040.0",
    ("right", 3, "HIGH", "NONE"): "1150",
    ("right", 4, "MEDIUM", "LOW"): "1150",
    ("right", 4, "MEDIUM", "MEDIUM"): "1150",
    ("right", 4, "MEDIUM", "HIGH"): "1150",
    ("right", 4, "MEDIUM", "NONE"): "1150",
    ("right", 4, "HIGH", "LOW"): "1150",
    ("right", 4, "HIGH", "MEDIUM"): "1150",
    ("right", 4, "HIGH", "HIGH"): "1150",
    ("right", 4, "HIGH", "NONE"): "1150",
    ("right", 5, "MEDIUM", "LOW"): "1200",
    ("right", 5, "MEDIUM", "MEDIUM"): "1200",
    ("right", 5, "MEDIUM", "HIGH"): "1180.0000000000000050",
    ("right", 5, "MEDIUM", "NONE"): "1200",
    ("right", 5, "HIGH", "LOW"): "1200",
    ("right", 5, "HIGH", "MEDIUM"): "1200",
    ("right", 5, "HIGH", "HIGH"): "1180.0000000000000050",
    ("right", 5, "HIGH", "NONE"): "1200",
    ("right", 6, "MEDIUM", "LOW"): "1150",
    ("right", 6, "MEDIUM", "MEDIUM"): "1150",
    ("right", 6, "MEDIUM", "HIGH"): "1150",
    ("right", 6, "MEDIUM", "NONE"): "1150",
    ("right", 6, "HIGH", "LOW"): "1150",
    ("right", 6, "HIGH", "MEDIUM"): "1150",
    ("right", 6, "HIGH", "HIGH"): "1150",
    ("right", 6, "HIGH", "NONE"): "1150",
}


@pytest.mark.parametrize(("key", "expected"), sorted(_GOLDEN.items()))
def test_anchor_output_is_identical_to_the_pre_change_output(
    key: tuple[str, int, str, str], expected: str
) -> None:
    family, n, confidence, band_label = key
    ratio = _RATIOS[band_label]
    band = determine_dispersion_band(ratio, _DISPERSION) if ratio is not None else None

    anchor = compute_valuation_anchor(
        _range(_SETS[family][n]), ConfidenceLevel(confidence), band
    ).anchor

    assert anchor is not None
    assert str(anchor) == expected


def test_golden_table_covers_every_input_combination() -> None:
    """★ 表の抜け(入力の組合せの追加漏れ)を防ぐ。"""
    expected_keys = {
        (family, n, confidence, band)
        for family, sets in _SETS.items()
        for n in sets
        for confidence in ("MEDIUM", "HIGH")
        for band in _RATIOS
    }
    assert set(_GOLDEN) == expected_keys
