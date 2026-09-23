"""Issue #503 レビュー指摘 F6: dedup_window_minutes / claim_stale_minutes は正の値のみ許可する。

0 や負の値が通ると、is_duplicate_within_window() / try_claim() の cutoff が「今この瞬間」
以前になり、直後の retry が即座に dedup window / claim stale を「経過済み」と判定してしまう
(#502 が解決した「Lambda retry で3通になる」問題が構造的に復活する)。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import IncidentNotificationConfig

_VALID = {"version": 1, "dedup_window_minutes": 30, "claim_stale_minutes": 5}


def test_valid_values_are_accepted() -> None:
    config = IncidentNotificationConfig(**_VALID)
    assert config.dedup_window_minutes == 30
    assert config.claim_stale_minutes == 5


def test_production_config_is_still_accepted() -> None:
    config = load_config().incident_notification
    assert config.dedup_window_minutes > 0
    assert config.claim_stale_minutes > 0


@pytest.mark.parametrize("value", [0, -1, -30])
def test_non_positive_dedup_window_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError, match="dedup_window_minutes"):
        IncidentNotificationConfig(**{**_VALID, "dedup_window_minutes": value})


@pytest.mark.parametrize("value", [0, -1, -5])
def test_non_positive_claim_stale_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError, match="claim_stale_minutes"):
        IncidentNotificationConfig(**{**_VALID, "claim_stale_minutes": value})
