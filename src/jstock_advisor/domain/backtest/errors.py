"""R1a の入力の契約違反(型・値が契約に反する)。値・識別子を含めない。"""

from __future__ import annotations


class ReplayInputError(ValueError):
    """入力の型・値が契約に反する(呼び出し側の誤り)。メッセージに値を含めない。"""


def require_int(value: object, name: str, *, minimum: int) -> int:
    """bool でない int で、minimum 以上であること。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayInputError(f"{name} must be an int")
    if value < minimum:
        raise ReplayInputError(f"{name} must be >= {minimum}")
    return value


def require_text(value: object, name: str) -> str:
    """空でない str であること。"""
    if not isinstance(value, str) or not value:
        raise ReplayInputError(f"{name} must be a non-empty string")
    return value
