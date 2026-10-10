"""保有判断の通知の記録(builder)の既存の出力の characterization(Issue #890 PR-2)。

PR-2 は、本番で動く既存のファイル(評価・builder・handler)に手を入れる。実装の前に、
**既存の出力を固定**し、PR-2 が足す項目(`hd_renotify_state`)以外は 1 つも変わらないことを
証明する土台にする。期待値は、PR-2 の変更を入れる前の main(b9fb02e7)の出力から生成した
golden(`tests/fixtures/hd_builder_characterization.json`)である。

固定するもの
  ・builder が作る Recommendation 全体(`config_values_used["hd_renotify_state"]` を除く)
  ・`config_values_used` の既存キーの集合(キーの追加・削除が無いこと)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import (
    HoldingDecisionCategory,
)
from jstock_advisor.domain.entities.holding_decision import (
    HoldingDecisionHardGate,
    HoldingDecisionResult,
)
from jstock_advisor.services.holding_decision_notification_builder import (
    build_holding_decision_recommendation,
)
from tests.unit.test_issue_67_recommendation_provenance_transfer import (
    _CONFIG,
    _NOT_EVALUATED_EXIT_PRICE_RANGE,
    _base_snapshot,
    _holding,
    _holding_decision_result,
    _register_fictional_stock,  # noqa: F401 - autouse fixture
)

_GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "hd_builder_characterization.json"
_NEW_KEY = "hd_renotify_state"


def characterization_cases() -> dict[str, HoldingDecisionResult]:
    base = _holding_decision_result()
    return {
        "sell_consideration": base,
        "strong_sell_consideration": base.model_copy(
            update={"category": HoldingDecisionCategory.STRONG_SELL_CONSIDERATION}
        ),
        "hard_gate_urgent": base.model_copy(
            update={
                "hard_gate": HoldingDecisionHardGate(
                    triggered=True,
                    reason_codes=("BANKRUPTCY_FILING",),
                    score_cap=-30.0,
                    adjustment_applied=True,
                ),
                "final_score": -30.0,
            }
        ),
    }


def build_case(name: str) -> dict[str, Any]:
    result = characterization_cases()[name]
    recommendation = build_holding_decision_recommendation(
        _holding(),
        result,
        _base_snapshot(),
        "rule-v1",
        _CONFIG,
        _NOT_EVALUATED_EXIT_PRICE_RANGE,
        recommendation_id=f"characterization-{name}",
    )
    dumped: dict[str, Any] = json.loads(recommendation.model_dump_json())
    dumped["config_values_used"].pop(_NEW_KEY, None)
    return dumped


@pytest.mark.parametrize("name", list(characterization_cases()))
def test_builder_output_is_unchanged_except_for_the_new_key(name: str) -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert build_case(name) == golden[name]


@pytest.mark.parametrize("name", list(characterization_cases()))
def test_existing_config_values_used_keys_are_unchanged(name: str) -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert set(build_case(name)["config_values_used"]) == set(golden[name]["config_values_used"])


def test_builder_output_is_deterministic_for_the_same_input() -> None:
    assert build_case("sell_consideration") == build_case("sell_consideration")
