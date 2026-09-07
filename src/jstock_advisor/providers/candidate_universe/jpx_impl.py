"""candidate_universe_provider のJPX実装(候補ユニバース本格対応・2026-08)。

東証プライム+スタンダード全銘柄(約3,122件)を自動取得しスクリーニング対象とする。
このモジュールは2つの責務を持つ。

1. パース関数(parse_listed_issues_xls/parse_jpx400_weight_csv): ダウンロード済みの
   生バイト列を受け取り、CandidateUniverseItem一覧・行数統計・ソース日付を返す
   純粋関数。ネットワークアクセスは一切行わない。Downloader(検証時)とProvider
   (キャッシュ読み取り時)の両方から同じ関数を呼ぶことで、パースロジックを
   一本化する(第6版修正プランのDownloader/Parser/Provider分離)。
2. JpxCandidateUniverseProvider: S3/ローカルにキャッシュされた生データのみを読み、
   ネットワークアクセスは一切行わない(6節)。キャッシュが存在しない、または
   最大許容経過時間(8節)を超えている場合はCandidateUniverseErrorを送出する。

証券コード正規化の順序(9節): (1)Excel数値セルなら小数部0を確認して整数化・
4桁ゼロ埋め、(2)文字列なら前後空白除去、(3)Unicode NFKC正規化、(4)大文字化、
(5)^[0-9A-Z]{4}$で最終検証。CsvCandidateUniverseProvider側は変更しない。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass

import xlrd

from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.interfaces.candidate_universe import (
    CandidateUniverseError,
    CandidateUniverseItem,
    CandidateUniverseResult,
)

# data_j.xlsの列名(実データで確認済み)。
_COL_DATE = "日付"
_COL_CODE = "コード"
_COL_NAME = "銘柄名"
_COL_MARKET_SEGMENT = "市場・商品区分"
_COL_INDUSTRY_33_CODE = "33業種コード"
_COL_INDUSTRY_33_NAME = "33業種区分"
_COL_INDUSTRY_17_CODE = "17業種コード"
_COL_INDUSTRY_17_NAME = "17業種区分"
_COL_SIZE_CODE = "規模コード"
_COL_SIZE_NAME = "規模区分"

_REQUIRED_LISTED_ISSUES_COLUMNS = {
    _COL_DATE,
    _COL_CODE,
    _COL_NAME,
    _COL_MARKET_SEGMENT,
    _COL_INDUSTRY_33_CODE,
    _COL_INDUSTRY_33_NAME,
    _COL_INDUSTRY_17_CODE,
    _COL_INDUSTRY_17_NAME,
    _COL_SIZE_CODE,
    _COL_SIZE_NAME,
}

# JPX400 CSVの日付列(実データで確認済み)。
_JPX400_COL_DATE = "日付"
_JPX400_COL_CODE = "コード"

# 運用ハードニング7節: data_j.xlsの"市場・商品区分"列で実際に取りうる既知の値
# (JPX公開資料で確認済み)。この集合に無い値が現れた場合、対象外市場区分の想定内の
# 行(グロース等)ではなく、パース対象列がずれた・ファイル形式が変わった等の
# 異常を疑う根拠として`unknown_market_segment_count`に計上する(除外の可否自体は
# 従来どおりtarget_market_segmentsのみで決める、この集合は異常検知専用)。
_KNOWN_MARKET_SEGMENTS = frozenset(
    {
        "プライム（内国株式）",
        "スタンダード（内国株式）",
        "グロース（内国株式）",
        "PRO Market",
        "ETF・ETN",
        "REIT・ベンチャーファンド・カントリーファンド・インフラファンド",
        "出資証券",
        "外国株式",
    }
)


def _normalize_stock_code(raw: object) -> str | None:
    """9節の5ステップで正規化する。不正な場合はNoneを返す。

    実体はExternalValueParser.stock_code()へ委譲する(全銘柄コード正規化の
    共通実装。infrastructure/external_value_parser.py参照)。
    """
    return ExternalValueParser.stock_code(raw)


def _resolve_jpx400_weight_column(fieldnames: list[str]) -> str | None:
    """ "JPX日経400"と"ウェイト"の両方を部分文字列として含む列名を探す(9節)。"""
    for name in fieldnames:
        if "JPX日経400" in name and "ウェイト" in name:
            return name
    return None


def _extract_excel_date(value: object, datemode: int | None = None) -> dt.date | None:
    """「日付」列のセル値を日付へ変換する。

    Issue #223(PR-2): .xlsxをopenpyxlで読む経路が加わったため、datemodeを
    Optionalにした。datemodeは**.xls固有の概念**(1900年系/1904年系のシリアル値
    の基準日)であり、openpyxlは日付セルをdatetimeとして返すためこの概念を持た
    ない。datemodeがNoneのときはxlrdを一切呼ばない。

    セルの型は読み出し実装で異なる。
      xlrd     数値セルは常にfloat(例: 20260630.0)
      openpyxl 整数セルはint、日付書式のセルはdatetime
    どちらでも同じdt.dateになるよう、int/float/datetime/strをすべて受ける。
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        # openpyxl経路。data_j.xlsxの「日付」列はYYYYMMDD形式の整数。
        return _parse_date_string(str(value)) if 19000101 <= value <= 99991231 else None
    if isinstance(value, float):
        # data_j.xlsの「日付」列はExcelのシリアル値ではなく、YYYYMMDD形式の数値が
        # 数値セルとして格納されている(例: 20260630.0)。シリアル値として解釈すると
        # 桁数が大きすぎてOverflowErrorになるため、YYYYMMDD形式を先に判定する。
        if value.is_integer() and 19000101 <= value <= 99991231:
            return _parse_date_string(str(int(value)))
        if datemode is None:
            # openpyxl経路でシリアル値が来ることは想定していない
            # (日付書式ならdatetimeで返るため)。xlrdへは委ねない。
            return None
        try:
            excel_date: dt.date = xlrd.xldate.xldate_as_datetime(value, datemode).date()
        except (xlrd.xldate.XLDateError, ValueError, OverflowError):
            return None
        else:
            return excel_date
    if isinstance(value, str):
        return _parse_date_string(value)
    return None


