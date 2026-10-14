"""購入側 Shadow の配線(Issue #603 PR-F2-2b。#925 kv1)。

handler 層。実在の repository・設定・鍵の provider を、F2-2a の計算(`prepare_batch_inputs` /
`compute_owner`)と観測のループ(`allocation_shadow_runner.make_runner`)へつなぐ。

## 守ること(契約テストで固定する)

  * ★ 設定 OFF(出荷値)では、この module は import も生成もされない ★: handler は
    `allocation_shadow_service.observe_allocation_shadow` の `wiring_factory`(遅延 import の
    クロージャ)を渡すだけで、入口が OFF の最初の判定で戻る限り、factory は呼ばれない。
    repository・鍵の provider にも触れない
  * ★ 環境変数 `ALLOCATION_SHADOW_REF_KEY_SECRET_ARN` を読むのはここだけ ★(provider は読まない)。
    読んだ値は provider の組み立て(`shared_key_provider`)にしか渡さず、真偽・条件・比較に流さない
    = 環境変数で Shadow を ON にできない(ON の入口は `allocation_shadow.yaml` の `enabled` だけ。
    #606 の契約テストが AST で固定)
  * この module は読取の組み立てだけ。BUY 判定・通知・finalize へは何も返さない(戻り値は
    『観測のループ』の関数だけ)
  * 鍵の provider の具象(`SecretsManagerKeyProvider`)はここで 1 か所だけ import する(遅延)。
    テストは `key_provider` 引数で fake を渡す

## 設定の出どころ(新しい閾値・係数を作らない)

  方針の束 = `config/allocation_policy.yaml`(売買単位は `profit_taking_rules.yaml` の既定値)/
  RAER の係数 = `config/raer_coefficients.yaml` / assembler の閾値・重み = 既存の
  `holding_decision_rules.yaml`(coverage_thresholds.company_quality_minimum)と
  `earnings_trend_rules.yaml` / 営業日 = 既存の `holiday_calendar`
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.available_cash import AvailableCash
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.identifier_ref import KeyedOwnerRef, KeyProvider
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.allocation_policy_config import load_allocation_policy
from jstock_advisor.domain.valuation.raer_coefficients_config import load_raer_coefficients
from jstock_advisor.domain.valuation.raer_inputs import AssemblerPolicy
from jstock_advisor.services.allocation_shadow_compute import (
    BatchInputs,
    BatchSpec,
    BatchUnavailable,
    ComputeConfig,
    OwnerComputation,
    compute_owner,
    prepare_batch_inputs,
)
from jstock_advisor.services.allocation_shadow_runner import make_runner
from jstock_advisor.services.allocation_shadow_service import Deadline, ShadowRunner

#: 鍵の Secret の ARN を渡す環境変数(template が Secret への !Ref だけを設定する。#947)
KEY_SECRET_ARN_ENV: Final = "ALLOCATION_SHADOW_REF_KEY_SECRET_ARN"


class RepositoryShadowInputReader:
    """`ShadowInputReader` の実体。repository を呼ぶだけ(読取専用。例外は計算側が理由に畳む)。"""

    def __init__(self, recommendation_repo: Any) -> None:
        self._recommendations = recommendation_repo

    def read_holdings(self) -> Sequence[Holding]:
        from jstock_advisor.infrastructure.local_repository.holding_repository import (
            HoldingRepository,
        )

        return HoldingRepository().list_all()

    def read_recommendations(self, ids: Sequence[str]) -> Mapping[str, Recommendation]:
        return self._recommendations.get_many(ids)  # type: ignore[no-any-return]

    def read_available_cash(self, owner: str) -> AvailableCash | None:
        from jstock_advisor.infrastructure.local_repository.available_cash_repository import (
            AvailableCashRepository,
        )

        return AvailableCashRepository().get(owner)


def _compute_config(config: AppConfig) -> ComputeConfig:
    minimum_unit = config.profit_taking.trading_unit.default_trading_unit
    return ComputeConfig(
        bundle=load_allocation_policy(minimum_trading_unit=minimum_unit),
        coefficients=load_raer_coefficients().coefficients,
        assembler_policy=AssemblerPolicy.from_rules(
            company_quality_minimum=config.holding_decision.coverage_thresholds.company_quality_minimum,
            trend_rules=config.earnings_trend,
        ),
        calendar=BusinessCalendar.from_config(config.holiday_calendar),
    )


def _key_provider() -> KeyProvider:
    from jstock_advisor.infrastructure.aws.allocation_shadow_key_provider import (
        shared_key_provider,
    )

    # 読んだ値は provider の組み立てにだけ渡す(未設定・空・不正は provider が NOT_CONFIGURED を返す)
    return shared_key_provider(os.environ.get(KEY_SECRET_ARN_ENV))


def build_shadow_runner(
    *,
    config: AppConfig,
    batch_id: str,
    now: dt.datetime,
    candidate_ids: Mapping[str, str],
    holding_recommendation_id: Callable[[str], str],
    recommendation_repo: Any,
    reader: Any | None = None,
    key_provider: KeyProvider | None = None,
    compute_config: ComputeConfig | None = None,
) -> ShadowRunner:
    """設定 ON のときだけ呼ばれる。実在の部品から『観測のループ』を組み立てる(読取のみ)。

    `reader`・`key_provider`・`compute_config` は試験用の差し込み(本番は既定 = 実在の部品)。
    """
    shadow_reader = (
        reader if reader is not None else RepositoryShadowInputReader(recommendation_repo)
    )
    shadow_config = compute_config if compute_config is not None else _compute_config(config)
    spec = BatchSpec(
        batch_id=batch_id,
        now=now,
        candidate_ids=candidate_ids,
        holding_recommendation_id=holding_recommendation_id,
    )

    def prepare(deadline: Deadline, io_timeout_seconds: float) -> BatchInputs | BatchUnavailable:
        return prepare_batch_inputs(
            spec,
            shadow_config,
            shadow_reader,
            deadline=deadline,
            io_timeout_seconds=io_timeout_seconds,
        )

    def compute(
        inputs: BatchInputs,
        owner: str,
        owner_ref: KeyedOwnerRef,
        deadline: Deadline,
        io_timeout_seconds: float,
        elapsed_ms: int | None,
    ) -> OwnerComputation:
        return compute_owner(
            inputs,
            owner,
            owner_ref,
            reader=shadow_reader,
            deadline=deadline,
            io_timeout_seconds=io_timeout_seconds,
            elapsed_ms=elapsed_ms,
        )

    return make_runner(
        prepare=prepare,
        compute_owner=compute,
        key_provider=key_provider if key_provider is not None else _key_provider(),
    )
