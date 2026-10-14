"""Issue #603 / PR-F1: 購入側 Shadow の入力の組み立て `assemble_allocation_inputs` の契約テスト。

純粋関数だけで、時計・I/O・永続化・配線に触れない。どこからも import されない(dormant)。

## 何を固定するのか

```
・候補: BUY 系だけが配分の候補。除外は理由つきの件数(重複・買い判定なし・BUY 系でない・
  価格なし・売買単位の不一致・保有の評価額が不明)。段階は買い判定から 1 対 1。入力の並びに依らない
・保有: portfolio_value は『全保有に価格があるときだけ Σ(株数 × 価格)』。
  1 件でも価格が無ければ None(補完しない)。保有 0 件は 0 + NO_HOLDINGS(価格欠測とは別の理由)
・業種: BuyIndustrySector の 1 表現(UNKNOWN = 不明)。1 件でも不明なら sector_exposure = None
・high-risk: 全保有の high-risk の語の観測がそろうときだけ数値。1 件でも観測できない
  (語の未観測・推奨なし)なら None。plan A(未観測を high-risk と数える)ではなく承認資料 B
・買付余力: None = 未登録(0 円ではない)。最終棚卸しからの日数は JST の暦日の差
・例外は入力の矛盾(型違反・owner の混在・保有の重複)だけで、メッセージに値を含めない。
  報告は件数と理由だけ
```

## 独立な参照実装

期待値は組み立ての式から導かない。保有の合計・業種別・high-risk 額は、テスト側で書き下した定義
(株数 × 価格を足す)で求める。
"""

from __future__ import annotations

import ast
import datetime as dt
import itertools
import random
import re
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    BuyAction,
    BuyIndustrySector,
    ConfidenceLevel,
)
from jstock_advisor.domain.signals import allocation_inputs as ai
from jstock_advisor.domain.signals import portfolio_allocation_types as pt
from jstock_advisor.domain.signals.allocation_inputs import (
    AllocationInputsError,
    CandidateExclusionReason,
    CandidateRecord,
    CashInput,
    HoldingRecord,
    IncompleteReason,
    PortfolioValueBasis,
    RiskNotRecorded,
    RiskObserved,
    assemble_allocation_inputs,
)
from jstock_advisor.domain.signals.allocation_policy_config import (
    AllocationPolicyBundle,
    load_allocation_policy,
)
from jstock_advisor.domain.valuation.raer import (
    MODEL_VERSION,
    AxisCode,
    RaerResult,
    RaerState,
    RiskFlag,
    make_comparison_key,
)

D = Decimal
_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "domain" / "signals" / "allocation_inputs.py"
_JST = dt.timezone(dt.timedelta(hours=9))
_NOW = dt.datetime(2026, 10, 14, 9, 5, tzinfo=_JST)
_ALL_AXES = frozenset(a.value for a in AxisCode)
OWNER = "owner-a"
UNIT = 100
KEY = make_comparison_key(MODEL_VERSION, _ALL_AXES)
HIGH_RISK = frozenset(
    {
        RiskFlag.MAJOR_CUSTOMER_DEPENDENCY.value,
        RiskFlag.TEMPORARY_EARNINGS_BOOST_RISK.value,
        RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY.value,
    }
)
S = BuyIndustrySector


# --- 部品(合成データ)-----------------------------------------------------------------------


def raer(value: str = "20", state: RaerState = RaerState.AVAILABLE) -> RaerResult:
    if state is RaerState.UNAVAILABLE:
        return RaerResult(
            state=state,
            value=None,
            confidence=None,
            risk_flags=frozenset(),
            risk_flags_available=False,
            model_version=MODEL_VERSION,
            comparison_key=None,
            axes_applied=frozenset(),
            axes_not_applied=_ALL_AXES,
            reason_codes=("COMPOSITION_RULE_NOT_DECIDED",),
        )
    return RaerResult(
        state=state,
        value=D(value),
        confidence=ConfidenceLevel.MEDIUM,
        risk_flags=frozenset(),
        risk_flags_available=True,
        model_version=MODEL_VERSION,
        comparison_key=KEY,
        axes_applied=_ALL_AXES,
        axes_not_applied=frozenset(),
        reason_codes=(),
    )


def cand(code: str = "0002", **over: Any) -> CandidateRecord:
    base: dict[str, Any] = {
        "stock_code": code,
        "buy_action": BuyAction.BUY,
        "current_price": D("1000"),
        "sector": S.GENERAL,
        "trading_unit": None,
        "raer": raer(),
    }
    base.update(over)
    return CandidateRecord(**base)


def observed(
    flags: frozenset[str] = frozenset(), unobserved: frozenset[str] = frozenset()
) -> RiskObserved:
    return RiskObserved(flags=flags, unobserved=unobserved)


