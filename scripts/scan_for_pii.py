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

Git管理ファイル以外の公開面(Issue / PR の本文・コメント・タイトル、
commit message等)を走査するための関数もここへ置く(Issue #131)。
denylistを二重管理しないため、走査対象が違っても同じ_KNOWN_PII_HASHESと
_candidate_tokens()を使う。ネットワークアクセスはこのモジュールでは行わない
(取得は呼び出し側の責務。単体テストをfixtureだけで完結させるため)。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
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


def scan(repo_root: Path, known_hashes: frozenset[str] | None = None) -> list[str]:
    """PII混入が検出されたファイルパスの一覧を返す(実際の一致文字列は
    呼び出し元・ログのいずれにも出力しない)。known_hashesを省略した場合は
    本番denylist(_KNOWN_PII_HASHES)を使う。テストが検出ロジック自体を
    検証する際、実在人物名を一切使わずに済むよう、別のdenylistを注入
    できるようにするためのパラメータ(tests/unit/test_scan_for_pii.py参照)。
    """
    hashes = known_hashes if known_hashes is not None else _KNOWN_PII_HASHES
    violating_paths: set[str] = set()
    for rel_path in _tracked_files(repo_root):
        path = repo_root / rel_path
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in _candidate_tokens(text):
            if _hash(token) in hashes:
                violating_paths.add(rel_path)
                break
    return sorted(violating_paths)


# 公開面の走査で使う検出理由。
REASON_DENYLIST = "DENYLIST"
REASON_EMAIL_PATTERN = "EMAIL_PATTERN"

# 特定個人へ到達しない機械アドレス。メール様式の検出から除外する。
#
# `noreply@anthropic.com` はcommitのCo-Authored-By trailerとして**付与を
# 義務づけている**値であり(CLAUDE.md / 開発ルール)、全commit messageに必ず
# 現れる。除外しないとPRのcommit message走査が常に失敗し、警告が意味を
# 失う(かつtrailerを消す是正は規約違反になるため直しようがない)。
# GitHubのnoreplyドメインは、GitHub自身が個人メールを隠すために発行する
# アドレスであり、露出させたくない実アドレスの反対物である。
#
# 除外は**この2系統に限定する**。個人が使う可能性のあるドメイン全体
# (例: anthropic.com 全体)を除外すると、実アドレスの見逃し口になる。
_NON_PERSONAL_EMAILS: frozenset[str] = frozenset({"noreply@anthropic.com"})
_NON_PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {"users.noreply.github.com", "noreply.github.com"}
)


def _is_non_personal_email(address: str) -> bool:
    normalized = address.strip().lower()
    if normalized in _NON_PERSONAL_EMAILS:
        return True
    _, _, domain = normalized.rpartition("@")
    return domain in _NON_PERSONAL_EMAIL_DOMAINS

# ハッシュ接頭辞の長さ。所在の突き合わせに足りる範囲だけを出す。
_HASH_PREFIX_LEN = 8


@dataclass(frozen=True)
class MetadataFinding:
    """公開面で検出した1件。**一致した文字列そのものは保持しない。**

    surface  走査した面(ISSUE_BODY / COMMIT_MESSAGE 等)
    location 所在(Issue / PR 番号、comment id、commit の短縮SHA 等)
    reason   REASON_DENYLIST / REASON_EMAIL_PATTERN
    token_hash_prefix  一致トークンのSHA-256の先頭。突き合わせ用
    """

    surface: str
    location: str
    reason: str
    token_hash_prefix: str


def scan_texts(
    items: Iterable[tuple[str, str, str]],
    known_hashes: frozenset[str] | None = None,
    *,
    detect_email_pattern: bool = True,
    apply_email_allowlist: bool = True,
) -> list[MetadataFinding]:
    """(surface, location, text)の並びを走査し、検出結果を返す。

    denylist一致に加えて、メールアドレス様式を検出する(denylistに無い未知の
    個人メールも公開面では拾えるようにするため)。電話番号・郵便番号・口座様式は
    本リポジトリの自然文に4桁の銘柄コードや件数が多く現れfalse positiveが高いため
    対象にしない(Issue #131 Phase A)。

    メール様式のうち機械アドレス(_NON_PERSONAL_EMAILS /
    _NON_PERSONAL_EMAIL_DOMAINS)は既定で除外する。`apply_email_allowlist=False`
    で除外を切れる(除外そのものを検証するテスト用)。

    戻り値は一致文字列を含まない。ログ・job summary・artifactのいずれへも
    平文を出さない設計を、呼び出し側に依存せずここで保証する。
    """
    hashes = known_hashes if known_hashes is not None else _KNOWN_PII_HASHES
    findings: list[MetadataFinding] = []
    for surface, location, text in items:
        if not text:
            continue
        seen: set[tuple[str, str]] = set()
        for token in _candidate_tokens(text):
            digest = _hash(token)
            if digest in hashes:
                key = (REASON_DENYLIST, digest[:_HASH_PREFIX_LEN])
                if key not in seen:
                    seen.add(key)
                    findings.append(
                        MetadataFinding(surface, location, REASON_DENYLIST, key[1])
                    )
        if detect_email_pattern:
            for match in _EMAIL.finditer(text):
                address = match.group(0)
                if apply_email_allowlist and _is_non_personal_email(address):
                    continue
                digest = _hash(address)
                key = (REASON_EMAIL_PATTERN, digest[:_HASH_PREFIX_LEN])
                if key not in seen:
                    seen.add(key)
                    findings.append(
                        MetadataFinding(surface, location, REASON_EMAIL_PATTERN, key[1])
                    )
    return findings


def format_findings(findings: Iterable[MetadataFinding]) -> list[str]:
    """報告用の行を組み立てる。**一致文字列を含めない。**"""
    return [
        f"  {f.surface} {f.location} reason={f.reason} hash_prefix={f.token_hash_prefix}"
        for f in sorted(
            findings, key=lambda f: (f.surface, f.location, f.reason, f.token_hash_prefix)
        )
    ]


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