def _parse_date_string(value: str) -> dt.date | None:
    """ExternalValueParser.date()へ委譲する(書式優先順位は従来と同一)。"""
    return ExternalValueParser.date(value)


# Issue #223(PR-2): 上場銘柄一覧の容れ物の判別。JPXは2026-09-03に
# data_j.xls(.xls)からdata_j.xlsx(.xlsx)へ差し替えたが、**URLのトークンも
# ファイル名の幹も変えなかった**ため、拡張子だけでは取り違えうる。
# 中身の先頭バイト(マジックナンバー)で判別する。
# H-6(管理者判断): xlrdを残して両対応にする。旧形式へ戻っても動くこと。
# 非ASCIIのエスケープをソースへ直接書かず、16進表記から組み立てる。
_XLS_MAGIC = bytes.fromhex("d0cf11e0")  # OLE2複合ドキュメント(.xls)
_XLSX_MAGIC = bytes.fromhex("504b0304")  # ZIP(.xlsx)


def _read_listed_issues_rows(data: bytes) -> tuple[list[list[object]], int | None]:
    """上場銘柄一覧の全セルを行のリストとして読み出す。

    戻り値は(rows, datemode)。rows[0]がヘッダ行。datemodeは.xls経路でのみ
    意味を持ち、.xlsx経路ではNoneを返す(_extract_excel_dateがこれで分岐する)。

    **判定・正規化・集計はこの関数の外に1本だけ置く**。容れ物が2種類に
    なっても、パース後のロジックは1実装のまま保つための分離である。
    """
    if data.startswith(_XLSX_MAGIC):
        return _read_rows_openpyxl(data), None
    if data.startswith(_XLS_MAGIC):
        return _read_rows_xlrd(data)
    raise CandidateUniverseError(
        "東証上場銘柄一覧の形式を判別できません"
        "(.xls/.xlsxのいずれのマジックナンバーとも一致しません)"
    )


def _read_rows_xlrd(data: bytes) -> tuple[list[list[object]], int]:
    book = xlrd.open_workbook(file_contents=data)
    sheet = book.sheet_by_index(0)
    rows = [
        [sheet.cell_value(row, col) for col in range(sheet.ncols)] for row in range(sheet.nrows)
    ]
    return rows, book.datemode