def hold(code: str = "9001", **over: Any) -> HoldingRecord:
    base: dict[str, Any] = {
        "owner": OWNER,
        "stock_code": code,
        "shares": 100,
        "current_price": D("1000"),
        "sector": S.GENERAL,
        "risk": observed(),
    }
    base.update(over)
    return HoldingRecord(**base)


_BUNDLE_CACHE: dict[str, AllocationPolicyBundle] = {}


def _policy_with_key(directory: Path, comparison_key: str | None) -> AllocationPolicyBundle:
    """出荷の方針の comparison_key だけを差し替えた版(出荷の config は不変)。None は未決(null)。"""
    text = (_REPO / "config" / "allocation_policy.yaml").read_text(encoding="utf-8")
    value = "null" if comparison_key is None else f'"{comparison_key}"'
    text, count = re.subn(r"(?m)^comparison_key:.*$", f"comparison_key: {value}", text)
    assert count == 1
    (directory / "allocation_policy.yaml").write_text(text, encoding="utf-8")
    return load_allocation_policy(directory, minimum_trading_unit=UNIT)


_UNDECIDED_DIR: list[Path] = []


def bundle(
    tmp_path: Path | None = None, comparison_key: str | None = None
) -> AllocationPolicyBundle:
    """鍵を決めた版は一時の config から読む(出荷は不変)。鍵を渡さない版は未決(null)の方針。

    #602 PR-G2 で出荷の方針へ鍵を設定したため、未決の日を確かめるテストは null の版を使う。
    """
    if comparison_key is None:
        if not _UNDECIDED_DIR:
            _UNDECIDED_DIR.append(Path(tempfile.mkdtemp(prefix="undecided_policy_")))
        return _policy_with_key(_UNDECIDED_DIR[0], None)
    assert tmp_path is not None
    return _policy_with_key(tmp_path, comparison_key)


def run(
    candidates: list[CandidateRecord] | None = None,
    holdings: list[HoldingRecord] | None = None,
    cash: CashInput | None = None,
    policy: AllocationPolicyBundle | None = None,
    now: dt.datetime = _NOW,
) -> ai.AllocationInputs:
    return assemble_allocation_inputs(
        owner=OWNER,
        now=now,
        candidates=candidates if candidates is not None else [cand()],
        holdings=holdings if holdings is not None else [],
        cash=cash if cash is not None else CashInput(D("1000000"), _NOW),
        bundle=policy if policy is not None else bundle(),
    )


# --- 段階: BUY 系の買い判定から 1 対 1 ----------------------------------------------------


@pytest.mark.parametrize(
    ("action", "tier"),
    [
        (BuyAction.STRONG_BUY, pt.PriceTier.STRONG),
        (BuyAction.BUY, pt.PriceTier.STANDARD),
        (BuyAction.SMALL_ENTRY, pt.PriceTier.ENTRY),
    ],
)
def test_the_price_tier_follows_the_buy_action(action: BuyAction, tier: pt.PriceTier) -> None:
    result = run([cand(buy_action=action)])

    assert [c.price_tier for c in result.candidates] == [tier]
    assert result.report.exclusions == {}


def test_the_tier_mapping_covers_every_buy_family_action() -> None:
    """BUY 系が増えたら落ちる(段階を決められない候補を黙って作らない)。"""
    assert set(ai._TIER_OF_ACTION) == set(BUY_FAMILY_ACTIONS)


# --- 候補の除外(理由つきの件数)-----------------------------------------------------------


def test_only_the_buy_family_is_a_candidate_and_the_others_are_counted_by_reason() -> None:
    non_buy = [a for a in BuyAction if a not in BUY_FAMILY_ACTIONS]
    records = [cand("0001"), cand("0002", buy_action=None)] + [
        cand(f"1{i:03d}", buy_action=a) for i, a in enumerate(non_buy)
    ]

    result = run(records)

    assert [c.stock_code for c in result.candidates] == ["0001"]
    assert result.report.exclusions == {
        CandidateExclusionReason.ACTION_MISSING: 1,
        CandidateExclusionReason.NOT_BUY_FAMILY: len(non_buy),
    }
    assert result.report.candidate_records == len(records)
    assert result.report.candidates == 1


@pytest.mark.parametrize("price", [None, D("0"), D("-1")])
def test_a_missing_or_non_positive_price_excludes_the_candidate(price: Decimal | None) -> None:
    result = run([cand(current_price=price)])

    assert result.candidates == ()
    assert result.report.exclusions == {CandidateExclusionReason.PRICE_UNAVAILABLE: 1}


