"""購入側 Shadow の owner の符号に付けるローカルのラベル(Issue #605。表示名)。

owner の符号(kv1)は鍵つきの不透明な識別子で、CLI は鍵も符号の計算も持たない。利用者が画面で所有者を
見分けられるように、**符号の桁の先頭 → 表示名**の対応表を、利用者の端末のローカルのファイルに持つ。

## ★ 性質: 符号と実名の対応表 = 機密 ★

* 場所は利用者の home 配下の固定のパス(リポジトリ配下は拒否。Git 管理外)。権限は本人のみ(0o600。
  Windows では ACL の確認が難しいため、手順書で home 配下を前提とする)
* ★ AWS・鍵・記録に触れない ★: この module は boto3・環境変数・通信を持たない(契約テストで固定)
* 表示名は画面の表示にだけ使う。記録・ログ・例外・JSON の既定の出力へ出さない。例外は固定の文言で、
  表示名・符号を含めない

## 形式

`{"version": 1, "labels": {"<符号の桁の先頭 6〜24 桁(小文字の 16 進)>": "<表示名>"}}`
キーどうしが前方一致する組(曖昧になる組)は作れない。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Final

FILE_VERSION: Final = 1
MIN_PREFIX_LENGTH: Final = 6
MAX_PREFIX_LENGTH: Final = 24
MAX_NAME_LENGTH: Final = 40
_PREFIX: Final = re.compile(r"^[0-9a-f]{6,24}$")
_REPO_ROOT: Final = Path(__file__).resolve().parents[3]


class LabelError(ValueError):
    """ラベルの操作の失敗。固定の文言だけを持つ(表示名・符号・path を含めない)。"""


def default_labels_path() -> Path:
    return Path.home() / ".jstock" / "allocation_owner_labels.json"


def _check_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = _REPO_ROOT.resolve()
    if resolved == root or root in resolved.parents:
        raise LabelError("the label file must not be inside the repository")
    return resolved


def normalize_prefix(value: str) -> str:
    """`--ref` の入力(`kv1.1:` 付きの符号または桁の先頭)→ 小文字の桁の先頭。不正は LabelError。"""
    text = value.strip().lower()
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    if not _PREFIX.match(text):
        raise LabelError("the reference prefix must be 6 to 24 lowercase hex digits")
    return text


def _validate_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or len(cleaned) > MAX_NAME_LENGTH:
        raise LabelError("the label must be 1 to 40 characters")
    if any(unicodedata.category(ch).startswith("C") for ch in cleaned):
        raise LabelError("the label must not contain control characters")
    return cleaned


def load_labels(path: Path | None = None) -> dict[str, str]:
    """ラベルを読む。無ければ空。形が不正なら LabelError(固定の文言)。"""
    target = _check_path(path or default_labels_path())
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise LabelError("the label file cannot be read") from None
    labels = raw.get("labels") if isinstance(raw, dict) else None
    if (
        not isinstance(raw, dict)
        or raw.get("version") != FILE_VERSION
        or not isinstance(labels, dict)
    ):
        raise LabelError("the label file has an unsupported form")
    result: dict[str, str] = {}
    for key, value in labels.items():
        if isinstance(key, str) and _PREFIX.match(key) and isinstance(value, str) and value:
            result[key] = value
    return result


def _write(target: Path, labels: dict[str, str]) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):  # Windows などでは効かない(手順書で home 配下を前提)
            target.parent.chmod(0o700)
        body = json.dumps(
            {"version": FILE_VERSION, "labels": dict(sorted(labels.items()))},
            ensure_ascii=False,
            indent=2,
        )
        descriptor, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".labels-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(body + "\n")
            with contextlib.suppress(OSError):
                Path(temp_name).chmod(0o600)
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
    except OSError:
        raise LabelError("the label file cannot be written") from None


def _conflicts(prefix: str, labels: dict[str, str]) -> bool:
    return any(
        other != prefix and (other.startswith(prefix) or prefix.startswith(other))
        for other in labels
    )


def set_label(prefix: str, name: str, path: Path | None = None) -> None:
    key = normalize_prefix(prefix)
    value = _validate_name(name)
    target = _check_path(path or default_labels_path())
    labels = load_labels(target)
    if _conflicts(key, labels):
        raise LabelError("the reference prefix overlaps another label (make it longer)")
    labels[key] = value
    _write(target, labels)


def remove_label(prefix: str, path: Path | None = None) -> bool:
    key = normalize_prefix(prefix)
    target = _check_path(path or default_labels_path())
    labels = load_labels(target)
    if key not in labels:
        return False
    del labels[key]
    _write(target, labels)
    return True
