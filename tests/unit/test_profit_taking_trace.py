"""利確判定(evaluate_profit_taking)の挙動不変の固定(characterization)と、trace の契約
(Issue #878 PR-3a)。

1. characterization: 変更前の実装で生成した期待値
   (tests/fixtures/profit_taking_characterization.json。合成入力 約 7900 件。結果の全 field の
   ハッシュ + 最終 action・origin・売却強度)と、現行の
   evaluate_profit_taking の結果が全件一致する。**この期待値は再生成しない**(trace の追加は判定を
   変えない変更であり、期待値が変わるなら判定が変わったということ)。判定ルール・設定を意図して
   変える変更の場合のみ、その変更の中で再生成し、差分を説明する。
2. 欠測の入力: 適正価格が使えない・bull 等の欠落・momentum なし・含み損・候補なし、の全組み合わせ
   で、例外を出さず、同じ入力で 2 回呼んでも同じ結果(副作用なし)。
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

from jstock_advisor.domain.signals.profit_taking import evaluate_profit_taking
from tests.support import profit_taking_cases as pc

_GOLDEN_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "profit_taking_characterization.json"
)


def _golden() -> dict[str, str]:
    payload = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    cases: dict[str, str] = payload["cases"]
    return cases


def _all_cases() -> list[pc.Case]:
    return [*pc.sampled_cases(), *pc.focused_cases(), *pc.missing_input_cases()]


def test_the_characterization_covers_every_case_of_the_grid() -> None:
    golden = _golden()
    assert set(golden) == {case.case_id for case in _all_cases()}
    assert len(golden) > 7000


def test_the_grid_reaches_every_action_and_origin() -> None:
    """格子が退化していない(全 action・全 origin に届く)ことの確認。"""
    golden = _golden()
    parsed = [value.split("|") for value in golden.values() if not value.startswith("EXC:")]
    actions = collections.Counter(parts[1] for parts in parsed)
    origins = collections.Counter(parts[2] for parts in parsed)
    assert set(actions) == {
        "HOLD",
        "WATCH",
        "PARTIAL_PROFIT_TAKE",
        "FULL_PROFIT_TAKE",
    }
    assert set(origins) == {
        "NONE",
        "OTHER_CONDITIONS",
        "PRICE_POSITION",
        "PROFIT_PROTECTION_STRONG",
        "FAIR_VALUE_STRONG",
        "FUNDAMENTAL_CRITICAL_RISK",
    }
    intensities = {parts[3] for parts in parsed}
    assert {"LIGHT", "STANDARD", "STRONG", "VERY_STRONG"} <= intensities


def test_evaluate_profit_taking_is_unchanged_for_every_case_of_the_grid() -> None:
    golden = _golden()
    mismatches: list[str] = []
    for case in _all_cases():
        actual = pc.compact(pc.evaluate(evaluate_profit_taking, case.kwargs))
        if actual != golden[case.case_id]:
            mismatches.append(f"{case.case_id}: {golden[case.case_id]} -> {actual}")
    assert mismatches == [], f"{len(mismatches)} 件の結果が変わった: {mismatches[:5]}"


def test_missing_inputs_never_raise_and_have_no_side_effects() -> None:
    cases = list(pc.missing_input_cases())
    assert len(cases) > 1500
    for case in cases:
        first = pc.evaluate(evaluate_profit_taking, case.kwargs)
        assert not isinstance(first, str), f"{case.case_id}: {first}"
        second = pc.evaluate(evaluate_profit_taking, case.kwargs)
        assert pc.digest(first) == pc.digest(second), case.case_id
