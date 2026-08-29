"""Issue #85 Phase B1: producer → consumer の safety contract 横断検証。

## このテストが検証する不変条件

**REQUIRED safety contract として登録された producer → consumer 経路では、
producer 側が事実を確定できなかった(failure)とき、危険な downstream action が
抑止される。**

個々の経路の詳細(除外理由の文言、data_error の内容、監査記録の項目など)は
各Issueの個別テストが担保する。本ファイルはそれらを焼き直さず、
**「登録された全経路で同じ不変条件が成り立つ」という横断的性質**だけを見る。

## なぜ横断で見る必要があるか

Issue #81 は「開示情報を取得できなかった銘柄をウォッチリストへ自動追加しない」
という保護が、**本番で使われていない policy にしか接続されていなかった**ために
機能していなかった、という欠陥だった。個別テストは存在したが、
「全 policy でその保護が接続されているか」を見る仕組みが無かった。

そのため本ファイルは、契約の検証に加えて
**registry の更新漏れを検知する網羅チェック**(下記 §policy網羅)を持つ。

## registry は第二の仕様実装ではない

`SafetyContractCase` は「どの consumer がどの fact に依存し、
failure 時に何を抑止するのか」を**宣言**するだけで、production のロジックを
テスト側で再実装しない。`act` は必ず production の実関数を呼び、
`dangerous_action_occurred` はその戻り値を読むだけにとどめる。

## 「取得できて0件」の意味は contract ごとに異なる

`successful_empty` を全 contract へ一律適用しない。
disclosure 系では「調べたが該当報告書が0件」= 正常だが、
provider failure 系では「取得に成功したが任意項目が空」= 正常であり、
表す対象がそもそも違う。各 contract が自分の意味を宣言する。
"""

from __future__ import annotations

import datetime as dt
import typing
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, cast

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import WatchlistScreeningRulesConfig
from jstock_advisor.domain.entities.enums import BUY_FAMILY_ACTIONS, RecommendationType
from jstock_advisor.domain.signals.watchlist_screening import categorize_exclusion_reasons
from jstock_advisor.interfaces.disclosure import (
    DisclosureAvailability,
    DisclosureUnavailableReason,
)
from jstock_advisor.interfaces.provider_errors import (
    ProviderDataError,
    ProviderFailureCategory,
)
from jstock_advisor.lambda_handlers import watchlist_worker_handler as worker_module
from jstock_advisor.services import buy_signal_service as buy_signal_service_module
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.screening_data_provider import (
    DISCLOSURE_AVAILABILITY_FIELD_NAME,
    ScreeningDataStatus,
)
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningService

from .test_buy_signal_service import (
    _CALENDAR,
    _CONFIG,
    _NIHON_SHINYAKU,
    _NOW,
    _build_snapshot,
    _providers,
)
from .test_watchlist_screening_policy import _good_input

_WATCHLIST_NOW = dt.datetime(2026, 8, 29, 0, 0, tzinfo=dt.UTC)


# ============================================================================
# 契約の宣言に使う語彙(テスト内でのみ使う。production へ abstraction を作らない)
# ============================================================================


class Requirement(StrEnum):
    """producer が提供する事実を、consumer がどの強さで必要とするか。

    **REQUIRED のものだけを本ファイルの横断検証対象にする。**
    FALLBACK_ALLOWED / OPTIONAL は期待挙動が「抑止」ではないため、
    同じ assert を当てると仕様を誤って固定してしまう(下記 §対象外 参照)。
    """

    REQUIRED = "REQUIRED"
    FALLBACK_ALLOWED = "FALLBACK_ALLOWED"
    OPTIONAL = "OPTIONAL"


class SemanticCase(StrEnum):
    """契約ごとに用意する入力の種類。**意味は contract 側が定義する。**"""

    FAILURE = "FAILURE"
    NORMAL = "NORMAL"
    SUCCESSFUL_EMPTY = "SUCCESSFUL_EMPTY"


