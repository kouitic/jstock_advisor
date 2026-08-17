#!/usr/bin/env python3
"""LINEリッチメニューの登録スクリプト。

人間が手元で実行するオペレーション用スクリプトであり、Lambda/CIからは
呼ばれない(LINEボタン起点会話型UI・実装プランv2 4節)。画像ファイルは
人間側で用意すること(このリポジトリには含めない)。

前提: 環境変数 LINE_CHANNEL_ACCESS_TOKEN に有効なチャネルアクセストークンを
設定しておくこと(Secrets Manager等の値をローカル実行時のみ環境変数として
与える運用とし、新たな認証情報保管の仕組みは追加しない)。

使い方(通常の流れ):
    1. 現在のデフォルトリッチメニューを確認しつつ新規作成・画像アップロードのみ行う:
       python infra/line_rich_menu/register_rich_menu.py --image path/to/menu.png
    2. 出力されたrichMenuIdを確認し、問題なければデフォルトへ設定する:
       python infra/line_rich_menu/register_rich_menu.py --rich-menu-id <richMenuId> --set-default

--set-defaultは既存の別用途リッチメニューを上書きする可能性があるため、
必ず一度目の実行で表示される「現在のデフォルト」を確認してから実行すること。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_API_HOST = "https://api.line.me"
# 画像アップロードのみこちら専用ホスト(API呼び出し用api.line.meとは異なる)。
_API_DATA_HOST = "https://api-data.line.me"


def _token() -> str:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not token:
        print("環境変数 LINE_CHANNEL_ACCESS_TOKEN が設定されていません。", file=sys.stderr)
        sys.exit(1)
    return token


def _request(
    method: str, url: str, token: str, data: bytes | None, content_type: str
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = response.read()
            result: dict[str, Any] = json.loads(body) if body else {}
            return result
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"LINE API呼び出しに失敗しました: {e.code} {e.reason}\n{detail}", file=sys.stderr)
        sys.exit(1)


def show_current_default(token: str) -> None:
    result = _request(
        "GET", f"{_API_HOST}/v2/bot/user/all/richmenu", token, None, "application/json"
    )
    print(f"現在のデフォルトリッチメニュー: {result}")


def create_rich_menu(token: str, definition_path: Path) -> str:
    definition = definition_path.read_text(encoding="utf-8").encode("utf-8")
    result = _request(
        "POST", f"{_API_HOST}/v2/bot/richmenu", token, definition, "application/json"
    )
    rich_menu_id: str = result["richMenuId"]
    print(f"リッチメニューを作成しました: richMenuId={rich_menu_id}")
    return rich_menu_id


def upload_image(token: str, rich_menu_id: str, image_path: Path) -> None:
    content_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    image_bytes = image_path.read_bytes()
    _request(
        "POST",
        f"{_API_DATA_HOST}/v2/bot/richmenu/{rich_menu_id}/content",
        token,
        image_bytes,
        content_type,
    )
    print(f"画像をアップロードしました: {image_path}")


def set_default(token: str, rich_menu_id: str) -> None:
    _request(
        "POST",
        f"{_API_HOST}/v2/bot/user/all/richmenu/{rich_menu_id}",
        token,
        b"",
        "application/json",
    )
    print(f"デフォルトリッチメニューに設定しました: richMenuId={rich_menu_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--definition",
        type=Path,
        default=Path(__file__).parent / "rich_menu.json",
        help="リッチメニュー定義JSON(既定: infra/line_rich_menu/rich_menu.json)",
    )
    parser.add_argument(
        "--image", type=Path, help="リッチメニュー画像ファイル(--rich-menu-id未指定時は必須)"
    )
    parser.add_argument(
        "--rich-menu-id",
        help="作成済みのrichMenuIdを指定する(--set-defaultのみを行う2回目の実行用)",
    )
    parser.add_argument(
        "--set-default",
        action="store_true",
        help="対象のリッチメニューをデフォルトに設定する(既存の別用途リッチメニューを"
        "上書きする可能性があるため、既定では行わない)",
    )
    args = parser.parse_args()

    token = _token()
    show_current_default(token)

    if args.rich_menu_id:
        rich_menu_id = args.rich_menu_id
    else:
        if args.image is None:
            parser.error("--image は --rich-menu-id 未指定時に必須です")
        if not args.image.exists():
            print(f"画像ファイルが見つかりません: {args.image}", file=sys.stderr)
            sys.exit(1)
        rich_menu_id = create_rich_menu(token, args.definition)
        upload_image(token, rich_menu_id, args.image)

    if args.set_default:
        set_default(token, rich_menu_id)
    else:
        print(
            "デフォルト設定はスキップしました。上記の現在のデフォルトを確認したうえで、"
            "問題なければ以下を実行してください:\n"
            f"  python {Path(__file__).name} --rich-menu-id {rich_menu_id} --set-default"
        )


if __name__ == "__main__":
    main()
