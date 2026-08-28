"""valuation shadow分析(Issue #20 Phase C)のテスト。

保存済み判定時点値のみからのraw shadow observation生成、H_Aによるsaved anchor
再構成self-check、RECONSTRUCTION_MISMATCHの可視化とsummaryからの除外、
UNAVAILABLE伝播、決定性、実サービス生成Recommendationとの照合を固定する。
既知値(9416相当の1341.775 / cluster縮約1208.75等)は説明可能性確認用の
fixture期待値であり、valuationの正解値としては扱わない。
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.analysis.valuation_shadow_analysis import (
    ReconstructionStatus,
    build_shadow_rows,
    write_shadow_export,
)
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.valuation import FairValueMethodResult

_NOW = dt.datetime(2026, 8, 24, 23, 1, tzinfo=dt.UTC)
_GENERATED_AT = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.UTC)


def _make_recommendation(**overrides: object) -> Recommendation:
    base: dict[str, object] = {
        "recommendation_id": "shadow-rec-1",
        "stock_code": "9416",
        "stock_name": "テスト銘柄",
        "recommended_at": _NOW,
        "recommendation_type": RecommendationType.BUY,
        "price_at_recommendation": Decimal("1200"),
        "confidence": ConfidenceLevel.MEDIUM,
        "rule_version": "v1-test",
    }
    base.update(overrides)
    return Recommendation(**base)  # type: ignore[arg-type]


def _method(method: str, fair_value: Decimal | None) -> FairValueMethodResult:
    return FairValueMethodResult(
        method=method, fair_value=fair_value, confidence=ConfidenceLevel.MEDIUM
    )


_V9416 = {
    "target_yield": Decimal("1450"),
    "per": Decimal("1499.6"),
    "pbr": Decimal("1468.5"),
    "historical_range": Decimal("949"),
    "dcf": Decimal("478.9"),
}


def _buy_recommendation(**overrides: object) -> Recommendation:
    """9416相当のgeneric BUY fixture(DCFがgeneric外れ値除外され、decision
    範囲949〜1499.6・saved anchor=trimmed_mean 1341.775・保存marginあり)。"""
    base: dict[str, object] = {
        "valuation_methods": tuple(
            _method(name, value) for name, value in sorted(_V9416.items())
        ),
        "buy_score_input_facts": {
            "valuation_outlier_exclusions": [
                {
                    "method": "dcf",
                    "code": "EXTREME_LOW_RELATIVE_TO_MEDIAN",
                    "actual_value": "478.9",
                    "reference_value": "587.4",
                }
            ]
        },
        "decision_valuation_min": Decimal("949"),
        "decision_valuation_max": Decimal("1499.6"),
        "valuation_anchor": Decimal("1341.775"),
        "required_margin_of_safety_entry": Decimal("0.20"),
        "required_margin_of_safety_standard": Decimal("0.15"),
        "required_margin_of_safety_strong": Decimal("0.10"),
    }
    base.update(overrides)
    return _make_recommendation(**base)


def _rows_by(rows: list[dict[str, object]], context: str, hypothesis_id: str) -> dict[str, object]:
    matches = [
        r for r in rows if r["context"] == context and r.get("hypothesis_id") == hypothesis_id
    ]
    assert len(matches) == 1
    return matches[0]


# --- BUY: H_A再構成self-check --------------------------------------------


def test_buy_decision_h_a_reconstructs_saved_anchor() -> None:
    rows = build_shadow_rows(_buy_recommendation())
    row = _rows_by(rows, "BUY_DECISION", "H_A_INDEPENDENT_METHODS")

    assert row["saved_valuation_anchor"] == "1341.775"
    assert row["shadow_reconstructed_anchor"] == "1341.775"
    assert row["reconstruction_delta"] == "0"
    assert row["reconstruction_status"] == ReconstructionStatus.MATCHED_MIN_WM_TRIMMED.value
    anchors = row["anchors"]
    assert isinstance(anchors, dict)
    assert anchors["weighted_median"] == "1450"
    assert anchors["min_wm_tm"] == "1341.775"
    # 除外詳細(historical factからの写し)が保持される
    excluded = row["excluded_methods"]
    assert isinstance(excluded, list) and excluded[0]["method"] == "dcf"


def test_buy_decision_reconstruction_mismatch_is_visible() -> None:
    """saved anchorがどの現行式とも一致しない場合、帳尻を合わせず
    RECONSTRUCTION_MISMATCHとして可視化する。"""
    rec = _buy_recommendation(valuation_anchor=Decimal("9999"))
    row = _rows_by(build_shadow_rows(rec), "BUY_DECISION", "H_A_INDEPENDENT_METHODS")
    assert row["reconstruction_status"] == ReconstructionStatus.RECONSTRUCTION_MISMATCH.value
    assert row["shadow_reconstructed_anchor"] is None
    assert row["reconstruction_delta"] is not None


def test_buy_cluster_hypothesis_reduces_population() -> None:
    """C1A(収益力3方式を1群): decision母集団は{historical 949, 群1468.5}へ
    縮約され、median/meanは1208.75(8/27概算1211と整合する説明可能性確認)。"""
    rows = build_shadow_rows(_buy_recommendation())
    row = _rows_by(rows, "BUY_DECISION", "H_C1A_EARNINGS_TRIO_3GROUP")

    assert row["population"] == [
        ["historical_range", "949"],
        ["pbr+per+target_yield", "1468.5"],
    ]
    anchors = row["anchors"]
    assert isinstance(anchors, dict)
    assert anchors["median"] == "1208.75"
    assert "reconstruction_status" not in row  # self-checkはH_A×BUY_DECISION限定


def test_buy_shadow_prices_use_saved_margins_only() -> None:
    """shadow価格は代表式anchor×(1−保存済みmargin)。SHADOW_PRICEであり
    約定・到達判定の列は存在しない。"""
    rows = build_shadow_rows(_buy_recommendation())
    row = _rows_by(rows, "BUY_DECISION", "H_A_INDEPENDENT_METHODS")

    assert row["representative_anchor_formula"] == "min_wm_tm"
    prices = row["shadow_prices"]
    assert isinstance(prices, dict)
    assert Decimal(prices["shadow_entry_price"]) == Decimal("1073.42")
    assert Decimal(prices["shadow_standard_price"]) == Decimal("1341.775") * Decimal("0.85")
    assert Decimal(prices["shadow_strong_price"]) == Decimal("1341.775") * Decimal("0.90")
    serialized = json.dumps(row, ensure_ascii=False)
    assert "reached" not in serialized
    assert "executed" not in serialized


def test_buy_shadow_prices_none_when_margin_not_saved() -> None:
    rec = _buy_recommendation(
        required_margin_of_safety_entry=None,
        required_margin_of_safety_standard=None,
        required_margin_of_safety_strong=None,
    )
    row = _rows_by(build_shadow_rows(rec), "BUY_DECISION", "H_A_INDEPENDENT_METHODS")
    prices = row["shadow_prices"]
    assert isinstance(prices, dict)
    assert prices["shadow_entry_price"] is None  # 現在configでの補完はしない


def test_exploratory_hypothesis_rows_are_labeled() -> None:
    rows = build_shadow_rows(_buy_recommendation())
    row = _rows_by(rows, "BUY_DECISION", "H_X1_CORRELATION_CLUSTER_2026_08")
    assert row["hypothesis_origin"] == "EXPLORATORY_DATA_DERIVED"


def test_effective_counts_are_observation_only_columns() -> None:
    rows = build_shadow_rows(_buy_recommendation())
    row = _rows_by(rows, "BUY_DECISION", "H_A_INDEPENDENT_METHODS")
    assert row["effective_count_methods"] == 4
    # decision母集団{per,pbr,target_yield,historical_range}のタグ集合は
    # {EARNINGS,MMH}/{BOOK_VALUE,MMH}/{DIVIDEND}/{MARKET_PRICE_HISTORY}の4種
    assert row["effective_count_distinct_tag_sets"] == 4
    # per/pbrはMARKET_MULTIPLE_HISTORYを共有(各1/2)、他2方式は独立(各1/1)
    assert row["effective_count_tag_jaccard"] == pytest.approx(3.0)


# --- SELL ----------------------------------------------------------------


def _sell_methods(values: dict[str, Decimal]) -> list[dict[str, object]]:
    return [
        {"method": name, "fair_value": str(value), "confidence": "MEDIUM"}
        for name, value in sorted(values.items())
    ]


def test_sell_raw_shadow_usability_and_no_flip() -> None:
    rec = _make_recommendation(
        recommendation_type=RecommendationType.WATCH,
        fair_value_methods=_sell_methods(_V9416),
        fair_value_bear=Decimal("478.9"),
        fair_value_bull=Decimal("1499.6"),
        fair_value_spread_ratio=float(Decimal("1499.6") / Decimal("478.9")),
        fair_value_usable_for_trading_judgment=False,
        fair_value_unusable_reason_code="METHOD_SPREAD_TOO_WIDE",
    )
    row = _rows_by(build_shadow_rows(rec), "SELL_RAW", "H_A_INDEPENDENT_METHODS")

    assert row["shadow_bear"] == "478.9"
    assert row["shadow_bull"] == "1499.6"
    assert row["shadow_usable_for_trading_judgment"] is False
    assert row["usability_flip"] is False
    assert row["saved_unusable_reason_code"] == "METHOD_SPREAD_TOO_WIDE"
    assert row["sell_usability_shadow_params"] == {
        "max_method_spread_ratio": 2.0,
        "min_methods_required": 2,
    }


def test_sell_cluster_hypothesis_can_flip_usability() -> None:
    """spread 2.1(unusable)の3方式が、PER/PBRペア化(H_D)でspread<2.0となり
    shadow上usableへflipするケースを固定する(観測のみ。本番判定は不変)。"""
    rec = _make_recommendation(
        recommendation_type=RecommendationType.WATCH,
        fair_value_methods=_sell_methods(
            {"per": Decimal("1000"), "pbr": Decimal("1300"), "target_yield": Decimal("2100")}
        ),
        fair_value_bear=Decimal("1000"),
        fair_value_bull=Decimal("2100"),
        fair_value_spread_ratio=2.1,
        fair_value_usable_for_trading_judgment=False,
        fair_value_unusable_reason_code="METHOD_SPREAD_TOO_WIDE",
    )
    rows = build_shadow_rows(rec)
    baseline = _rows_by(rows, "SELL_RAW", "H_A_INDEPENDENT_METHODS")
    paired = _rows_by(rows, "SELL_RAW", "H_D_PER_PBR_PAIR")

    assert baseline["shadow_usable_for_trading_judgment"] is False
    assert baseline["usability_flip"] is False
    assert paired["population"] == [["pbr+per", "1150"], ["target_yield", "2100"]]
    assert paired["shadow_usable_for_trading_judgment"] is True
    assert paired["usability_flip"] is True


def test_sell_saved_usable_none_means_flip_not_comparable() -> None:
    """#21導入前の旧レコード(saved usable=None)はflip比較不能(None)。"""
    rec = _make_recommendation(
        recommendation_type=RecommendationType.WATCH,
        fair_value_methods=_sell_methods({"per": Decimal("1000"), "pbr": Decimal("1100")}),
        fair_value_bear=Decimal("1000"),
        fair_value_bull=Decimal("1100"),
    )
    row = _rows_by(build_shadow_rows(rec), "SELL_RAW", "H_A_INDEPENDENT_METHODS")
    assert row["usability_flip"] is None


