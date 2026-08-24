"""infra/line_rich_menu/rich_menu.json のスキーマ検証(LINEボタン起点会話型UI・
実装プランv2 4節、LINE UI第二弾Phase 2-A・6ボタン版で更新)。

register_rich_menu.py自体は人間が手元で実行する運用スクリプトのため自動テスト
対象外とするが(Lambda/CIからは呼ばれない)、定義JSONの面積合計・重複領域の
有無・action.data値が確定postback data定義と一致することは回帰的に検証する。

Phase 2-A時点では銘柄分析(start_analyze)はまだ実装しないため、6ボタン版
(上段: 買った/売った/お気に入り登録、下段: 保有銘柄/ウォッチリスト/対象確認)
のみを対象とする。Phase 2-B完成後、7ボタン版へ差し替える際に本テストも
更新すること。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_RICH_MENU_PATH = (
    Path(__file__).resolve().parents[2] / "infra" / "line_rich_menu" / "rich_menu.json"
)

_EXPECTED_POSTBACK_DATA = {
    "action=start_buy",
    "action=start_sell",
    "action=start_watch",
    "action=show_holdings",
    "action=show_watchlist",
    "action=show_targets",
}


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
    assert len(data["areas"]) == 6


def test_rich_menu_areas_cover_full_grid_without_gaps_or_overlap() -> None:
    """上段(y=0)・下段(y=843)の2行、各行が幅方向に隙間・重複なくタイルする。"""
    data = _load()
    width = data["size"]["width"]
    height = data["size"]["height"]
    areas = data["areas"]

    rows: dict[int, list[dict[str, Any]]] = {}
    for area in areas:
        y = area["bounds"]["y"]
        rows.setdefault(y, []).append(area)

    assert set(rows.keys()) == {0, height // 2}
    for row_areas in rows.values():
        sorted_areas = sorted(row_areas, key=lambda a: a["bounds"]["x"])
        cursor = 0
        row_height = None
        for area in sorted_areas:
            bounds = area["bounds"]
            if row_height is None:
                row_height = bounds["height"]
            assert bounds["height"] == row_height
            assert bounds["x"] == cursor  # 隙間・重複が無い(直前の右端と一致)
            cursor += bounds["width"]
        assert cursor == width  # 行内の合計幅がリッチメニュー全体の幅と一致

    # 上段+下段の高さ合計がリッチメニュー全体の高さと一致(縦方向も隙間・重複なし)
    top_height = rows[0][0]["bounds"]["height"]
    bottom_height = rows[height // 2][0]["bounds"]["height"]
    assert top_height + bottom_height == height


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
