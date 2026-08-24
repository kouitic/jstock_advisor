"""既知の実在人物名・個人メールアドレス等がGit管理ファイルへ再混入していないかを
検査する(CLAUDE.md「個人情報のGit管理対象へ含めない」ルール、CIの`pii-scan`
ジョブから実行)。

denylistは平文ではなくSHA-256ハッシュで保持する。このスクリプト自身が検出対象の
個人情報を平文でGit管理下に記録してしまっては本末転倒であるため。検出時も
一致した実際の文字列はログへ出力しない(ハッシュとファイルパスのみ)。

本スキャンは「これまでに実際にリポジトリへ混入したことが判明している既知の
文字列」に限定したdenylist方式であり、これを通過したからといって他の個人情報が
一切存在しないことを保証するものではない。CLAUDE.mdの開発ルール
(実在人物の氏名・個人メール等をそもそも記録しない)と併用すること。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

# 実際に本リポジトリへ混入したことが判明している既知の文字列のSHA-256
# ハッシュ(小文字化・前後空白除去後にUTF-8エンコードしてハッシュ化)。
# 2026-08-25コードレビュー対応で発見・除去した実在人物名(漢字2〜3文字の
# 名・そのローマ字表記)・実際に使用されていた個人メールアドレス。
_KNOWN_PII_HASHES: frozenset[str] = frozenset(
    {
        "38b01b2a92a4709b958bc76c0ebf72e1452c72a9e1b1b823367069c3b31fa364",
        "a39436337109030e63d5a079604f481df40f248604984d8101638034acaf2db6",
        "ccf9ea9e390b568e850db2d7ce674642ff6ab306ae92d841a1efcf67e5274106",
        "84ff3a2369862a1f505e4850e395d5629d333d5559f5ad894e6aebd6d7cff254",
        "ec5a78f63ce1c7dc4efcf7d41d6a6e81b34c2da055c1486924342c4e9f5401b6",
        "6883dd3a52f81d097145ec98912a2c14fdc1465dbed800cbf7d7869341a52497",
        "7306eecbc3d3a911ebbda34239a3fdde2c8212ef2a668d3e3a4eac968488c756",
        "71ff17114430b91a35569be8dc440f68b34c94529b08f9b4ab44238bfaebda25",
        "de9c1a03dd625f408d5fa8c1ce49fddf65688a23a03aada473d446d6c6d949d1",
    }
)

# 漢字2〜4文字の連続(日本人の名・姓によくある長さ)。
_HAN_RUN = re.compile(r"[一-鿿]{2,4}")
# ASCII単語(ローマ字表記の名等)。
_ASCII_WORD = re.compile(r"[A-Za-z]{3,}")
# メールアドレス。
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# バイナリ・生成物・ロックファイル等、スキャン対象外にするパスプレフィックス。
_EXCLUDED_PREFIXES = (
    ".git/",
    ".venv/",
    "venv/",
    "node_modules/",
    "__pycache__/",
)


def _hash(token: str) -> str:
    return hashlib.sha256(token.strip().lower().encode("utf-8")).hexdigest()


def _tracked_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True, cwd=repo_root
    )
    return [
        line
        for line in result.stdout.splitlines()
        if line and not line.startswith(_EXCLUDED_PREFIXES)
    ]


def _candidate_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in _HAN_RUN.finditer(text):
        run = match.group(0)
        for length in (2, 3, 4):
            for i in range(len(run) - length + 1):
                tokens.add(run[i : i + length])
    tokens.update(match.group(0) for match in _ASCII_WORD.finditer(text))
    tokens.update(match.group(0) for match in _EMAIL.finditer(text))
    return tokens


def scan(repo_root: Path) -> list[str]:
    """PII混入が検出されたファイルパスの一覧を返す(実際の一致文字列は
    呼び出し元・ログのいずれにも出力しない)。"""
    violating_paths: set[str] = set()
    for rel_path in _tracked_files(repo_root):
        path = repo_root / rel_path
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in _candidate_tokens(text):
            if _hash(token) in _KNOWN_PII_HASHES:
                violating_paths.add(rel_path)
                break
    return sorted(violating_paths)


def main() -> int:
    repo_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    violations = scan(repo_root)
    if violations:
        print(
            "PIIスキャン失敗: 既知の実在人物の個人情報を検出しました"
            "(一致した文字列自体はログへ出力しません)。該当ファイル:",
            file=sys.stderr,
        )
        for path in violations:
            print(f"  {path}", file=sys.stderr)
        print(
            "CLAUDE.mdの「実在人物の個人情報をGit管理対象へ含めない」ルールに"
            "従い、架空値(例: 「所有者A」)へ置き換えてください。",
            file=sys.stderr,
        )
        return 1
    print("PIIスキャン: 既知の実在人物の個人情報は検出されませんでした。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