# --- UNAVAILABLE伝播 ------------------------------------------------------


def test_unavailable_contexts_expand_to_all_hypotheses() -> None:
    """canonical grain(1 Rec×1 context×1 仮説)はUNAVAILABLEでも維持され、
    3context×全仮説数の行が生成される(仮説別denominatorを後段で計算可能)。"""
    from jstock_advisor.analysis.valuation_shadow_hypotheses import ALL_HYPOTHESES

    rows = build_shadow_rows(_make_recommendation())
    assert len(rows) == 3 * len(ALL_HYPOTHESES)
    hypothesis_ids = {h.hypothesis_id for h in ALL_HYPOTHESES}
    for context in ("BUY_RAW", "BUY_DECISION", "SELL_RAW"):
        context_rows = [r for r in rows if r["context"] == context]
        assert {r["hypothesis_id"] for r in context_rows} == hypothesis_ids
    for row in rows:
        assert row["observation_status"] == "OBSERVATION_UNAVAILABLE"
        assert row["hypothesis_id"] is not None
        assert row["hypothesis_origin"] in ("PREDEFINED", "EXPLORATORY_DATA_DERIVED")
        assert row["unavailable_reason"]
        # 仮説計算値は推測せずNone(shadow値・価格・bear/bullを生成しない)
        assert row["population"] is None
        assert row["anchors"] is None
        assert "shadow_prices" not in row
        assert "shadow_bear" not in row


