"""Issue #22 Phase B1: C4 V2 Shadow フィールドの読み取り互換性。

## 本フェーズの位置づけ

B1 で行うのは **読み取り互換性の確立のみ**である。

```
B1  V2 shadow フィールドを Optional / デフォルト値つきで追加する（本モジュール）
B2  判定時点入力の保存を追加する
B3  共有 Common Quality の shadow 算出
B4  Style Attractiveness の shadow 算出
```

`DecisionSnapshot` は `extra="forbid"` であるため、**新フィールドを書き始めた後に
それを知らない版へ戻すと読み込みが失敗する**。したがって read 側を先に本番へ入れ、
安全な rollback 先を確保してから write を開始する（`READ_COMPATIBILITY_FIRST`）。

## 本モジュールが固定する契約

```
旧レコード（V2 フィールドを一切持たない）      読める
新レコード（V2 フィールドを持つ）              読める
V2 フィールドが null                          読める
serialize -> deserialize の往復               値が保持される
B1 時点では算出・書き込みを行わない            既定値のまま
BUY 側と保有側の version namespace は別        混同しない
```

判定ロジック・スコア・通知・価格・valuation は本フェーズで一切変更していない。
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.decision_snapshot import DecisionSnapshot
from jstock_advisor.domain.entities.enums import DecisionType
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)

# V2 フィールドを一切持たない旧形式レコード。
# 実際に保存されている JSON と同じ経路（リポジトリ）で読めることを確認する。
_OLD_SHAPE_DECISION_SNAPSHOT = {
    "decision_id": "decision|old-rec-v1",
    "decision_type": "BUY",
    "stock_code": "2914",
    "evaluated_at": "2026-07-01T00:00:00Z",
    "evaluation_date_jst": "2026-07-01",
    "market_price": "4200",
    "rule_version": "v1-mvp",
    "model_version": "v1",
}

_V2_FIELD_NAMES = (
    "common_quality_score",
    "common_quality_state",
    "common_quality_coverage",
    "common_quality_reason_codes",
    "common_quality_metrics",
    "style_attractiveness_state",
    "style_attractiveness_reason_codes",
    "style_attractiveness_metrics",
    "matched_styles",
    "qualified_styles",
    "hypothetical_buy_action",
    "hypothetical_layer_reasons",
)


def _write_store(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    store_dir = tmp_path / "local_store"
    store_dir.mkdir()
    (store_dir / "decision_snapshots.json").write_text(
        json.dumps(records), encoding="utf-8"
    )
    return store_dir


def _minimal_snapshot(**overrides: object) -> DecisionSnapshot:
    base: dict[str, object] = {
        "decision_id": "decision|new-rec-v2",
        "decision_type": DecisionType.BUY,
        "stock_code": "2914",
        "evaluated_at": dt.datetime(2026, 9, 5, tzinfo=dt.UTC),
        "evaluation_date_jst": dt.date(2026, 9, 5),
        "market_price": Decimal("4200"),
        "rule_version": "v1-mvp",
        "model_version": "v1",
    }
    base.update(overrides)
    return DecisionSnapshot(**base)  # type: ignore[arg-type]


# --- A: 旧レコード(V2 フィールドなし)が読めること ---------------------------


def test_a_old_record_without_v2_fields_loads(tmp_path: Path) -> None:
    """V2 フィールドを持たない既存レコードがそのまま読めること。

    `extra="forbid"` のため、既定値だけで後方互換が成立することを
    実際の保存形式（JSON ファイル経由）で確認する。
    """
    store_dir = _write_store(tmp_path, [_OLD_SHAPE_DECISION_SNAPSHOT])

    snapshot = DecisionSnapshotRepository(store_dir=store_dir).get("decision|old-rec-v1")

    assert snapshot is not None
    assert snapshot.common_quality_score is None
    assert snapshot.common_quality_state is None
    assert snapshot.common_quality_coverage is None
    assert snapshot.common_quality_reason_codes == ()
    assert snapshot.common_quality_metrics == {}
    assert snapshot.style_attractiveness_state is None
    assert snapshot.style_attractiveness_reason_codes == ()
    assert snapshot.style_attractiveness_metrics == {}
    assert snapshot.matched_styles == ()
    assert snapshot.qualified_styles == ()
    assert snapshot.hypothetical_buy_action is None
    assert snapshot.hypothetical_layer_reasons == {}


def test_a_old_record_keeps_existing_version_default(tmp_path: Path) -> None:
    """既存の version 既定値（v1）を B1 が変えていないこと。"""
    store_dir = _write_store(tmp_path, [_OLD_SHAPE_DECISION_SNAPSHOT])

    snapshot = DecisionSnapshotRepository(store_dir=store_dir).get("decision|old-rec-v1")

    assert snapshot is not None
    assert snapshot.company_quality_score_model_version == "v1"


# --- B: V2 フィールドを持つ新レコードが読めること ------------------------------


def test_b_new_record_with_v2_fields_loads(tmp_path: Path) -> None:
    """B3 / B4 が将来書き込む形の値を、読み取り側が受理できること。"""
    new_shape = dict(_OLD_SHAPE_DECISION_SNAPSHOT)
    new_shape["decision_id"] = "decision|new-rec-v2"
    new_shape.update(
        {
            "common_quality_score": 41.5,
            "common_quality_state": "PASS",
            "common_quality_coverage": 0.92,
            "common_quality_reason_codes": ["CQ_EVALUATED"],
            "common_quality_metrics": {"axis": {"financial_health": 13.0}},
            "style_attractiveness_state": "EVALUATED",
            "style_attractiveness_reason_codes": ["SA_EVALUATED"],
            "style_attractiveness_metrics": {"GROWTH": {"score": 62.0, "state": "PASS"}},
            "matched_styles": ["GROWTH", "QUALITY"],
            "qualified_styles": ["GROWTH"],
            "hypothetical_buy_action": "WATCH_FOR_PRICE",
            "hypothetical_layer_reasons": {"layer_1": "PASS", "layer_3": "PRICE_NOT_REACHED"},
        }
    )
    store_dir = _write_store(tmp_path, [new_shape])

    snapshot = DecisionSnapshotRepository(store_dir=store_dir).get("decision|new-rec-v2")

    assert snapshot is not None
    assert snapshot.common_quality_score == 41.5
    assert snapshot.common_quality_state == "PASS"
    assert snapshot.common_quality_coverage == 0.92
    assert snapshot.common_quality_reason_codes == ("CQ_EVALUATED",)
    assert snapshot.matched_styles == ("GROWTH", "QUALITY")
    assert snapshot.qualified_styles == ("GROWTH",)
    assert snapshot.hypothetical_buy_action == "WATCH_FOR_PRICE"
    assert snapshot.hypothetical_layer_reasons["layer_3"] == "PRICE_NOT_REACHED"


def test_b_multi_style_is_preserved_without_primary_type(tmp_path: Path) -> None:
    """複数 style 該当時に全 style が保持され、代表値へ潰されないこと。

    要件 7（`primary_type` を使わない / 最大値を代表にしない）を schema 側で固定する。
    """
    new_shape = dict(_OLD_SHAPE_DECISION_SNAPSHOT)
    new_shape["decision_id"] = "decision|multi-style"
    new_shape["matched_styles"] = ["GROWTH", "QUALITY", "VALUE"]
    new_shape["qualified_styles"] = ["GROWTH", "VALUE"]
    store_dir = _write_store(tmp_path, [new_shape])

    snapshot = DecisionSnapshotRepository(store_dir=store_dir).get("decision|multi-style")

    assert snapshot is not None
    assert len(snapshot.matched_styles) == 3
    assert len(snapshot.qualified_styles) == 2
    assert not hasattr(snapshot, "primary_style")


# --- C: V2 フィールドが null でも読めること -----------------------------------


def test_c_null_v2_fields_load(tmp_path: Path) -> None:
    """値が明示的に null で保存された場合も読めること。

    「フィールドが無い」と「値が null」を別経路として確認する。
    """
    new_shape = dict(_OLD_SHAPE_DECISION_SNAPSHOT)
    new_shape["decision_id"] = "decision|null-v2"
    new_shape.update(
        {
            "common_quality_score": None,
            "common_quality_state": None,
            "common_quality_coverage": None,
            "style_attractiveness_state": None,
            "hypothetical_buy_action": None,
        }
    )
    store_dir = _write_store(tmp_path, [new_shape])

    snapshot = DecisionSnapshotRepository(store_dir=store_dir).get("decision|null-v2")

    assert snapshot is not None
    assert snapshot.common_quality_score is None
    assert snapshot.common_quality_state is None
    assert snapshot.hypothetical_buy_action is None


# --- D: serialize -> deserialize の往復 ---------------------------------------


def test_d_serialization_roundtrip_preserves_v2_fields() -> None:
    """保存経路（model_dump_json）と読み取り経路（model_validate）で値が保持されること。

    リポジトリはモデルを汎用的に JSON 化するため、往復で欠落しないことを固定する。
    """
    original = _minimal_snapshot(
        common_quality_score=38.25,
        common_quality_state="DATA_MISSING",
        common_quality_coverage=0.4,
        common_quality_reason_codes=("CQ_LOW_COVERAGE",),
        common_quality_metrics={"axis": {"governance": 5.0}},
        style_attractiveness_state="NOT_APPLICABLE",
        style_attractiveness_reason_codes=("SA_NO_MATCHED_STYLE",),
        style_attractiveness_metrics={},
        matched_styles=(),
        qualified_styles=(),
        hypothetical_buy_action="NOT_ATTRACTIVE",
        hypothetical_layer_reasons={"layer_1": "DATA_MISSING"},
    )

    restored = DecisionSnapshot.model_validate_json(original.model_dump_json())

    assert restored == original
    assert restored.common_quality_state == "DATA_MISSING"
    assert restored.style_attractiveness_state == "NOT_APPLICABLE"
    assert restored.hypothetical_layer_reasons == {"layer_1": "DATA_MISSING"}


def test_d_roundtrip_of_default_snapshot_keeps_defaults() -> None:
    """既定値のみのレコードでも往復で既定値が保たれること。"""
    original = _minimal_snapshot()

    restored = DecisionSnapshot.model_validate_json(original.model_dump_json())

    assert restored == original
    for name in _V2_FIELD_NAMES:
        assert getattr(restored, name) == getattr(original, name)


# --- E/F/G: 既存挙動を変えていないこと ----------------------------------------


def test_e_existing_fields_are_unchanged_by_b1() -> None:
    """B1 が既存フィールドの既定値・必須性を変えていないこと。"""
    snapshot = _minimal_snapshot()

    assert snapshot.rule_version == "v1-mvp"
    assert snapshot.model_version == "v1"
    assert snapshot.company_quality_score_model_version == "v1"
    assert snapshot.recommendation_id is None
    assert snapshot.config_values_used == {}
    assert snapshot.data_sources == ()


def test_f_b1_does_not_add_buy_decision_fields() -> None:
    """B1 が BUY 判定そのものを表すフィールドを増やしていないこと。

    hypothetical_buy_action は **仮定の記録**であり、現行の判定結果
    （existing_action）とは別フィールドである。両者を混同しないことを固定する。
    """
    snapshot = _minimal_snapshot(hypothetical_buy_action="STRONG_BUY")

    assert snapshot.existing_action is None
    assert snapshot.hypothetical_buy_action == "STRONG_BUY"


def test_g_unknown_field_is_still_rejected() -> None:
    """extra 契約を緩めていないこと。

    後方互換のために `extra="ignore"` へ変更する、という対応は取っていない。
    未知フィールドは従来どおり拒否される。
    """
    payload = json.loads(_minimal_snapshot().model_dump_json())
    payload["totally_unknown_field"] = 1

    try:
        DecisionSnapshot.model_validate(payload)
    except Exception:  # pydantic.ValidationError
        return
    raise AssertionError("未知フィールドが拒否されませんでした（extra 契約が緩んでいます）")


# --- H: version namespace の分離 ----------------------------------------------


def test_h_buy_and_holding_version_namespaces_are_separate() -> None:
    """買い側の品質モデル版が、保有側の判定ルール版と混同されないこと。

    DecisionSnapshot が持つのは買い側の版（文字列）だけであり、
    保有側の版（整数の判定ルール版）を保持しない。
    同じ "v2" という文字列を意味の違う版として共有しないための固定。
    """
    snapshot = _minimal_snapshot()

    assert isinstance(snapshot.company_quality_score_model_version, str)
    assert not hasattr(snapshot, "scoring_model_version")
    assert not hasattr(snapshot, "holding_scoring_model_version")


# --- I: B1 では算出経路へ接続していないこと ------------------------------------


def test_i_b1_does_not_compute_v2_values() -> None:
    """B1 の時点で V2 の値が自動的に埋まらないこと。

    read compatibility だけを提供するフェーズであるため、
    スナップショットを素直に構築しただけで V2 値が算出されてはならない。
    B3 / B4 で書き込み経路を接続した際に、この期待は更新される。
    """
    snapshot = _minimal_snapshot()

    assert snapshot.common_quality_score is None
    assert snapshot.common_quality_state is None
    assert snapshot.style_attractiveness_state is None
    assert snapshot.matched_styles == ()
    assert snapshot.qualified_styles == ()
    assert snapshot.hypothetical_buy_action is None


def test_i_snapshot_builder_does_not_populate_v2_fields() -> None:
    """判定 snapshot の生成経路が V2 値を埋めていないこと。

    B1 では builder を変更していないため、Recommendation から生成しても
    V2 フィールドは既定値のままである。
    """
    from jstock_advisor.domain.decision_snapshot_builder import build_decision_snapshot
    from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
    from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
    from jstock_advisor.domain.entities.recommendation import Recommendation

    recommendation = Recommendation(
        recommendation_id="rec-b1",
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=dt.datetime(2026, 9, 5, tzinfo=dt.UTC),
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            standard=PriceWithRationale(price=Decimal("3359"), rationale="x"),
        ),
        price_at_recommendation=Decimal("4200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )

    snapshot = build_decision_snapshot(recommendation, decision_type=DecisionType.BUY)

    for name in _V2_FIELD_NAMES:
        value = getattr(snapshot, name)
        assert value in (None, (), {}), f"{name} が B1 で既に埋められています: {value!r}"