@dataclass(frozen=True)
class SafetyContractCase:
    """1つの producer → consumer 経路の safety contract 宣言。

    **守るべき契約**(dataclass のフィールド集合そのものは固定しない。
    description / related_issue 等の監査用metadataを将来追加してよい):

    - production の判定を registry へ再実装しない
    - threshold / business expected value を registry へ持たせない
    - `act` は production の実経路を呼ぶ
    - `spec_ref` を必須とする
    - `SemanticCase` の意味を case ごとに明示する
    - `case_id` は一意
    """

    case_id: str
    producer: str
    fact: str
    consumer: str
    dangerous_action: str
    requirement: Requirement
    # 仕様上の裏付け。**空文字を許さない**(裏付けの無い契約を登録させない)
    spec_ref: str
    # SemanticCase → その contract における意味の説明(何を表す入力か)
    semantics: dict[SemanticCase, str]
    arrange: Callable[[SemanticCase], Any]
    act: Callable[[Any], Any]
    dangerous_action_occurred: Callable[[Any], bool]

    def __str__(self) -> str:  # pytest の parametrize id に使う
        return self.case_id


# ============================================================================
# P1: disclosure UNAVAILABLE → BUY判定
# ============================================================================


def _p1_arrange(case: SemanticCase) -> DisclosureAvailability:
    return {
        SemanticCase.FAILURE: DisclosureAvailability.UNAVAILABLE,
        SemanticCase.NORMAL: DisclosureAvailability.AVAILABLE,
        SemanticCase.SUCCESSFUL_EMPTY: DisclosureAvailability.AVAILABLE,
    }[case]


def _p1_act(availability: DisclosureAvailability, monkeypatch: pytest.MonkeyPatch) -> Any:
    snapshot = _build_snapshot(
        _NIHON_SHINYAKU,
        disclosure_availability=availability,
        disclosure_unavailable_reason=(
            DisclosureUnavailableReason.TEMPORARY_FAILURE
            if availability is DisclosureAvailability.UNAVAILABLE
            else None
        ),
        disclosure_risk_keywords_found=[],
    )
    monkeypatch.setattr(
        buy_signal_service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None)
    )
    service = BuySignalService(providers=_providers(), config=_CONFIG, business_calendar=_CALENDAR)
    return service.analyze(_NIHON_SHINYAKU.stock_code, _NOW, RecommendationType.BUY)


def _p1_dangerous(outcome: Any) -> bool:
    """危険な downstream action = 「新規買い候補として通す」。"""
    return outcome.buy_action in BUY_FAMILY_ACTIONS or outcome.recommendation is not None


P1_BUY_DISCLOSURE = SafetyContractCase(
    case_id="disclosure_unavailable__to__buy_candidate",
    producer="EDINET disclosure provider",
    fact="DisclosureAvailability.UNAVAILABLE",
    consumer="BuySignalService.analyze",
    dangerous_action="新規買い候補として通す",
    requirement=Requirement.REQUIRED,
    spec_ref="docs/functional_spec.md 5.10節「買い候補の判定…新規の候補から外します」",
    semantics={
        SemanticCase.FAILURE: "EDINETを調べられなかった(重大リスク開示の有無が不明)",
        SemanticCase.NORMAL: "調べられて、通常の判定が成立する",
        SemanticCase.SUCCESSFUL_EMPTY: "調べられて対象の報告書が0件(=開示リスクなし。正常)",
    },
    arrange=_p1_arrange,
    act=_p1_act,
    dangerous_action_occurred=_p1_dangerous,
)


# ============================================================================
# P2: disclosure UNAVAILABLE → watchlist 自動追加
# ============================================================================


def _p2_arrange(case: SemanticCase) -> Any:
    base = _good_input(disclosure_risk_keywords_found=[])
    if case is SemanticCase.FAILURE:
        # providerは型付きフラグと項目名の両方を同時に立てる(#81)
        return replace(
            base,
            disclosure_available=False,
            missing_required_fields=[
                *base.missing_required_fields,
                DISCLOSURE_AVAILABILITY_FIELD_NAME,
            ],
        )
    return base