def test_available_zero_method_population_is_valid_with_empty_values() -> None:
    """AVAILABLE+有効方式0件(NO_VALID_METHODS相当)は正当な状態であり、
    母集団空・anchors全Noneの行として出力される(UNAVAILABLEと混同しない)。"""
    rec = _make_recommendation(
        recommendation_type=RecommendationType.WATCH,
        fair_value_methods=[
            {"method": "per", "fair_value": None, "confidence": "LOW"},
            {"method": "pbr", "fair_value": None, "confidence": "LOW"},
        ],
        fair_value_bear=None,
        fair_value_bull=None,
        fair_value_usable_for_trading_judgment=False,
        fair_value_unusable_reason_code="NO_VALID_METHODS",
    )
    row = _rows_by(build_shadow_rows(rec), "SELL_RAW", "H_A_INDEPENDENT_METHODS")
    assert row["observation_status"] == "AVAILABLE"
    assert row["population"] == []
    assert row["population_count"] == 0
    anchors = row["anchors"]
    assert isinstance(anchors, dict)
    assert all(v is None for v in anchors.values())
    assert row["shadow_usable_for_trading_judgment"] is False


# --- export ---------------------------------------------------------------


def test_export_is_deterministic_and_carries_versions(tmp_path: Path) -> None:
    recs = [
        _buy_recommendation(),
        _make_recommendation(recommendation_id="shadow-rec-0"),  # UNAVAILABLE
    ]
    out_a = tmp_path / "a.jsonl"
    out_b = tmp_path / "b.jsonl"
    result_a = write_shadow_export(recs, out_a, generated_at=_GENERATED_AT)
    result_b = write_shadow_export(list(reversed(recs)), out_b, generated_at=_GENERATED_AT)

    assert out_a.read_bytes() == out_b.read_bytes()  # 入力順に依らず決定的
    assert result_a == result_b
    lines = out_a.read_text(encoding="utf-8").splitlines()
    metadata = json.loads(lines[0])
    assert metadata["valuation_shadow_export_schema_version"] == "vs1"
    assert metadata["valuation_taxonomy_version"] == "vt1"
    assert metadata["valuation_hypothesis_set_version"] == "vh1"
    assert metadata["recommendation_count"] == 2
    assert metadata["row_count"] == len(lines) - 1
    assert any(
        h["hypothesis_id"] == "H_X1_CORRELATION_CLUSTER_2026_08"
        and h["origin"] == "EXPLORATORY_DATA_DERIVED"
        for h in metadata["hypotheses"]
    )
    assert any(
        h["hypothesis_id"] == "H_D_PER_PBR_PAIR" and "C1c" in h["aliases"]
        for h in metadata["hypotheses"]
    )
    # UNAVAILABLE contexts = BUY fixtureのSELL_RAW(1)+snapshotなしrecの3context = 4組。
    # shadow行はcanonical grain統一により4組×全仮説数へ展開される
    assert result_a.unavailable_context_count == 4
    assert result_a.unavailable_shadow_row_count == 4 * len(metadata["hypotheses"])


