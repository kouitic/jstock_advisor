"""#579: 分析系CLI(before-after / compare / backtest)のowner伝播のテスト。

これらのCLI・サービスは従来`DEFAULT_OWNER`固定で保有を引いていたため、
本人以外のownerの保有が「未登録」「NOT_EVALUATED_NON_HOLDING」と誤分類され、
`--stock-code`省略時の「全保有銘柄」の列挙も全ownerを横断していた。

契約(USER承認済み):
  - CLIは`--owner`(既定はDEFAULT_OWNER)を持つ。service層のownerは必須引数で、
    DEFAULT_OWNERへ解決するのはCLI層だけ。
  - `--stock-code`省略時の列挙も指定ownerの保有のみ(省略owner = DEFAULT_OWNERのみ)。
  - before-after / compare / backtestの3経路は、同じownerで保有lookupまで一貫して届く。

テスト用のownerはすべて架空値(owner-a / owner-b)。銘柄コードも架空または
mock providerが持つものだけを使い、実際の保有・氏名は一切使わない。

★ 反証: 修正前の実装(DEFAULT_OWNER固定・全owner列挙)では、owner-b指定のテストが
  すべて「owner-bの保有がNOT_EVALUATED_NON_HOLDINGへ誤分類される」「他ownerの
  銘柄が混入する」で落ちる(実装時にrevertして確認した)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import holding_decision as hd_cli
from jstock_advisor.cli import review as review_cli
from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import (
    DEFAULT_OWNER,
    InvalidOwnerError,
    build_holding_id,
)
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services.before_after_report_service import BeforeAfterReportService
from jstock_advisor.services.holding_decision_backtest_service import (
    placeholder_holding,
    resolve_target_stock_codes,
    run_live_comparison,
)
from jstock_advisor.services.holding_decision_compare_service import run_compare
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_mock_provider_bundle

_OWNER_A = "owner-a"
_OWNER_B = "owner-b"
_NOW = dt.datetime(2026, 8, 5, tzinfo=dt.UTC)
_CFG = load_config()
_PROVIDERS = build_mock_provider_bundle(_NOW)
_NOT_HELD = "NOT_EVALUATED_NON_HOLDING"
_runner = CliRunner()


def _holding(owner: str, stock_code: str) -> Holding:
    return Holding(
        owner=owner,
        holding_id=build_holding_id(owner, stock_code),
        stock_code=stock_code,
        stock_name="x",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _seed(repo: HoldingRepository, holdings: dict[str, list[str]]) -> None:
    for owner, codes in holdings.items():
        for code in codes:
            repo.upsert(_holding(owner, code))


def _mock_bundle() -> ProviderBundle:
    return ProviderBundle(
        market_data=MockMarketDataProvider(now=_NOW),
        financial_data=MockFinancialDataProvider(now=_NOW),
        dividend_data=MockDividendDataProvider(now=_NOW),
        shareholder_benefit=MockShareholderBenefitProvider(now=_NOW),
        disclosure=MockDisclosureProvider(now=_NOW),
        corporate_action=MockCorporateActionProvider(),
    )


# ===== 銘柄の列挙(resolve_target_stock_codes) =====


def _enumeration_repo(store_dir: Path) -> PortfolioService:
    """USER指定の構成: owner A = 1111 / 2222、owner B = 3333、DEFAULT_OWNER = 4444。"""
    repo = HoldingRepository(store_dir=store_dir)
    _seed(
        repo,
        {_OWNER_A: ["1111", "2222"], _OWNER_B: ["3333"], DEFAULT_OWNER: ["4444"]},
    )
    return PortfolioService(holding_repository=repo)


def test_enumeration_is_scoped_to_owner_a(store_dir: Path) -> None:
    portfolio = _enumeration_repo(store_dir)
    result = resolve_target_stock_codes([], _OWNER_A, portfolio_service=portfolio)
    assert sorted(result) == ["1111", "2222"]


def test_enumeration_is_scoped_to_owner_b(store_dir: Path) -> None:
    portfolio = _enumeration_repo(store_dir)
    result = resolve_target_stock_codes([], _OWNER_B, portfolio_service=portfolio)
    assert result == ["3333"]


def test_enumeration_for_default_owner_excludes_other_owners(store_dir: Path) -> None:
    portfolio = _enumeration_repo(store_dir)
    result = resolve_target_stock_codes([], DEFAULT_OWNER, portfolio_service=portfolio)
    assert result == ["4444"]


def test_enumeration_normalizes_owner_before_matching(store_dir: Path) -> None:
    """全角・前後空白の入力揺れでも、同じownerとして列挙される(owner規約S-18と同じ正規化)。"""
    portfolio = _enumeration_repo(store_dir)
    result = resolve_target_stock_codes([], "  owner-b  ", portfolio_service=portfolio)
    assert result == ["3333"]


def test_enumeration_for_owner_without_holdings_is_empty(store_dir: Path) -> None:
    portfolio = _enumeration_repo(store_dir)
    assert resolve_target_stock_codes([], "owner-c", portfolio_service=portfolio) == []


def test_explicit_stock_codes_are_used_as_given_regardless_of_owner(store_dir: Path) -> None:
    """明示的な銘柄指定は従来どおり(指定ownerの保有として評価される。列挙のscopeは関与しない)。"""
    portfolio = _enumeration_repo(store_dir)
    assert resolve_target_stock_codes(["3333", "1111", "3333"], _OWNER_A, portfolio) == [
        "3333",
        "1111",
    ]


@pytest.mark.parametrize("bad_owner", ["", "   ", "a#b", "x" * 21])
def test_invalid_owner_is_rejected_even_with_explicit_stock_codes(
    store_dir: Path, bad_owner: str
) -> None:
    portfolio = _enumeration_repo(store_dir)
    with pytest.raises(InvalidOwnerError):
        resolve_target_stock_codes(["2914"], bad_owner, portfolio_service=portfolio)
    with pytest.raises(InvalidOwnerError):
        resolve_target_stock_codes([], bad_owner, portfolio_service=portfolio)


# ===== 保有lookupの一貫したowner scope(before-after / compare / backtestの3経路) =====
# mock providerが持つ銘柄(2914 / 9861)で、owner-aは2914、owner-bは9861を保有する。


@pytest.fixture
def two_owner_world(store_dir: Path):
    holding_repo = HoldingRepository(store_dir=store_dir)
    _seed(holding_repo, {_OWNER_A: ["2914"], _OWNER_B: ["9861"]})
    portfolio = PortfolioService(holding_repository=holding_repo)
    before_after = BeforeAfterReportService(
        providers=_mock_bundle(),
        config=_CFG,
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        audit_log_repository=AuditLogRepository(store_dir=store_dir),
        holding_repository=holding_repo,
    )
    return portfolio, before_after


def _classify_all_paths(two_owner_world, owner: str, stock_code: str) -> dict[str, bool]:
    """3経路が、その(owner, 銘柄)を「保有している」と判定したかを返す。"""
    portfolio, before_after = two_owner_world
    entry = before_after.build_entry(stock_code, _NOW, owner)
    compare_row = run_compare(
        [stock_code], _PROVIDERS, _CFG, _NOW, owner, portfolio_service=portfolio
    )[0]
    backtest_row = run_live_comparison(
        [stock_code], _PROVIDERS, _CFG, _NOW, owner, portfolio_service=portfolio
    )[0]
    return {
        "before_after": entry.holding is not None,
        "compare": compare_row.legacy_category != _NOT_HELD,
        "backtest": backtest_row.legacy_recommendation_type != _NOT_HELD,
    }


@pytest.mark.parametrize(
    ("owner", "stock_code", "expected_held"),
    [
        (_OWNER_A, "2914", True),
        (_OWNER_B, "2914", False),  # owner-bは2914を保有していない(他ownerの保有を借りない)
        (_OWNER_B, "9861", True),  # ★ 反証: DEFAULT_OWNER固定の旧実装ではFalseになり落ちる
        (_OWNER_A, "9861", False),
        (DEFAULT_OWNER, "2914", False),
        (DEFAULT_OWNER, "9861", False),
    ],
)
def test_all_three_paths_use_the_same_owner_scope(
    two_owner_world, owner: str, stock_code: str, expected_held: bool
) -> None:
    result = _classify_all_paths(two_owner_world, owner, stock_code)
    assert result == {
        "before_after": expected_held,
        "compare": expected_held,
        "backtest": expected_held,
    }


def test_non_held_row_is_a_real_non_holding_not_a_cross_owner_miss(two_owner_world) -> None:
    """owner-bで2914を引くと「未登録」になる(owner-aの保有をowner-bのものとして扱わない)。"""
    result = _classify_all_paths(two_owner_world, _OWNER_B, "2914")
    assert set(result.values()) == {False}


# ===== 仮の保有(placeholder / --purchase-price等)が指定ownerのholding_idを名乗る =====


def test_placeholder_holding_uses_the_given_owner() -> None:
    holding = placeholder_holding("2914", _NOW, _OWNER_B)
    assert holding.owner == _OWNER_B
    assert holding.holding_id == build_holding_id(_OWNER_B, "2914")


def test_placeholder_holding_normalizes_owner() -> None:
    holding = placeholder_holding("2914", _NOW, "  owner-b ")
    assert holding.owner == _OWNER_B
    assert holding.holding_id == build_holding_id(_OWNER_B, "2914")


def test_build_holding_override_uses_the_given_owner() -> None:
    holding = hd_cli._build_holding_override("2914", "500", "2026-01-01", "200", _NOW, _OWNER_B)
    assert holding.owner == _OWNER_B
    assert holding.holding_id == build_holding_id(_OWNER_B, "2914")


def test_live_comparison_rejects_override_of_a_different_owner(store_dir: Path) -> None:
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    foreign_override = _holding(_OWNER_A, "2914")
    with pytest.raises(ValueError, match="ownerが、指定されたownerと一致しません"):
        run_live_comparison(
            ["2914"],
            _PROVIDERS,
            _CFG,
            _NOW,
            _OWNER_B,
            portfolio_service=portfolio,
            holding_overrides={"2914": foreign_override},
        )


def test_live_comparison_accepts_override_of_the_same_owner(store_dir: Path) -> None:
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    override = _holding(_OWNER_B, "2914")
    rows = run_live_comparison(
        ["2914"],
        _PROVIDERS,
        _CFG,
        _NOW,
        _OWNER_B,
        portfolio_service=portfolio,
        holding_overrides={"2914": override},
    )
    assert rows[0].legacy_recommendation_type != _NOT_HELD


# ===== service層のowner検証(不正なownerは握りつぶさず例外) =====


@pytest.mark.parametrize("bad_owner", ["", "a#b"])
def test_services_reject_invalid_owner(two_owner_world, bad_owner: str) -> None:
    portfolio, before_after = two_owner_world
    with pytest.raises(InvalidOwnerError):
        before_after.build_entry("2914", _NOW, bad_owner)
    with pytest.raises(InvalidOwnerError):
        run_compare(["2914"], _PROVIDERS, _CFG, _NOW, bad_owner, portfolio_service=portfolio)
    with pytest.raises(InvalidOwnerError):
        run_live_comparison(
            ["2914"], _PROVIDERS, _CFG, _NOW, bad_owner, portfolio_service=portfolio
        )
    with pytest.raises(InvalidOwnerError):
        placeholder_holding("2914", _NOW, bad_owner)
    # 全銘柄がデータ取得に失敗する入力でも、不正なownerは握りつぶされない。
    with pytest.raises(InvalidOwnerError):
        run_compare(["0000"], _PROVIDERS, _CFG, _NOW, bad_owner, portfolio_service=portfolio)


def test_service_functions_have_no_owner_default() -> None:
    """service層はownerを必須引数にする(DEFAULT_OWNERへ解決するのはCLI層だけ。USER契約)。"""
    import inspect

    for fn in (
        run_compare,
        run_live_comparison,
        placeholder_holding,
        resolve_target_stock_codes,
        BeforeAfterReportService.build_entry,
        BeforeAfterReportService.build_report,
    ):
        param = inspect.signature(fn).parameters["owner"]
        assert param.default is inspect.Parameter.empty, fn.__qualname__


# ===== CLI(--owner。既定はDEFAULT_OWNER) =====
# tests/conftest.pyのautouse fixtureにより、CLIが使う既定storeはtmp_pathへ隔離される。


def _cli_seed() -> None:
    repo = HoldingRepository()
    _seed(
        repo,
        {_OWNER_A: ["1111", "2222"], _OWNER_B: ["3333"], DEFAULT_OWNER: ["4444"]},
    )


def _stock_codes_in(output: str) -> set[str]:
    return {code for code in ("1111", "2222", "3333", "4444") if code in output}


@pytest.mark.parametrize(
    ("args", "expected_codes"),
    [
        (["--owner", _OWNER_A], {"1111", "2222"}),
        (["--owner", _OWNER_B], {"3333"}),
        ([], {"4444"}),  # --owner省略 = DEFAULT_OWNERの保有のみ
    ],
)
@pytest.mark.parametrize("command", ["backtest", "compare"])
def test_cli_enumeration_is_scoped_to_the_owner(
    command: str, args: list[str], expected_codes: set[str]
) -> None:
    _cli_seed()
    result = _runner.invoke(hd_cli.app, [command, "--source", "mock", *args])
    assert result.exit_code == 0, result.output
    # 他ownerの銘柄が行として混入しない(かつ「未登録」として誤って並ばない)。
    assert _stock_codes_in(result.output) == expected_codes


def test_cli_explicit_stock_code_is_evaluated_as_the_given_owners_holding() -> None:
    """明示的に銘柄を指定した経路の回帰: 指定ownerの保有として評価される(従来どおり)。"""
    _cli_seed()
    result = _runner.invoke(
        hd_cli.app, ["backtest", "--source", "mock", "--stock-code", "3333", "--owner", _OWNER_B]
    )
    assert result.exit_code == 0, result.output
    assert _stock_codes_in(result.output) == {"3333"}


def test_cli_reports_empty_target_with_the_owner_name() -> None:
    _cli_seed()
    result = _runner.invoke(hd_cli.app, ["compare", "--source", "mock", "--owner", "owner-c"])
    assert result.exit_code == 1
    assert "owner-c" in result.output
    assert "対象銘柄がありません" in result.output


def test_cli_before_after_scopes_the_lookup_to_the_owner(tmp_path: Path) -> None:
    """before-after: 同じ--stocksでも、--ownerによって保有lookupのscopeが変わる。"""
    repo = HoldingRepository()
    _seed(repo, {_OWNER_B: ["9861"]})
    out_b = tmp_path / "b.md"
    out_default = tmp_path / "default.md"
    base = ["before-after", "--stocks", "9861", "--source", "mock"]

    result_b = _runner.invoke(review_cli.app, [*base, "--owner", _OWNER_B, "--output", str(out_b)])
    result_default = _runner.invoke(review_cli.app, [*base, "--output", str(out_default)])

    assert result_b.exit_code == 0, result_b.output
    assert result_default.exit_code == 0, result_default.output
    not_held_marker = "保有銘柄として登録されていない"
    assert not_held_marker not in out_b.read_text(encoding="utf-8")
    # --owner省略 = DEFAULT_OWNER。owner-bの保有はDEFAULT_OWNERの保有として扱われない。
    assert not_held_marker in out_default.read_text(encoding="utf-8")
