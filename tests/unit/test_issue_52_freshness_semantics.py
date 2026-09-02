"""Issue #52 Phase B1: データ鮮度semanticsの分離。

## 何が問題だったか

`DataSourceReference.fetched_at` が「APIを叩いた時刻」と「データが真である時点」の
両方の意味で使われていた。この未分離から、互いに反対方向の2つの誤動作が同時に出ていた。

1. 株主優待レジストリだけが「登録操作の時刻」を永続化するため、
   `min(s.fetched_at for s in data_sources)` がそれを拾い、
   **優待を登録した銘柄は登録の数営業日後に「データが古い」でBUYからハード除外**される。
2. yfinance系providerは常に `fetched_at = now` を返すため、
   10営業日前の終値を今取得しても年齢0日となり鮮度ゲートが発火しない。

既存の mock provider は優待についても `fetched_at = now` を返していたため、
**1 は既存テストでは再現しなかった**。本モジュールはProduction経路
(`LocalRegistryShareholderBenefitProvider` が保存値をそのまま返す)を再現する。

## 本モジュールが固定する契約

```
T1  古い優待登録日時があっても、それだけでは stale exclusion されない
T2  市場等のgeneric freshness対象sourceが古ければ、従来どおり鮮度判定は発火する
    (「優待を外したら鮮度ゲート全体が無効になった」regressionの防止)
T3  優待が無い場合の既存動作は変わらない
T5  expected latest completed trading session の境界
T6  missed trading sessions の境界
T7  UTC/JSTの時刻境界で結果が変わらない(JPX calendar / JST基準)
```

T4(#120 read-purity regression 維持)は
`tests/unit/test_issue_120_registry_read_purity.py` が引き続き担保する。
本Issueは同 provider を経由するため、あわせて実行して確認すること。

## 本モジュールが固定「しない」こと

価格の許容鮮度(何セッション取りこぼしたら警告か停止か)。
Phase B1 では計算基盤のみを追加し、**業務判定へは接続していない**。
閾値と挙動は Phase B2 で人間が確定する。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import BenefitUtilityCategory, SourceType
from jstock_advisor.domain.market_session import (
    JPX_REGULAR_SESSION_CLOSE_JST,
    MISSED_SESSIONS_UNKNOWN,
    expected_latest_completed_trading_session,
    missed_trading_sessions,
)
from jstock_advisor.interfaces.types import BenefitDetail, ShareholderBenefit
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

_CFG = load_config()
_CALENDAR = BusinessCalendar.from_config(_CFG.holiday_calendar)
_STOCK_CODE = "2914"

# 2026-09-02 は水曜(平日)。JST 08:00 の日次バッチ実行を想定。
_NOW = dt.datetime(2026, 9, 1, 23, 0, tzinfo=dt.UTC)  # = 2026-09-02 08:00 JST

_JST = dt.timezone(dt.timedelta(hours=9))


def _jst(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=_JST)


# --- 優待 provider: Production 経路の再現 -------------------------------------


class _RegisteredBenefitProvider:
    """Production の `LocalRegistryShareholderBenefitProvider` と同じ性質のフェイク。

    既存の mock provider は `fetched_at = now` かつ既定の source_type を返すため、
    「登録時刻が永続化されている」という Production の実態を再現しない
    (これがこの欠陥を既存テストで検出できなかった理由である)。

    委譲はしない。mock は `MOCK_STOCKS` に優待を持つ銘柄しか返さず、委譲すると
    `None` になってテストが**空虚に通る**ため、ここで優待を直接構築する。
    """

    def __init__(self, registered_at: dt.datetime) -> None:
        self._registered_at = registered_at

    def get_shareholder_benefit(self, stock_code: str) -> ShareholderBenefit:
        return ShareholderBenefit(
            stock_code=stock_code,
            min_shares_required=100,
            benefits=[
                BenefitDetail(
                    category=BenefitUtilityCategory.CASH_EQUIVALENT,
                    description="クオカード1000円分",
                    estimated_value=Decimal("1000"),
                    min_shares_for_tier=100,
                )
            ],
            frequency_per_year=1,
            benefit_record_dates=[dt.date(2027, 3, 31)],
            is_abolished=False,
            is_major_downgrade=False,
            # Production の登録経路と同じ形。登録操作の時刻がそのまま永続化される。
            source=DataSourceReference(
                provider="manual_registry",
                fetched_at=self._registered_at,
                source_type=SourceType.MANUAL_REGISTRY,
                primary_source_flag=True,
            ),
        )


class _NoBenefitProvider:
    def get_shareholder_benefit(self, stock_code: str) -> object | None:
        return None


def _bundle_with_benefit_registered_at(registered_at: dt.datetime) -> object:
    bundle = build_mock_provider_bundle(_NOW)
    return dataclasses.replace(
        bundle,
        shareholder_benefit=_RegisteredBenefitProvider(registered_at),
    )


def _build(bundle: object, now: dt.datetime = _NOW) -> object:
    snapshot, error = build_stock_snapshot(bundle, _STOCK_CODE, now, _CFG, _CALENDAR)
    assert error is None, f"snapshot 構築に失敗した: {error}"
    assert snapshot is not None
    return snapshot


# --- T1: 古い優待登録日時だけでは stale にならない -----------------------------


@pytest.mark.parametrize(
    ("label", "days_ago"),
    [
        ("登録から4営業日", 6),
        ("登録から1か月", 30),
        ("登録から1年", 365),
    ],
)
def test_t1_old_benefit_registration_does_not_make_data_stale(
    label: str, days_ago: int
) -> None:
    """優待の登録が古いというだけで generic freshness が古くならない(F-J1)。

    修正前は `min(fetched_at)` が登録時刻を拾い、
    `max_data_age_business_days`(3)を超えて BUY からハード除外されていた。
    """
    registered_at = _NOW - dt.timedelta(days=days_ago)
    snapshot = _build(_bundle_with_benefit_registered_at(registered_at))

    assert snapshot.data_fetched_at > registered_at, (
        f"{label}: 優待の登録時刻が data_fetched_at へ混入している"
    )
    # 市場・財務・配当はいずれも now 取得のため、年齢は0営業日であるべき
    age = _CALENDAR.business_days_between(
        snapshot.data_fetched_at.astimezone(_JST).date(), _NOW.astimezone(_JST).date()
    )
    assert age == 0, f"{label}: データ年齢が {age} 営業日と評価された"


def test_t1b_benefit_source_is_still_recorded_for_provenance() -> None:
    """鮮度の分母から外しても、出所(provenance)としては記録し続ける。

    監査・説明可能性のために `data_sources` からは落とさない。
    """
    registered_at = _NOW - dt.timedelta(days=365)
    snapshot = _build(_bundle_with_benefit_registered_at(registered_at))

    registry_sources = [
        s for s in snapshot.data_sources if s.source_type is SourceType.MANUAL_REGISTRY
    ]
    assert len(registry_sources) == 1, "優待の出所が data_sources から失われている"
    assert registry_sources[0].fetched_at == registered_at


# --- T2: 鮮度ゲート全体が無効化していないこと ---------------------------------


def test_t2_market_source_staleness_still_drives_data_fetched_at() -> None:
    """generic freshness 対象の source が古ければ、従来どおりそれが反映される。

    「優待を分母から外した結果、鮮度ゲートそのものが効かなくなった」という
    regression を防ぐ。市場データ側を古くすると data_fetched_at も古くなること、
    かつその値が優待の登録時刻ではなく市場側の時刻であることを固定する。
    """
    stale_fetched_at = _NOW - dt.timedelta(days=10)
    bundle = build_mock_provider_bundle(_NOW)

    class _StaleMarketDataProvider:
        def __init__(self, delegate: object) -> None:
            self._delegate = delegate

        def get_latest_price(self, stock_code: str) -> object | None:
            snap = self._delegate.get_latest_price(stock_code)  # type: ignore[attr-defined]
            if snap is None:
                return None
            return snap.model_copy(
                update={
                    "source": snap.source.model_copy(
                        update={"fetched_at": stale_fetched_at}
                    )
                }
            )

        def __getattr__(self, name: str) -> object:
            return getattr(self._delegate, name)

    bundle = dataclasses.replace(
        bundle,
        market_data=_StaleMarketDataProvider(bundle.market_data),
        shareholder_benefit=_RegisteredBenefitProvider(_NOW - dt.timedelta(days=365)),
    )

    snapshot = _build(bundle)

    assert snapshot.data_fetched_at == stale_fetched_at, (
        "generic freshness 対象(市場データ)の古さが data_fetched_at へ反映されていない"
    )


# --- T3: 優待なしの既存動作が変わらないこと -----------------------------------


def test_t3_behaviour_without_benefit_is_unchanged() -> None:
    """優待が登録されていない銘柄の data_fetched_at は従来どおり。"""
    bundle = dataclasses.replace(
        build_mock_provider_bundle(_NOW),
        shareholder_benefit=_NoBenefitProvider(),
    )
    snapshot = _build(bundle)

    non_registry = [
        s for s in snapshot.data_sources if s.source_type is not SourceType.MANUAL_REGISTRY
    ]
    assert snapshot.data_fetched_at == min(s.fetched_at for s in non_registry)


def test_t3b_benefit_presence_does_not_change_data_fetched_at() -> None:
    """優待の有無で data_fetched_at が変わらないこと(分母から外れている証明)。"""
    without = _build(
        dataclasses.replace(
            build_mock_provider_bundle(_NOW), shareholder_benefit=_NoBenefitProvider()
        )
    )
    with_old_benefit = _build(
        _bundle_with_benefit_registered_at(_NOW - dt.timedelta(days=365))
    )

    assert without.data_fetched_at == with_old_benefit.data_fetched_at


# --- T5: expected latest completed trading session の境界 ---------------------


@pytest.mark.parametrize(
    ("label", "now", "expected"),
    [
        # 2026-09-02 は水曜。大引けは 15:30 JST。
        ("平日・大引け前(08:00)", _jst(2026, 9, 2, 8, 0), dt.date(2026, 9, 1)),
        ("平日・大引け直前(15:29)", _jst(2026, 9, 2, 15, 29), dt.date(2026, 9, 1)),
        ("平日・大引け直後(15:30)", _jst(2026, 9, 2, 15, 30), dt.date(2026, 9, 2)),
        ("平日・大引け後(18:00)", _jst(2026, 9, 2, 18, 0), dt.date(2026, 9, 2)),
        # 2026-09-05 は土曜 / 2026-09-06 は日曜。直近営業日は金曜 09-04。
        ("土曜", _jst(2026, 9, 5, 12, 0), dt.date(2026, 9, 4)),
        ("日曜", _jst(2026, 9, 6, 12, 0), dt.date(2026, 9, 4)),
        # 2026-09-07 は月曜。大引け前なので前営業日=金曜 09-04。
        ("月曜・大引け前", _jst(2026, 9, 7, 8, 0), dt.date(2026, 9, 4)),
    ],
)
def test_t5_expected_latest_completed_trading_session(
    label: str, now: dt.datetime, expected: dt.date
) -> None:
    actual = expected_latest_completed_trading_session(now, _CALENDAR)
    assert actual == expected, f"{label}: {actual} != {expected}"


def test_t5b_holiday_is_skipped() -> None:
    """祝日・連休は営業日として数えず、その前の営業日まで遡る。

    具体的な祝日はカレンダー実装(jpholiday + config)に委ねるため、
    ここでは「返ってきた日付が必ず営業日である」ことを不変条件として固定する。
    """
    for day in range(1, 32):
        now = _jst(2026, 1, day, 8, 0) if day <= 31 else None
        if now is None:
            continue
        session = expected_latest_completed_trading_session(now, _CALENDAR)
        assert _CALENDAR.is_business_day(session), (
            f"{now:%Y-%m-%d} の期待セッション {session} が営業日でない"
        )
        assert session <= now.date()


# --- T6: missed trading sessions の境界 --------------------------------------


def test_t6_missed_sessions_zero_when_latest_session_is_present() -> None:
    now = _jst(2026, 9, 2, 8, 0)  # 期待セッション = 2026-09-01(火)
    assert missed_trading_sessions(dt.date(2026, 9, 1), now, _CALENDAR) == 0


def test_t6b_missed_sessions_one() -> None:
    now = _jst(2026, 9, 2, 8, 0)  # 期待 = 09-01。1営業日前は 08-31(月)
    assert missed_trading_sessions(dt.date(2026, 8, 31), now, _CALENDAR) == 1


def test_t6c_missed_sessions_two() -> None:
    now = _jst(2026, 9, 2, 8, 0)  # 期待 = 09-01。2営業日前は 08-28(金)
    assert missed_trading_sessions(dt.date(2026, 8, 28), now, _CALENDAR) == 2


def test_t6d_missed_sessions_multiple_counts_business_days_only() -> None:
    """週末を跨いでも、実際に開いていた営業日の本数だけを数える。"""
    now = _jst(2026, 9, 2, 8, 0)  # 期待 = 09-01
    # 08-24(月)から 09-01 までの営業日数を暦から導出し、ハードコードしない
    expected = _CALENDAR.business_days_between(dt.date(2026, 8, 24), dt.date(2026, 9, 1))
    assert missed_trading_sessions(dt.date(2026, 8, 24), now, _CALENDAR) == expected
    assert expected > 2


def test_t6e_future_as_of_is_not_negative() -> None:
    """期待セッションより未来の as_of は 0(取りこぼしは負にならない)。"""
    now = _jst(2026, 9, 2, 8, 0)
    assert missed_trading_sessions(dt.date(2026, 9, 10), now, _CALENDAR) == 0


def test_t6f_unknown_as_of_is_not_treated_as_fresh() -> None:
    """as_of 不明を 0(新鮮)へ丸めない。fetched_at への fallback もしない。

    fallback すると「常に新鮮」と誤判定する原因そのものを再導入することになる。
    """
    now = _jst(2026, 9, 2, 8, 0)
    result = missed_trading_sessions(None, now, _CALENDAR)

    assert result is MISSED_SESSIONS_UNKNOWN
    assert result != 0


# --- T7: UTC/JST 境界で結果が変わらないこと -----------------------------------


@pytest.mark.parametrize(
    ("label", "instant"),
    [
        # いずれも同一の瞬間を異なる表現で与える。JST暦日は 2026-09-02。
        ("JST表現", _jst(2026, 9, 2, 8, 0)),
        ("UTC表現", dt.datetime(2026, 9, 1, 23, 0, tzinfo=dt.UTC)),
        ("+05:00表現", dt.datetime(2026, 9, 2, 4, 0, tzinfo=dt.timezone(dt.timedelta(hours=5)))),
    ],
)
def test_t7_same_instant_gives_same_session_regardless_of_tzinfo(
    label: str, instant: dt.datetime
) -> None:
    """同一の瞬間なら、tzinfo の表現によらず同じ結果になる。"""
    assert expected_latest_completed_trading_session(instant, _CALENDAR) == dt.date(
        2026, 9, 1
    ), label
    assert missed_trading_sessions(dt.date(2026, 8, 31), instant, _CALENDAR) == 1, label


def test_t7b_utc_calendar_day_is_not_used() -> None:
    """UTC暦日を使っていないこと。

    JST 2026-09-02 08:00 は UTC では 2026-09-01 である。UTC暦日で判定していると
    「前日の大引け後」と誤認して当日(UTC暦日=09-01)を返しうる。
    JST基準であれば期待セッションは 09-01(火)で、これは偶然一致するため、
    差が出る時刻(JST 18:00 = UTC 09:00 同日)でも検証する。
    """
    # JST 2026-09-02 18:00(大引け後) -> 期待セッションは当日 09-02
    jst_evening = _jst(2026, 9, 2, 18, 0)
    assert expected_latest_completed_trading_session(jst_evening, _CALENDAR) == dt.date(
        2026, 9, 2
    )
    # UTC暦日で見ると 09-02 09:00 であり同日だが、時刻は 09:00 で大引け前。
    # JSTへ変換せず時刻比較していれば 09-01 を返してしまう。
    assert expected_latest_completed_trading_session(jst_evening, _CALENDAR) != dt.date(
        2026, 9, 1
    )


def test_t7c_session_close_boundary_is_evaluated_in_jst() -> None:
    """大引け判定が JST の時刻で行われること。"""
    just_before = _jst(2026, 9, 2, 15, 29)
    just_after = _jst(2026, 9, 2, 15, 30)

    assert expected_latest_completed_trading_session(just_before, _CALENDAR) == dt.date(
        2026, 9, 1
    )
    assert expected_latest_completed_trading_session(just_after, _CALENDAR) == dt.date(
        2026, 9, 2
    )
    assert dt.time(15, 30) == JPX_REGULAR_SESSION_CLOSE_JST


# --- Phase B1 の scope 境界: 業務判定へ接続していないこと ----------------------


def test_price_freshness_is_not_wired_into_business_decisions_yet() -> None:
    """Phase B1 では価格 freshness を業務判定へ接続していないことを固定する。

    Phase B2 で閾値(BUY: missed>=2 で HARD_STOP、HOLDINGS: missed>=1 で
    DATA_INSUFFICIENT)を確定してから接続する。B1 の段階で誤って接続されると、
    閾値が未確定のまま Production の判定が変わってしまう。
    """
    src_root = Path(__file__).resolve().parents[2] / "src"
    referencing = sorted(
        path.relative_to(src_root).as_posix()
        for path in src_root.rglob("*.py")
        if "missed_trading_sessions" in path.read_text(encoding="utf-8")
    )

    assert referencing == ["jstock_advisor/domain/market_session.py"], (
        "価格 freshness が定義元以外から参照されている。"
        f"Phase B1 では業務判定へ接続しない: {referencing}"
    )


# --- T8 / T9: naive datetime を暗黙処理しない ---------------------------------


def test_t8_expected_session_rejects_naive_datetime() -> None:
    """naive な `now` は ValueError(暗黙にUTC扱いしない)。

    naive を暗黙にUTC扱いすると、JST 00:00〜08:59 に相当する時刻で
    1営業日ずれたセッションを返す。本Issueが是正しようとしている
    「基準がずれたまま鮮度を判定する」問題を別の形で再導入することになるため、
    入口で拒否する(`domain/jst.py` の規約に従う)。
    """
    naive = dt.datetime(2026, 9, 2, 8, 0)  # noqa: DTZ001 - naive であること自体が検証対象

    with pytest.raises(ValueError, match="timezone-aware"):
        expected_latest_completed_trading_session(naive, _CALENDAR)


def test_t9_missed_sessions_rejects_naive_datetime() -> None:
    """`missed_trading_sessions` も naive な `now` を受け付けない。

    検証は `expected_latest_completed_trading_session()` の入口1箇所で行い、
    ここでは重複チェックを置かない。呼び出し経路として拒否されることを固定する。
    """
    naive = dt.datetime(2026, 9, 2, 8, 0)  # noqa: DTZ001 - naive であること自体が検証対象

    with pytest.raises(ValueError, match="timezone-aware"):
        missed_trading_sessions(dt.date(2026, 8, 31), naive, _CALENDAR)


def test_t9b_unknown_as_of_returns_before_timezone_validation() -> None:
    """`as_of_date` が None の経路では `now` を検証しない(仕様の明示)。

    評価に使わない `now` の形式で例外にする必要はないため、
    UNKNOWN を返す経路は naive でも通す。この非対称は意図的であり、
    docstring と本テストで契約として固定する。
    """
    naive = dt.datetime(2026, 9, 2, 8, 0)  # noqa: DTZ001 - naive であること自体が検証対象

    assert missed_trading_sessions(None, naive, _CALENDAR) is MISSED_SESSIONS_UNKNOWN