def test_a_different_trading_unit_excludes_and_a_missing_unit_does_not() -> None:
    result = run([cand("0001", trading_unit=UNIT), cand("0002", trading_unit=1), cand("0003")])

    assert [c.stock_code for c in result.candidates] == ["0001", "0003"]
    assert result.report.exclusions == {CandidateExclusionReason.TRADING_UNIT_MISMATCH: 1}


def test_a_candidate_without_a_trading_unit_gets_the_policy_unit_and_is_counted() -> None:
    """売買単位が無い候補は方針の売買単位を適用する(黙って補完せず、適用した件数を残す)。"""
    result = run(
        [cand("0001"), cand("0002", trading_unit=UNIT), cand("0003"), cand("0004", trading_unit=1)]
    )

    assert [c.stock_code for c in result.candidates] == ["0001", "0002", "0003"]
    assert result.report.trading_unit_defaulted == 2
    assert result.report.exclusions == {CandidateExclusionReason.TRADING_UNIT_MISMATCH: 1}


def test_an_excluded_candidate_is_not_counted_as_defaulted() -> None:
    result = run([cand("0001", current_price=None), cand("0002", buy_action=None)])

    assert result.report.trading_unit_defaulted == 0


def test_a_duplicated_stock_code_excludes_every_record_of_it() -> None:
    result = run([cand("0001"), cand("0001", raer=raer("30")), cand("0002")])

    assert [c.stock_code for c in result.candidates] == ["0002"]
    assert result.report.exclusions == {CandidateExclusionReason.DUPLICATE_STOCK_CODE: 2}


def test_each_record_is_counted_once_with_the_first_matching_reason() -> None:
    """重複 > 買い判定なし > BUY 系でない > 価格なし > 売買単位の順。1 件を 2 回数えない。"""
    records = [
        cand("0001"),
        cand("0001"),
        cand("0002", buy_action=None, current_price=None),
        cand("0003", buy_action=BuyAction.WATCH_FOR_PRICE, current_price=None),
        cand("0004", current_price=None, trading_unit=1),
    ]

    result = run(records)

    assert result.report.exclusions == {
        CandidateExclusionReason.DUPLICATE_STOCK_CODE: 2,
        CandidateExclusionReason.ACTION_MISSING: 1,
        CandidateExclusionReason.NOT_BUY_FAMILY: 1,
        CandidateExclusionReason.PRICE_UNAVAILABLE: 1,
    }
    assert sum(result.report.exclusions.values()) + result.report.candidates == len(records)


def test_the_candidates_are_sorted_by_stock_code_whatever_the_input_order() -> None:
    codes = ["0005", "0001", "0003", "0002", "0004"]
    expected = sorted(codes)
    for order in itertools.islice(itertools.permutations(codes), 24):
        assert [c.stock_code for c in run([cand(code) for code in order]).candidates] == expected


def test_an_unknown_candidate_sector_is_none_and_a_known_one_is_the_value() -> None:
    result = run([cand("0001", sector=S.UNKNOWN), cand("0002", sector=S.BANK)])

    assert [(c.stock_code, c.sector) for c in result.candidates] == [
        ("0001", None),
        ("0002", "BANK"),
    ]


def test_the_raer_is_passed_through_untouched_even_when_unavailable() -> None:
    unavailable = raer(state=RaerState.UNAVAILABLE)

    result = run([cand("0001", raer=unavailable), cand("0002", raer=raer("11"))])

    assert result.candidates[0].raer is unavailable
    assert result.candidates[1].raer.value == D("11")


# --- 種類と評価額 --------------------------------------------------------------------------


def test_a_held_candidate_is_an_additional_buy_with_the_value_of_the_holding() -> None:
    result = run(
        [cand("9001", current_price=D("1200")), cand("0002")],
        holdings=[hold("9001", shares=300, current_price=D("1000"))],
    )

    by = {c.stock_code: c for c in result.candidates}
    assert by["9001"].kind is pt.CandidateKind.ADDITIONAL_BUY
    assert by["9001"].current_position_value == D("300000")  # 株数 × 保有の価格(候補の価格ではない)
    assert by["0002"].kind is pt.CandidateKind.NEW_BUY
    assert by["0002"].current_position_value == D("0")
    assert result.report.owner_holds_candidate == 1


def test_a_held_candidate_with_an_unpriced_holding_is_excluded_not_valued_at_its_own_price() -> (
    None
):
    result = run(
        [cand("9001")],
        holdings=[hold("9001", current_price=None)],
    )

    assert result.candidates == ()
    assert result.report.exclusions == {CandidateExclusionReason.HOLDING_VALUE_UNAVAILABLE: 1}


# --- ポートフォリオ: 評価額・業種別・high-risk(独立な参照実装)---------------------------------


