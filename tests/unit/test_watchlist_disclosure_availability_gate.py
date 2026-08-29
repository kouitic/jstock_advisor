"""Issue #81: watchlistのcritical data availability gate(開示情報の取得可否)。

#53 Phase B2は「開示情報を確認できなかった銘柄はウォッチリストへ自動追加しない」
という契約(docs/functional_spec.md 5.10節)を定めたが、実際の除外判定は
`missing_required_fields` → `DATA_INSUFFICIENT` という**旧Policyにしか存在しない**
経路へ接続されていたため、本番の`multi_style_monitoring`では機能していなかった。

このテストは、定数値の照合ではなく**実際にServiceを実行して**挙動を固定する
(定数だけをassertするテストは、保護が完全に壊れていても失敗しないため)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.signals.watchlist_screening import (
    ExclusionReason,
    HighDividendFinancialHealthPolicy,
    MultiStyleMonitoringPolicy,
    categorize_exclusion_reasons,
)
from jstock_advisor.services.screening_data_provider import (
    DISCLOSURE_AVAILABILITY_FIELD_NAME,
    REQUIRED_FIELD_NAMES,
    WatchlistScreeningInput,
)
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningService

from .test_watchlist_screening_policy import _good_input

_NOW = dt.datetime(2026, 8, 29, 0, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def app_config():
    return load_config()


def _multi_style_service(app_config) -> WatchlistScreeningService:
    return WatchlistScreeningService(
        app_config, policies=[MultiStyleMonitoringPolicy(app_config.screening)]
    )


def _high_dividend_service(app_config) -> WatchlistScreeningService:
    return WatchlistScreeningService(app_config, policies=[HighDividendFinancialHealthPolicy()])


def _unavailable(**overrides) -> WatchlistScreeningInput:
    """開示情報を取得できなかった状態のinput(providerが作るものと同じ形)。

    providerは型付きの`disclosure_available=False`と、監査・表示用の
    `missing_required_fields`への項目名追加を**両方**行う。
    """
    base = _good_input(**overrides)
    return replace(
        base,
        disclosure_available=False,
        missing_required_fields=[
            *base.missing_required_fields,
            DISCLOSURE_AVAILABILITY_FIELD_NAME,
        ],
    )


def _evaluate(service: WatchlistScreeningService, input: WatchlistScreeningInput):
    return service.evaluate("9999", "テスト株式会社", input, _NOW)


# --- 1 / 7: AVAILABLE + 開示0件は正常系(#53導入前と同じ) --------------------


def test_multi_style_available_with_no_disclosures_passes(app_config) -> None:
    """AVAILABLE + [] は DATA_INSUFFICIENT ではない。auto-add対象になれる。"""
    result = _evaluate(_multi_style_service(app_config), _good_input())

    assert result.passed is True
    assert ExclusionReason.DATA_INSUFFICIENT not in result.exclusion_reasons
    _, evaluation_result = categorize_exclusion_reasons(result.exclusion_reasons)
    assert evaluation_result == "PASSED"


# --- 2 / 12: UNAVAILABLE は DATA_INSUFFICIENT ---------------------------------


def test_multi_style_unavailable_is_data_insufficient(app_config) -> None:
    """本番Policyでも開示取得不能はDATA_INSUFFICIENTになる(#81の中心)。"""
    result = _evaluate(_multi_style_service(app_config), _unavailable())

    assert result.passed is False
    assert result.exclusion_reasons == [ExclusionReason.DATA_INSUFFICIENT]


def test_available_and_unavailable_are_distinguished(app_config) -> None:
    """「取得できて0件」と「取得できなかった」を同一視しない回帰テスト。"""
    service = _multi_style_service(app_config)

    available = _evaluate(service, _good_input())
    unavailable = _evaluate(service, _unavailable())

    assert available.passed is True
    assert unavailable.passed is False
    assert available.exclusion_reasons != unavailable.exclusion_reasons


def test_unavailable_never_becomes_disclosure_risk(app_config) -> None:
    """「調べられなかった」を「危険な開示があった」へ変換しない。"""
    result = _evaluate(_multi_style_service(app_config), _unavailable())

    assert ExclusionReason.HARD_EXCLUDED not in result.exclusion_reasons
    for policy_result in result.policy_results:
        assert policy_result.hard_exclusion_reasons == []
        assert policy_result.hard_exclusion_codes == []


# --- 3: worker の evaluation_result --------------------------------------------


def test_worker_evaluation_result_is_data_insufficient(app_config) -> None:
    """worker(watchlist_worker_handler.py)が記録する値までDATA_INSUFFICIENTになる。"""
    result = _evaluate(_multi_style_service(app_config), _unavailable())

    category, evaluation_result = categorize_exclusion_reasons(result.exclusion_reasons)

    assert category == "data_insufficient"
    assert evaluation_result == "DATA_INSUFFICIENT"


# --- 4: finalizer が auto-add 対象に選ばない ------------------------------------


def test_finalizer_does_not_auto_add_unavailable(app_config) -> None:
    """finalizerはPASSED行のみをauto-add対象に選ぶため、対象へ入らない。"""
    from jstock_advisor.infrastructure.aws.batch_tracker import CandidateProgressRecord
    from jstock_advisor.services.watchlist_batch_finalizer import _compute_finalize_target

    service = _multi_style_service(app_config)
    result = _evaluate(service, _unavailable())
    _, evaluation_result = categorize_exclusion_reasons(result.exclusion_reasons)
    ranking_entry = service.to_ranking_entry(result)

    record = CandidateProgressRecord(
        batch_id="batch-1",
        stock_code="9999",
        status="COMPLETED",
        dispatched=True,
        evaluation_result=evaluation_result,
        ranking_entry=ranking_entry.model_dump_json() if ranking_entry is not None else None,
        lease_owner_id=None,
        attempt_count=1,
        total_processing_duration_ms=0,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        total_score=result.total_score,
        notification_detail=None,
    )

    within_limit, over_limit = _compute_finalize_target([record], app_config)

    assert within_limit == []
    assert over_limit == []


# --- 5: HighDividend の既存挙動は不変 -------------------------------------------


def test_high_dividend_unavailable_still_data_insufficient(app_config) -> None:
    """旧Policyでも従来どおりDATA_INSUFFICIENT(共通gate追加後も結果が変わらない)。"""
    result = _evaluate(_high_dividend_service(app_config), _unavailable())

    assert result.passed is False
    assert result.exclusion_reasons == [ExclusionReason.DATA_INSUFFICIENT]


def test_high_dividend_policy_level_contract_unchanged(app_config) -> None:
    """Policy単体(service経由でない)の`missing_required_fields`契約は変更しない。"""
    policy_result = HighDividendFinancialHealthPolicy().evaluate(
        _unavailable(), app_config.watchlist_screening
    )

    assert policy_result.passed is False
    assert policy_result.exclusion_reasons == [ExclusionReason.DATA_INSUFFICIENT]


# --- 6: 開示リスクキーワード検出の既存ハード除外は不変 ---------------------------


def test_disclosure_risk_keyword_still_hard_excluded(app_config) -> None:
    """「調べた結果リスクがあった」は従来どおりHARD_EXCLUDED(gateとは別経路)。"""
    result = _evaluate(
        _multi_style_service(app_config),
        _good_input(disclosure_risk_keywords_found=["不適切会計"]),
    )

    assert result.passed is False
    assert result.exclusion_reasons == [ExclusionReason.HARD_EXCLUDED]
    _, evaluation_result = categorize_exclusion_reasons(result.exclusion_reasons)
    assert evaluation_result == "FAILED_REQUIRED"


# --- 8 / 9 / 10: 他のrequired fieldへ波及しない ---------------------------------


@pytest.mark.parametrize("field_name", REQUIRED_FIELD_NAMES)
def test_other_missing_required_fields_do_not_become_hard_gate(
    app_config, field_name: str
) -> None:
    """#81のgateは開示取得可否のみが対象。他のrequired fieldの扱いは変更しない。

    multi_style_monitoringは`shares_outstanding`/`operating_cashflow`の欠損を
    除外理由にしない(加点対象外になるだけ)。ここを一括ハード除外すると
    #53と無関係なPolicy変更になるため、その退行を防ぐ。
    """
    result = _evaluate(
        _multi_style_service(app_config), _good_input(missing_required_fields=[field_name])
    )

    assert result.passed is True
    assert ExclusionReason.DATA_INSUFFICIENT not in result.exclusion_reasons


def test_gate_does_not_key_off_missing_required_fields_string(app_config) -> None:
    """gateは型付きフィールドを見る。文字列リストへの混入だけでは除外しない。

    (providerは両方を必ず同時にセットするため、この状態は本来発生しない。
    実装が文字列照合へ退行していないことを固定するためのテスト。)
    """
    result = _evaluate(
        _multi_style_service(app_config),
        _good_input(missing_required_fields=[DISCLOSURE_AVAILABILITY_FIELD_NAME]),
    )

    assert result.passed is True


def test_provider_sets_both_typed_flag_and_field_name() -> None:
    """provider契約: 型付きフラグと項目名の両方が同時に立つ。"""
    input_dto = _unavailable()

    assert input_dto.disclosure_available is False
    assert DISCLOSURE_AVAILABILITY_FIELD_NAME in input_dto.missing_required_fields
