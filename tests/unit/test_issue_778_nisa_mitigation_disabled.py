"""Issue #778(USER 決定 2026-10-03、案 A): 口座区分(NISA / 特定 / 一般)を利確判定に使わない。
利確判定の NISA 軽減(`nisa_long_term_benefit`。「NISA口座で長期保有メリットが大きい」の
downgrade)をやめる。

原本: Issue #122 の USER_DECISION_RECORD(issuecomment-5968181968 の案 A・issuecomment-5968256759)。
実装方式は MANAGER 判断で (i) = 出荷 config の `enabled` を false にする(最小・可逆)。
判定のコードは残るため、**出荷 config が false であること**と、
**NISA の保有で downgrade が適用されないこと**を、本ファイルが固定する。

## 検査

```
G1  出荷 config(リポジトリの config/ を絶対パスで直接読む。JSTOCK_CONFIG_DIR の影響を
    受けない)が enabled: false
G2  domain: 同じ入力で is_nisa_account の有無によらず結果が同一(FULL_PROFIT_TAKE。理由文なし)
G3  service 経路: mock 4 銘柄 × 取得単価の複数点 × 口座区分 3 種で、実 build_stock_snapshot →
    ProfitTakingService の Recommendation が(識別子を除き)同一で、
    counter_factors に NISA の理由文が無い
G4  profit_taking_service が `.account_type` を読む箇所が 1 箇所から増えない
    (新しい読み手が追加されたら落ちる)
A3  空振り防止(反証): enabled=True の config のコピーでは、domain の入力で結果が変わり
    (FULL → PARTIAL)、service 経路の NISA の保有に理由文が付くことを前提として固定する
    (= 変更前の実装では差が出る観測を含む)
```

## このテストが検査している範囲 / していない範囲(★ 正確に)

している    出荷 config の enabled が false であること / domain の評価関数と ProfitTakingService の
            経路での NISA 有無の同一性 / `.account_type` の読み取りが profit_taking_service に
            1 箇所であること。
していない  ・Lambda 実行時の config(ConfigLayer の中身)そのもの。出荷ファイルを検査するのみで、
              誤編集で true に戻った場合に気づくのは CI のテストだけ
              (実行時には検査しない)
            ・mock の格子の外(実データ)での Action の差の有無。mock の実 snapshot 経由の
              20 通り(×口座区分 3 種)では、変更前でも Action の差は出ず、
              理由文の有無のみが差になる。
              Action の差は G2 の domain の入力で確認する
            ・出荷 false の下では、口座区分の取り違え(例: 特定口座も NISA 扱いにする)の変異は
              結果に効かないため検出できない(enabled=True のコピーでは A3 が検出する)
            ・DecisionSnapshot / 監査ログの `is_nisa_account` の記録
              (変更していない。事実の記録として残す)
            ・account_type のデータ項目・CLI の入力と表示・Transaction への受け渡し
              (#619 F7 / #626)。判定に使わなくなるため無害だが不要になる配線で、
              戻す・残すは USER 判断(本 Issue の範囲外)

fixture は mock provider のみ(銘柄コードは mock の一覧から機械的に取る。銘柄名は架空の
「テスト銘柄」)。時刻は固定。Production・AWS へは触れない。
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    AccountType,
    IndustryClassification,
    RecommendationType,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.signals.judgment_safety_shadow_config import (
    JudgmentSafetyShadowConfig,
    ShadowMode,
)
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    evaluate_profit_taking,
)
from jstock_advisor.providers import mock_fixtures
from jstock_advisor.services import profit_taking_service as profit_taking_service_module
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot
from tests.unit.test_profit_taking import _fair_value_range

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED_CONFIG_DIR = _REPO_ROOT / "config"
_SHIPPED_YAML = _SHIPPED_CONFIG_DIR / "profit_taking_rules.yaml"
_NISA_REASON = "NISA口座で長期保有メリットが大きい"

_CONFIG = load_config(_SHIPPED_CONFIG_DIR)
_CALENDAR = BusinessCalendar.from_config(_CONFIG.holiday_calendar)
# 営業日の大引け後(水曜 16:00 JST)に固定する(wall clock を使わない)。
_NOW = dt.datetime(2026, 9, 2, 16, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
_MOCK_STOCK_CODES = tuple(mock_fixtures.MOCK_STOCKS)
# 取得単価 = 現在値 × この比率。含み益の水準を変えて、判定が分かれる範囲を通す。
_PRICE_FRACTIONS = (Decimal("0.3"), Decimal("0.5"), Decimal("0.7"), Decimal("0.8"), Decimal("0.9"))
_ACCOUNT_TYPES = (AccountType.GENERAL, AccountType.NISA, AccountType.SPECIFIC)


def _config_with_nisa_enabled(config: AppConfig) -> AppConfig:
    """出荷 config から NISA の enabled だけを true にしたコピー(変更前の挙動を再現する)。"""
    factors = config.profit_taking.mitigating_factors
    nisa = factors.nisa_long_term_benefit.model_copy(update={"enabled": True})
    return config.model_copy(
        update={
            "profit_taking": config.profit_taking.model_copy(
                update={
                    "mitigating_factors": factors.model_copy(
                        update={"nisa_long_term_benefit": nisa}
                    )
                }
            )
        }
    )


# --- G1: 出荷 config ----------------------------------------------------------------


def test_g1_the_shipped_yaml_file_has_the_nisa_mitigation_disabled() -> None:
    """★ 出荷ファイルを直接読む(JSTOCK_CONFIG_DIR の影響を受けない)。
    true に戻すには USER 決定の変更が要る。"""
    raw = yaml.safe_load(_SHIPPED_YAML.read_text(encoding="utf-8"))

    factor = raw["mitigating_factors"]["nisa_long_term_benefit"]

    assert factor["enabled"] is False, (
        "利確判定の NISA 軽減は USER 決定(2026-10-03、案 A。#122 issuecomment-5968181968)でやめた。"
        "true に戻す場合は USER の決定の変更と、本 guard の更新が要る"
    )
    assert "downgrade_levels" in factor  # 値は据え置き(enabled: false の間は使われない)


def test_g1_the_shipped_config_loaded_through_the_model_is_also_disabled() -> None:
    """モデルの検証(StrictModel)を通した値も false。ファイル読み込みとモデルの食い違いを防ぐ。"""
    config = load_config(_SHIPPED_CONFIG_DIR)

    assert config.profit_taking.mitigating_factors.nisa_long_term_benefit.enabled is False


def test_g1_the_other_mitigating_factors_are_unchanged() -> None:
    """他の緩和要因は有効のまま(NISA だけをやめた。#778 の範囲外の要因を巻き込まない)。"""
    factors = _CONFIG.profit_taking.mitigating_factors

    assert factors.continuous_dividend_increase.enabled is True
    assert factors.progressive_dividend_or_doe_policy.enabled is True
    assert factors.long_term_holding_benefit_imminent.enabled is True
    assert factors.few_reinvestment_alternatives.enabled is True


def test_a3_the_enabled_copy_differs_from_the_shipped_config() -> None:
    """★ 前提: enabled=True のコピーは、出荷 config と NISA の enabled だけが異なる。"""
    enabled = _config_with_nisa_enabled(_CONFIG)

    assert enabled.profit_taking.mitigating_factors.nisa_long_term_benefit.enabled is True
    assert _CONFIG.profit_taking.mitigating_factors.nisa_long_term_benefit.enabled is False
    assert enabled.profit_taking.model_dump(
        exclude={"mitigating_factors": {"nisa_long_term_benefit"}}
    ) == (
        _CONFIG.profit_taking.model_dump(exclude={"mitigating_factors": {"nisa_long_term_benefit"}})
    )


# --- G2: domain の入力 ------------------------------------------------------------------


def _full_profit_take_inputs(config: AppConfig, *, is_nisa_account: bool) -> Any:
    """既存の domain テスト(test_mitigating_factors_downgrade_full_to_partial)と同じ入力。
    NISA 以外の緩和要因は無し。含み益 +60%・上値余地が小さく、価格位置由来の FULL になる。"""
    return evaluate_profit_taking(
        current_price=Decimal("1600"),
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=4.0,
        forecast_annual_dividend_per_share=Decimal("40"),
        mitigating_inputs=MitigatingFactorInputs(is_nisa_account=is_nisa_account),
        config=config.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(
            fair_value_range=_fair_value_range(
                neutral=Decimal("1560"), bull=Decimal("1620"), bear=Decimal("1500"), method_count=3
            ),
            fair_value_reflects_latest_earnings=True,
            industry_classification=IndustryClassification.GENERAL_CORPORATE,
        ),
    )


@pytest.mark.parametrize("is_nisa_account", [False, True])
def test_g2_the_shipped_config_gives_full_profit_take_without_the_nisa_reason(
    is_nisa_account: bool,
) -> None:
    result = _full_profit_take_inputs(_CONFIG, is_nisa_account=is_nisa_account)

    assert result.recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    assert result.mitigating_factors_applied == []


def test_a3_with_the_enabled_copy_the_nisa_holding_is_downgraded_to_partial() -> None:
    """★ 前提(反証): 変更前の挙動(enabled=True)では、同じ入力で NISA の保有だけが PARTIAL へ
    1 段階弱まり、理由文が付く。一般口座の保有は FULL のまま。
    G2 が空振りしていないこと(旧実装で差が出ること)を固定する。"""
    config = _config_with_nisa_enabled(_CONFIG)

    general = _full_profit_take_inputs(config, is_nisa_account=False)
    nisa = _full_profit_take_inputs(config, is_nisa_account=True)

    assert general.recommendation_type == RecommendationType.FULL_PROFIT_TAKE
    assert nisa.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert nisa.mitigating_factors_applied == [_NISA_REASON]
    assert general.mitigating_factors_applied == []


# --- G3: service 経路(実 build_stock_snapshot → ProfitTakingService)--------------------------


def _holding(
    stock_code: str, average_purchase_price: Decimal, account_type: AccountType
) -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="テスト銘柄",
        shares=300,
        average_purchase_price=average_purchase_price,
        total_purchase_amount=average_purchase_price * 300,
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=account_type,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _RecordingAudit:
    def record(self, **kwargs: Any) -> Any:
        return SimpleNamespace(audit_id="audit-1")


def _analyze(
    config: AppConfig, stock_code: str, fraction: Decimal, account_type: AccountType
) -> Any:
    providers = build_mock_provider_bundle(_NOW)
    snapshot, error = build_stock_snapshot(
        providers, stock_code, _NOW, config, business_calendar=_CALENDAR
    )
    assert snapshot is not None, error
    average_purchase_price = (snapshot.current_price * fraction).quantize(Decimal("0.1"))
    service = ProfitTakingService(
        providers=providers,
        config=config,
        business_calendar=_CALENDAR,
        shadow_config=JudgmentSafetyShadowConfig(mode=ShadowMode.OFF),
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(service, "_audit", _RecordingAudit())
        return service.analyze(
            _holding(stock_code, average_purchase_price, account_type), _NOW, snapshot=snapshot
        )


def _comparable(outcome: Any) -> dict[str, Any]:
    """口座区分以外が同一かを比べるための辞書(毎回変わる識別子を除く)。"""
    recommendation = outcome.recommendation
    dumped = recommendation.model_dump(mode="json") if recommendation is not None else None
    if dumped is not None:
        dumped.pop("recommendation_id", None)
    return {"recommendation": dumped, "data_error": outcome.data_error}


_FIXTURES = [(code, fraction) for code in _MOCK_STOCK_CODES for fraction in _PRICE_FRACTIONS]


@pytest.mark.parametrize(
    ("stock_code", "fraction"), _FIXTURES, ids=[f"fixture-{i}" for i in range(len(_FIXTURES))]
)
def test_g3_the_account_type_does_not_change_the_profit_taking_outcome(
    stock_code: str, fraction: Decimal
) -> None:
    """★ 口座区分 3 種(一般 / NISA / 特定)で、Recommendation が(識別子を除き)同一で、
    NISA の理由文が無い。"""
    outcomes = {at: _analyze(_CONFIG, stock_code, fraction, at) for at in _ACCOUNT_TYPES}
    baseline = _comparable(outcomes[AccountType.GENERAL])

    for account_type, outcome in outcomes.items():
        assert _comparable(outcome) == baseline, account_type
        recommendation = outcome.recommendation
        if recommendation is not None:
            assert _NISA_REASON not in recommendation.counter_factors
            assert _NISA_REASON not in recommendation.reasons


def test_a3_the_grid_produces_recommendations_so_g3_is_not_vacuous() -> None:
    """★ 前提: 格子上で Recommendation が実際に作られる(作られないと G3 が空振りする)。"""
    produced = [
        _analyze(_CONFIG, code, fraction, AccountType.NISA).recommendation is not None
        for code, fraction in _FIXTURES
    ]

    assert sum(produced) >= len(_FIXTURES) // 2, (
        f"Recommendation が作られた fixture: {sum(produced)} / {len(_FIXTURES)}"
    )


def test_a3_with_the_enabled_copy_the_nisa_reason_appears_on_every_nisa_recommendation() -> None:
    """★ 前提(反証): 変更前の挙動(enabled=True)では、NISA の保有の Recommendation の
    counter_factors に NISA の理由文が付き、一般口座では付かない。
    G3 が旧実装で差を検出できること(理由文の観測)を固定する。
    ※ mock の実 snapshot では Action の差は出ない(理由文のみ)。
    Action の差は G2 の domain の入力で確認する。"""
    config = _config_with_nisa_enabled(_CONFIG)
    checked = 0
    for code, fraction in _FIXTURES:
        nisa = _analyze(config, code, fraction, AccountType.NISA).recommendation
        general = _analyze(config, code, fraction, AccountType.GENERAL).recommendation
        if nisa is None or general is None:
            continue
        checked += 1
        assert _NISA_REASON in nisa.counter_factors
        assert _NISA_REASON not in general.counter_factors

    assert checked >= len(_FIXTURES) // 2


# --- G4: account_type の読み手 -------------------------------------------------------------


def test_g4_profit_taking_service_reads_account_type_in_exactly_one_place() -> None:
    """判定の入力としての account_type の読み手は 1 箇所(`is_nisa_account` の導出)から増えない。
    新しい読み手(口座区分を判定に使う変更)が加わったら落ちる。"""
    tree = ast.parse(
        Path(inspect.getsourcefile(profit_taking_service_module) or "").read_text("utf-8")
    )
    reads = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "account_type"
        and isinstance(node.ctx, ast.Load)
    ]

    assert len(reads) == 1, f"profit_taking_service の account_type の読み取り: {reads}"