def reference(holdings: list[HoldingRecord]) -> tuple[Decimal, dict[str, Decimal], Decimal]:
    total = D(0)
    sectors: dict[str, Decimal] = {}
    high = D(0)
    for h in holdings:
        value = h.current_price * h.shares  # type: ignore[operator]
        total += value
        sectors[h.sector.value] = sectors.get(h.sector.value, D(0)) + value
        assert isinstance(h.risk, RiskObserved)
        if h.risk.flags & HIGH_RISK:
            high += value
    return total, sectors, high


def test_the_portfolio_is_the_sum_of_shares_times_price_with_its_sector_and_high_risk_parts() -> (
    None
):
    holdings = [
        hold("9001", shares=100, current_price=D("1500"), sector=S.BANK),
        hold(
            "9002",
            shares=200,
            current_price=D("2500.5"),
            sector=S.BANK,
            risk=observed(flags=frozenset({RiskFlag.SMALL_CAP_OR_LOW_LIQUIDITY.value})),
        ),
        hold(
            "9003",
            shares=300,
            current_price=D("400"),
            sector=S.FOOD,
            risk=observed(
                flags=frozenset({RiskFlag.CYCLICAL_INDUSTRY.value})
            ),  # high-risk の語ではない
        ),
    ]
    total, sectors, high = reference(holdings)

    result = run(holdings=holdings)

    assert result.portfolio_value == total
    assert dict(result.sector_exposure or {}) == sectors
    assert result.report.holdings == len(holdings)
    assert result.high_risk_exposure == high == D("500100.0")
    assert result.report.portfolio_value_basis is PortfolioValueBasis.COMPLETE
    assert result.report.incomplete_reasons == ()


def test_random_portfolios_match_the_reference() -> None:
    rng = random.Random(603)
    sectors = [S.BANK, S.FOOD, S.GENERAL, S.UTILITY]
    for _ in range(200):
        count = rng.randint(1, 8)
        holdings = [
            hold(
                f"9{i:03d}",
                shares=rng.randint(1, 50) * 100,
                current_price=D(rng.randint(100, 90000)) / D(rng.choice([1, 10, 100])),
                sector=rng.choice(sectors),
                risk=observed(
                    flags=frozenset(rng.sample(sorted(HIGH_RISK), rng.randint(0, 2))),
                    unobserved=frozenset({RiskFlag.EARNINGS_WITHIN_3_BUSINESS_DAYS.value}),
                ),
            )
            for i in range(count)
        ]
        total, by_sector, high = reference(holdings)

        result = run(holdings=holdings)

        assert result.portfolio_value == total
        assert dict(result.sector_exposure or {}) == by_sector
        assert result.high_risk_exposure == high
        assert sum(by_sector.values(), D(0)) == total


def test_no_holdings_is_a_zero_portfolio_with_its_own_reason_not_a_price_gap() -> None:
    result = run(holdings=[])

    assert result.portfolio_value == D("0")
    assert result.sector_exposure is not None  # 空の保有は『業種別 = 空』で、不明(None)ではない
    assert dict(result.sector_exposure) == {}
    assert result.report.holdings == 0
    assert result.high_risk_exposure == D("0")
    assert result.report.portfolio_value_basis is PortfolioValueBasis.NO_HOLDINGS
    assert result.report.incomplete_reasons == (IncompleteReason.NO_HOLDINGS,)


@pytest.mark.parametrize("price", [None, D("0"), D("-5")])
def test_one_holding_without_a_price_makes_every_portfolio_figure_unknown(
    price: Decimal | None,
) -> None:
    result = run(holdings=[hold("9001"), hold("9002", current_price=price)])

    assert result.portfolio_value is None
    assert result.sector_exposure is None
    assert result.high_risk_exposure is None
    assert result.report.portfolio_value_basis is PortfolioValueBasis.PRICE_MISSING
    assert result.report.holdings_price_missing == 1
    assert IncompleteReason.HOLDING_PRICE_MISSING in result.report.incomplete_reasons


def test_one_unknown_sector_holding_makes_the_sector_exposure_unknown_but_keeps_the_total() -> None:
    holdings = [hold("9001"), hold("9002", sector=S.UNKNOWN)]
    total, _, high = reference(holdings)

    result = run(holdings=holdings)

    assert result.sector_exposure is None
    assert result.portfolio_value == total
    assert result.high_risk_exposure == high
    assert result.report.holdings_sector_unknown == 1
    assert result.report.incomplete_reasons == (IncompleteReason.HOLDING_SECTOR_UNKNOWN,)


# --- high-risk: 承認資料 B のとおり(plan A を採らない)--------------------------------------


