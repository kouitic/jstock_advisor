"""infra/line_rich_menu/rich_menu.json のスキーマ検証(LINEボタン起点会話型UI・
実装プランv2 4節)。

register_rich_menu.py自体は人間が手元で実行する運用スクリプトのため自動テスト
対象外とするが(Lambda/CIからは呼ばれない)、定義JSONの面積合計・重複領域の
有無・action.data値が4節の確定postback data定義と一致することは回帰的に
検証する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_RICH_MENU_PATH = (
    Path(__file__).resolve().parents[2] / "infra" / "line_rich_menu" / "rich_menu.json"
)

_EXPECTED_POSTBACK_DATA = {"action=start_buy", "action=start_sell", "action=start_watch"}


def _load() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(_RICH_MENU_PATH.read_text(encoding="utf-8"))
    return result


def test_rich_menu_json_is_valid_json_with_required_top_level_fields() -> None:
    data = _load()
    assert data["size"]["width"] > 0
    assert data["size"]["height"] > 0
    assert data["selected"] is True
    assert data["name"]
    assert data["chatBarText"]
    assert isinstance(data["areas"], list)
    assert len(data["areas"]) == 3


def test_rich_menu_areas_cover_full_width_without_gaps_or_overlap() -> None:
    data = _load()
    width = data["size"]["width"]
    height = data["size"]["height"]
    areas = sorted(data["areas"], key=lambda a: a["bounds"]["x"])

    cursor = 0
    for area in areas:
        bounds = area["bounds"]
        assert bounds["y"] == 0
        assert bounds["height"] == height
        assert bounds["x"] == cursor  # 隙間・重複が無い(直前の右端と一致)
        cursor += bounds["width"]
    assert cursor == width  # 合計幅がリッチメニュー全体の幅と一致


def test_rich_menu_action_data_matches_confirmed_postback_definitions() -> None:
    data = _load()
    action_data_values = {area["action"]["data"] for area in data["areas"]}
    assert action_data_values == _EXPECTED_POSTBACK_DATA


def test_rich_menu_actions_are_all_postback_type_with_display_text() -> None:
    data = _load()
    for area in data["areas"]:
        action = area["action"]
        assert action["type"] == "postback"
        assert action["displayText"]
