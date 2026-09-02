"""Issue #120 の Production incident (2026-09-02 08:00 JST) の回帰テスト。

## 何が起きたか

優待レジストリの健全性チェック(`check_registry_health`)が、読み取りAPIに見える
`list_all()`を呼んだところ、その内部で権利確定日の再計算結果が
`repository.save()`(DynamoDB PutItem)へ書き戻されていた。BUY/保有Lambdaは
`ShareholderBenefitsTable`へ読み取り専用IAMしか持たないため
`AccessDeniedException`となり、**dispatch前にバッチ全体が停止**した
(当日の買い候補判定・保有/利確判定・LINE通知が全件未実行)。

書き戻しは「再計算値が保存値と異なるとき」だけ発生するため、権利確定日の
繰り上がりが起きる日付境界を越えて初めて顕在化した。かつAccessDeniedにより
保存値が更新されないため、**修正するまで毎回失敗し続ける**。

## このテストが固定する契約

1. 読み取りAPI(`get`/`list_all`/provider)は`save`へ到達しない
2. 繰り上がりが必要な状態でも、何度読んでも、日を跨いでも到達しない
3. 健全性チェックは自身の失敗でバッチを停止させない(fail-soft)。ただし沈黙しない
4. 真の書き込みAPIは従来どおり保存する

## このテストが固定「しない」こと

`next_benefit_record_date`が**JSTの何日に**切り替わるべきか。現行実装は
`now.date()`(UTC暦日)を基準としており、JST暦日規約とは1日ずれる。この是正は
Issue #120のスコープ外(別Issue)であり、ここでUTC基準の挙動を「正しい仕様」
として固定してはならない。本ファイルは「境界を越えても読み取りが書き込まない」
ことだけを検証し、境界日そのものの正しさは主張しない。
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.enums import BenefitUtilityCategory
from jstock_advisor.domain.valuation.shareholder_benefit_matching import (
    compute_next_record_date,
)
from jstock_advisor.infrastructure.local_repository.shareholder_benefit_registry_repository import (
    ShareholderBenefitRegistryRepository,
)
from jstock_advisor.providers.shareholder_benefit.local_registry_impl import (
    LocalRegistryShareholderBenefitProvider,
)
from jstock_advisor.services.shareholder_benefit_registry_service import (
    ShareholderBenefitRegistryService,
    check_registry_health,
)

_STOCK = "2914"
# 「毎年8月末」。2026-08-31 が権利確定日であり、これを越えると 2027-08-31 へ繰り上がる。
_RECURRENCE = [8]

# incidentを再現する3点。08:00 JST 実行時に now.date() (UTC暦日) が
# 2026-08-31 / 2026-09-01 のどちらになるかで save 要否が変わっていた。
_BEFORE_ROLLOVER = dt.datetime(2026, 8, 31, 23, 0, tzinfo=dt.UTC)  # = 2026-09-01 08:00 JST
_ON_ROLLOVER = dt.datetime(2026, 9, 1, 23, 0, tzinfo=dt.UTC)  # = 2026-09-02 08:00 JST(incident当日)
_AFTER_ROLLOVER = dt.datetime(2026, 9, 2, 23, 0, tzinfo=dt.UTC)  # = 2026-09-03 08:00 JST

_ACCESS_DENIED = ClientError(
    {
        "Error": {
            "Code": "AccessDeniedException",
            "Message": (
                "User: .../jstock-advisor-buy-candidates is not authorized to perform: "
                "dynamodb:PutItem on resource: .../table/jstock-shareholder_benefits"
            ),
        }
    },
    "PutItem",
)


class _WriteDeniedRepository(ShareholderBenefitRegistryRepository):
    """読み取りは通常どおり、書き込みは本番同様にAccessDeniedを返すリポジトリ。

    「save()が呼ばれないこと」をカウンタで確認するだけでは、呼ばれた場合に
    テストが素通りしうる。本番の失敗そのものを再現し、読み取り経路がsaveへ
    到達したらテストが必ず落ちるようにする。
    """

    def __init__(self, store_dir: Path) -> None:
        super().__init__(store_dir=store_dir)
        self.save_attempts = 0
        self.deny = False

    def save(self, benefit: object) -> None:  # type: ignore[override]
        self.save_attempts += 1
        if self.deny:
            raise _ACCESS_DENIED
        super().save(benefit)  # type: ignore[arg-type]


@pytest.fixture
def repository(tmp_path: Path) -> _WriteDeniedRepository:
    return _WriteDeniedRepository(tmp_path)


@pytest.fixture
def service(repository: _WriteDeniedRepository) -> ShareholderBenefitRegistryService:
    return ShareholderBenefitRegistryService(repository=repository)


def _register(
    service: ShareholderBenefitRegistryService, now: dt.datetime
) -> None:
    service.register(
        stock_code=_STOCK,
        min_shares_required=100,
        frequency_per_year=1,
        category=BenefitUtilityCategory.CASH_EQUIVALENT,
        description="優待",
        min_shares_for_tier=100,
        estimated_value=Decimal("1000"),
        benefit_record_date_recurrence_months=_RECURRENCE,
        now=now,
    )


def _arm(repository: _WriteDeniedRepository) -> None:
    """登録が終わったあと、以後のsaveを本番同様のAccessDeniedにする。"""
    repository.save_attempts = 0
    repository.deny = True


# --- A. read purity -----------------------------------------------------------


def test_registered_state_is_stale_before_the_boundary_is_crossed(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    """前提の確認: 保存値は繰り上がり前の日付(2026-08-31)で保存されている。

    この保存値が古いままであることが、以降のテストが再現する条件の起点になる。
    """
    _register(service, _BEFORE_ROLLOVER)
    stored = repository.get(_STOCK)
    assert stored is not None
    assert stored.next_benefit_record_date == dt.date(2026, 8, 31)


@pytest.mark.parametrize(
    ("label", "now"),
    [
        ("rollover前日", _BEFORE_ROLLOVER),
        ("rollover当日", _ON_ROLLOVER),
        ("rollover翌日", _AFTER_ROLLOVER),
    ],
)
def test_get_never_persists_across_the_rollover_boundary(
    service: ShareholderBenefitRegistryService,
    repository: _WriteDeniedRepository,
    label: str,
    now: dt.datetime,
) -> None:
    _register(service, _BEFORE_ROLLOVER)
    _arm(repository)

    benefit = service.get(_STOCK, now=now)

    assert benefit is not None, label
    assert repository.save_attempts == 0, f"{label}: get()がsave()へ到達した"


@pytest.mark.parametrize(
    ("label", "now"),
    [
        ("rollover前日", _BEFORE_ROLLOVER),
        ("rollover当日", _ON_ROLLOVER),
        ("rollover翌日", _AFTER_ROLLOVER),
    ],
)
def test_list_all_never_persists_across_the_rollover_boundary(
    service: ShareholderBenefitRegistryService,
    repository: _WriteDeniedRepository,
    label: str,
    now: dt.datetime,
) -> None:
    _register(service, _BEFORE_ROLLOVER)
    _arm(repository)

    entries = service.list_all(now=now)

    assert len(entries) == 1, label
    assert repository.save_attempts == 0, f"{label}: list_all()がsave()へ到達した"


def test_read_returns_the_rederived_value_without_persisting_it(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    """読み取りは保存済みの派生値をそのまま返さず、現行の計算契約で再導出した値を返す。

    その一方で保存値は書き換えない(読み取りは永続化を伴わない)。
    """
    _register(service, _BEFORE_ROLLOVER)
    _arm(repository)

    returned = service.get(_STOCK, now=_ON_ROLLOVER)
    stored = repository.get(_STOCK)

    assert returned is not None
    assert stored is not None
    assert returned.next_benefit_record_date != stored.next_benefit_record_date, (
        "再導出されていない(保存済みの派生値をそのまま返している)"
    )
    assert stored.next_benefit_record_date == dt.date(2026, 8, 31), "読み取りが保存値を書き換えた"
    assert repository.save_attempts == 0


def test_repeated_reads_do_not_persist(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    _register(service, _BEFORE_ROLLOVER)
    _arm(repository)

    for _ in range(5):
        service.get(_STOCK, now=_ON_ROLLOVER)
        service.list_all(now=_ON_ROLLOVER)

    assert repository.save_attempts == 0


def test_reads_across_successive_days_do_not_persist(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    """日を跨いで繰り返し読んでも書き込みが発生しない(incidentの再発シナリオ)。"""
    _register(service, _BEFORE_ROLLOVER)
    _arm(repository)

    day = _BEFORE_ROLLOVER
    for _ in range(40):
        service.list_all(now=day)
        day += dt.timedelta(days=1)

    assert repository.save_attempts == 0


# --- B. incident reproduction -------------------------------------------------


def test_health_check_survives_the_incident_conditions(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    """2026-09-02 incidentの入力条件そのもの。

    古い next_benefit_record_date + 繰り上がり後の基準日 + save()がAccessDenied。
    修正前の実装ではここで ClientError が check_registry_health の外へ伝播し、
    BUY/保有 handler が dispatch 前に停止していた。
    """
    _register(service, _BEFORE_ROLLOVER)
    _arm(repository)

    # 例外が外へ出ないこと(handlerがここで停止しない)
    check_registry_health(min_expected_entries=1, service=service)

    assert repository.save_attempts == 0, "incidentの直接原因(読み取り経路の書き込み)が残っている"


def test_health_check_still_reports_the_count_under_incident_conditions(
    service: ShareholderBenefitRegistryService,
    repository: _WriteDeniedRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """incident条件でも「握り潰して無言」にならず、通常どおり件数INFOが出る。

    読み取りが純粋になった結果、fail-softへ退避することなく本来の観測が成立する。
    """
    _register(service, _BEFORE_ROLLOVER)
    _arm(repository)

    with caplog.at_level("INFO"):
        check_registry_health(min_expected_entries=1, service=service)

    assert "loaded 1 entries" in caplog.text
    assert "health_check_failed" not in caplog.text


# --- C. fail-soft -------------------------------------------------------------


class _ExplodingService(ShareholderBenefitRegistryService):
    def __init__(self, error: Exception) -> None:
        self._error = error

    def list_all(self, now: dt.datetime | None = None) -> list:  # type: ignore[override]
        raise self._error


@pytest.mark.parametrize(
    ("label", "error"),
    [
        ("AccessDeniedException", _ACCESS_DENIED),
        (
            "ClientError(Throttling)",
            ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
                "Scan",
            ),
        ),
        ("想定外例外", RuntimeError("unexpected")),
    ],
)
def test_health_check_is_fail_soft_for_any_failure(
    label: str, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """健全性チェック自身の失敗はバッチを停止させない。

    読み取りを純粋化しても`list_all()`はDynamoDB Scanのままであり、
    スロットリング等の一過性エラーで同じ全停止が起こりうる。fail-softは
    書き込み除去とは独立に必要。
    """
    with caplog.at_level("ERROR"):
        check_registry_health(min_expected_entries=1, service=_ExplodingService(error))

    assert "shareholder_benefit_registry_health_check_failed" in caplog.text, (
        f"{label}: 失敗を沈黙させている(観測できない)"
    )


# --- D. 既存の正常系(件数ログ・WARNING条件)を壊さない ------------------------


def test_health_check_warns_when_below_threshold(
    service: ShareholderBenefitRegistryService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        check_registry_health(min_expected_entries=1, service=service)
    assert "loaded 0 entries" in caplog.text
    assert "expected at least 1" in caplog.text


def test_health_check_disabled_threshold_still_logs_count(
    service: ShareholderBenefitRegistryService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        check_registry_health(min_expected_entries=0, service=service)
    assert "loaded 0 entries" in caplog.text
    assert "expected at least" not in caplog.text


# --- E. 真の書き込みAPIは従来どおり保存する ------------------------------------


def test_register_still_persists(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    _register(service, _BEFORE_ROLLOVER)
    assert repository.save_attempts == 1
    assert repository.get(_STOCK) is not None


def test_set_record_date_recurrence_still_persists_the_recomputed_date(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    _register(service, _BEFORE_ROLLOVER)
    repository.save_attempts = 0

    updated = service.set_record_date_recurrence(_STOCK, [3], now=_ON_ROLLOVER)

    assert repository.save_attempts == 1
    stored = repository.get(_STOCK)
    assert stored is not None
    assert stored.benefit_record_date_recurrence_months == [3]
    assert stored.next_benefit_record_date == updated.next_benefit_record_date


def test_update_status_still_persists(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    _register(service, _BEFORE_ROLLOVER)
    repository.save_attempts = 0
    service.update_status(_STOCK, is_abolished=True, now=_ON_ROLLOVER)
    assert repository.save_attempts == 1
    stored = repository.get(_STOCK)
    assert stored is not None and stored.is_abolished is True


def test_add_benefit_detail_still_persists(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    _register(service, _BEFORE_ROLLOVER)
    repository.save_attempts = 0
    service.add_benefit_detail(
        stock_code=_STOCK,
        category=BenefitUtilityCategory.CASH_EQUIVALENT,
        description="追加段階",
        min_shares_for_tier=200,
        now=_ON_ROLLOVER,
    )
    assert repository.save_attempts == 1
    stored = repository.get(_STOCK)
    assert stored is not None and len(stored.benefits) == 2


def test_delete_still_removes(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    _register(service, _BEFORE_ROLLOVER)
    assert service.delete(_STOCK) is True
    assert repository.get(_STOCK) is None


# --- F. business provider -----------------------------------------------------


def test_business_provider_does_not_write(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    """判定側が使うprovider経由でもShareholderBenefitsTableへの書き込みが発生しない。"""
    _register(service, _BEFORE_ROLLOVER)
    _arm(repository)

    provider = LocalRegistryShareholderBenefitProvider(repository=repository, now=_ON_ROLLOVER)
    benefit = provider.get_shareholder_benefit(_STOCK)

    assert benefit is not None
    assert repository.save_attempts == 0


def test_business_provider_rederives_instead_of_returning_the_stored_value(
    service: ShareholderBenefitRegistryService, repository: _WriteDeniedRepository
) -> None:
    """保存済みの派生値をそのまま返さず、現行の計算契約に従って再導出する。

    サービス層の読み取りAPIと同じ値になること(経路によって値が食い違わないこと)を
    固定する。どの暦日を基準にするかはIssue #120のスコープ外。
    """
    _register(service, _BEFORE_ROLLOVER)
    _arm(repository)

    provider = LocalRegistryShareholderBenefitProvider(repository=repository, now=_ON_ROLLOVER)
    from_provider = provider.get_shareholder_benefit(_STOCK)
    from_service = service.get(_STOCK, now=_ON_ROLLOVER)
    stored = repository.get(_STOCK)

    assert from_provider is not None and from_service is not None and stored is not None
    assert from_provider.next_benefit_record_date == from_service.next_benefit_record_date
    assert from_provider.next_benefit_record_date != stored.next_benefit_record_date
    assert repository.save_attempts == 0


def test_provider_returns_none_for_unregistered_stock(
    repository: _WriteDeniedRepository,
) -> None:
    provider = LocalRegistryShareholderBenefitProvider(repository=repository, now=_ON_ROLLOVER)
    assert provider.get_shareholder_benefit("9999") is None
    assert repository.save_attempts == 0


# --- E. 採用設計の固定(Issue #120 duplicate reconciliation で移植) -----------
#
# 以下2契約は、独立実装 4b99269 で固定されていたがcanonical側に無かったため移植した。
# production codeは変更していない(canonical実装のまま)。


class _BusinessDataUnavailableRepository(ShareholderBenefitRegistryRepository):
    """判定に必要な優待データの取得自体が失敗するリポジトリ。"""

    def list_all(self) -> list:  # type: ignore[override]
        raise RuntimeError("registry backend is unavailable")

    def get(self, stock_code: str) -> object | None:  # type: ignore[override]
        raise RuntimeError("registry backend is unavailable")


def test_fail_soft_is_scoped_to_the_health_check_only(tmp_path: Path) -> None:
    """fail-softは健全性チェック自身の失敗に限る(Contract B)。

    `check_registry_health`は失敗を握り潰してよいが、**判定に使う取得経路まで
    握り潰してはならない**。握り潰すと、優待データが取れていないのに
    「優待なし」として判定が進み、誤った投資判断につながる。

    canonical実装のdocstringはこの限定を明記しているが、テストで固定されて
    いなかったため追加する(実装は変更していない)。
    """
    repository = _BusinessDataUnavailableRepository(store_dir=tmp_path)
    service = ShareholderBenefitRegistryService(repository=repository)
    provider = LocalRegistryShareholderBenefitProvider(
        repository=repository, now=_ON_ROLLOVER
    )

    # 健全性チェックは止まらない(fail-soft)
    check_registry_health(min_expected_entries=1, service=service)

    # 判定側の取得経路は従来どおり失敗として終端する(fail-close)
    with pytest.raises(RuntimeError):
        service.list_all(now=_ON_ROLLOVER)
    with pytest.raises(RuntimeError):
        service.get(_STOCK, now=_ON_ROLLOVER)
    with pytest.raises(RuntimeError):
        provider.get_shareholder_benefit(_STOCK)


class _CfnLoader(yaml.SafeLoader):
    """CloudFormationの短縮形組み込み関数を素朴なdictへ変換する構文解析専用Loader
    (tests/unit/test_issue_116_jpx_shadow_wiring.py と同一手法)。
    """


def _cfn_multi_constructor(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.ScalarNode):
        return {tag_suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag_suffix: loader.construct_sequence(node)}
    assert isinstance(node, yaml.MappingNode)  # noqa: S101 - CFNタグはこの3種のみ
    return {tag_suffix: loader.construct_mapping(node)}


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)  # type: ignore[no-untyped-call]

_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "infra" / "template.yaml"
_TABLE_LOGICAL_ID = "ShareholderBenefitsTable"
_WRITE_ACTIONS = frozenset(
    {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
    }
)
# Issue #120 の hidden-write audit で、ShareholderBenefitsTable を読み取り専用で
# 参照していることを確認した全Function。
_READ_ONLY_CONSUMERS = (
    "BuyCandidatesFunction",
    "HoldingsWatchlistFunction",
    "WatchlistDispatcherFunction",
    "WatchlistWorkerFunction",
    "WatchlistBatchReconcilerFunction",
)


def _granted_actions_for_table(logical_id: str, table_logical_id: str) -> set[str]:
    resources = yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_CfnLoader)[
        "Resources"
    ]
    assert logical_id in resources, f"{logical_id} が template.yaml に存在しない"
    granted: set[str] = set()
    for policy in resources[logical_id]["Properties"].get("Policies", []):
        if not (isinstance(policy, dict) and "Statement" in policy):
            continue
        for statement in policy["Statement"]:
            actions = statement.get("Action")
            actions = actions if isinstance(actions, list) else [actions]
            resource = statement.get("Resource")
            resource = resource if isinstance(resource, list) else [resource]
            if table_logical_id not in json.dumps(resource, ensure_ascii=False):
                continue
            granted.update(str(a) for a in actions)
    return granted


@pytest.mark.parametrize("function_logical_id", _READ_ONLY_CONSUMERS)
def test_shareholder_benefits_table_stays_read_only(function_logical_id: str) -> None:
    """優待テーブルへ書き込み権限を足して解決しないこと(Contract A)。

    Issue #120 では案A(`dynamodb:PutItem` を付与する)を**採用しなかった**
    (`IAM_PUTITEM_ADDITION=NO`)。read APIの裏でwriteする設計を追認してしまい、
    同じ欠陥が別の場所で再発するためである。是正すべきは権限側ではなく責務側。

    将来「AccessDeniedが出たから権限を足す」という安易な修正へ戻ることを防ぐ。
    """
    granted = _granted_actions_for_table(function_logical_id, _TABLE_LOGICAL_ID)

    assert granted, f"{function_logical_id} が {_TABLE_LOGICAL_ID} を参照していない"
    assert not (granted & _WRITE_ACTIONS), (
        f"{function_logical_id} に {_TABLE_LOGICAL_ID} への書き込み権限が付与されている: "
        f"{sorted(granted & _WRITE_ACTIONS)}。"
        "Issue #120 は権限追加ではなく読み取りの純化で解決する方針である。"
    )


def _tz_neutral_now(reference_date: dt.date) -> dt.datetime:
    """指定した暦日を、UTC暦日とJST暦日が一致する時刻として返す(Contract C)。

    本ファイル上部の`_BEFORE_ROLLOVER`等はUTC暦日とJST暦日が食い違う時刻
    (23:00Z = 翌日08:00 JST)であり、incidentの実行時刻を忠実に再現する一方、
    **繰り上がりの境界がどちら側に来るかが実装の採る暦に依存する**。
    そのためIssue #66でJST暦日基準へ是正すると、それらのfixtureは境界を
    跨がなくなる。

    ここでは 00:00 UTC(= 09:00 JST)を用いることで、どちらの暦を採っても
    同じ暦日となる時刻を作り、**暦の選択から独立に read purity を保証する**。
    """
    now = dt.datetime.combine(reference_date, dt.time(0, 0), tzinfo=dt.UTC)
    jst_date = now.astimezone(dt.timezone(dt.timedelta(hours=9))).date()
    # 中立性を維持するためのガード。15:00Z以降の時刻へ書き換えるとここで落ちる。
    assert now.date() == jst_date == reference_date, (  # noqa: S101
        "テスト時刻がUTC暦日とJST暦日で食い違っている。"
        "Issue #66 の論点を固定しないよう、暦日が一致する時刻を使うこと。"
    )
    return now


@pytest.mark.parametrize(
    "reference_date",
    [
        dt.date(2026, 8, 31),  # 権利確定日当日(「以降」に含まれるため繰り上がらない)
        dt.date(2026, 9, 1),  # 翌日(繰り上がる = 保存値と乖離しはじめる)
        dt.date(2027, 1, 15),  # 十分に後
    ],
)
def test_read_purity_at_boundary_is_independent_of_calendar_choice(
    service: ShareholderBenefitRegistryService,
    repository: _WriteDeniedRepository,
    reference_date: dt.date,
) -> None:
    """境界の両側で読み取りが書き込まないことを、暦の選択から独立に固定する。

    **固定するのは「書き込みが発生しないこと」であって、どの暦日で繰り上がるかではない。**
    期待値は`compute_next_record_date()`から導出し、切り替わり日をテスト側へ
    ハードコードしない。基準日をUTC暦日/JST暦日のどちらで決めるかはIssue #66の
    対象であり、本Issueでは変更しない(`_tz_neutral_now()`のdocstring参照)。
    """
    _register(service, _tz_neutral_now(dt.date(2026, 1, 15)))
    _arm(repository)

    now = _tz_neutral_now(reference_date)
    expected = compute_next_record_date(_RECURRENCE, reference_date)

    returned = service.get(_STOCK, now=now)

    assert returned is not None
    assert returned.next_benefit_record_date == expected
    assert repository.save_attempts == 0