def _p2_act(screening_input: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """consumer は Service → worker の categorize までを実関数で通す。"""
    service = WatchlistScreeningService(load_config())
    result = service.evaluate("9999", "テスト株式会社", screening_input, _WATCHLIST_NOW)
    _category, evaluation_result = categorize_exclusion_reasons(result.exclusion_reasons)
    return evaluation_result


def _p2_dangerous(evaluation_result: str) -> bool:
    """危険な downstream action = 「auto-add の対象に選ばれる」。

    finalizer は evaluation_result == "PASSED" の行のみを追加対象に選ぶ
    (services/watchlist_batch_finalizer.py の _compute_finalize_target)。
    """
    return evaluation_result == "PASSED"


P2_WATCHLIST_DISCLOSURE = SafetyContractCase(
    case_id="disclosure_unavailable__to__watchlist_auto_add",
    producer="EDINET disclosure provider",
    fact="disclosure_available=False",
    consumer="WatchlistScreeningService.evaluate",
    dangerous_action="ウォッチリストへ自動追加される",
    requirement=Requirement.REQUIRED,
    spec_ref="docs/functional_spec.md 5.10節「ウォッチリストへの自動追加…新規の候補から外します」",
    semantics={
        SemanticCase.FAILURE: "EDINETを調べられなかった",
        SemanticCase.NORMAL: "調べられて、通常の判定が成立する",
        SemanticCase.SUCCESSFUL_EMPTY: "調べられて対象の報告書が0件(=正常。追加候補になり得る)",
    },
    arrange=_p2_arrange,
    act=_p2_act,
    dangerous_action_occurred=_p2_dangerous,
)


# ============================================================================
# P5: provider FAILURE(ProviderDataError) → watchlist 自動追加
# ============================================================================


class _StubScreeningDataProvider:
    """`get_screening_input` の戻り値だけを固定する最小のstub。

    #59 の contract(FAILURE ≠ SUCCESS + missing/empty)を再実装せず、
    provider が返す2状態を worker へ与えるだけ。
    """

    def __init__(self, *, raise_provider_error: bool) -> None:
        self._raise = raise_provider_error

    def get_screening_input(self, stock_code: str, now: dt.datetime) -> Any:
        from jstock_advisor.services.screening_data_provider import ScreeningDataResult

        if self._raise:
            # provider層の失敗は screening_data_provider が DATA_ERROR へ写像する
            exc = ProviderDataError(
                provider_name="yfinance",
                operation="get_latest_price",
                retryable=True,
                failure_category=ProviderFailureCategory.RETRYABLE_PROVIDER_FAILURE,
                error_type="HTTPError",
                error_summary="429 Too Many Requests",
            )
            return ScreeningDataResult(
                status=ScreeningDataStatus.DATA_ERROR,
                input=None,
                missing_fields=[],
                error_message=str(exc),
            )
        return ScreeningDataResult(
            status=ScreeningDataStatus.OK,
            input=_good_input(disclosure_risk_keywords_found=[]),
            missing_fields=[],
            error_message=None,
        )


def _p5_arrange(case: SemanticCase) -> _StubScreeningDataProvider:
    return _StubScreeningDataProvider(raise_provider_error=case is SemanticCase.FAILURE)


def _p5_act(provider: _StubScreeningDataProvider, monkeypatch: pytest.MonkeyPatch) -> str:
    """production の `_evaluate_candidate()` を**そのまま**呼ぶ。

    差し替えるのは **producer 境界(provider の生成)だけ**で、
    `ScreeningDataStatus` → `evaluation_result` の変換は
    production の worker 実装が行う。テスト側でこの分岐を再実装すると、
    worker で `DATA_ERROR → PASSED` へ退行しても本テストが green のままになる。
    """
    monkeypatch.setattr(
        worker_module, "build_screening_data_provider", lambda *a, **kw: provider
    )
    # 監査記録は本契約の対象外(外部依存を増やさないため無効化する)
    monkeypatch.setattr(worker_module, "record_candidate_audit", lambda *a, **kw: None)

    outcome = worker_module._evaluate_candidate(
        stock_code="9999",
        batch_id="batch-safety-contract",
        now=_WATCHLIST_NOW,
        providers=cast(Any, object()),
        config=load_config(),
    )
    return outcome.evaluation_result


P5_PROVIDER_FAILURE = SafetyContractCase(
    case_id="provider_failure__to__watchlist_auto_add",
    producer="market/dividend/corporate_action/financial provider",
    fact="ProviderDataError(取得失敗)",
    consumer="watchlist worker(ScreeningDataStatus 分岐)",
    dangerous_action="ウォッチリストへ自動追加される",
    requirement=Requirement.REQUIRED,
    spec_ref="Issue #59(provider例外semanticsの分離)。FAILURE ≠ SUCCESS + missing/zero/empty",
    semantics={
        SemanticCase.FAILURE: "provider が例外を返した(取得できていない)",
        SemanticCase.NORMAL: "provider が正常に値を返した",
        SemanticCase.SUCCESSFUL_EMPTY: "取得に成功し、任意項目が空(=欠測。障害ではない)",
    },
    arrange=_p5_arrange,
    act=_p5_act,
    dangerous_action_occurred=_p2_dangerous,
)


# ============================================================================
# registry(REQUIRED のみ)
# ============================================================================

REQUIRED_SAFETY_CONTRACTS: list[SafetyContractCase] = [
    P1_BUY_DISCLOSURE,
    P2_WATCHLIST_DISCLOSURE,
    P5_PROVIDER_FAILURE,
]

# --- 意図的に登録しない経路(over-constraint を避けるための記録)---------------
#
# 以下は「provider が事実を確定できなかったときに危険な行動を抑止する」という
# 本ファイルの不変条件に**当てはまらない**。同じ assert を当てると仕様に反する。
#
#   P3 disclosure UNAVAILABLE → 利確/保有継続判定
#      requirement=FALLBACK_ALLOWED。期待挙動は「抑止」ではなく
#      **「売却理由にしない」**(EDINET障害を売却の引き金にしてはならない)。
#      spec 5.10節・profit_taking_service.py のコメント参照。
#
#   P4 disclosure UNAVAILABLE → 保有銘柄の開示リスク速報
#      requirement=FALLBACK_ALLOWED。期待挙動は**「リスク通知を送らない」**。
#      取得失敗を警告通知へ変換しない(spec 5.10節)。
#
#   P6 JpxLookupStatus.SOURCE_UNAVAILABLE → BUY判定
#      requirement=OPTIONAL(観測専用)。canonical_industry.py に
#      **「B-1ではいずれの状態でもBUY判定を変えない」**と明記されている。
#      抑止を期待するテストは仕様違反になる。
#
#   P7 sector_entries coverage 不足 → 買い増し集中度ゲート
#      現在は fail-close だが、**Issue #82 で契約自体が変更予定**のため登録しない。
# ----------------------------------------------------------------------------


# ============================================================================
# 横断不変条件
# ============================================================================


@pytest.mark.parametrize("contract", REQUIRED_SAFETY_CONTRACTS, ids=str)
def test_failure_suppresses_dangerous_downstream_action(
    contract: SafetyContractCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """【本ファイルの中心】producer が失敗したとき、危険な行動が抑止される。"""
    arranged = contract.arrange(SemanticCase.FAILURE)
    result = contract.act(arranged, monkeypatch)

    assert not contract.dangerous_action_occurred(result), (
        f"[{contract.case_id}] {contract.fact} にもかかわらず"
        f"「{contract.dangerous_action}」が抑止されていない。"
        f" 根拠: {contract.spec_ref}"
    )


@pytest.mark.parametrize("contract", REQUIRED_SAFETY_CONTRACTS, ids=str)
def test_normal_input_is_not_suppressed_by_this_contract(
    contract: SafetyContractCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常時は当該契約を理由に抑止しない。

    これが無いと「常に抑止する」実装でも上のテストが通ってしまう
    (抑止テストがトートロジーでないことの担保)。
    """
    arranged = contract.arrange(SemanticCase.NORMAL)
    result = contract.act(arranged, monkeypatch)

    assert contract.dangerous_action_occurred(result), (
        f"[{contract.case_id}] 正常入力にもかかわらず"
        f"「{contract.dangerous_action}」が成立しない。"
        " 抑止テストがトートロジーになっている可能性がある"
    )


@pytest.mark.parametrize("contract", REQUIRED_SAFETY_CONTRACTS, ids=str)
def test_successful_empty_is_not_treated_as_failure(
    contract: SafetyContractCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「取得できて中身が空」を failure と同一視しない。

    **意味は contract ごとに異なる**(semantics で宣言)。
    disclosure 系は「調べたが対象0件」、provider 系は「取得成功だが任意項目が空」。
    """
    arranged = contract.arrange(SemanticCase.SUCCESSFUL_EMPTY)
    result = contract.act(arranged, monkeypatch)

    assert contract.dangerous_action_occurred(result), (
        f"[{contract.case_id}] {contract.semantics[SemanticCase.SUCCESSFUL_EMPTY]}"
        " を failure と同一視して抑止している"
    )


# ============================================================================
# registry の健全性 / 更新漏れ検知
# ============================================================================


@pytest.mark.parametrize("contract", REQUIRED_SAFETY_CONTRACTS, ids=str)
def test_every_contract_declares_spec_reference(contract: SafetyContractCase) -> None:
    """仕様の裏付けが無い契約を登録できないようにする。"""
    assert contract.spec_ref.strip(), f"[{contract.case_id}] spec_ref が空"
    assert contract.requirement is Requirement.REQUIRED, (
        f"[{contract.case_id}] REQUIRED 以外を本registryへ入れない"
        "(FALLBACK_ALLOWED / OPTIONAL は期待挙動が異なる)"
    )
    for case in SemanticCase:
        assert case in contract.semantics, f"[{contract.case_id}] {case} の意味が未宣言"


def test_contract_case_ids_are_unique() -> None:
    ids = [c.case_id for c in REQUIRED_SAFETY_CONTRACTS]

    assert len(ids) == len(set(ids))


# --- policy 網羅チェック(#81 の再発防止の中核) ------------------------------


def _declared_screening_policies() -> tuple[str, ...]:
    """screening policy 名の**正式な宣言元**から集合を取得する。

    `config/models.py` の `WatchlistScreeningRulesConfig.screening_policy` は
    `Literal[...]` で有効値を宣言しており、これが config schema 上の正本である。
    `watchlist_screening_service._build_policy()` にも分岐があるが、
    private 実装よりこちらを優先する。
    """
    annotation = WatchlistScreeningRulesConfig.model_fields["screening_policy"].annotation
    return typing.get_args(annotation)


def test_every_declared_policy_suppresses_auto_add_when_disclosure_unavailable() -> None:
    """**宣言済みの全 screening policy** で、開示情報を取得できなかったときに
    auto-add が抑止されることを確認する。

    Issue #81 は「保護が**本番で使われている policy へ接続されていなかった**」
    欠陥だった。個別テストは active policy 1つしか見ておらず、
    policy を差し替えた瞬間に保護が失われる構造を検出できなかった。

    本テストが保証するのは **policy 集合の網羅**であって、
    registry(`REQUIRED_SAFETY_CONTRACTS`)との対応ではない。
    新しい policy を追加した場合、その policy でも安全gateが効いていなければ
    **本テストを更新しなくても失敗する**(= 実装側の未接続を検出する)。

    policy 名の列挙元は `config/models.py` の
    `WatchlistScreeningRulesConfig.screening_policy: Literal[...]` である。
    これは config schema 上の正本であり、private な `_build_policy()` には
    依存しない。ここで列挙を使うのは private 実装の検証のためではなく、
    **「宣言された policy をひとつも取りこぼさない」ことを担保するため**である。
    """
    declared = set(_declared_screening_policies())

    covered = set()
    for policy_name in declared:
        config = load_config()
        service = WatchlistScreeningService(
            config.model_copy(
                update={
                    "watchlist_screening": config.watchlist_screening.model_copy(
                        update={"screening_policy": policy_name}
                    )
                }
            )
        )
        screening_input = _p2_arrange(SemanticCase.FAILURE)
        result = service.evaluate("9999", "テスト株式会社", screening_input, _WATCHLIST_NOW)
        _category, evaluation_result = categorize_exclusion_reasons(result.exclusion_reasons)
        if not _p2_dangerous(evaluation_result):
            covered.add(policy_name)

    assert covered == declared, (
        "開示情報を取得できなかったときに auto-add を抑止しない policy がある: "
        f"{sorted(declared - covered)}。"
        " 新しい policy を追加した場合は、その policy でも安全gateが"
        " 接続されていることを確認すること(Issue #81 と同型の欠陥)"
    )


def test_active_policy_is_a_declared_policy() -> None:
    """config の実値が宣言集合に含まれる(config だけ差し替えた場合の検知)。"""
    active = load_config().watchlist_screening.screening_policy

    assert active in _declared_screening_policies()