def test_summary_excludes_reconstruction_mismatch_from_delta_stats(tmp_path: Path) -> None:
    good = _buy_recommendation()
    bad = _buy_recommendation(
        recommendation_id="shadow-rec-2", valuation_anchor=Decimal("9999")
    )
    out = tmp_path / "rows.jsonl"
    summary = tmp_path / "summary.csv"
    result = write_shadow_export([good, bad], out, generated_at=_GENERATED_AT, summary_path=summary)

    assert result.reconstruction_mismatch_count == 1
    text = summary.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line]
    header = lines[0].split(",")
    assert header[2] == "sample_count"
    assert header[3] == "unavailable_row_count"
    assert header[4] == "reconstruction_excluded_count"
    # 件数semantics: shadow行数と(Recommendation, context)組数を分離して出力
    assert any(line.startswith("_ALL_,_UNAVAILABLE_SHADOW_ROWS_") for line in lines)
    assert any(line.startswith("_ALL_,_UNAVAILABLE_CONTEXTS_") for line in lines)
    decision_h_a = next(
        line for line in lines if line.startswith("BUY_DECISION,H_A_INDEPENDENT_METHODS")
    )
    cols = decision_h_a.split(",")
    assert cols[2] == "1"  # mismatchレコードはsampleから除外
    assert cols[3] == "0"  # BUY_DECISIONにUNAVAILABLE行なし
    assert cols[4] == "1"  # 除外件数として可視化
    # 自動結論(ランキング・優劣・閾値提案)を出力しない
    for forbidden in ("best", "rank", "GOOD", "FAIL", "threshold"):
        assert forbidden not in text


# --- integration: 実サービス生成Recommendationとの照合 --------------------


def test_integration_buy_service_h_a_reconstruction_matches() -> None:
    from jstock_advisor.config.loader import load_config
    from jstock_advisor.domain.business_calendar import BusinessCalendar
    from jstock_advisor.services.buy_signal_service import BuySignalService
    from jstock_advisor.services.provider_factory import build_mock_provider_bundle

    config = load_config()
    now = dt.datetime(2026, 8, 9, tzinfo=dt.UTC)
    service = BuySignalService(
        providers=build_mock_provider_bundle(now),
        config=config,
        business_calendar=BusinessCalendar.from_config(config.holiday_calendar),
    )
    outcome = service.analyze("2914", now, RecommendationType.BUY)
    assert outcome.recommendation is not None
    rec = outcome.recommendation

    row = _rows_by(build_shadow_rows(rec), "BUY_DECISION", "H_A_INDEPENDENT_METHODS")
    if rec.valuation_anchor is None:
        assert row["reconstruction_status"] == ReconstructionStatus.NOT_APPLICABLE.value
    else:
        assert row["reconstruction_status"] != ReconstructionStatus.RECONSTRUCTION_MISMATCH.value
        assert row["reconstruction_delta"] == "0"