@pytest.mark.parametrize("word", sorted(HIGH_RISK))
def test_an_unobserved_high_risk_word_makes_the_high_risk_exposure_unknown(word: str) -> None:
    result = run(holdings=[hold("9001"), hold("9002", risk=observed(unobserved=frozenset({word})))])

    assert result.high_risk_exposure is None
    assert result.report.holdings_risk_unknown == 1
    assert result.report.incomplete_reasons == (IncompleteReason.HOLDING_RISK_UNKNOWN,)
    # 評価額・業種別は影響を受けない
    assert result.portfolio_value == D("200000")
    assert result.sector_exposure is not None


def test_a_holding_without_a_recommendation_makes_the_high_risk_exposure_unknown() -> None:
    result = run(holdings=[hold("9001"), hold("9002", risk=RiskNotRecorded())])

    assert result.high_risk_exposure is None
    assert result.report.holdings_risk_unknown == 1


def test_unobserved_words_outside_the_high_risk_set_do_not_matter() -> None:
    outside = frozenset(set(w.value for w in RiskFlag) - HIGH_RISK)
    holdings = [hold("9001", risk=observed(unobserved=outside)), hold("9002")]

    result = run(holdings=holdings)

    assert result.high_risk_exposure == D("0")
    assert result.report.holdings_risk_unknown == 0


def test_unknown_risk_is_never_counted_as_high_risk_nor_as_zero() -> None:
    """『観測できない』を 0・high-risk・全額のどれにも写さない(None のまま)。"""
    result = run(holdings=[hold("9001", risk=RiskNotRecorded())])

    assert result.high_risk_exposure is None
    assert result.high_risk_exposure != result.portfolio_value
    assert result.high_risk_exposure != D(0)


def test_the_high_risk_words_come_from_the_policy_not_from_the_assembler() -> None:
    wider = {RiskFlag.CYCLICAL_INDUSTRY.value} | HIGH_RISK
    holding = hold("9001", risk=observed(flags=frozenset({RiskFlag.CYCLICAL_INDUSTRY.value})))

    default = run(holdings=[holding])
    base = bundle()
    custom = AllocationPolicyBundle(
        constraints=pt.AllocationConstraints(
            max_single_stock_amount=base.constraints.max_single_stock_amount,
            max_single_stock_ratio=base.constraints.max_single_stock_ratio,
            max_sector_ratio=base.constraints.max_sector_ratio,
            max_high_risk_ratio=base.constraints.max_high_risk_ratio,
            high_risk_flags=frozenset(wider),
            minimum_trading_unit=UNIT,
        ),
        policy=base.policy,
        policy_id=base.policy_id,
        unknown_price_tier_as=base.unknown_price_tier_as,
        config_hash=base.config_hash,
        source=base.source,
    )

    assert default.high_risk_exposure == D("0")
    assert run(holdings=[holding], policy=custom).high_risk_exposure == D("100000")


# --- 買付余力と最終棚卸し日数(JST の暦日)------------------------------------------------


def test_an_unregistered_cash_is_none_and_zero_yen_is_a_registered_state() -> None:
    unregistered = run(cash=CashInput(None, None))
    zero = run(cash=CashInput(D("0"), _NOW))

    assert unregistered.available_cash is None
    assert IncompleteReason.CASH_NOT_REGISTERED in unregistered.report.incomplete_reasons
    assert zero.available_cash == D("0")
    assert IncompleteReason.CASH_NOT_REGISTERED not in zero.report.incomplete_reasons


@pytest.mark.parametrize(
    ("reconciled", "now", "days"),
    [
        # JST の暦日の差(UTC の日付ではない)
        (
            dt.datetime(2026, 10, 13, 23, 59, tzinfo=_JST),
            dt.datetime(2026, 10, 14, 0, 0, tzinfo=_JST),
            1,
        ),
        (
            dt.datetime(2026, 10, 14, 0, 0, tzinfo=_JST),
            dt.datetime(2026, 10, 14, 23, 59, tzinfo=_JST),
            0,
        ),
        # UTC では同じ日付でも JST では 1 日違う(JST の 00:00 の境界)
        (
            dt.datetime(2026, 10, 13, 14, 59, tzinfo=dt.UTC),  # JST 23:59(10/13)
            dt.datetime(2026, 10, 13, 15, 0, tzinfo=dt.UTC),  # JST 00:00(10/14)
            1,
        ),
        (
            dt.datetime(2026, 10, 13, 15, 0, tzinfo=dt.UTC),  # JST 00:00(10/14)
            dt.datetime(2026, 10, 14, 14, 59, tzinfo=dt.UTC),  # JST 23:59(10/14)
            0,
        ),
        (
            dt.datetime(2026, 10, 5, 12, 0, tzinfo=_JST),
            dt.datetime(2026, 10, 14, 9, 5, tzinfo=_JST),
            9,
        ),
    ],
)
def test_the_cash_age_is_the_difference_of_jst_calendar_dates(
    reconciled: dt.datetime, now: dt.datetime, days: int
) -> None:
    result = run(cash=CashInput(D("1"), reconciled), now=now)

    assert result.report.cash_age_days == days


