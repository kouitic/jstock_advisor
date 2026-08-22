"""移行CLIの--target(local | aws)厳密化(cli/trading_pause.pyと同じ設計)。

各CLIモジュールが独立して自分専用のtarget切替ヘルパーを持つのがこの
コードベースの既存の流儀(cli/holding_decision.py・cli/trading_pause.py参照)
のため、共通ユーティリティへ集約せずここでも同じ設計を独立に複製する。
"""

from __future__ import annotations

import contextlib
import enum
import os
from collections.abc import Iterator

_AWS_OVERRIDE_ENV_VAR = "AWS_LAMBDA_FUNCTION_NAME"


class MigrationTarget(enum.StrEnum):
    """--targetの許容値を"local"/"aws"の2値のみへ厳密に限定する。

    本番データを書き換えるmigrationコマンドである以上、タイプミスによる
    意図しないバックエンド操作は特に致命的(誤ってlocalへ書き込んで本番は
    未移行のまま、または逆)なため、trading-pause CLIと同じくEnumで
    未知の値をlocalへ暗黙にフォールバックさせない設計とする。
    """

    LOCAL = "local"
    AWS = "aws"


@contextlib.contextmanager
def target_backend(target: MigrationTarget) -> Iterator[None]:
    """--target aws指定時、ローカルCLIから本番DynamoDBバックエンドを直接操作する。"""
    if target is not MigrationTarget.AWS:
        yield
        return
    previous = os.environ.get(_AWS_OVERRIDE_ENV_VAR)
    os.environ[_AWS_OVERRIDE_ENV_VAR] = "cli-target-aws-override"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_AWS_OVERRIDE_ENV_VAR, None)
        else:
            os.environ[_AWS_OVERRIDE_ENV_VAR] = previous