def _read_rows_openpyxl(data: bytes) -> list[list[object]]:
    # read_only: 3,000行超を一度にメモリへ載せない(Lambdaのメモリ制約)。
    # data_only: 数式ではなく計算済みの値を読む。
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        return [
            [_normalize_openpyxl_cell(cell) for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
    finally:
        # read_onlyで開いた場合、明示的に閉じないとファイルハンドルが残る。
        workbook.close()


def _normalize_code_cell(value: object) -> str | None:
    """業種コード・規模コードのセルを、**読み手に依存しない**文字列にする。

    Issue #223 PR-2 の実測(2026-09-07): これらの列はJPXのファイル上で
    **数値として**格納されている。同じ値でも読み手で型が違う。

        .xls  / xlrd     float  50.0  -> 素朴なstr()は "50.0"
        .xlsx / openpyxl int    50    -> 素朴なstr()は "50"

    素朴な`str()`のままだと、**JPXが配る容れ物が変わっただけで同じ銘柄の
    業種コードが変わってしまう**。`industry_33_code`は
    canonical_industry.pyが「canonicalの安定キー」と定めている値であり、
    容れ物に依存して揺れてはならない。整数値の小数表記(.0)を落として
    両者をそろえる。

    ★ ゼロ埋め等の桁合わせは**行わない**。どちらのファイルにもその形の値は
      無く、この実装で新しい表記を発明すべきではないため
      (銘柄コードの4桁ゼロ埋めは_normalize_stock_codeが別途担っている)。

    数値でない値(ETF/REIT行の "-" 等)はそのまま文字列として扱う。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip() or None


def _normalize_openpyxl_cell(value: object) -> object:
    """openpyxlのセル値を、xlrdが返す表現へ合わせる。

    **空セルの表現が両者で違う**。xlrdは空文字列""を返すが、openpyxlはNoneを
    返す。この差を吸収しないと、後段の`str(value).strip()`が文字列"None"を
    作ってしまい、銘柄名・業種コード・規模区分へ"None"が入る
    (市場・商品区分では未知区分として数えられる)。**空セルの契約を1つに
    そろえる**のが読み出し層の責務であり、判定側では吸収しない。

    数値セルの型もxlrdはfloat、openpyxlはintで異なるが、こちらは
    ExternalValueParser/_normalize_stock_code/_extract_excel_dateが
    どちらも受けるため、ここでは変換しない
    (勝手にstrへ寄せるとゼロ埋めの情報が失われるため)。
    """
    return "" if value is None else value


@dataclass(frozen=True)
class ParsedListedIssues:
    items: list[CandidateUniverseItem]
    raw_row_count: int
    invalid_code_count: int
    duplicate_count: int
    unknown_market_segment_count: int
    source_date: dt.date | None


def parse_listed_issues_xls(
    data: bytes, target_market_segments: set[str] | None
) -> ParsedListedIssues:
    """東証上場銘柄一覧(data_j.xls)をパースする。

    target_market_segmentsが指定されている場合、市場・商品区分がこの集合に
    含まれる行のみをitemsへ残す(プライム+スタンダードへの絞り込み)。除外された
    行はraw_row_count/invalid_code_countのいずれにもカウントしない(形式不正では
    なく対象外の市場区分のため)。unknown_market_segment_countのみ、
    target_market_segmentsによる絞り込みより前の全行を対象に集計する(運用
    ハードニング7節: 対象外市場区分による正常な除外と、列ずれ等の異常を区別する)。
    """
    rows, datemode = _read_listed_issues_rows(data)
    if not rows:
        raise CandidateUniverseError("東証上場銘柄一覧が空です")
    header = ["" if cell is None else str(cell).strip() for cell in rows[0]]
    missing = _REQUIRED_LISTED_ISSUES_COLUMNS - set(header)
    if missing:
        raise CandidateUniverseError(f"東証上場銘柄一覧に必須列がありません: {sorted(missing)}")
    col_index = {name: idx for idx, name in enumerate(header)}

    items: list[CandidateUniverseItem] = []
    raw_row_count = 0
    invalid_code_count = 0
    duplicate_count = 0
    unknown_market_segment_count = 0
    source_date: dt.date | None = None
    seen: set[str] = set()

    for row_cells in rows[1:]:
        if len(row_cells) <= max(col_index.values()):
            continue  # 列数が足りない行(末尾の空行等)
        code_cell = row_cells[col_index[_COL_CODE]]
        if code_cell in ("", None):
            continue  # 空行

        market_segment = str(row_cells[col_index[_COL_MARKET_SEGMENT]]).strip()
        if market_segment not in _KNOWN_MARKET_SEGMENTS:
            unknown_market_segment_count += 1
        if target_market_segments is not None and market_segment not in target_market_segments:
            continue  # 対象外市場区分(raw_row_countに含めない)

        raw_row_count += 1

        if source_date is None:
            source_date = _extract_excel_date(
                row_cells[col_index[_COL_DATE]], datemode
            )

        stock_code = _normalize_stock_code(code_cell)
        if stock_code is None:
            invalid_code_count += 1
            continue
        if stock_code in seen:
            duplicate_count += 1
            continue
        seen.add(stock_code)

        items.append(
            CandidateUniverseItem(
                stock_code=stock_code,
                stock_name=str(row_cells[col_index[_COL_NAME]]).strip() or None,
                market_segment=market_segment or None,
                industry_33_code=_normalize_code_cell(
                    row_cells[col_index[_COL_INDUSTRY_33_CODE]]
                ),
                industry_33_name=str(
                    row_cells[col_index[_COL_INDUSTRY_33_NAME]]
                ).strip()
                or None,
                industry_17_code=_normalize_code_cell(
                    row_cells[col_index[_COL_INDUSTRY_17_CODE]]
                ),
                industry_17_name=str(
                    row_cells[col_index[_COL_INDUSTRY_17_NAME]]
                ).strip()
                or None,
                size_code=_normalize_code_cell(row_cells[col_index[_COL_SIZE_CODE]]),
                size_name=str(row_cells[col_index[_COL_SIZE_NAME]]).strip() or None,
            )
        )

    return ParsedListedIssues(
        items=items,
        raw_row_count=raw_row_count,
        invalid_code_count=invalid_code_count,
        duplicate_count=duplicate_count,
        unknown_market_segment_count=unknown_market_segment_count,
        source_date=source_date,
    )


@dataclass(frozen=True)
class ParsedJpx400Membership:
    member_codes: set[str]
    raw_row_count: int
    invalid_code_count: int
    duplicate_count: int
    source_date: dt.date | None


def parse_jpx400_weight_csv(data: bytes) -> ParsedJpx400Membership:
    """JPX400構成銘柄CSV(Nikkei配信)をパースし、構成銘柄コード集合を返す。

    スコアリングには使わず、CandidateUniverseItem.is_jpx400_memberフラグの
    算出のみに使う(候補ユニバース本格対応: 全体はプライム+スタンダード全銘柄、
    JPX400はフラグとして付与するのみ)。
    """
    # Nikkei配信CSVはCP932(Shift-JIS)の場合があるため、UTF-8で失敗したらCP932で再試行する。
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp932")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CandidateUniverseError("JPX400構成銘柄CSVにヘッダー行がありません")
    fieldnames = list(reader.fieldnames)
    if _JPX400_COL_CODE not in fieldnames or _JPX400_COL_DATE not in fieldnames:
        raise CandidateUniverseError(
            f"JPX400構成銘柄CSVに必須列がありません: {_JPX400_COL_CODE}/{_JPX400_COL_DATE}"
        )
    weight_column = _resolve_jpx400_weight_column(fieldnames)
    if weight_column is None:
        raise CandidateUniverseError("JPX400構成銘柄CSVにウェイト列が見つかりません")

    member_codes: set[str] = set()
    raw_row_count = 0
    invalid_code_count = 0
    duplicate_count = 0
    source_date: dt.date | None = None

    for row in reader:
        raw_code = (row.get(_JPX400_COL_CODE) or "").strip()
        if not raw_code:
            continue
        raw_row_count += 1

        if source_date is None:
            source_date = _parse_date_string(row.get(_JPX400_COL_DATE) or "")

        stock_code = _normalize_stock_code(raw_code)
        if stock_code is None:
            invalid_code_count += 1
            continue
        if stock_code in member_codes:
            duplicate_count += 1
            continue
        member_codes.add(stock_code)

    return ParsedJpx400Membership(
        member_codes=member_codes,
        raw_row_count=raw_row_count,
        invalid_code_count=invalid_code_count,
        duplicate_count=duplicate_count,
        source_date=source_date,
    )


class JpxCandidateUniverseProvider:
    """S3/ローカルのキャッシュのみを読む(ネットワークアクセスなし、6節)。"""

    def __init__(
        self,
        target_market_segments: set[str] | None,
        listed_issues_max_stale_hours: int,
        jpx400_max_stale_hours: int,
        now: dt.datetime,
    ) -> None:
        self._target_market_segments = target_market_segments
        self._listed_issues_max_stale_hours = listed_issues_max_stale_hours
        self._jpx400_max_stale_hours = jpx400_max_stale_hours
        self._now = now

    def get_candidate_universe(self) -> CandidateUniverseResult:
        # 遅延import(candidate_universe_downloader.py -> jpx_impl.pyの循環importを避ける。
        # CandidateUniverseCacheIOはDownloader側の責務だが、Provider側の読み取りにも
        # 同じキャッシュアクセス手段を使う必要があるため、ここでのみ参照する)。
        from jstock_advisor.services.candidate_universe_downloader import (
            CandidateUniverseCacheIO,
        )

        cache_io = CandidateUniverseCacheIO()

        listed_cached = cache_io.read_current("listed_issues")
        if listed_cached is None:
            raise CandidateUniverseError(
                "東証上場銘柄一覧のキャッシュが存在しません(初回のDownloader実行が未完了です)"
            )
        listed_data, listed_metadata = listed_cached
        self._check_staleness(
            "東証上場銘柄一覧", listed_metadata.source_date, self._listed_issues_max_stale_hours
        )

        jpx400_cached = cache_io.read_current("jpx400")
        if jpx400_cached is None:
            raise CandidateUniverseError(
                "JPX400構成銘柄のキャッシュが存在しません(初回のDownloader実行が未完了です)"
            )
        jpx400_data, jpx400_metadata = jpx400_cached
        self._check_staleness(
            "JPX400構成銘柄", jpx400_metadata.source_date, self._jpx400_max_stale_hours
        )

        listed = parse_listed_issues_xls(listed_data, self._target_market_segments)
        jpx400 = parse_jpx400_weight_csv(jpx400_data)

        items = [
            CandidateUniverseItem(
                stock_code=item.stock_code,
                stock_name=item.stock_name,
                market_segment=item.market_segment,
                industry_33_code=item.industry_33_code,
                industry_33_name=item.industry_33_name,
                industry_17_code=item.industry_17_code,
                industry_17_name=item.industry_17_name,
                size_code=item.size_code,
                size_name=item.size_name,
                is_jpx400_member=item.stock_code in jpx400.member_codes,
            )
            for item in listed.items
        ]

        cache_age_hours = (
            (
                self._now
                - dt.datetime.combine(listed_metadata.source_date, dt.time(), tzinfo=dt.UTC)
            ).total_seconds()
            / 3600
            if listed_metadata.source_date is not None
            else None
        )

        return CandidateUniverseResult(
            items=items,
            raw_row_count=listed.raw_row_count,
            duplicate_count=listed.duplicate_count,
            invalid_code_count=listed.invalid_code_count,
            selected_count=len(items),
            source_date=listed_metadata.source_date,
            fetched_at=self._now,
            cache_last_modified=listed_metadata.promoted_at,
            cache_age_hours=cache_age_hours,
        )

    def _check_staleness(
        self, label: str, source_date: dt.date | None, max_stale_hours: int
    ) -> None:
        if source_date is None:
            raise CandidateUniverseError(
                f"{label}のソース日付が不明なためキャッシュを利用できません"
            )
        age_hours = (
            self._now - dt.datetime.combine(source_date, dt.time(), tzinfo=dt.UTC)
        ).total_seconds() / 3600
        if age_hours > max_stale_hours:
            raise CandidateUniverseError(
                f"{label}のキャッシュが最大許容経過時間を超えています"
                f"(source_date={source_date}, 経過={age_hours:.0f}h, 上限={max_stale_hours}h)"
            )
