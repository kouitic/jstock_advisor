"""Issue #64 F-A2 / F-A3 / F-I3: 集中度の分子・分母 scope と、根拠の保存。

3 件をまとめて 1 ファイルにする理由:
  いずれも ★ **同じ 2 関数**（`_estimate_portfolio_totals` /
  `_evaluate_portfolio_concentration_and_notify`）を触る。別々に直すと同じ場所を
  3 回触ることになる。

  F-A3  分子が 1 owner・分母が全 owner という ★ **中間状態**を、
        ★ **家計全体**（分子も全 owner の同一銘柄合算）へ揃える。
        ★ USER 判断 = #64 issuecomment-5629242431。
  F-A2  owner / holding_id / raw_type を保存する。
        ★ 家計全体基準では「どの holding か」が自明でないため、
        ★ **寄与した holding_id の一覧**を保存する（MANAGER 判断）。
  F-I3  価格の出所・時刻・対象 scope・★ **閾値**を保存する。
        ★ 保存のために価格を再取得しない。★ 財務 provenance は捏造しない。

★ 判定の粒度を ★ **銘柄単位**へ揃えた（親側で 1 回だけ判定する）。
  ★ 子 Lambda は holding ごとに 1 つ起動するため、子で合算比率を判定すると
  ★ **同一銘柄で Recommendation が 2 件**作られる。これを構造的に防ぐ。

★ 集中度は ★ **INTERNAL_ONLY のまま**である（LINE 送信対象に変えていない）。
★ 閾値 20.0% の値は ★ **1 文字も変えていない**（★ 保存するだけ）。

fixture は架空値のみ。銘柄コードは ★ 割り当てが存在しない "0000" / "0001" を使う。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    AccountType,
    NotificationCategory,
    NotificationIntent,
    RecommendationType,
    SourceType,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.notification.notification_intent import resolve_notification_intent
from jstock_advisor.interfaces.types import PriceSnapshot
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as handler_module
from jstock_advisor.services.line_notification_service import (
    NotificationOutcome,
    NotificationStatus,
)

# ★ JPX の証券コードとして割り当てが存在しない値。実在銘柄と衝突しない。
_STOCK_A = "0000"  # 2 owner が持つ銘柄
_STOCK_B = "0001"  # 1 owner だけが持つ銘柄
_OWNER_A = "所有者A"
_OWNER_B = "所有者B"
_NOW = dt.datetime(2026, 6, 30, 9, 0, tzinfo=dt.UTC)
_FETCHED_AT = dt.datetime(2026, 6, 30, 8, 55, tzinfo=dt.UTC)


def _holding(owner: str, stock_code: str, shares: int, cost: str) -> Holding:
    return Holding(
        owner=owner,
        holding_id=f"{owner}#{stock_code}",
        stock_code=stock_code,
        stock_name="テスト銘柄",
        shares=shares,
        average_purchase_price=Decimal(cost) / shares,
        total_purchase_amount=Decimal(cost),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _price(stock_code: str, close: str) -> PriceSnapshot:
    return PriceSnapshot(
        stock_code=stock_code,
        as_of_date=dt.date(2026, 6, 30),
        close_price=Decimal(close),
        source=DataSourceReference(
            provider="test-market-data",
            fetched_at=_FETCHED_AT,
            source_type=SourceType.CONTRACTED_PROVIDER,
        ),
    )


class _FakeMarketData:
    """指定した銘柄だけ価格を返す（他は None = 取得失敗）。"""

    def __init__(self, prices: dict[str, PriceSnapshot]) -> None:
        self._prices = prices
        self.calls: list[str] = []

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        self.calls.append(stock_code)
        return self._prices.get(stock_code)


class _FakeProviders:
    def __init__(self, market_data: _FakeMarketData) -> None:
        self.market_data = market_data


class _SpyRepo:
    def __init__(self) -> None:
        self.saved: list[object] = []

    def save(self, recommendation: object) -> None:
        self.saved.append(recommendation)


class _SpyNotificationService:
    def __init__(self) -> None:
        self.notified: list[object] = []

    def notify_recommendation_with_status(
        self, recommendation: object, now: dt.datetime
    ) -> NotificationOutcome:
        self.notified.append(recommendation)
        return NotificationOutcome(status=NotificationStatus.SENT, sent=True)


class _FakeRuleVersionService:
    def get_active_version_or(self, default: str) -> str:
        return "rule-v-test"


# --- 入力の組み立て -------------------------------------------------------------------
#
# ★ 2 owner が同じ銘柄 A を持つ。合算しないと閾値未満、合算すると閾値を超える。
#   銘柄 A  owner-a 取得額 120,000 / owner-b 取得額 120,000 -> ★ 合算 240,000
#   銘柄 B  owner-a 取得額 760,000
#   合計 1,000,000 -> A 単独 = 12.0%（閾値未満）/ ★ A 合算 = **24.0%**（閾値超え）

_HOLDINGS = [
    _holding(_OWNER_A, _STOCK_A, 100, "120000"),
    _holding(_OWNER_B, _STOCK_A, 100, "120000"),
    _holding(_OWNER_A, _STOCK_B, 100, "760000"),
]
_PRICES = {_STOCK_A: _price(_STOCK_A, "1200"), _STOCK_B: _price(_STOCK_B, "7600")}


def _run(holdings=None, prices=None):
    """親側で銘柄単位の集中度判定を 1 回だけ走らせ、保存された Recommendation を返す。"""
    market = _FakeMarketData(dict(_PRICES if prices is None else prices))
    providers = _FakeProviders(market)
    repo, notifier = _SpyRepo(), _SpyNotificationService()
    handler_module.evaluate_household_concentration_and_notify(
        list(_HOLDINGS if holdings is None else holdings),
        providers,
        handler_module.load_config(),
        repo,
        notifier,
        _FakeRuleVersionService(),
        _NOW,
        True,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
    )
    return repo.saved, notifier.notified, market


def _for_stock(saved, stock_code: str):
    return [r for r in saved if r.stock_code == stock_code]


# --- F-A3: 家計全体の分子 --------------------------------------------------------------


def test_f_a3_numerator_is_summed_across_owners() -> None:
    """★ 2 owner が持つ銘柄は ★ **合算**で判定される。

    ★ 合算しなければ 12.0% で閾値未満、合算すれば 24.0% で閾値超え。
      ★ どちらの式が使われたかが ★ **発火の有無そのもの**で分かる。
    """
    saved, _notified, _market = _run()

    hits = _for_stock(saved, _STOCK_A)
    assert len(hits) == 1
    assert hits[0].portfolio_acquisition_cost_weight_pct == pytest.approx(24.0)


def test_f_a3_single_owner_stock_is_unchanged() -> None:
    """★ 1 owner だけの銘柄は、合算しても値が変わらない（回帰の確認）。"""
    saved, _notified, _market = _run()

    hits = _for_stock(saved, _STOCK_B)
    assert len(hits) == 1
    assert hits[0].portfolio_acquisition_cost_weight_pct == pytest.approx(76.0)


def test_f_a3_below_threshold_does_not_fire() -> None:
    """★ 逆側。合算しても閾値に届かなければ発火しない。"""
    small = [
        _holding(_OWNER_A, _STOCK_A, 100, "50000"),
        _holding(_OWNER_B, _STOCK_A, 100, "50000"),
        _holding(_OWNER_A, _STOCK_B, 100, "900000"),
    ]
    # ★ 時価ベースでも閾値未満になるよう価格も合わせる（片方だけで発火させない）。
    prices = {_STOCK_A: _price(_STOCK_A, "500"), _STOCK_B: _price(_STOCK_B, "9000")}
    saved, _notified, _market = _run(holdings=small, prices=prices)

    assert _for_stock(saved, _STOCK_A) == []
    assert len(_for_stock(saved, _STOCK_B)) == 1


# --- ★ 重複発火しないこと（本変更で新しく生じうる欠陥） -------------------------------


def test_two_owners_produce_exactly_one_recommendation() -> None:
    """★★ 同一銘柄を 2 owner が持っても Recommendation は ★ **1 件**。

    ★ 判定を holding 単位のまま合算比率にすると、同じ銘柄で 2 件作られる。
      ★ 「1 つの事実に記録が 2 件」を構造的に防ぐことを固定する。
    """
    saved, notified, _market = _run()

    assert len(_for_stock(saved, _STOCK_A)) == 1
    assert len([r for r in notified if r.stock_code == _STOCK_A]) == 1


def test_price_is_fetched_once_per_stock_not_per_holding() -> None:
    """★ 価格取得は ★ **銘柄ごとに 1 回**（holding ごとではない）。

    ★ 保存のために価格を再取得しないこと（F-I3）の裏返しでもある。
    """
    _saved, _notified, market = _run()

    assert sorted(market.calls) == [_STOCK_A, _STOCK_B]


# --- F-A2: owner / holding_id / raw_type ------------------------------------------------


def test_f_a2_single_contributor_records_owner_and_holding_id() -> None:
    """★ 寄与した holding が 1 件なら、その owner / holding_id を保存する。"""
    saved, _notified, _market = _run()

    rec = _for_stock(saved, _STOCK_B)[0]
    assert rec.owner == _OWNER_A
    assert rec.holding_id == f"{_OWNER_A}#{_STOCK_B}"


def test_f_a2_multiple_contributors_record_the_list_not_a_representative() -> None:
    """★★ 寄与が 2 件以上なら、★ **一覧**を保存し、代表 1 件を選ばない。

    ★ 単一の owner を入れると「その人の話」と読めてしまい、事実と異なる。
      ★ 誰の分がいくら寄与したかを追えるよう、★ **一覧**で残す。
    """
    saved, _notified, _market = _run()

    rec = _for_stock(saved, _STOCK_A)[0]
    assert rec.owner is None
    assert rec.holding_id is None
    assert rec.config_values_used["contributing_holding_ids"] == [
        f"{_OWNER_A}#{_STOCK_A}",
        f"{_OWNER_B}#{_STOCK_A}",
    ]
    assert rec.config_values_used["contributing_owner_count"] == 2


def test_f_a2_raw_type_matches_the_generated_type() -> None:
    """★ raw_recommendation_type が生成時の Type と一致すること。"""
    saved, _notified, _market = _run()

    for rec in saved:
        assert rec.recommendation_type == RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW
        assert rec.raw_recommendation_type == rec.recommendation_type


# --- F-I3: 根拠の保存 -------------------------------------------------------------------


def test_f_i3_price_sources_are_recorded_without_refetching() -> None:
    """★ 分子・分母に使った価格の出所と時刻が保存されること。

    ★ 保存のために価格を取り直していない（同じ PriceSnapshot の source を転記）。
    """
    saved, _notified, _market = _run()

    rec = _for_stock(saved, _STOCK_A)[0]
    assert [s.provider for s in rec.data_sources] == ["test-market-data"]
    assert rec.data_sources[0].fetched_at == _FETCHED_AT


def test_f_i3_threshold_and_scope_are_recorded() -> None:
    """★★ 閾値と対象 scope が保存されること。

    ★ 設定を変えた後で過去の判定を読んだとき、
      ★ **どの閾値で発火したのか**を復元できるようにする。
    """
    saved, _notified, _market = _run()

    cfg = _for_stock(saved, _STOCK_A)[0].config_values_used
    assert cfg["single_stock_weight_threshold_pct"] == pytest.approx(20.0)
    assert cfg["concentration_scope"] == "HOUSEHOLD"
    assert cfg["denominator_scope"] == "ALL_OWNERS"


def test_f_i3_financial_provenance_is_not_fabricated() -> None:
    """★ 集中度は財務 snapshot を消費しない。★ provenance を捏造しない。"""
    saved, _notified, _market = _run()

    for rec in saved:
        assert rec.financial_input_provenance is None


# --- F-A3 M-1: 分母不明 -----------------------------------------------------------------


def test_m1_missing_price_elsewhere_leaves_market_basis_undecided() -> None:
    """★★ 他の銘柄の価格が欠けたら、★ **時価ベースは判定しない**（None のまま）。

    ★ 分母（全体の時価総額）が作れないため。★ ゼロや部分合計で「強い判定」を作らない。
    ★ 対象銘柄自身の価格は取れているので、★ **記録は作られる**。
    """
    saved, _notified, _market = _run(prices={_STOCK_A: _price(_STOCK_A, "1200")})

    rec = _for_stock(saved, _STOCK_A)[0]
    assert rec.portfolio_weight_pct is None
    assert rec.config_values_used["market_value_basis_available"] is False


def test_m1_acquisition_cost_basis_still_decides_when_denominator_is_unknown() -> None:
    """★ 取得価格ベースは ★ **価格に依存しない**ため、引き続き判定する。

    ★ ここを落とすと「価格取得に失敗した日は集中リスクを見なくなる」= fail-open。
      ★ 意図して fail-safe 側（判定を続ける）へ倒していることを固定する。
    """
    saved, _notified, _market = _run(prices={_STOCK_A: _price(_STOCK_A, "1200")})

    rec = _for_stock(saved, _STOCK_A)[0]
    assert rec.portfolio_acquisition_cost_weight_pct == pytest.approx(24.0)
    assert any("取得価格ベース" in r for r in rec.reasons)


def test_m1_missing_price_for_the_stock_itself_is_skipped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★★ 対象銘柄**自身**の価格が取れないときは、★ **記録を作らない**。

    ★ 理由 = `Recommendation.price_at_recommendation` は ★ **必須・null 不可**であり、
      取得単価で代用すると「判定時点の株価」という項目の意味が壊れます。
      ★ ★ **事実で埋められない項目を、それらしい値で埋めません。**
    ★ ★ ただし ★ **黙って落としません**。件数を WARNING に残します
      （★ 銘柄コードは出しません。Issue #135）。
    ★ ★ これは ★ **意図的な妥協**です。この場合に限り集中度を見られません。
      ★ 恒久策（項目を null 許容にする等）は共有 entity の契約変更のため別途判断が要ります。
    """
    with caplog.at_level("WARNING"):
        saved, _notified, _market = _run(prices={_STOCK_B: _price(_STOCK_B, "7600")})

    assert _for_stock(saved, _STOCK_A) == []
    assert any("latest price unavailable" in r.message for r in caplog.records)


# --- ★ INTERNAL_ONLY が維持されること ---------------------------------------------------


def test_concentration_stays_internal_only() -> None:
    """★★ 集中度は ★ **LINE 送信対象ではない**まま（本変更で変えていない）。"""
    saved, _notified, _market = _run()

    rec = _for_stock(saved, _STOCK_A)[0]
    category = handler_module.resolve_notification_category(rec)
    assert category is NotificationCategory.WATCH
    assert (
        resolve_notification_intent(category, rec.profit_protection_signal)
        is NotificationIntent.INTERNAL_ONLY
    )