def test_an_unreconciled_or_future_cash_has_no_age_and_never_stops_the_assembly() -> None:
    never = run(cash=CashInput(D("1"), None))
    future = run(cash=CashInput(D("1"), _NOW + dt.timedelta(days=2)))

    assert never.report.cash_age_days is None
    assert future.report.cash_age_days is None
    assert never.available_cash == future.available_cash == D("1")


def test_a_naive_clock_or_naive_reconciliation_time_is_rejected() -> None:
    with pytest.raises(ValueError):
        run(now=dt.datetime(2026, 10, 14, 9, 5))
    with pytest.raises(AllocationInputsError):
        CashInput(D("1"), dt.datetime(2026, 10, 14, 9, 5))


# --- 算出不能の理由の並び -----------------------------------------------------------------


def test_the_incomplete_reasons_are_listed_in_a_fixed_order() -> None:
    result = run(
        holdings=[
            hold("9001", current_price=None, sector=S.UNKNOWN, risk=RiskNotRecorded()),
        ],
        cash=CashInput(None, None),
    )

    assert result.report.incomplete_reasons == (
        IncompleteReason.CASH_NOT_REGISTERED,
        IncompleteReason.HOLDING_PRICE_MISSING,
        IncompleteReason.HOLDING_SECTOR_UNKNOWN,
        IncompleteReason.HOLDING_RISK_UNKNOWN,
    )


def test_a_complete_input_has_no_incomplete_reason() -> None:
    assert run(holdings=[hold()]).report.incomplete_reasons == ()


# --- 入力の矛盾 = 例外(値を含めない)---------------------------------------------------------


def test_a_holding_of_another_owner_is_rejected() -> None:
    with pytest.raises(AllocationInputsError):
        run(holdings=[hold("9001"), hold("9002", owner="owner-b")])


def test_the_same_stock_held_twice_is_rejected() -> None:
    with pytest.raises(AllocationInputsError):
        run(holdings=[hold("9001"), hold("9001", shares=200)])


@pytest.mark.parametrize(
    "build",
    [
        lambda: cand(current_price=1000.0),
        lambda: cand(current_price=D("NaN")),
        lambda: cand(current_price=D("Infinity")),
        lambda: cand(stock_code=""),
        lambda: cand(stock_code=None),
        lambda: cand(buy_action="BUY"),
        lambda: cand(sector="GENERAL"),
        lambda: cand(trading_unit=0),
        lambda: cand(trading_unit=True),
        lambda: cand(raer=None),
        lambda: hold(shares=0),
        lambda: hold(shares=True),
        lambda: hold(shares=1.5),
        lambda: hold(current_price=1000),
        lambda: hold(owner=""),
        lambda: hold(sector="GENERAL"),
        lambda: hold(risk=None),
        lambda: hold(risk=frozenset()),
        lambda: RiskObserved(flags={"a"}, unobserved=frozenset()),  # type: ignore[arg-type]
        lambda: RiskObserved(flags=frozenset({"a"}), unobserved=frozenset({"a"})),
        lambda: CashInput(D("-1"), None),
        lambda: CashInput(1000, None),  # type: ignore[arg-type]
        lambda: CashInput(D("1"), "2026-10-14"),  # type: ignore[arg-type]
    ],
)
def test_a_malformed_record_is_rejected(build: Any) -> None:
    with pytest.raises(AllocationInputsError):
        build()


def test_the_assembler_rejects_the_wrong_argument_types() -> None:
    good = {
        "owner": OWNER,
        "now": _NOW,
        "candidates": [cand()],
        "holdings": [],
        "cash": CashInput(D("1"), None),
        "bundle": bundle(),
    }
    for key, wrong in (("cash", None), ("bundle", None), ("owner", ""), ("candidates", [object()])):
        with pytest.raises(AllocationInputsError):
            assemble_allocation_inputs(**{**good, key: wrong})  # type: ignore[arg-type]
    with pytest.raises(AllocationInputsError):
        assemble_allocation_inputs(**{**good, "holdings": [object()]})  # type: ignore[arg-type]


def test_an_error_never_echoes_a_value() -> None:
    sentinels = ["sentinel-owner-zz", "SENTINEL9999", "123456789.123"]
    attempts: list[Callable[[], object]] = [
        lambda: run(holdings=[hold("SENTINEL9999", owner="sentinel-owner-zz")]),
        lambda: run(holdings=[hold("SENTINEL9999"), hold("SENTINEL9999", shares=300)]),
        lambda: cand("SENTINEL9999", current_price=123456789.123),
        lambda: hold("SENTINEL9999", shares=-123456),
    ]
    for attempt in attempts:
        with pytest.raises(AllocationInputsError) as caught:
            attempt()
        message = str(caught.value)
        assert not any(s in message for s in sentinels)
        assert not any(ch.isdigit() for ch in message), message


