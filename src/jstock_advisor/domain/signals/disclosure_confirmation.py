"""開示の『確認』判定(Issue #889 / #888 PR-1。dormant な純粋関数)。

適時開示(title / summary)から、3 つの rule(major_scandal / accounting_problem /
listing_maintenance_risk)について『危険な言葉の検出』と『重大事象の確認』を判定する。
**どこからも import されない**(現行の判定・通知・スナップショットは変えない。配線は PR-2 以降)。
現行の ``classify_disclosure_risk_keywords_with_confirmation`` が持つ次の欠陥の是正方針
(USER 決定 A1 + B2 + C1 + D1 + D'α + E1。#122)を、新しい関数として定義する。

    A1 結びつけ  確認は『同じ 1 件の開示の中』にある危険な言葉と確認の言葉の組だけを数える
                 (別の開示にある語同士は結びつけない)
    B2 否定      否定を伴う出現は『格上げ』に数えない。**検出は残す**(確認要止まり)
    C1 自己充足  確認の言葉から『継続企業』を外す(危険な言葉『継続企業の前提に関する重要事象』の
                 中に含まれ、危険な言葉だけで確認になっていた)。危険な言葉の出現範囲と重なる
                 確認の言葉の出現は、一般に数えない
    D'α 会計     accounting_problem は 2 段階(検出 -> 確認)にし、確認は 4 語それぞれの限定を
                 満たす場合だけにする。確認は 2 区分に分ける(下記)
    E1           理由コードの名前は変えない(本 module は理由コードを持たない)

## 会計の確認の 2 区分(混ぜない)

    A 重大な会計上の問題の確認(SERIOUS_PROBLEM_CONFIRMED)
        会計の危険な言葉と、4 語のいずれかの『限定を満たす確認の表現』が同じ開示にある
    B 会計不正の事実の確認(FRAUD_FACT_CONFIRMED)
        A のうち、『不正の事実』の限定を満たす場合に限る。**他の 3 語(決算訂正・監査意見・
        経営陣の責任)と危険な言葉だけでは B にならない**(B ⊂ A)

## 設計上の約束

* **誤りは『格上げしない』側に倒す**(B2 と同じ)。ただし検出(RISK_KEYWORD_DETECTED)は
  決して消さない: ``detected_keywords`` は現行の ``detect_disclosure_risk_keywords`` と一致する。
* 判定の単位は『文』(title と summary をそれぞれ『。』と改行で区切った断片。括弧の中の『。』では
  区切らない)。否定・非断定の
  言い回しは、**出現を含む文**にあるときだけ、その出現に効く(別の文の否定は影響しない)。
  読点『、』では区切らない(同じ文に仮定と確定が同居すると、その文の出現は数えない = 格上げ
  しない側。既知の限界として契約テストで固定している)。
* 結果は開示の並び順・重複に依存しない(件数でなく、昇順の語の組)。
* 語彙(言葉・限定・否定・非断定の言い回し)は ``DisclosureConfirmationRules`` のデータで、
  数値の閾値ではない。既定値は module 定数。語は業務判断を含み、PR で一覧を示して review を受ける。
* 純粋: 時計・乱数・I/O・logger・永続化を持たず、入力を変更しない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from jstock_advisor.interfaces.types import Disclosure

MAJOR_SCANDAL = "major_scandal"
ACCOUNTING_PROBLEM = "accounting_problem"
LISTING_MAINTENANCE_RISK = "listing_maintenance_risk"

#: 判定する rule(固定。新しい rule は足さない)
ASSESSED_RULES: tuple[str, ...] = (MAJOR_SCANDAL, ACCOUNTING_PROBLEM, LISTING_MAINTENANCE_RISK)

#: 2 段階(検出 -> 確認)の rule。会計は別の型(AccountingAssessment)で 3 段階
_TWO_STAGE_RULES: tuple[str, ...] = (MAJOR_SCANDAL, LISTING_MAINTENANCE_RISK)

#: 危険な言葉 -> rule(sell_signal._KEYWORD_RULE_MAP と同一。契約テストで一致を固定)
RISK_KEYWORD_TO_RULE: Mapping[str, str] = MappingProxyType(
    {
        "特別調査委員会": MAJOR_SCANDAL,
        "第三者委員会": MAJOR_SCANDAL,
        "内部統制上の重要な不備": ACCOUNTING_PROBLEM,
        "不適切な会計処理": ACCOUNTING_PROBLEM,
        "上場廃止基準": LISTING_MAINTENANCE_RISK,
        "監理銘柄": LISTING_MAINTENANCE_RISK,
        "整理銘柄": LISTING_MAINTENANCE_RISK,
        "継続企業の前提に関する重要事象": LISTING_MAINTENANCE_RISK,
    }
)

#: 確認の言葉(major_scandal / listing_maintenance_risk)。現行の MATERIAL_EVENT_KEYWORDS から
#: 『継続企業』(C1)を除いた 8 語。この 8 語は通常の開示にも現れうる(弱さは範囲外。PR 本文に記載)
_CONFIRMATION_WORDS_2_STAGE: tuple[str, ...] = (
    "決算訂正",
    "決算発表延期",
    "監査意見",
    "業績予想の大幅修正",
    "上場維持",
    "重大な財務損失",
    "経営陣の責任",
    "不正の事実",
)

#: 否定の言い回し(全 rule に効く。B2)。『ない』『なし』のような短い語は誤判定が多いため含めない。
#: 二重否定・複雑な構文は誤りうる(誤りは『格上げしない』側)。一覧は網羅ではない。
DEFAULT_NEGATION_PHRASES: tuple[str, ...] = (
    "認められませんでした",
    "認められておりません",
    "認められていません",
    "認められていない",
    "認められません",
    "認められない",
    "認められなかった",
    "認められず",
    "確認されませんでした",
    "確認されておりません",
    "確認されていません",
    "確認されていない",
    "確認されなかった",
    "確認されず",
    "判明しておりません",
    "判明していません",
    "判明していない",
    "判明しなかった",
    "判明せず",
    "該当しません",
    "該当しない",
    "該当いたしません",
    "存在しません",
    "存在しない",
    "存在いたしません",
    "ありません",
    "ございません",
    "否定されました",
    "否定されており",
    "否定されている",
    "否定しました",
    "否定しております",
    "認めておりません",
    "認めていません",
    "認めません",
    "認めない",
    # 動詞の否定(『追及しません』『訂正は行いません』のように、列挙した形に無い否定を拾う)
    "しません",
    "行いません",
    "おりません",
    "いません",
)

#: 非断定の言い回し(会計の確認だけに効く。仮定・条件・将来・調査中・疑義)。
#: 『予定』は含めない(決定を知らせる確定の開示がありうる。境界として PR 本文に明記)。
#: 『調査』『検討』『次第』『なら』『ば』の単独の語は含めない(『調査の結果、〜』
#: 『お知らせする次第です』『ならびに』のような確定の文を落とすため)。
DEFAULT_NON_ASSERTIVE_PHRASES: tuple[str, ...] = (
    # 条件・仮定
    "場合",
    "し次第",
    "判明次第",
    "確認次第",
    "れば",
    "たら",
    "ならば",
    "仮に",
    "万一",
    "もし",
    # 将来・可能性・推量
    "おそれ",
    "恐れ",
    "可能性",
    "見込み",
    "かどうか",
    "とみられ",
    "と見られ",
    "と思われ",
    "懸念",
    "見通し",
    "方針",
    # 調査中・疑義
    "調査中",
    "調査しています",
    "調査しております",
    "精査中",
    "確認中",
    "検討中",
    "検討しています",
    "検討しております",
    "検討いたします",
    "検討します",
    "調査します",
    "調査いたします",
    "疑義",
    "疑い",
    "疑念",
    # 伝聞・真偽不明(事実の確定ではない)
    "不明",
    "との報道",
    "旨の報道",
    "との情報",
    "とのこと",
    # 調査・検討の対象を表す語(『〜の要否』『〜の有無』『〜の適否』は事実の確定ではなく論点)
    "要否",
    "有無",
    "適否",
)


@dataclass(frozen=True)
class ConfirmationExpression:
    """会計の『確認の表現』1 区分(4 語のうち 1 つ)。

    ``alternatives`` は OR、各 alternative は AND、各 group は OR の語の集合。1 つの**文**が
    いずれかの alternative の全 group を満たすとき、その文は表現を満たす。
    ``is_fraud_fact`` が True の表現だけが、B(会計不正の事実の確認)を作る。
    """

    key: str
    alternatives: tuple[tuple[tuple[str, ...], ...], ...]
    is_fraud_fact: bool = False

    def __post_init__(self) -> None:
        if not self.key or not self.alternatives:
            raise ValueError("ConfirmationExpression needs a key and at least one alternative")
        for alternative in self.alternatives:
            if not alternative:
                raise ValueError("an alternative must have at least one group")
            for group in alternative:
                if not group or any(not token for token in group):
                    raise ValueError("a group must be non-empty and contain no empty token")

    def satisfied_by(self, sentence: str, risk_keywords: Sequence[str] = ()) -> bool:
        """文が表現を満たすか。

        **危険な言葉の出現範囲の語は、限定の語として数えない**(自己充足の防止。C1 の一般化):
        危険な言葉『不適切な会計処理』の中の『不適切』で、決算訂正の限定の『不適切』が満たされない。
        ただし、限定の語が危険な言葉そのものである場合(訂正報告書 + 『不適切な会計処理』)は、
        その危険な言葉の出現を語として数える(意図: 訂正報告書と危険な言葉の同伴)。
        """
        masked = sentence
        for keyword in sorted(risk_keywords, key=len, reverse=True):
            masked = masked.replace(keyword, "\u25a0" * len(keyword))
        keyword_set = set(risk_keywords)

        def present(token: str) -> bool:
            return token in (sentence if token in keyword_set else masked)

        return any(
            all(any(present(token) for token in group) for group in alternative)
            for alternative in self.alternatives
        )


#: 会計の確認の 4 語それぞれの限定(Q-1a。差し替え可能なデータ。内容は USER が PR review で確認)
DEFAULT_ACCOUNTING_EXPRESSIONS: tuple[ConfirmationExpression, ...] = (
    ConfirmationExpression(
        key="決算訂正",
        alternatives=(
            (("過年度",), ("訂正",), ("誤り", "不適切", "誤謬")),
            (("訂正報告書",), ("不適切な会計処理", "誤り")),
            (("決算訂正",), ("誤り", "不適切", "誤謬")),
        ),
    ),
    ConfirmationExpression(
        key="監査意見",
        alternatives=((("限定付適正意見", "不適正意見", "意見不表明", "監査意見の不表明"),),),
    ),
    ConfirmationExpression(
        key="不正の事実",
        alternatives=(
            (
                ("不正の事実",),
                ("認められました", "認められた", "判明", "確認されました", "確認された", "認定"),
            ),
        ),
        is_fraud_fact=True,
    ),
    ConfirmationExpression(
        key="経営陣の責任",
        alternatives=((("経営陣の責任",), ("認め", "追及", "減額", "辞任")),),
    ),
)


@dataclass(frozen=True)
class DisclosureConfirmationRules:
    """語彙の表(数値の閾値ではない)。既定値は module 定数。"""

    risk_keyword_to_rule: Mapping[str, str]
    confirmation_words: Mapping[str, tuple[str, ...]]
    accounting_expressions: tuple[ConfirmationExpression, ...]
    negation_phrases: tuple[str, ...]
    non_assertive_phrases: tuple[str, ...]

    def __post_init__(self) -> None:
        for keyword, rule in self.risk_keyword_to_rule.items():
            if not keyword or rule not in ASSESSED_RULES:
                raise ValueError(f"invalid risk keyword mapping: {keyword!r} -> {rule!r}")
        if set(self.confirmation_words) != set(_TWO_STAGE_RULES):
            raise ValueError("confirmation_words must be defined for exactly the 2-stage rules")
        phrases = (
            *self.negation_phrases,
            *self.non_assertive_phrases,
            *(w for words in self.confirmation_words.values() for w in words),
        )
        if any(not phrase for phrase in phrases):
            raise ValueError("empty phrase would match every sentence")


DEFAULT_RULES = DisclosureConfirmationRules(
    risk_keyword_to_rule=RISK_KEYWORD_TO_RULE,
    confirmation_words=MappingProxyType(
        {rule: _CONFIRMATION_WORDS_2_STAGE for rule in _TWO_STAGE_RULES}
    ),
    accounting_expressions=DEFAULT_ACCOUNTING_EXPRESSIONS,
    negation_phrases=DEFAULT_NEGATION_PHRASES,
    non_assertive_phrases=DEFAULT_NON_ASSERTIVE_PHRASES,
)


class ConfirmationLevel(StrEnum):
    """major_scandal / listing_maintenance_risk の段階(値は現行の DisclosureRiskConfirmationLevel
    と同じ文字列。NONE = 現行の None に相当)。"""

    NONE = "NONE"
    RISK_KEYWORD_DETECTED = "RISK_KEYWORD_DETECTED"
    MATERIAL_EVENT_CONFIRMED = "MATERIAL_EVENT_CONFIRMED"


class AccountingLevel(StrEnum):
    """accounting_problem の段階(3 段階。確認が 2 区分)。"""

    NONE = "NONE"
    RISK_KEYWORD_DETECTED = "RISK_KEYWORD_DETECTED"
    SERIOUS_PROBLEM_CONFIRMED = "SERIOUS_PROBLEM_CONFIRMED"
    FRAUD_FACT_CONFIRMED = "FRAUD_FACT_CONFIRMED"


class ConfirmationContractError(ValueError):
    """評価結果の不変条件に反する構築(実装の誤り)。"""


@dataclass(frozen=True)
class RuleAssessment:
    """major_scandal / listing_maintenance_risk の評価。"""

    rule: str
    detected_keywords: tuple[str, ...]
    level: ConfirmationLevel
    confirmed_pairs: tuple[tuple[str, str], ...]
    #: 否定を無視すれば確認になる組があり、否定のためだけに格上げされなかった(B2 の可視化)
    blocked_by_negation: bool

    def __post_init__(self) -> None:
        if self.detected_keywords != tuple(sorted(set(self.detected_keywords))):
            raise ConfirmationContractError("detected_keywords must be sorted and unique")
        if self.confirmed_pairs != tuple(sorted(set(self.confirmed_pairs))):
            raise ConfirmationContractError("confirmed_pairs must be sorted and unique")
        has_pairs = bool(self.confirmed_pairs)
        if self.level is ConfirmationLevel.NONE and (self.detected_keywords or has_pairs):
            raise ConfirmationContractError("NONE must have no detection and no pairs")
        if self.level is ConfirmationLevel.RISK_KEYWORD_DETECTED and (
            not self.detected_keywords or has_pairs
        ):
            raise ConfirmationContractError("DETECTED needs detection and no pairs")
        if self.level is ConfirmationLevel.MATERIAL_EVENT_CONFIRMED and not has_pairs:
            raise ConfirmationContractError("CONFIRMED needs at least one pair")
        if self.level is ConfirmationLevel.MATERIAL_EVENT_CONFIRMED and self.blocked_by_negation:
            raise ConfirmationContractError("a confirmed rule cannot be blocked by negation")


@dataclass(frozen=True)
class AccountingAssessment:
    """accounting_problem の評価。確認は 2 区分: A(serious)と B(fraud 事実。B ⊂ A)。"""

    detected_keywords: tuple[str, ...]
    level: AccountingLevel
    serious_pairs: tuple[tuple[str, str], ...]
    fraud_pairs: tuple[tuple[str, str], ...]
    blocked_by_negation: bool
    blocked_by_non_assertive: bool

    def __post_init__(self) -> None:
        named = (("serious_pairs", self.serious_pairs), ("fraud_pairs", self.fraud_pairs))
        for name, pairs in named:
            if pairs != tuple(sorted(set(pairs))):
                raise ConfirmationContractError(f"{name} must be sorted and unique")
        if self.detected_keywords != tuple(sorted(set(self.detected_keywords))):
            raise ConfirmationContractError("detected_keywords must be sorted and unique")
        if not set(self.fraud_pairs) <= set(self.serious_pairs):
            raise ConfirmationContractError("fraud_pairs must be a subset of serious_pairs")
        confirmed = self.level in (
            AccountingLevel.SERIOUS_PROBLEM_CONFIRMED,
            AccountingLevel.FRAUD_FACT_CONFIRMED,
        )
        if self.level is AccountingLevel.FRAUD_FACT_CONFIRMED and not self.fraud_pairs:
            raise ConfirmationContractError("FRAUD_FACT_CONFIRMED needs fraud_pairs")
        if self.level is AccountingLevel.SERIOUS_PROBLEM_CONFIRMED and (
            self.fraud_pairs or not self.serious_pairs
        ):
            raise ConfirmationContractError("SERIOUS needs serious_pairs and no fraud_pairs")
        if not confirmed and (self.serious_pairs or self.fraud_pairs):
            raise ConfirmationContractError("pairs are only allowed on a confirmed level")
        if self.level is AccountingLevel.NONE and self.detected_keywords:
            raise ConfirmationContractError("NONE must have no detection")
        if self.level is AccountingLevel.RISK_KEYWORD_DETECTED and not self.detected_keywords:
            raise ConfirmationContractError("DETECTED needs a detected keyword")
        if confirmed and (self.blocked_by_negation or self.blocked_by_non_assertive):
            raise ConfirmationContractError("a confirmed level cannot be blocked")


@dataclass(frozen=True)
class DisclosureConfirmation:
    """3 rule の評価。"""

    major_scandal: RuleAssessment
    accounting_problem: AccountingAssessment
    listing_maintenance_risk: RuleAssessment


# --- 文の分割と出現 -----------------------------------------------------------------

_OPENING_BRACKETS = "（(「『【［["
_CLOSING_BRACKETS = "）)」』】］]"


def _split_sentences(text: str | None) -> list[str]:
    """『。』と改行で文に区切る。**括弧の中の『。』では区切らない**(『不正の事実が判明した
    (詳細は別紙。)場合は』のように、括弧の中の句点が条件の語を事実から切り離して、仮定の文を
    確定の文に見せるのを防ぐ)。閉じていない括弧は、次の改行までその文に続く(既知の限界:
    その間の文が 1 文になる = 格上げしない側)。
    """
    if not text:
        return []
    sentences: list[str] = []
    buffer: list[str] = []
    depth = 0

    def flush() -> None:
        sentence = "".join(buffer)
        if sentence.strip():
            sentences.append(sentence)
        buffer.clear()

    for char in text:
        if char in "\r\n":
            flush()
            depth = 0
        elif char == "。" and depth == 0:
            flush()
        else:
            if char in _OPENING_BRACKETS:
                depth += 1
            elif char in _CLOSING_BRACKETS:
                depth = max(0, depth - 1)
            buffer.append(char)
    flush()
    return sentences


def _disclosure_sentences(disclosure: Disclosure) -> list[str]:
    """title と summary をそれぞれ区切る(別の文。『。』のない title は 1 文)。"""
    return [*_split_sentences(disclosure.title), *_split_sentences(disclosure.summary)]


def _occurrences(sentence: str, word: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = sentence.find(word)
    while start != -1:
        spans.append((start, start + len(word)))
        start = sentence.find(word, start + 1)
    return spans


def _contains_any(sentence: str, phrases: Sequence[str]) -> bool:
    return any(phrase in sentence for phrase in phrases)


def _overlaps(span: tuple[int, int], others: Sequence[tuple[int, int]]) -> bool:
    return any(span[0] < other[1] and other[0] < span[1] for other in others)


def _pairs(left: set[str], right: set[str]) -> set[tuple[str, str]]:
    return {(a, b) for a in left for b in right}


# --- 評価 -------------------------------------------------------------------------


def _rule_keywords(rules: DisclosureConfirmationRules, rule: str) -> tuple[str, ...]:
    return tuple(sorted(k for k, r in rules.risk_keyword_to_rule.items() if r == rule))


def _assess_two_stage(
    rule: str, disclosures: Sequence[Disclosure], rules: DisclosureConfirmationRules
) -> RuleAssessment:
    risk_words = _rule_keywords(rules, rule)
    every_risk_word = tuple(rules.risk_keyword_to_rule)
    confirmation_words = rules.confirmation_words[rule]
    detected: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    pairs_ignoring_negation: set[tuple[str, str]] = set()
    for disclosure in disclosures:
        risk_counted: set[str] = set()
        risk_any: set[str] = set()
        words_counted: set[str] = set()
        words_any: set[str] = set()
        for sentence in _disclosure_sentences(disclosure):
            negated = _contains_any(sentence, rules.negation_phrases)
            risk_spans = [s for w in every_risk_word for s in _occurrences(sentence, w)]
            for word in risk_words:
                if word in sentence:
                    detected.add(word)
                    risk_any.add(word)
                    if not negated:
                        risk_counted.add(word)
            for word in confirmation_words:
                if any(not _overlaps(s, risk_spans) for s in _occurrences(sentence, word)):
                    words_any.add(word)
                    if not negated:
                        words_counted.add(word)
        pairs |= _pairs(risk_counted, words_counted)
        pairs_ignoring_negation |= _pairs(risk_any, words_any)
    if pairs:
        level = ConfirmationLevel.MATERIAL_EVENT_CONFIRMED
    elif detected:
        level = ConfirmationLevel.RISK_KEYWORD_DETECTED
    else:
        level = ConfirmationLevel.NONE
    return RuleAssessment(
        rule=rule,
        detected_keywords=tuple(sorted(detected)),
        level=level,
        confirmed_pairs=tuple(sorted(pairs)),
        blocked_by_negation=bool(pairs_ignoring_negation) and not pairs,
    )


def _assess_accounting(
    disclosures: Sequence[Disclosure], rules: DisclosureConfirmationRules
) -> AccountingAssessment:
    risk_words = _rule_keywords(rules, ACCOUNTING_PROBLEM)
    every_risk_word = tuple(rules.risk_keyword_to_rule)
    fraud_keys = {e.key for e in rules.accounting_expressions if e.is_fraud_fact}
    detected: set[str] = set()
    serious: set[tuple[str, str]] = set()
    serious_ignoring_negation: set[tuple[str, str]] = set()
    serious_ignoring_non_assertive: set[tuple[str, str]] = set()
    for disclosure in disclosures:
        risk_counted: set[str] = set()
        risk_any: set[str] = set()
        strict: set[str] = set()  # 否定でも非断定でもない文の表現
        no_negation_filter: set[str] = set()  # 否定を無視(非断定は除く)
        no_non_assertive_filter: set[str] = set()  # 非断定を無視(否定は除く)
        for sentence in _disclosure_sentences(disclosure):
            negated = _contains_any(sentence, rules.negation_phrases)
            non_assertive = _contains_any(sentence, rules.non_assertive_phrases)
            for word in risk_words:
                if word in sentence:
                    detected.add(word)
                    risk_any.add(word)
                    if not negated:
                        risk_counted.add(word)
            for expression in rules.accounting_expressions:
                if not expression.satisfied_by(sentence, every_risk_word):
                    continue
                if not non_assertive:
                    no_negation_filter.add(expression.key)
                    if not negated:
                        strict.add(expression.key)
                if not negated:
                    no_non_assertive_filter.add(expression.key)
        serious |= _pairs(risk_counted, strict)
        serious_ignoring_negation |= _pairs(risk_any, no_negation_filter)
        serious_ignoring_non_assertive |= _pairs(risk_counted, no_non_assertive_filter)
    fraud = {pair for pair in serious if pair[1] in fraud_keys}
    return AccountingAssessment(
        detected_keywords=tuple(sorted(detected)),
        level=_accounting_level(bool(detected), bool(serious), bool(fraud)),
        serious_pairs=tuple(sorted(serious)),
        fraud_pairs=tuple(sorted(fraud)),
        blocked_by_negation=bool(serious_ignoring_negation) and not serious,
        blocked_by_non_assertive=bool(serious_ignoring_non_assertive) and not serious,
    )


def _accounting_level(has_detection: bool, has_serious: bool, has_fraud: bool) -> AccountingLevel:
    if has_fraud:
        return AccountingLevel.FRAUD_FACT_CONFIRMED
    if has_serious:
        return AccountingLevel.SERIOUS_PROBLEM_CONFIRMED
    if has_detection:
        return AccountingLevel.RISK_KEYWORD_DETECTED
    return AccountingLevel.NONE


def assess_disclosure_confirmation(
    disclosures: Sequence[Disclosure],
    rules: DisclosureConfirmationRules = DEFAULT_RULES,
) -> DisclosureConfirmation:
    """開示の並びから、3 rule の検出と確認を判定する(純粋。入力を変更しない)。"""
    return DisclosureConfirmation(
        major_scandal=_assess_two_stage(MAJOR_SCANDAL, disclosures, rules),
        accounting_problem=_assess_accounting(disclosures, rules),
        listing_maintenance_risk=_assess_two_stage(LISTING_MAINTENANCE_RISK, disclosures, rules),
    )
