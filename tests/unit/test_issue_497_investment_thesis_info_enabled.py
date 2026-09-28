"""Issue #497(#413): investment_thesis_service の INFO を Lambda で出力されるようにする。

`investment_thesis_service.py`はVALIDATION mode専用の2経路(baseline活性化・
investment thesis生成)で`logger.info()`を呼ぶが、Lambdaのroot logger既定
(WARNING)のもとではmoduleがlevelを宣言していないとINFOは出力されない。
ここでは次を確認する。

    1 宣言が実際に効く: Lambdaのroot(WARNING)のもとで、INFOが有効になる。
    2 INFOが実際に出力される: VALIDATION modeで、2経路とも1行以上出る。
    3 NORMAL modeでは対象のINFOが出ない(2経路ともVALIDATION専用の分岐でしか
      呼ばれないため)。
    4 出力にholding_ref(log_ref済みハッシュ)とversionのみが含まれ、生の
      owner・holding_id・baseline_idが含まれないこと(#135/#416)。
      架空のholding_idを「実際に読まれるデータ」へ実際に渡して確認する
      (#413 PR-3のF1教訓と同型)。
    5 検査そのものの確認: 生のholding_idを出す変異を注入すると検査が赤に
      なる(空振りしない)。

宣言があること自体はtests/unit/test_issue_413_logger_level_declared.py
(#413のguard)が見る。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import BaselineOrigin, ExecutionMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding_decision import BaselineValueSnapshot
from jstock_advisor.domain.entities.owner import log_ref
from jstock_advisor.services import investment_thesis_service
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService

_MODULE = investment_thesis_service.__name__
_VALUES = BaselineValueSnapshot(total_yield_pct=4.0, equity_ratio_pct=45.0)
_VALIDATION = ExecutionContext(mode=ExecutionMode.VALIDATION)

#: 実在しない架空値。試験対象のmoduleが実際に読むデータへ入れ、出力に現れたら
#: 生の値を出している。
_OWNER = "owner-a"
_HOLDING_ID = f"{_OWNER}:holding-1"
_STOCK_CODE = "0000"


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _MODULE]


def test_declared_level_takes_effect_under_the_lambda_root_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lambdaのroot(WARNING)のもとで、このmoduleのloggerはINFOが有効(宣言が効いている)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)

    logger = logging.getLogger(_MODULE)
    assert logger.level == logging.INFO
    assert logger.isEnabledFor(logging.INFO)


def test_baseline_activation_info_is_emitted_in_validation_mode_and_carries_the_holding_id(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """VALIDATION modeでactivate_baseline()を呼ぶと、transient baseline経路のINFOが
    実際に出力される(架空holding_idが実際にデータへ入っている状態で確認する)。
    """
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    service = InvestmentThesisService(store_dir=store_dir, execution_context=_VALIDATION)

    created = service.activate_baseline(
        _HOLDING_ID, _STOCK_CODE, BaselineOrigin.SYSTEM_INITIALIZED, _VALUES
    )
    assert created.version == 1
    # 架空値が、試験対象が実際に読むデータへ入っている(入っていなければ以下の検査は
    # 何も保証しない)
    assert created.holding_id == _HOLDING_ID
    assert created.baseline_id == f"{_HOLDING_ID}:v1"

    infos = [r for r in _records(caplog) if r.levelno == logging.INFO]
    assert len(infos) == 1
    message = infos[0].getMessage()
    assert message.startswith("VALIDATION MODE baseline activation transient")
    assert f"holding_ref={log_ref(_HOLDING_ID)}" in message
    assert "version=1" in message


def test_get_or_create_thesis_info_is_emitted_in_validation_mode_and_carries_the_holding_id(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """VALIDATION modeでget_or_create_thesis()を呼ぶと、transient thesis経路の
    INFOが実際に出力される。
    """
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    service = InvestmentThesisService(store_dir=store_dir, execution_context=_VALIDATION)
    now = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

    thesis = service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, now)
    assert thesis.holding_id == _HOLDING_ID

    infos = [r for r in _records(caplog) if r.levelno == logging.INFO]
    assert len(infos) == 1
    message = infos[0].getMessage()
    assert message.startswith("VALIDATION MODE investment thesis transient")
    assert f"holding_ref={log_ref(_HOLDING_ID)}" in message


def test_no_info_is_emitted_in_normal_mode(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """NORMAL modeでは両経路ともVALIDATION専用の分岐を通らないため、対象INFOは出ない。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    service = InvestmentThesisService(
        store_dir=store_dir, execution_context=ExecutionContext.normal()
    )
    now = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

    service.activate_baseline(_HOLDING_ID, _STOCK_CODE, BaselineOrigin.SYSTEM_INITIALIZED, _VALUES)
    service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, now)

    infos = [r for r in _records(caplog) if r.levelno == logging.INFO]
    assert infos == []


def test_the_output_contains_neither_the_raw_holding_id_nor_owner_nor_baseline_id(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """出力(文面と引数)に、実際に読まれたholding_id・owner・baseline_idの生値が現れない。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    service = InvestmentThesisService(store_dir=store_dir, execution_context=_VALIDATION)
    now = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

    service.activate_baseline(_HOLDING_ID, _STOCK_CODE, BaselineOrigin.SYSTEM_INITIALIZED, _VALUES)
    service.get_or_create_thesis(_HOLDING_ID, _STOCK_CODE, now)

    records = _records(caplog)
    assert records  # 検査が空振りしない(INFOが実際に出ている)
    raw_baseline_id = f"{_HOLDING_ID}:v1"
    for record in records:
        rendered = record.getMessage() + repr(record.args)
        assert _OWNER not in rendered
        assert _HOLDING_ID not in rendered
        assert raw_baseline_id not in rendered


def test_the_leak_check_fails_when_a_record_contains_the_raw_holding_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """検査そのものの確認: 架空値を含む記録があれば検査は赤になる(常に緑の検査ではない)。"""
    for leaked in (f"x owner={_OWNER}", f"x holding_id={_HOLDING_ID}"):
        caplog.clear()
        logging.getLogger(_MODULE).info("%s", leaked)

        records = _records(caplog)
        with pytest.raises(AssertionError):
            for record in records:
                rendered = record.getMessage() + repr(record.args)
                assert _OWNER not in rendered
                assert _HOLDING_ID not in rendered
