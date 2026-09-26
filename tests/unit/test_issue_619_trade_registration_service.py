"""Issue #619(#128 A3-CLI): CLI経由の通常売買登録とAvailable Cash更新を
整合した1更新単位として扱う。`TradeRegistrationService`のsemanticsを固定する。

LINE経路(#590/#591)はDynamoDB TransactWriteItemsで原子性を保証するが、
CLIは常にローカル実行のみのため、本サービスは適用前スナップショット+
例外時ロールバックで同等の「全部成功 or 全部不成功」契約を満たす。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.enums import AccountType, TransactionType
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.infrastructure.local_repository.available_cash_repository import (
    AvailableCashRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    TransactionRepository,
)
from jstock_advisor.services.available_cash_service import (
    AvailableCashNotRegisteredError,
    AvailableCashService,
    InsufficientAvailableCashError,
)
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.trade_registration_service import (
    IdempotencyKeyReusedForDifferentTradeError,
    TradeRegistrationService,
)
from jstock_advisor.services.transaction_history_service import TransactionHistoryService
from jstock_advisor.services.write_plan import ConcurrentUpdateError

_NOW = dt.datetime(2026, 9, 26, 9, 0, tzinfo=dt.UTC)
_STOCK = "8306"
_HOLDING_ID = build_holding_id(DEFAULT_OWNER, _STOCK)


@pytest.fixture
def env(tmp_path):
    ac_repo = AvailableCashRepository(tmp_path)
    lot_repo = PurchaseLotRepository(tmp_path)
    holding_repo = HoldingRepository(tmp_path)
    tx_repo = TransactionRepository(tmp_path)
    ac_service = AvailableCashService(repository=ac_repo)
    portfolio = PortfolioService(lot_repository=lot_repo, holding_repository=holding_repo)
    tx_history = TransactionHistoryService(transaction_repository=tx_repo)
    service = TradeRegistrationService(
        portfolio_service=portfolio,
        transaction_history_service=tx_history,
        available_cash_service=ac_service,
        transaction_repository=tx_repo,
        lot_repository=lot_repo,
        holding_repository=holding_repo,
        available_cash_repository=ac_repo,
    )
    return {
        "service": service,
        "ac_repo": ac_repo,
        "ac_service": ac_service,
        "lot_repo": lot_repo,
        "holding_repo": holding_repo,
        "tx_repo": tx_repo,
        "portfolio": portfolio,
    }


def _seed_cash(env, amount: str, owner: str = DEFAULT_OWNER) -> None:
    env["ac_service"].reconcile(owner, Decimal(amount), _NOW)


# --- 基本のBUY/SELL契約 ------------------------------------------------------


def test_register_buy_writes_holding_transaction_and_debits_cash(env) -> None:
    _seed_cash(env, "1000000")

    result = env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1500"), _NOW.date(), "idem-1", _NOW
    )

    assert result.already_registered is False
    assert result.resulting_holding is not None
    assert result.resulting_holding.shares == 100
    assert env["tx_repo"].get("idem-1") is not None
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("850000")


def test_register_sell_credits_cash(env) -> None:
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "idem-buy", _NOW
    )

    result = env["service"].register_sell(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1800"), _NOW.date(), "idem-sell", _NOW
    )

    assert result.already_registered is False
    assert env["holding_repo"].get(_HOLDING_ID) is None  # 全部売却
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("1080000")  # 900000+180000


# --- idempotency(D2) --------------------------------------------------------


def test_retrying_same_idempotency_key_does_not_double_debit_cash(env) -> None:
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1500"), _NOW.date(), "idem-1", _NOW
    )

    retry = env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1500"), _NOW.date(), "idem-1", _NOW
    )

    assert retry.already_registered is True
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("850000")  # 二重減算なし


def test_retrying_same_idempotency_key_does_not_duplicate_lot(env) -> None:
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1500"), _NOW.date(), "idem-1", _NOW
    )

    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1500"), _NOW.date(), "idem-1", _NOW
    )

    assert env["holding_repo"].get(_HOLDING_ID).shares == 100  # 100株のまま(200株にならない)


def test_omitting_idempotency_key_each_call_is_treated_as_a_new_trade(env) -> None:
    """D2: --idempotency-key省略時は非冪等(呼び出し側が毎回異なるキーを渡す
    運用を想定。本テストは異なるキーを渡した場合の挙動を固定する)。"""
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1500"), _NOW.date(), "key-a", _NOW
    )

    result = env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1500"), _NOW.date(), "key-b", _NOW
    )

    assert result.already_registered is False
    assert env["holding_repo"].get(_HOLDING_ID).shares == 200
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("700000")


# --- サブちゃんレビュー#624 F1対応: idempotency-keyの取り違え検出 -------------
# 既存の冪等キー(CSV importのcsv:{sha256}:{row}等)は内容由来で取り違えが
# 構造的に起こらないが、本Issueで初めて「人が手で打つ自由入力のキー」を
# 導入したため、別の取引へ誤って使い回した場合を明示的に検出する契約を固定する。


def test_reusing_key_from_buy_for_a_different_sell_is_rejected_not_silently_dropped(env) -> None:
    """取り違えの実害を固定する回帰テスト: 修正前はこの誤用でSELLがno-opとして
    黙って消え、保有株数・買付余力が実態より多いまま残っていた。"""
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
    )

    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        env["service"].register_sell(
            DEFAULT_OWNER, _STOCK, 100, Decimal("1800"), _NOW.date(), "shared-key", _NOW
        )

    # SELLが黙って消えていない(保有株数・買付余力とも変化しない)ことを確認する。
    assert env["holding_repo"].get(_HOLDING_ID).shares == 100
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("900000")


def test_reusing_key_for_buy_vs_sell_with_identical_amounts_is_rejected(env) -> None:
    """サブちゃんレビュー#624 F5(N3)対応: 上記テストは単価もずれている
    (1000→1800)ため、実際にはBUY/SELL区分ではなく単価不一致で通っていた
    (区分そのものの取り違え検出を検証していなかった、という指摘)。銘柄・
    株数・単価・日付を全て揃え、BUY/SELL区分のみを変える。

    ★ サブちゃんレビュー#624 F8注記: F7でaccount_typeを照合対象へ追加した
    ため、本テストはBUY(既定account_type=GENERAL)とSELL(常にaccount_type=
    None)の比較になり、区分ではなくaccount_type不一致でも拒否されうる
    (区分照合の単独固定にはならない)。区分照合を単独で固定するテストは
    下記`test_reusing_key_for_buy_vs_sell_with_identical_account_type_is_rejected`
    が担う。本テストはそれとは別に「実際のCLI利用(account_type省略)での
    BUY/SELL取り違えが拒否される」という現実的な回帰として残す。"""
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
    )

    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        env["service"].register_sell(
            DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
        )


def test_reusing_key_for_buy_vs_sell_with_identical_account_type_is_rejected(env) -> None:
    """サブちゃんレビュー#624 F8対応: 上記テストはaccount_type(BUYの既定
    GENERAL・SELLの常にNone)も異なるため、区分(is_buy)照合だけを落とす
    変異がSURVIVEDしていた(F7でaccount_typeを照合対象へ追加した副作用。
    F5(N3)で一度固定した「区分照合の単独性」が退行していた)。

    register_buy()/register_sell()の公開APIだけではaccount_typeを完全に
    揃えたBUY/SELLの組を作れない(register_sell()はaccount_type概念を
    持たず常にNoneを要求するため、register_buy()側もaccount_type=Noneを
    渡す必要があるが、それはPurchaseLot/Holdingの必須フィールド検証で
    失敗する)。そのため、既存Transaction側を`TransactionHistoryService`
    経由で直接account_type=None(SELLが要求する値と同一)で用意し、
    stock_code/shares/price/execution_date/owner/account_typeの6条件を
    全て揃えたうえで、区分(is_buy)だけを変えたSELLが拒否されることを
    固定する。"""
    tx_history = TransactionHistoryService(transaction_repository=env["tx_repo"])
    existing = tx_history.build_execution_plan(
        transaction_id="shared-key",
        owner=DEFAULT_OWNER,
        stock_code=_STOCK,
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("1000"),
        execution_date=_NOW.date(),
        account_type=None,
        now=_NOW,
    )
    assert env["tx_repo"].save_if_absent(existing)

    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        env["service"].register_sell(
            DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
        )


def test_reusing_key_for_different_trade_date_is_rejected(env) -> None:
    """サブちゃんレビュー#624 F1'対応: 銘柄・株数・単価・区分が全て同じで
    約定日だけが異なる場合も、日付違いの別取引が黙って消える(F1と同じ
    失敗モード)ため拒否する(fail-closedを優先する判断。#624 issuecomment
    参照)。"""
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), dt.date(2026, 9, 26), "shared-key", _NOW
    )

    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        env["service"].register_buy(
            DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), dt.date(2026, 9, 27), "shared-key", _NOW
        )

    assert env["holding_repo"].get(_HOLDING_ID).shares == 100  # 2回目は消えている
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("900000")


def test_race_path_also_rejects_mismatched_trade(env) -> None:
    """サブちゃんレビュー#624 F5(N2)対応: fast-pathの照合だけを外す変異が
    SURVIVEDした、という指摘。fast-pathを迂回させ(モンキーパッチ)、
    `_commit_locally()`内のrace経路自身の照合が独立して機能することを
    固定する。"""
    _seed_cash(env, "1000000")
    service = env["service"]
    service.register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
    )

    # fast-pathを常に「未登録」として扱わせ、race経路(save_if_absent()が
    # Falseを返す分岐)へ強制的に到達させる。
    service._fast_path_if_already_registered = lambda *args, **kwargs: None  # noqa: SLF001

    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        service.register_sell(
            DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
        )


def test_reusing_key_for_different_shares_is_rejected(env) -> None:
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
    )

    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        env["service"].register_buy(
            DEFAULT_OWNER, _STOCK, 200, Decimal("1000"), _NOW.date(), "shared-key", _NOW
        )


def test_reusing_key_for_different_price_is_rejected(env) -> None:
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
    )

    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        env["service"].register_buy(
            DEFAULT_OWNER, _STOCK, 100, Decimal("2000"), _NOW.date(), "shared-key", _NOW
        )


def test_reusing_key_for_different_stock_code_is_rejected(env) -> None:
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
    )

    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        env["service"].register_buy(
            DEFAULT_OWNER, "7203", 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
        )


def test_genuinely_identical_retry_still_treated_as_already_registered(env) -> None:
    """取り違え検出が、真の冪等retry(全項目が一致)を誤って拒否しないこと。"""
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
    )

    result = env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
    )

    assert result.already_registered is True


def test_genuinely_identical_sell_retry_is_still_treated_as_already_registered(env) -> None:
    """サブちゃんレビュー#624 F9対応: SELLの真の冪等retry(同一owner・銘柄・
    株数・単価・約定日・区分)を固定するテストが無かった。BUY側の対称テスト
    (`test_genuinely_identical_retry_still_treated_as_already_registered`)は
    account_type既定GENERALを暗黙に使うためBUY専用の検証にしかならず、
    `register_sell()`がaccount_type=Noneを渡すべきところを誤って別の値へ
    変更する変異が入っても検出できなかった(崩れると正当なSELLのretryが
    exit 1で誤って拒否される。fail-closed側の誤りなのでF1/F6/F7ほど深刻
    ではないがSHOULD FIX)。"""
    _seed_cash(env, "1000000")
    env["portfolio"].register_purchase(
        owner=DEFAULT_OWNER,
        stock_code=_STOCK,
        stock_name=None,
        shares=100,
        purchase_price=Decimal("1000"),
        purchase_date=_NOW.date(),
        account_type=AccountType.GENERAL,
    )
    env["service"].register_sell(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1800"), _NOW.date(), "idem-sell", _NOW
    )

    retry = env["service"].register_sell(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1800"), _NOW.date(), "idem-sell", _NOW
    )

    assert retry.already_registered is True


def test_race_path_also_accepts_genuine_buy_retry(env) -> None:
    """サブちゃんレビュー#624 F9再レビュー指摘対応(任意・追加実施): 上記
    `test_genuinely_identical_retry_still_treated_as_already_registered`は
    fast-pathのみを固定しており、race経路(`_commit_locally()`内、
    `save_if_absent()`がFalseを返す分岐)自身でBUYの真の冪等retryが正しく
    受理されることは固定されていなかった(register_buy()が`_commit_locally()`
    へ渡すaccount_typeを誤って固定値へ変更する変異がSURVIVEDしうる)。
    fast-pathを迂回させ(F5/F6/F7と同じmonkeypatchパターン)、race経路自身の
    成功側契約を固定する。"""
    _seed_cash(env, "1000000")
    service = env["service"]
    service.register_buy(
        DEFAULT_OWNER,
        _STOCK,
        100,
        Decimal("1000"),
        _NOW.date(),
        "idem-1",
        _NOW,
        account_type=AccountType.NISA,
    )

    service._fast_path_if_already_registered = lambda *args, **kwargs: None  # noqa: SLF001

    retry = service.register_buy(
        DEFAULT_OWNER,
        _STOCK,
        100,
        Decimal("1000"),
        _NOW.date(),
        "idem-1",
        _NOW,
        account_type=AccountType.NISA,
    )

    assert retry.already_registered is True
    assert env["holding_repo"].get(_HOLDING_ID).shares == 100  # 二重適用されていない
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("900000")  # 二重減算なし


def test_race_path_also_accepts_genuine_sell_retry(env) -> None:
    """サブちゃんレビュー#624 F9再レビュー指摘対応(任意・追加実施): 上記
    `test_genuinely_identical_sell_retry_is_still_treated_as_already_registered`
    はfast-pathのみを固定しており、race経路自身でSELLの真の冪等retryが
    正しく受理されることは固定されていなかった(サブちゃんの実測で、
    `register_sell()`が`_commit_locally()`へ渡すaccount_type=Noneを誤って
    別の値へ変更する変異がSURVIVEDすることを確認済み)。retry時にも
    `build_sale_write_plan()`(plan構築のみ、race経路がsave_if_absent()で
    Falseと判定すれば適用されない)が成立するよう、1回目のSELLで保有株を
    使い切らない数量にしておく。"""
    _seed_cash(env, "1000000")
    env["portfolio"].register_purchase(
        owner=DEFAULT_OWNER,
        stock_code=_STOCK,
        stock_name=None,
        shares=250,
        purchase_price=Decimal("1000"),
        purchase_date=_NOW.date(),
        account_type=AccountType.GENERAL,
    )
    service = env["service"]
    service.register_sell(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1800"), _NOW.date(), "idem-sell", _NOW
    )
    assert env["holding_repo"].get(_HOLDING_ID).shares == 150

    service._fast_path_if_already_registered = lambda *args, **kwargs: None  # noqa: SLF001

    retry = service.register_sell(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1800"), _NOW.date(), "idem-sell", _NOW
    )

    assert retry.already_registered is True
    # race経路のno-opがLot/Holdingへ二重適用されていない(150株のまま)。
    assert env["holding_repo"].get(_HOLDING_ID).shares == 150
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("1180000")  # 1回分のみ


# --- サブちゃんレビュー#624 F6対応: idempotency-key照合へownerを追加 ---------
# Transactionはowner-scopeでCLIにも--ownerがある。owner=Aの1回目登録と、
# 銘柄・株数・単価・約定日・BUY/SELL区分が全て同一だがownerだけ異なる
# owner=Bの2回目が、F1/F1'と同じ失敗モード(黙って消える)にならないことを
# 固定する(fast-path・race-pathの双方)。

_OTHER_OWNER = "owner-b"


def test_reusing_key_for_different_owner_is_rejected(env) -> None:
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
    )

    # owner=Bはavailable_cash未登録のままでも、fast-pathでの照合が
    # cashチェックより先に働き拒否されることを確認する(owner不一致検出が
    # 他のガードに依存しない独立した防御であることの確認を兼ねる)。
    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        env["service"].register_buy(
            _OTHER_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
        )

    # owner=Aの取引が黙って消えていない(owner=Bのholdingが作られていない)ことを確認する。
    assert env["holding_repo"].get(build_holding_id(DEFAULT_OWNER, _STOCK)).shares == 100
    assert env["holding_repo"].get(build_holding_id(_OTHER_OWNER, _STOCK)) is None


def test_race_path_also_rejects_owner_mismatch(env) -> None:
    """F5(N2)と同じ考え方: fast-pathを迂回させ、race経路
    (`_commit_locally()`内、`save_if_absent()`がFalseを返す分岐)自身の
    owner照合が独立して機能することを固定する。"""
    _seed_cash(env, "1000000")
    _seed_cash(env, "1000000", owner=_OTHER_OWNER)
    service = env["service"]
    service.register_buy(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
    )

    service._fast_path_if_already_registered = lambda *args, **kwargs: None  # noqa: SLF001

    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        service.register_buy(
            _OTHER_OWNER, _STOCK, 100, Decimal("1000"), _NOW.date(), "shared-key", _NOW
        )


# --- サブちゃんレビュー#624 F7対応: account_typeをTransactionへも伝搬・照合 ---
# register_buy()はaccount_type(既定GENERAL)をLot/Holdingへは反映していたが、
# Transactionへは渡しておらずaccount_type=Noneになっていた(サービスAPIとして
# account_typeを公開している以上、永続データ間で一致させるべき、というUSER
# 判断〔決定A〕)。あわせて、口座種別だけが異なる場合も別取引として扱う。


def test_register_buy_propagates_account_type_to_lot_holding_and_transaction(env) -> None:
    _seed_cash(env, "1000000")

    env["service"].register_buy(
        DEFAULT_OWNER,
        _STOCK,
        100,
        Decimal("1000"),
        _NOW.date(),
        "idem-nisa",
        _NOW,
        account_type=AccountType.NISA,
    )

    assert env["holding_repo"].get(_HOLDING_ID).account_type == AccountType.NISA
    assert env["lot_repo"].get("idem-nisa").account_type == AccountType.NISA
    assert env["tx_repo"].get("idem-nisa").account_type == AccountType.NISA


def test_reusing_key_for_different_account_type_is_rejected(env) -> None:
    _seed_cash(env, "1000000")
    env["service"].register_buy(
        DEFAULT_OWNER,
        _STOCK,
        100,
        Decimal("1000"),
        _NOW.date(),
        "shared-key",
        _NOW,
        account_type=AccountType.GENERAL,
    )

    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        env["service"].register_buy(
            DEFAULT_OWNER,
            _STOCK,
            100,
            Decimal("1000"),
            _NOW.date(),
            "shared-key",
            _NOW,
            account_type=AccountType.NISA,
        )


def test_race_path_also_rejects_account_type_mismatch(env) -> None:
    """F5(N2)と同じ考え方: fast-pathを迂回させ、race経路自身の
    account_type照合が独立して機能することを固定する。"""
    _seed_cash(env, "1000000")
    service = env["service"]
    service.register_buy(
        DEFAULT_OWNER,
        _STOCK,
        100,
        Decimal("1000"),
        _NOW.date(),
        "shared-key",
        _NOW,
        account_type=AccountType.GENERAL,
    )

    service._fast_path_if_already_registered = lambda *args, **kwargs: None  # noqa: SLF001

    with pytest.raises(IdempotencyKeyReusedForDifferentTradeError):
        service.register_buy(
            DEFAULT_OWNER,
            _STOCK,
            100,
            Decimal("1000"),
            _NOW.date(),
            "shared-key",
            _NOW,
            account_type=AccountType.NISA,
        )


# --- D3: 未登録owner・余力不足の拒否(#591のAvailableCashServiceをそのまま再利用) ---


def test_register_buy_rejects_unregistered_owner(env) -> None:
    with pytest.raises(AvailableCashNotRegisteredError):
        env["service"].register_buy(
            DEFAULT_OWNER, _STOCK, 100, Decimal("1500"), _NOW.date(), "idem-1", _NOW
        )

    assert env["tx_repo"].get("idem-1") is None
    assert env["holding_repo"].get(_HOLDING_ID) is None


def test_register_buy_rejects_insufficient_cash_before_any_write(env) -> None:
    _seed_cash(env, "1000")

    with pytest.raises(InsufficientAvailableCashError):
        env["service"].register_buy(
            DEFAULT_OWNER, _STOCK, 100, Decimal("1500"), _NOW.date(), "idem-1", _NOW
        )

    assert env["tx_repo"].get("idem-1") is None
    assert env["holding_repo"].get(_HOLDING_ID) is None
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("1000")  # 変化なし


def test_register_sell_never_rejected_by_available_cash_amount(env) -> None:
    # register_purchase()はavailable_cashに一切触れない既存の直接適用経路
    # (register_buy()経由ではない)ため、cash=0のままholdingだけ用意できる。
    env["portfolio"].register_purchase(
        owner=DEFAULT_OWNER,
        stock_code=_STOCK,
        stock_name=None,
        shares=100,
        purchase_price=Decimal("1000"),
        purchase_date=_NOW.date(),
        account_type=AccountType.GENERAL,
    )
    _seed_cash(env, "0")

    result = env["service"].register_sell(
        DEFAULT_OWNER, _STOCK, 100, Decimal("1800"), _NOW.date(), "idem-sell", _NOW
    )

    assert result.already_registered is False
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("180000")


# --- 部分適用防止(#590の性質のCLI版での再確認) -------------------------------


def test_concurrent_cash_change_between_plan_build_and_commit_rolls_back_everything(env) -> None:
    """delta基準値とCASのexpected_dataが同一の読み取りから導出される(#620 F1)
    ため、plan構築後・commit前に別経路が割り込んだ場合はCASが正しく失敗し、
    Transaction/Lot/Holding/AvailableCashのいずれも部分適用にならない。"""
    _seed_cash(env, "1000000")
    ac_service = env["ac_service"]
    original = ac_service.build_trade_update_plan

    def racy(owner, delta, now):
        plan = original(owner, delta, now)
        ac_service.reconcile(owner, Decimal("999999"), now)  # 別経路の割り込み
        return plan

    ac_service.build_trade_update_plan = racy

    # ConcurrentUpdateErrorはValueErrorのサブクラス(write_plan.py)であり、
    # CLI層がgeneric except ValueErrorで捕捉して生のtracebackを出さずに
    # 済んでいる(サブちゃんレビュー#624 F3)。型を固定して検証する。
    with pytest.raises(ConcurrentUpdateError):
        env["service"].register_buy(
            DEFAULT_OWNER, _STOCK, 100, Decimal("1500"), _NOW.date(), "idem-1", _NOW
        )

    assert env["tx_repo"].get("idem-1") is None
    assert env["holding_repo"].get(_HOLDING_ID) is None
    assert env["lot_repo"].get("idem-1") is None
    # 割り込んだ側の値(999999)がそのまま残り、こちらの失敗した試行による
    # 変更は一切残らない。
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("999999")


def test_holding_update_failure_rolls_back_already_created_lot(env) -> None:
    """Holding側のCASが失敗した場合、直前に書き込んだLotもロールバックされる
    (Transaction/AvailableCashも含め全て未適用の状態へ戻る)。"""
    _seed_cash(env, "1000000")
    holding_repo = env["holding_repo"]

    # apply_conditional_put()は新規Holding(expected_data=None)を
    # insert_if_absent()経由で書き込む。ここを失敗させて、直前に書いた
    # Lotまでロールバックされることを確認する。
    def failing_insert(model):
        raise RuntimeError("simulated holding write failure")

    holding_repo._store.insert_if_absent = failing_insert

    with pytest.raises(RuntimeError):
        env["service"].register_buy(
            DEFAULT_OWNER, _STOCK, 100, Decimal("1500"), _NOW.date(), "idem-1", _NOW
        )

    assert env["tx_repo"].get("idem-1") is None
    assert env["lot_repo"].get("idem-1") is None  # 直前に書いたLotもロールバック
    assert env["holding_repo"].get(_HOLDING_ID) is None
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("1000000")  # 変化なし


def test_sell_failure_restores_deleted_and_updated_lots(env) -> None:
    """サブちゃんレビュー#624 F2対応: SELLは「消費した既存ロットを戻す」形状
    (lot_deletes/lot_puts)であり、BUY(新規lot_putのみ)とは異なる。この
    ロールバック経路がBUYの検証だけでは1件もカバーされていなかった
    (lot_deletesのsnapshotを取らない変異がSURVIVEDした、という指摘)ため、
    FIFO消費で1ロットを全部消費(delete)+もう1ロットを一部消費(put)する
    ケースで、失敗時に両方とも元の内容へ戻ることを固定する。"""
    _seed_cash(env, "1000000")
    portfolio = env["portfolio"]
    # 2ロットを直接作る(register_buyを2回使うとavailable_cashも絡むため、
    # 既存のregister_purchase()〔available_cashに触れない既存経路〕で
    # FIFO対象のロット構成だけを単純に用意する)。
    portfolio.register_purchase(
        owner=DEFAULT_OWNER,
        stock_code=_STOCK,
        stock_name=None,
        shares=50,
        purchase_price=Decimal("1000"),
        purchase_date=dt.date(2026, 9, 1),
        account_type=AccountType.GENERAL,
        lot_id="lot-a",
    )
    portfolio.register_purchase(
        owner=DEFAULT_OWNER,
        stock_code=_STOCK,
        stock_name=None,
        shares=60,
        purchase_price=Decimal("1000"),
        purchase_date=dt.date(2026, 9, 2),
        account_type=AccountType.GENERAL,
        lot_id="lot-b",
    )
    assert env["holding_repo"].get(_HOLDING_ID).shares == 110

    # SELL 70株: FIFOでlot-a(50株)を全部消費(delete)、lot-b(60株)を
    # 一部消費(40株へ更新)する構成になる。
    holding_repo = env["holding_repo"]

    def failing_replace(item_id, expected_raw_data, item):
        raise RuntimeError("simulated holding write failure")

    holding_repo._store.replace_if_raw_matches = failing_replace

    with pytest.raises(RuntimeError):
        env["service"].register_sell(
            DEFAULT_OWNER, _STOCK, 70, Decimal("1800"), _NOW.date(), "idem-sell", _NOW
        )

    assert env["tx_repo"].get("idem-sell") is None
    assert env["lot_repo"].get("lot-a") is not None  # 削除されたロットが復元されている
    assert env["lot_repo"].get("lot-a").shares == 50
    assert env["lot_repo"].get("lot-b").shares == 60  # 更新されたロットが元に戻っている
    assert env["holding_repo"].get(_HOLDING_ID).shares == 110  # 変化なし
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("1000000")  # 変化なし