def test_the_error_is_built_in_exactly_one_place() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AllocationInputsError"
    ]
    assert len(constructions) == 1


# --- 報告に値を含めない --------------------------------------------------------------------


def test_the_report_holds_only_counts_and_reasons() -> None:
    result = run(
        [cand("0001"), cand("0002", current_price=None)],
        holdings=[hold("0001", shares=777, current_price=D("123.45"))],
    )
    report = result.report

    def walk(value: Any) -> list[Any]:
        if isinstance(value, dict | type(report.exclusions)):
            return [x for k, v in value.items() for x in (*walk(k), *walk(v))]
        if isinstance(value, tuple):
            return [x for v in value for x in walk(v)]
        return [value]

    for leaf in walk(vars(report)):
        assert isinstance(
            leaf,
            int
            | str
            | type(None)
            | CandidateExclusionReason
            | IncompleteReason
            | PortfolioValueBasis,
        )
        assert not isinstance(leaf, Decimal | float)
    flat = repr(report)
    assert "0001" not in flat and "777" not in flat and "123.45" not in flat and OWNER not in flat


# --- 決定性・不変性 -----------------------------------------------------------------------


def test_the_same_input_gives_the_same_output_whatever_the_order() -> None:
    holdings = [
        hold("9001", shares=100, current_price=D("1500"), sector=S.BANK),
        hold("9002", shares=200, current_price=D("2500"), sector=S.FOOD),
        hold("9003", shares=300, current_price=D("400"), sector=S.UTILITY),
    ]
    candidates = [cand("0001"), cand("9001"), cand("0003", sector=S.BANK)]
    base = run(candidates, holdings)

    for hs in itertools.permutations(holdings):
        for cs in itertools.permutations(candidates):
            other = run(list(cs), list(hs))
            assert other.candidates == base.candidates
            assert other.portfolio_value == base.portfolio_value
            assert dict(other.sector_exposure or {}) == dict(base.sector_exposure or {})
            assert other.high_risk_exposure == base.high_risk_exposure
            assert other.report == base.report


def test_the_outputs_are_frozen() -> None:
    result = run(holdings=[hold()])

    with pytest.raises(AttributeError):
        result.portfolio_value = D("1")  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.sector_exposure["GENERAL"] = D("1")  # type: ignore[index]
    with pytest.raises(TypeError):
        result.report.exclusions[CandidateExclusionReason.PRICE_UNAVAILABLE] = 1  # type: ignore[index]


# --- 結合: assemble -> optimize_allocation(承認資料 B のとおりの結果)----------------------------


def test_an_undecided_comparison_key_allocates_nothing() -> None:
    result = run([cand()], holdings=[hold()]).optimize(bundle())

    assert result.status is pt.AllocationStatus.UNAVAILABLE
    assert result.unavailable_reason is pt.UnavailableReason.POLICY_INCOMPLETE
    assert result.total_allocated == D(0)


def test_with_a_decided_key_a_complete_input_allocates_within_every_cap(tmp_path: Path) -> None:
    decided = bundle(tmp_path, KEY)
    # 評価額 10,000,000 の保有(銀行)。候補は別の業種(一般)で、業種・high-risk の上限は効かない
    holdings = [hold("9001", shares=10000, current_price=D("1000"), sector=S.BANK)]

    result = run(
        [cand("0002", sector=S.GENERAL)], holdings, CashInput(D("5000000"), _NOW), decided
    ).optimize(decided)

    assert result.status is pt.AllocationStatus.ALLOCATED_SOME
    allocated = [a for a in result.allocations if a.decision is pt.CandidateDecision.ALLOCATED]
    assert [a.stock_code for a in allocated] == ["0002"]
    only = allocated[0]
    # 独立な計算(式を写さず、定義から): 1 銘柄 15% は購入後の分母(総額 + 購入額)で見る
    #   購入額 x <= 0.15 × (10,000,000 + x) -> x <= 1,764,705.88
    #   -> 1,764 株 -> 単元 100 株の床 = 1,700 株
    #   段階 STANDARD = 67% -> 1,700 × 0.67 = 1,139 株 -> 単元の床 = 1,100 株
    assert only.shares == 1100
    assert only.amount == D("1100000")
    assert only.price_tier_used is pt.PriceTier.STANDARD
    assert only.amount <= D("5000000")  # 買付余力を超えない
    assert result.remaining_cash == D("5000000") - only.amount


