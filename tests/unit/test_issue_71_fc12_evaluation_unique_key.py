"""Issue #71 F-C12: 定点評価の並行二重保存を一意キーでの条件付き insert で防ぐ。

## なぜこのテストが要るか

`run_due_evaluations()` は run 開始時に `CompletedHorizonIndex` を **1 回だけ**
読み、そのあとは索引だけを見て「未評価か」を判定する(Issue #113。ループ内で
全件 Scan しないため)。★ したがって索引は **run 開始時点のスナップショット**で
あり、その後に別実行が保存した分は見えない。

    実行 A  索引を読む(rec-1 / 5 営業日 は未評価)
    実行 B  索引を読む(同上)
    実行 B  評価して保存
    実行 A  評価して保存   <- ★ 是正前はここで 2 件目が入っていた

保存先の `evaluation_id` が `uuid4()` だったため、同じ評価でもキーが毎回違い、
無条件 `upsert` が両方成立していた。

## このテストが固定すること

    (i)   一意キーが決定的であること(同じ評価 -> 同じキー)
    (ii)  軸・ホライズンが違えば別キーであること(衝突させない)
    (iii) ★ semantics 版が違えば別レコードとして保存できること(移行窓の表現)
    (iv)  ★ 事前確認を両実行が通り抜けても、保存は 1 件・成功計上は 1 回であること
    (v)   競合が無い通常経路は従来どおりであること

★ (iv) は**是正前のコードでは必ず失敗する**。2 件保存されるためである。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvaluationLabel,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import (
    EVALUATION_SEMANTICS_V1,
    EvaluationResult,
    build_evaluation_id,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
)

# ★ リポジトリ共通の mock fixture が持つ銘柄コード(providers/mock_fixtures.py の
# MOCK_STOCKS)。MockMarketDataProvider は fixture に無いコードだと株価を返さず、
# 評価が「データ取得不能」になるため、既存の
# tests/unit/test_recommendation_evaluation_service.py と同じ値を使う。
# ★ 保有銘柄ではない(保有数量・取得価格も一切扱わない)。
_STOCK_CODE = "2914"
_RECOMMENDATION_ID = "rec-fc12"
_RECOMMENDED_AT = dt.datetime(2024, 1, 4, tzinfo=dt.UTC)
_NOW = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)
_SEMANTICS_V2 = "v2"


@pytest.fixture
def config() -> AppConfig:
    return load_config()


@pytest.fixture
def calendar(config: AppConfig) -> BusinessCalendar:
    return BusinessCalendar.from_config(config.holiday_calendar)


def _make_recommendation() -> Recommendation:
    return Recommendation(
        recommendation_id=_RECOMMENDATION_ID,
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        recommended_at=_RECOMMENDED_AT,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            standard=PriceWithRationale(price=Decimal("2000"), rationale="x"),
        ),
        price_at_recommendation=Decimal("2200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _make_evaluation(
    *,
    horizon_business_days: int | None = 5,
    horizon_calendar_days: int | None = None,
    semantics_version: str = EVALUATION_SEMANTICS_V1,
    price_return_pct: float = 1.0,
) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=build_evaluation_id(
            _RECOMMENDATION_ID,
            horizon_business_days=horizon_business_days,
            horizon_calendar_days=horizon_calendar_days,
            semantics_version=semantics_version,
        ),
        recommendation_id=_RECOMMENDATION_ID,
        horizon_business_days=horizon_business_days,
        horizon_calendar_days=horizon_calendar_days,
        evaluated_at=_NOW,
        evaluation_date=dt.date(2024, 1, 12),
        price_at_evaluation=Decimal("2222"),
        price_return_pct=price_return_pct,
        evaluation_label=EvaluationLabel.ACCEPTABLE,
        label_evidence="test",
        evaluation_semantics_version=semantics_version,
    )


# --- (i)(ii)(iii) 一意キーの性質 ---------------------------------------------


def test_key_is_deterministic_for_the_same_evaluation() -> None:
    """★ 決定的でなければ条件付き insert は効かない。ここが土台である。"""
    first = build_evaluation_id(_RECOMMENDATION_ID, horizon_business_days=5)
    second = build_evaluation_id(_RECOMMENDATION_ID, horizon_business_days=5)
    assert first == second


def test_key_differs_by_axis_and_horizon() -> None:
    """営業日軸と暦日軸、異なるホライズンは衝突してはならない。"""
    business_5 = build_evaluation_id(_RECOMMENDATION_ID, horizon_business_days=5)
    business_20 = build_evaluation_id(_RECOMMENDATION_ID, horizon_business_days=20)
    calendar_5 = build_evaluation_id(_RECOMMENDATION_ID, horizon_calendar_days=5)
    assert len({business_5, business_20, calendar_5}) == 3


def test_key_differs_by_semantics_version() -> None:
    """★ (iii) 版が違えば別キー。これが移行窓を表現できる根拠である。"""
    v1 = build_evaluation_id(_RECOMMENDATION_ID, horizon_business_days=5)
    v2 = build_evaluation_id(
        _RECOMMENDATION_ID, horizon_business_days=5, semantics_version=_SEMANTICS_V2
    )
    assert v1 != v2


def test_key_requires_exactly_one_horizon() -> None:
    """両方指定・両方未指定はいずれも誤りとして弾く。"""
    with pytest.raises(ValueError, match="どちらか一方のみ"):
        build_evaluation_id(_RECOMMENDATION_ID)
    with pytest.raises(ValueError, match="どちらか一方のみ"):
        build_evaluation_id(_RECOMMENDATION_ID, horizon_business_days=5, horizon_calendar_days=5)


# --- 条件付き insert のリポジトリ契約 ----------------------------------------


def test_second_insert_with_the_same_key_is_rejected_and_does_not_overwrite(
    tmp_path: Path,
) -> None:
    """★ 2 回目は False を返し、1 回目の値を書き換えない。"""
    repo = EvaluationResultRepository(store_dir=tmp_path)

    assert repo.insert_if_absent(_make_evaluation(price_return_pct=1.0)) is True
    assert repo.insert_if_absent(_make_evaluation(price_return_pct=99.0)) is False

    saved = repo.list_by_recommendation(_RECOMMENDATION_ID)
    assert len(saved) == 1
    assert saved[0].price_return_pct == 1.0, "★ 先に保存した側の値が維持されること"


def test_different_semantics_versions_are_stored_as_separate_records(tmp_path: Path) -> None:
    """★ (iii) 移行窓。v1 と v2 は互いを弾かず、2 件として共存できる。"""
    repo = EvaluationResultRepository(store_dir=tmp_path)

    assert repo.insert_if_absent(_make_evaluation()) is True
    assert repo.insert_if_absent(_make_evaluation(semantics_version=_SEMANTICS_V2)) is True

    saved = repo.list_by_recommendation(_RECOMMENDATION_ID)
    assert len(saved) == 2
    assert {e.evaluation_semantics_version for e in saved} == {
        EVALUATION_SEMANTICS_V1,
        _SEMANTICS_V2,
    }


def test_existing_records_without_the_version_field_default_to_v1() -> None:
    """★ 既定値が無いと、本変更より前の行が読めなくなり全推奨が未評価扱いになる。"""
    evaluation = EvaluationResult(
        evaluation_id="legacy-uuid",
        recommendation_id=_RECOMMENDATION_ID,
        horizon_business_days=5,
        evaluated_at=_NOW,
        evaluation_date=dt.date(2024, 1, 12),
        price_at_evaluation=Decimal("2222"),
        price_return_pct=1.0,
        evaluation_label=EvaluationLabel.ACCEPTABLE,
        label_evidence="test",
    )
    assert evaluation.evaluation_semantics_version == EVALUATION_SEMANTICS_V1


# --- (iv) 両実行が事前確認を通り抜ける競合 ------------------------------------


class _ConcurrentWriterRepository(EvaluationResultRepository):
    """★ 事前確認(索引)を通ったあと、保存の直前に別実行が先に保存した状況を作る。

    実運用での競合(2 実行が同じ run 開始時点の索引を見る)を決定的に再現する
    ためのテスト用リポジトリ。★ 最初の 1 回だけ割り込み、以降は素の振る舞いに戻す。

    ★ `save()` と `insert_if_absent()` の **両方**に割り込む。是正前のコードは
    `save()` を、是正後は `insert_if_absent()` を呼ぶため、片方だけに仕掛けると
    「是正前は注入点に到達しないから落ちた」という無意味な失敗になり、
    **欠陥そのものを示せない**。
    """

    def __init__(self, store_dir: Path) -> None:
        super().__init__(store_dir=store_dir)
        self._injected = False
        self.competitor_horizon: int | None = None

    def _inject_once(self, evaluation: EvaluationResult) -> None:
        if self._injected or evaluation.horizon_business_days is None:
            return
        self._injected = True
        # ★ 競合側は「別実行が自分でキーを決めて保存した」状況を表す。
        #   是正後は同じ決定的キーになるため弾かれ、
        #   是正前は本体側が uuid4 を持つためキーが一致せず **2 件保存される**。
        #   ここを evaluation.model_copy() のままにすると、是正前は同じ uuid で
        #   上書きされてしまい、★ 二重保存という欠陥そのものを示せない。
        competitor = evaluation.model_copy(
            update={
                "evaluation_id": build_evaluation_id(
                    evaluation.recommendation_id,
                    horizon_business_days=evaluation.horizon_business_days,
                ),
                "price_return_pct": 42.0,
                "label_evidence": "competitor",
            }
        )
        super().insert_if_absent(competitor)
        self.competitor_horizon = evaluation.horizon_business_days

    def insert_if_absent(self, evaluation: EvaluationResult) -> bool:
        self._inject_once(evaluation)
        return super().insert_if_absent(evaluation)

    def save(self, evaluation: EvaluationResult) -> None:
        self._inject_once(evaluation)
        super().save(evaluation)


def _build_service(
    tmp_path: Path,
    config: AppConfig,
    calendar: BusinessCalendar,
    evaluation_repo: EvaluationResultRepository,
) -> tuple[RecommendationEvaluationService, RecommendationRepository]:
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    service = RecommendationEvaluationService(
        market_data_provider=MockMarketDataProvider(now=_NOW),  # type: ignore[arg-type]
        config=config,
        business_calendar=calendar,
        recommendation_repository=recommendation_repo,
        evaluation_repository=evaluation_repo,
    )
    return service, recommendation_repo


def test_concurrent_run_stores_one_record_and_counts_one_success(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    """★ (iv) 是正前はここで 2 件保存されていた。1 件・1 回であることを固定する。

    ★ assert は**キーではなく論理的な単位**(推奨 ID + 営業日ホライズン)で行う。
    是正前はキーが uuid4 で毎回違うため、キーで数えると「重複が無い」ように
    見えてしまい、欠陥を検出できないからである。
    """
    evaluation_repo = _ConcurrentWriterRepository(tmp_path)
    service, recommendation_repo = _build_service(tmp_path, config, calendar, evaluation_repo)
    recommendation_repo.save(_make_recommendation())

    outcome = service.run_due_evaluations(_NOW)

    horizon = evaluation_repo.competitor_horizon
    assert horizon is not None, "競合を注入できていること(テスト自体の前提)"
    saved = evaluation_repo.list_by_recommendation(_RECOMMENDATION_ID)
    same_evaluation = [e for e in saved if e.horizon_business_days == horizon]
    assert len(same_evaluation) == 1, "★ 同じ推奨・同じホライズンの評価が 2 件保存されてはならない"
    assert same_evaluation[0].label_evidence == "competitor", "★ 先に保存した側が維持されること"

    assert outcome.summary.concurrent_conflict_count == 1, "★ 競合が見えること"
    assert not any(e.horizon_business_days == horizon for e in outcome.evaluated), (
        "★ 保存できなかった評価を成功として計上しないこと"
    )


# --- (v) 競合が無い通常経路 ---------------------------------------------------


def test_normal_path_without_conflict_is_unchanged(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    """★ 競合が無ければ、条件付き insert 化しても結果は 1 つも変わらない。"""
    evaluation_repo = EvaluationResultRepository(store_dir=tmp_path)
    service, recommendation_repo = _build_service(tmp_path, config, calendar, evaluation_repo)
    recommendation_repo.save(_make_recommendation())

    outcome = service.run_due_evaluations(_NOW)

    assert outcome.evaluated
    assert outcome.summary.concurrent_conflict_count == 0
    saved = evaluation_repo.list_by_recommendation(_RECOMMENDATION_ID)
    assert len(saved) == len(outcome.evaluated)
    assert all(e.evaluation_semantics_version == EVALUATION_SEMANTICS_V1 for e in saved)


def test_rerun_is_idempotent_and_does_not_recount(
    tmp_path: Path, config: AppConfig, calendar: BusinessCalendar
) -> None:
    """同一 run を 2 回流しても保存件数が増えないこと(索引と条件付き insert の二重の歯止め)。"""
    evaluation_repo = EvaluationResultRepository(store_dir=tmp_path)
    service, recommendation_repo = _build_service(tmp_path, config, calendar, evaluation_repo)
    recommendation_repo.save(_make_recommendation())

    first = service.run_due_evaluations(_NOW)
    saved_after_first = len(evaluation_repo.list_by_recommendation(_RECOMMENDATION_ID))
    second = service.run_due_evaluations(_NOW)

    assert first.evaluated
    assert second.evaluated == []
    assert len(evaluation_repo.list_by_recommendation(_RECOMMENDATION_ID)) == saved_after_first
