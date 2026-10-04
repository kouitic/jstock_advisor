"""Issue #580(#64 A-2): Recommendation の scope 種別(``scope_type``)の不変条件。

背景:
    ``shares_at_recommendation`` を持つ Recommendation は ``owner`` / ``holding_id`` を持つ
    (= holding scope)という暗黙の不変条件が、#329(集中度 Recommendation)の実装で崩れた。
    寄与する保有が 2 件以上のとき、合算株数を持ちながら ``owner`` / ``holding_id`` を None にする。
    本 Issue は、種別をレコード側が明示する(``RecommendationScope`` と ``scope_type``)。

固定するもの(MANAGER 判断 = #580 issuecomment-5978072579 / 訂正 = 同 Issue の訂正コメント):
    1. ``RecommendationScope`` の値(4 つ)と、``scope_type`` が Optional で、既定は None であること
    2. 生成箇所(src 全体の ``Recommendation(...)`` の構築)が、例外なく ``scope_type`` を明示すること
       (生成箇所の無登録の追加・``scope_type`` の脱落で、このテストが赤くなる)
    3. 5 つの生成箇所が、それぞれ正しい種別を設定し、その種別が旧レコードの legacy inference
       (フィールドの組合せからの復元)と一致すること(単一 / 複数保有寄与の両ケースを含む)
    4. legacy inference の 4 規則。復元できない・矛盾するレコードは、★ 黙って STOCK_SCOPE 等へ倒さず
       ``UNKNOWN_LEGACY`` になる(warning を出す。識別子・所有者・株数をログに出さない)
    5. 直列化の往復と、``scope_type`` を持たない旧レコードの読み込み(新コードが旧レコードを読める)
    6. ★ 既知の rollback の窓: 新コードで保存したレコードを、``scope_type`` を知らない旧形式のモデル
       (``extra="forbid"``)で読むと失敗する。運用手順書 30.7(#368)と同型。

★ 6 を「固定テスト」にした理由(設計判断):
    この失敗は「実害が出うる既知の窓」であり、記録だけの文書化では、後でモデルの設定
    (extra / 保存時の
    exclude_none など)が変わって窓が閉じたり広がったりしても、誰も気づかない。固定テストにすると、
    窓の性質(どのキーで・どの理由で失敗するか)が機械的に固定され、#200(追加フィールドの安全な戻し先)
    の対応でモデルの読み方が変わったとき、このテストを意図して更新することになる(黙って変わらない)。
    ただし本テストは旧コードの挙動そのものではなく、「scope_type を知らない同じ形のモデル」
    で近似している
    (実際の旧コードでの確認ではない)。

fixture は架空値のみ(実在の氏名・所有者・数量・単価を使わない)。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError, create_model

from jstock_advisor.domain.entities.base import ImmutableSnapshot
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    RecommendationScope,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import (
    Recommendation,
    infer_legacy_recommendation_scope,
    resolve_recommendation_scope,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from tests.unit.test_buy_signal_service import (
    _CALENDAR as _BUY_CALENDAR,
)
from tests.unit.test_buy_signal_service import (
    _CONFIG as _BUY_CONFIG,
)
from tests.unit.test_buy_signal_service import (
    _NIHON_SHINYAKU,
    _build_snapshot,
)
from tests.unit.test_buy_signal_service import (
    _NOW as _BUY_NOW,
)
from tests.unit.test_buy_signal_service import (
    _providers as _buy_providers,
)
from tests.unit.test_buy_signal_service import (
    service_module as buy_signal_service_module,
)
from tests.unit.test_issue_64_household_concentration_scope import (
    _STOCK_A,
    _STOCK_B,
    _for_stock,
    _run,
)
from tests.unit.test_issue_67_recommendation_provenance_transfer import (
    _base_snapshot,
    _holding_recommendation,
    _register_fictional_stock,  # noqa: F401 - autouse fixture
    _sell_recommendation,
)
from tests.unit.test_profit_taking_service import (
    _CONFIG as _PT_CONFIG,
)
from tests.unit.test_profit_taking_service import (
    _NOW as _PT_NOW,
)
from tests.unit.test_profit_taking_service import (
    _STALE_EARNINGS_DATE as _PT_STALE_EARNINGS_DATE,
)
from tests.unit.test_profit_taking_service import (
    _canned_result,
)
from tests.unit.test_profit_taking_service import (
    _holding as _pt_holding,
)
from tests.unit.test_profit_taking_service import (
    _providers as _pt_providers,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_NOW = dt.datetime(2026, 10, 4, 9, 0, tzinfo=dt.UTC)


def _rec(**overrides: Any) -> Recommendation:
    """最小の Recommendation(架空値)。"""
    base: dict[str, Any] = {
        "recommendation_id": "rec-580-test",
        "stock_code": "0000",
        "stock_name": "架空銘柄",
        "recommended_at": _NOW,
        "recommendation_type": RecommendationType.BUY,
        "price_at_recommendation": Decimal("100"),
        "confidence": ConfidenceLevel.MEDIUM,
        "rule_version": "test",
    }
    base.update(overrides)
    return Recommendation(**base)


# --- 1 enum と field の形 ------------------------------------------------------------------


def test_recommendation_scope_has_exactly_the_four_designed_values() -> None:
    """★ 設計(CORRECTION 2)の 4 値だけ。値の追加・改名は legacy inference・消費側へ波及する。"""
    assert {m.name: m.value for m in RecommendationScope} == {
        "SINGLE_HOLDING": "SINGLE_HOLDING",
        "HOUSEHOLD_AGGREGATE": "HOUSEHOLD_AGGREGATE",
        "STOCK_SCOPE": "STOCK_SCOPE",
        "UNKNOWN_LEGACY": "UNKNOWN_LEGACY",
    }


def test_scope_type_is_optional_and_defaults_to_none() -> None:
    """★ 旧レコードは scope_type を持たない。必須にはできない。"""
    field = Recommendation.model_fields["scope_type"]
    assert not field.is_required()
    assert field.default is None
    assert _rec().scope_type is None


# --- 2 生成箇所の静的な不変条件(src 全体)---------------------------------------------------

# 期待する生成箇所(ファイル → 設定してよい種別)。無登録の追加・削除でこのテストが赤くなる。
_EXPECTED_SITES: dict[str, set[str]] = {
    "services/buy_signal_service.py": {"STOCK_SCOPE"},
    "services/holding_decision_notification_builder.py": {"SINGLE_HOLDING"},
    "services/profit_taking_service.py": {"SINGLE_HOLDING"},
    "services/sell_signal_service.py": {"SINGLE_HOLDING"},
    "lambda_handlers/holdings_watchlist_handler.py": {"SINGLE_HOLDING", "HOUSEHOLD_AGGREGATE"},
}


def _scope_members(node: ast.expr) -> set[str] | None:
    """``RecommendationScope.X`` または、その条件式(IfExp)から種別名の集合を取り出す。"""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "RecommendationScope"
    ):
        return {node.attr}
    if isinstance(node, ast.IfExp):
        left = _scope_members(node.body)
        right = _scope_members(node.orelse)
        if left is not None and right is not None:
            return left | right
    return None


def _recommendation_construction_sites() -> list[tuple[str, ast.Call]]:
    sites: list[tuple[str, ast.Call]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Recommendation"
            ):
                sites.append((path.relative_to(_SRC).as_posix(), node))
    return sites


def test_every_recommendation_construction_site_sets_scope_type_explicitly() -> None:
    """★ src 全体の ``Recommendation(...)`` の構築が ``scope_type=`` を明示する(省略は旧専用)。

    ``**kwargs`` で組み立てる構築は静的に検証できないため、それ自体を失敗にする(見逃さない)。
    """
    sites = _recommendation_construction_sites()
    problems: list[str] = []
    seen: dict[str, set[str]] = {}
    for rel, call in sites:
        if any(kw.arg is None for kw in call.keywords):
            problems.append(f"{rel}:{call.lineno} ** で組み立てる構築は scope_type を検証できない")
            continue
        scope_kw = [kw for kw in call.keywords if kw.arg == "scope_type"]
        if len(scope_kw) != 1:
            problems.append(f"{rel}:{call.lineno} scope_type が無い")
            continue
        members = _scope_members(scope_kw[0].value)
        if members is None:
            problems.append(f"{rel}:{call.lineno} scope_type が RecommendationScope.X の形でない")
            continue
        seen.setdefault(rel, set()).update(members)
    assert problems == []
    # 生成箇所のファイル集合と、各ファイルが設定する種別が、設計どおりであること。
    assert seen == _EXPECTED_SITES


def test_no_site_sets_unknown_legacy() -> None:
    """★ UNKNOWN_LEGACY は旧レコードの復元結果専用で、新しい構築コードは設定しない。"""
    for members in _EXPECTED_SITES.values():
        assert "UNKNOWN_LEGACY" not in members


# --- 3 5 つの生成箇所(動的)----------------------------------------------------------------


def _legacy_view(rec: Recommendation) -> RecommendationScope:
    """scope_type を除いた(旧形式の)レコードとして見たときの、legacy inference の結果。"""
    return infer_legacy_recommendation_scope(rec.model_copy(update={"scope_type": None}))


def test_buy_signal_service_sets_stock_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _build_snapshot(_NIHON_SHINYAKU, price_as_of_date=dt.date(2026, 8, 3))
    monkeypatch.setattr(
        buy_signal_service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None)
    )
    service = buy_signal_service_module.BuySignalService(
        providers=_buy_providers(), config=_BUY_CONFIG, business_calendar=_BUY_CALENDAR
    )

    rec = service.analyze(
        _NIHON_SHINYAKU.stock_code, _BUY_NOW, RecommendationType.BUY
    ).recommendation

    assert rec is not None
    assert rec.scope_type is RecommendationScope.STOCK_SCOPE
    assert rec.owner is None and rec.holding_id is None and rec.shares_at_recommendation is None
    assert _legacy_view(rec) is RecommendationScope.STOCK_SCOPE


def test_sell_signal_service_sets_single_holding(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _sell_recommendation(monkeypatch, _base_snapshot())

    assert rec.scope_type is RecommendationScope.SINGLE_HOLDING
    assert rec.owner is not None and rec.holding_id is not None
    assert _legacy_view(rec) is RecommendationScope.SINGLE_HOLDING


def test_holding_decision_builder_sets_single_holding() -> None:
    rec = _holding_recommendation(_base_snapshot())

    assert rec.scope_type is RecommendationScope.SINGLE_HOLDING
    assert rec.owner is not None and rec.holding_id is not None
    assert _legacy_view(rec) is RecommendationScope.SINGLE_HOLDING


def test_profit_taking_service_sets_single_holding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    providers = _pt_providers(_PT_STALE_EARNINGS_DATE, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_PT_CONFIG)

    rec = service.analyze(_pt_holding("2914"), _PT_NOW).recommendation

    assert rec is not None
    assert rec.scope_type is RecommendationScope.SINGLE_HOLDING
    assert rec.owner is not None and rec.holding_id is not None
    assert _legacy_view(rec) is RecommendationScope.SINGLE_HOLDING


def test_household_concentration_single_contributor_is_single_holding() -> None:
    """★ 寄与する保有が 1 件(単一保有寄与)= owner・holding_id を持つ SINGLE_HOLDING。"""
    saved, _notified, _market = _run()

    rec = _for_stock(saved, _STOCK_B)[0]
    assert rec.scope_type is RecommendationScope.SINGLE_HOLDING
    assert rec.owner is not None and rec.holding_id is not None
    assert _legacy_view(rec) is RecommendationScope.SINGLE_HOLDING


def test_household_concentration_multiple_contributors_is_household_aggregate() -> None:
    """★ 寄与する保有が 2 件以上(#329)= 合算株数を持ち owner・holding_id が None の合算。

    これが、暗黙の不変条件「shares_at_recommendation を持つなら owner・holding_id を持つ」を
    崩す唯一の生成箇所。旧形式のレコードとして見ても、legacy inference が同じ種別へ復元する。
    """
    saved, _notified, _market = _run()

    rec = _for_stock(saved, _STOCK_A)[0]
    assert rec.scope_type is RecommendationScope.HOUSEHOLD_AGGREGATE
    assert rec.owner is None and rec.holding_id is None
    assert rec.shares_at_recommendation is not None
    assert len(rec.config_values_used["contributing_holding_ids"]) >= 2
    assert _legacy_view(rec) is RecommendationScope.HOUSEHOLD_AGGREGATE


# --- 4 legacy inference(4 規則)-------------------------------------------------------------

_IDS2 = ["id-a", "id-b"]
_IDS1 = ["id-a"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # 規則 1: owner・holding_id がともに非 None、寄与一覧が無いか 1 件以下 -> SINGLE_HOLDING
        (
            {"owner": "所有者A", "holding_id": "所有者A#0000", "shares_at_recommendation": 100},
            RecommendationScope.SINGLE_HOLDING,
        ),
        (
            {
                "owner": "所有者A",
                "holding_id": "所有者A#0000",
                "shares_at_recommendation": 100,
                "config_values_used": {"contributing_holding_ids": _IDS1},
            },
            RecommendationScope.SINGLE_HOLDING,
        ),
        # 規則 1 は株数を条件にしない(株数が無くても owner・holding_id があれば SINGLE_HOLDING)
        (
            {"owner": "所有者A", "holding_id": "所有者A#0000"},
            RecommendationScope.SINGLE_HOLDING,
        ),
        # 規則 2: owner・holding_id が None・株数あり・寄与一覧が 2 件以上 -> HOUSEHOLD_AGGREGATE
        (
            {
                "shares_at_recommendation": 300,
                "config_values_used": {"contributing_holding_ids": _IDS2},
            },
            RecommendationScope.HOUSEHOLD_AGGREGATE,
        ),
        # 規則 3: 株数が無く、owner・holding_id も無く、寄与一覧が無い(または空)-> STOCK_SCOPE
        ({}, RecommendationScope.STOCK_SCOPE),
        ({"config_values_used": {"contributing_holding_ids": []}}, RecommendationScope.STOCK_SCOPE),
    ],
)
def test_legacy_inference_rules_1_to_3(
    overrides: dict[str, Any], expected: RecommendationScope
) -> None:
    rec = _rec(**overrides)
    assert rec.scope_type is None
    assert infer_legacy_recommendation_scope(rec) is expected
    assert resolve_recommendation_scope(rec) is expected


@pytest.mark.parametrize(
    "overrides",
    [
        # 規則 4: owner・holding_id が None で株数だけを持ち、寄与一覧が 1 件以下・欠落
        {"shares_at_recommendation": 100},
        {
            "shares_at_recommendation": 100,
            "config_values_used": {"contributing_holding_ids": _IDS1},
        },
        {"shares_at_recommendation": 100, "config_values_used": {"contributing_holding_ids": []}},
        # 寄与一覧が list / tuple でない値(壊れた記録)
        {
            "shares_at_recommendation": 100,
            "config_values_used": {"contributing_holding_ids": "id-a,id-b"},
        },
        # owner だけ・holding_id だけ(片方だけを持つ)
        {"owner": "所有者A", "shares_at_recommendation": 100},
        {"holding_id": "所有者A#0000", "shares_at_recommendation": 100},
        # 株数が無いのに owner を持つ(holding 固有の情報が一部だけある)
        {"owner": "所有者A"},
        # owner・holding_id があるのに寄与一覧が 2 件以上(規則 1 の不変条件に反する矛盾)
        {
            "owner": "所有者A",
            "holding_id": "所有者A#0000",
            "shares_at_recommendation": 100,
            "config_values_used": {"contributing_holding_ids": _IDS2},
        },
        # 株数が無いのに寄与一覧が非空(規則 3 の不変条件〔owner・holding_id・株数なし〕に反する)
        {"config_values_used": {"contributing_holding_ids": _IDS1}},
        # ★ owner・holding_id・株数のいずれも無く、寄与する保有が 2 件以上(株数が無いのに合算の
        #   一覧がある = 矛盾)。規則 2 の「株数が非 None」を外すと HOUSEHOLD_AGGREGATE へ倒れる
        {"config_values_used": {"contributing_holding_ids": _IDS2}},
    ],
)
def test_legacy_inference_rule_4_unrestorable_records_become_unknown_legacy(
    overrides: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """★ 復元できない・矛盾するレコードを、黙って STOCK_SCOPE 等へ倒さない(UNKNOWN_LEGACY)。"""
    rec = _rec(**overrides)
    with caplog.at_level(logging.WARNING):
        result = infer_legacy_recommendation_scope(rec)

    assert result is RecommendationScope.UNKNOWN_LEGACY
    assert result is not RecommendationScope.STOCK_SCOPE
    assert any("could not be restored" in r.getMessage() for r in caplog.records)


def test_unknown_legacy_warning_does_not_leak_identifiers(caplog: pytest.LogCaptureFixture) -> None:
    """★ ログに識別子・所有者・holding_id・株数・銘柄コードを出さない(真偽値と件数だけ)。"""
    rec = _rec(
        recommendation_id="rec-secret-id",
        stock_code="9999",
        owner="所有者X",
        shares_at_recommendation=12345,
    )
    with caplog.at_level(logging.WARNING):
        infer_legacy_recommendation_scope(rec)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert text
    for leaked in ("rec-secret-id", "9999", "所有者X", "12345"):
        assert leaked not in text


def test_well_formed_records_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        infer_legacy_recommendation_scope(_rec())
        infer_legacy_recommendation_scope(
            _rec(owner="所有者A", holding_id="所有者A#0000", shares_at_recommendation=100)
        )
    assert caplog.records == []


# --- resolve: scope_type が正本 ------------------------------------------------------------


def test_resolve_prefers_explicit_scope_type_over_other_fields() -> None:
    """★ scope_type があればそれが正本で、他のフィールドを見ない(legacy inference を通らない)。"""
    contradictory = _rec(
        scope_type=RecommendationScope.HOUSEHOLD_AGGREGATE,
        owner="所有者A",
        holding_id="所有者A#0000",
        shares_at_recommendation=100,
    )
    assert resolve_recommendation_scope(contradictory) is RecommendationScope.HOUSEHOLD_AGGREGATE


def test_resolve_does_not_warn_when_scope_type_is_explicit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        resolve_recommendation_scope(
            _rec(scope_type=RecommendationScope.STOCK_SCOPE, shares_at_recommendation=100)
        )
    assert caplog.records == []


# --- 5 直列化 ------------------------------------------------------------------------------


@pytest.mark.parametrize("scope", list(RecommendationScope))
def test_scope_type_round_trips_through_json_as_a_string(scope: RecommendationScope) -> None:
    rec = _rec(scope_type=scope)
    raw = rec.model_dump_json()

    assert json.loads(raw)["scope_type"] == scope.value
    assert Recommendation.model_validate_json(raw) == rec


def test_unset_scope_type_is_written_as_null_and_reads_back_as_none() -> None:
    """★ 保存は exclude_none しない(None も null で出る)。新コードの全レコードがキーを持つ。"""
    raw = _rec().model_dump_json()

    assert "scope_type" in json.loads(raw)
    assert json.loads(raw)["scope_type"] is None
    assert Recommendation.model_validate_json(raw).scope_type is None


def test_a_legacy_record_without_the_key_is_readable_by_the_new_model() -> None:
    """★ 新コードは旧レコード(scope_type のキーが無い)を読める。"""
    data = json.loads(_rec(owner="所有者A", holding_id="所有者A#0000").model_dump_json())
    del data["scope_type"]

    rec = Recommendation.model_validate_json(json.dumps(data))

    assert rec.scope_type is None
    assert resolve_recommendation_scope(rec) is RecommendationScope.SINGLE_HOLDING


def test_repository_round_trip_preserves_scope_type_and_reads_legacy_records(
    tmp_path: Path,
) -> None:
    repo = RecommendationRepository(store_dir=tmp_path)
    explicit = _rec(
        recommendation_id="rec-explicit",
        scope_type=RecommendationScope.HOUSEHOLD_AGGREGATE,
        shares_at_recommendation=300,
        config_values_used={"contributing_holding_ids": _IDS2},
    )
    legacy = _rec(
        recommendation_id="rec-legacy",
        shares_at_recommendation=300,
        config_values_used={"contributing_holding_ids": _IDS2},
    )
    repo.save(explicit)
    repo.save(legacy)

    got_explicit = repo.get("rec-explicit")
    got_legacy = repo.get("rec-legacy")

    assert (
        got_explicit is not None
        and got_explicit.scope_type is RecommendationScope.HOUSEHOLD_AGGREGATE
    )
    assert got_legacy is not None and got_legacy.scope_type is None
    assert resolve_recommendation_scope(got_legacy) is RecommendationScope.HOUSEHOLD_AGGREGATE


def test_equality_includes_scope_type_but_identity_is_the_recommendation_id() -> None:
    """★ 等価性の影響(設計の確認事項)。同じ内容でも scope_type が違えば等しくない。

    同一性のキーは recommendation_id(store のキー。insert_if_absent は id で判定する)で、
    Recommendation は dict フィールドを持つため hash() は元から使えない。
    """
    with_scope = _rec(scope_type=RecommendationScope.STOCK_SCOPE)
    without_scope = _rec()

    assert with_scope != without_scope
    assert with_scope.recommendation_id == without_scope.recommendation_id
    with pytest.raises(TypeError):
        hash(with_scope)


# --- 6 既知の rollback の窓(★ 固定テスト。理由は module docstring)------------------------------


def _pre_scope_model() -> type[ImmutableSnapshot]:
    """``scope_type`` を知らない、同じ形のモデル(rollback した旧コードの近似)。"""
    fields: dict[str, Any] = {
        name: (info.annotation, info)
        for name, info in Recommendation.model_fields.items()
        if name != "scope_type"
    }
    return create_model("PreScopeRecommendation", __base__=ImmutableSnapshot, **fields)


def test_pre_scope_model_has_the_same_shape_except_for_scope_type() -> None:
    old = _pre_scope_model()
    assert set(Recommendation.model_fields) - set(old.model_fields) == {"scope_type"}
    assert old.model_config.get("extra") == "forbid"


@pytest.mark.parametrize(
    "scope",
    [RecommendationScope.STOCK_SCOPE, RecommendationScope.SINGLE_HOLDING, None],
)
def test_known_rollback_window_pre_scope_model_cannot_read_new_records(
    scope: RecommendationScope | None,
) -> None:
    """★ 既知の rollback の窓(30.7 と同型): 新コードの保存したレコードを、旧形式のモデルは読めない。

    ``scope_type`` が None のレコードも同じ(保存は None を null で出すため、新コードで保存した
    全レコードが窓の対象)。失敗の理由は extra_forbidden(キーを知らない)で、他の理由ではない。
    """
    raw = _rec(scope_type=scope).model_dump_json()

    with pytest.raises(ValidationError) as excinfo:
        _pre_scope_model().model_validate_json(raw)

    errors = excinfo.value.errors()
    assert [(e["type"], e["loc"]) for e in errors] == [("extra_forbidden", ("scope_type",))]


def test_known_rollback_window_pre_scope_model_reads_records_without_the_key() -> None:
    """★ 窓は片方向: キーを持たない旧レコードは、旧形式のモデルでも新モデルでも読める。"""
    data = json.loads(_rec().model_dump_json())
    del data["scope_type"]
    raw = json.dumps(data)

    assert _pre_scope_model().model_validate_json(raw) is not None
    assert Recommendation.model_validate_json(raw).scope_type is None