def test_an_unobserved_holding_makes_the_whole_day_unavailable_end_to_end(tmp_path: Path) -> None:
    """high-risk の上限が効いている(0.20 < 1)ので、1 件の未観測で配分が 1 件も出ない。"""
    decided = bundle(tmp_path, KEY)
    holdings = [hold("9001"), hold("9002", risk=RiskNotRecorded())]

    result = run([cand("0002")], holdings, policy=decided).optimize(decided)

    assert result.status is pt.AllocationStatus.UNAVAILABLE
    assert result.unavailable_reason is pt.UnavailableReason.PORTFOLIO_VALUE_UNAVAILABLE
    assert result.total_allocated == D(0)


def test_an_unregistered_cash_is_unavailable_end_to_end(tmp_path: Path) -> None:
    decided = bundle(tmp_path, KEY)

    result = run([cand()], [hold()], CashInput(None, None), decided).optimize(decided)

    assert result.unavailable_reason is pt.UnavailableReason.CASH_NOT_REGISTERED


def test_no_holdings_is_unavailable_because_the_ratio_caps_have_no_base(tmp_path: Path) -> None:
    decided = bundle(tmp_path, KEY)

    result = run([cand()], [], policy=decided).optimize(decided)

    assert result.unavailable_reason is pt.UnavailableReason.PORTFOLIO_VALUE_UNAVAILABLE


def test_a_sector_cap_is_binding_for_a_candidate_in_the_held_sector(tmp_path: Path) -> None:
    """候補と保有の業種の語彙が同じなので、業種上限が『静かに効かない』ことはない。"""
    decided = bundle(tmp_path, KEY)
    # 保有 1,000,000 がすべて同じ業種 = 業種の比率は 100% > 30%(上限ちょうど以上)→ 候補は 0 株
    holdings = [hold("9001", shares=1000, current_price=D("1000"), sector=S.BANK)]

    result = run(
        [cand("0002", sector=S.BANK)], holdings, CashInput(D("5000000"), _NOW), decided
    ).optimize(decided)

    only = result.allocations[0]
    assert only.decision is pt.CandidateDecision.NOT_ALLOCATED
    assert only.reason_code is pt.ReasonCode.SECTOR_RATIO_CAP_APPLIED


def test_an_unknown_candidate_sector_is_a_reason_when_the_sector_cap_is_in_force(
    tmp_path: Path,
) -> None:
    decided = bundle(tmp_path, KEY)
    holdings = [hold("9001", shares=10000, current_price=D("1000"), sector=S.BANK)]

    result = run(
        [cand("0002", sector=S.UNKNOWN)], holdings, CashInput(D("5000000"), _NOW), decided
    ).optimize(decided)

    assert result.allocations[0].reason_code is pt.ReasonCode.SECTOR_UNKNOWN


# --- 構造(純粋・dormant)-----------------------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_the_module_imports_only_pure_domain_dependencies() -> None:
    imported = {
        node.module
        for node in ast.walk(_tree())
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(_tree())
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    allowed_roots = {
        "__future__",
        "datetime",
        "collections",
        "dataclasses",
        "decimal",
        "enum",
        "types",
        "typing",
    }
    for name in imported:
        root = name.split(".")[0]
        if root == "jstock_advisor":
            assert name.startswith("jstock_advisor.domain."), name
            assert not name.startswith(("jstock_advisor.domain.notification",)), name
        else:
            assert root in allowed_roots, name


def test_the_module_reads_no_clock_and_does_no_io() -> None:
    forbidden = {"now", "today", "utcnow", "open", "read_text", "write_text", "getenv", "sleep"}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in forbidden, name
        assert not (isinstance(node, ast.Name) and node.id == "logger")


def test_the_module_uses_no_float_and_no_aws_or_service_layer() -> None:
    source = _MODULE_PATH.read_text(encoding="utf-8")
    for token in (
        "boto3",
        "botocore",
        "services.",
        "infrastructure.",
        "config.loader",
        "config.models",
    ):
        assert token not in source, token
    for node in ast.walk(_tree()):
        assert not (isinstance(node, ast.Constant) and isinstance(node.value, float))
        assert not (isinstance(node, ast.Name) and node.id == "float")


def test_nothing_in_src_imports_the_assembler() -> None:
    """dormant: PR-F2-2 が配線するまで、本流・handler・CLI から import されない。"""
    importers = sorted(
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if path != _MODULE_PATH and "allocation_inputs" in path.read_text(encoding="utf-8")
    )
    # 許可は PR-F2-1 の記録の組み立て(入力の型を受ける。それ自体が dormant)の明示の 1 件だけ
    assert importers == ["domain/signals/allocation_shadow_record.py"]
