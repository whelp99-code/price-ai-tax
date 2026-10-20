"""Synthetic Hometax purchase CSV/xlsx fixtures. No real company text."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import importlib.util
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "notion_finance_s2.py"
    spec = importlib.util.spec_from_file_location("notion_finance_s2_csv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S2 = _load()


def _xlsx_with_header(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """Row 1 is the real export header. Data starts at row 2."""
    strings: list[str] = []
    index_of: dict[str, int] = {}

    def sid(value: str) -> int:
        if value not in index_of:
            index_of[value] = len(strings)
            strings.append(value)
        return index_of[value]

    cells = []
    header = {2: "승인번호", 9: "공급가액", 10: "세액", 11: "합계금액"}
    for col, value in header.items():
        letter = chr(64 + col)
        cells.append(f'<c r="{letter}1" t="s"><v>{sid(value)}</v></c>')
    for row_no, (approval, net, tax, gross) in enumerate(rows, start=2):
        for col, value in ((2, approval), (9, net), (10, tax), (11, gross)):
            letter = chr(64 + col)
            cells.append(f'<c r="{letter}{row_no}" t="s"><v>{sid(value)}</v></c>')
    shared = "".join(f"<si><t>{escape(item)}</t></si>" for item in strings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as book:
        book.writestr(
            "xl/sharedStrings.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{NS}">{shared}</sst>',
        )
        book.writestr(
            "xl/worksheets/sheet1.xml",
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<worksheet xmlns="{NS}"><sheetData>{"".join(cells)}</sheetData></worksheet>',
        )


def _xlsx(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """Minimal shared-string sheet. B approval, I/J/K = net/tax/gross."""
    strings: list[str] = []
    index_of: dict[str, int] = {}

    def sid(value: str) -> int:
        if value not in index_of:
            index_of[value] = len(strings)
            strings.append(value)
        return index_of[value]

    cells = []
    for row_no, (approval, net, tax, gross) in enumerate(rows, start=2):
        for col, value in ((2, approval), (9, net), (10, tax), (11, gross)):
            letter = chr(64 + col)
            cells.append(f'<c r="{letter}{row_no}" t="s"><v>{sid(value)}</v></c>')
    shared = "".join(f"<si><t>{escape(item)}</t></si>" for item in strings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as book:
        book.writestr(
            "xl/sharedStrings.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{NS}">{shared}</sst>',
        )
        book.writestr(
            "xl/worksheets/sheet1.xml",
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<worksheet xmlns="{NS}"><sheetData>{"".join(cells)}</sheetData></worksheet>',
        )


HEADER = "작성일자,승인번호,발급일자,전송일자,a,b,c,d,e,f,g,h,i,j,합계금액,공급가액,세액\n"


def _csv_row(approval: str, gross: str, net: str, tax: str) -> str:
    blanks = [""] * 10

    def cell(value: str) -> str:
        if any(ch in value for ch in ",\"\n"):
            return '"' + value.replace('"', '""') + '"'
        return value

    return ",".join(["2026-01-01", cell(approval), "2026-01-01", "2026-01-01", *blanks, cell(gross), cell(net), cell(tax)]) + "\n"


def test_hyphen_stripped_24_digit_matches_notion_key(tmp_path: Path):
    approval = "202601151234567890123456"
    raw = "20260115-12345678-90123456"
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "hometax_purchase.csv").write_text(
        "\ufeff" + HEADER + _csv_row(raw, "1,100", "1,000", "100"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert found[approval] == (1000, 100, 1100)
    assert approval.replace("-", "") == raw.replace("-", "")


def test_letter_suffix_is_separate_not_coerced(tmp_path: Path):
    (tmp_path / "매입세금계산서.csv").write_text(
        HEADER + _csv_row("20261015-41000021-b0009795", "384510", "349555", "34955"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found == {}
    assert len(separate) == 1
    assert separate[0]["approval_tail"] == "9795"
    assert "approval_raw" not in separate[0]
    assert separate[0]["gross"] == "384510"
    assert separate[0]["net"] == "349555"
    assert separate[0]["tax"] == "34955"
    assert separate[0]["row"] == "2"
    assert "not coerced" in separate[0]["reason"]
    assert "b0009795" not in found


def test_blank_approval_skipped_zero_kept_comma_exact(tmp_path: Path):
    body = (
        HEADER
        + _csv_row("", "0", "0", "0")
        + _csv_row("202601010000000000000000", "0", "0", "0")
        + _csv_row("202601010000000000000001", "1,100", "1,000", "100")
    )
    (tmp_path / "hometax_purchase.csv").write_text(body, encoding="utf-8")
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert found["202601010000000000000000"] == (0, 0, 0)
    assert found["202601010000000000000001"] == (1000, 100, 1100)


def test_generic_hometax_name_is_not_a_purchase_source(tmp_path: Path):
    sale = "202601010000000000000202"
    shared = "작성일자,승인번호,합계금액,공급가액,세액\n"
    (tmp_path / "hometax-bad.csv").write_text("승인번호,합계금액\n1,2\n", encoding="utf-8")
    (tmp_path / "hometax_sales.csv").write_text(
        shared + "2026-01-01," + sale + ",99000,90000,9000\n",
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found == {} and separate == []
    assert sale not in found

def test_missing_amount_column_fails_closed(tmp_path: Path):
    (tmp_path / "bad-export.csv").write_text("승인번호,합계금액,공급가액\n1,2,3\n", encoding="utf-8")
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found == {} and separate == []


def test_float_text_rejected(tmp_path: Path):
    (tmp_path / "매입-bad.csv").write_text(
        HEADER + _csv_row("202601010000000000000009", "1100.0", "1000", "100"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="exact integer"):
        S2.read_hometax_purchase_sources(tmp_path)


def test_empty_amount_fails_closed(tmp_path: Path):
    (tmp_path / "매입-empty.csv").write_text(
        HEADER + _csv_row("202601010000000000000008", "", "1000", "100"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="empty"):
        S2.read_hometax_purchase_sources(tmp_path)


def test_xlsx_and_csv_same_approval_not_double_counted(tmp_path: Path):
    approval = "202601151234567890123456"
    _xlsx(tmp_path / "TAX_demo_BuyHomeTax.xlsx", [(approval, "1000", "100", "1100")])
    (tmp_path / "child").mkdir()
    (tmp_path / "child" / "hometax_purchase_same.csv").write_text(
        HEADER + _csv_row("20260115-12345678-90123456", "1100", "1000", "100"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert list(found) == [approval]
    assert found[approval] == (1000, 100, 1100)
    assert sum(item[2] for item in found.values()) == 1100


def test_xlsx_csv_amount_clash_fails(tmp_path: Path):
    approval = "202601151234567890123456"
    _xlsx(tmp_path / "TAX_demo_BuyHomeTax.xlsx", [(approval, "1000", "100", "1100")])
    (tmp_path / "hometax_purchase_other.csv").write_text(
        HEADER + _csv_row(approval, "2200", "2000", "200"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="duplicate hometax approval"):
        S2.read_hometax_purchase_sources(tmp_path)


def test_xlsx_glob_stays_top_level_only(tmp_path: Path):
    approval = "202601151234567890123499"
    nested = tmp_path / "nested"
    nested.mkdir()
    _xlsx(nested / "TAX_hidden_BuyHomeTax.xlsx", [(approval, "10", "1", "11")])
    found, _separate = S2.read_hometax_purchase_sources(tmp_path)
    assert approval not in found


def test_notion_match_uses_stripped_key():
    from price.notion_finance_map import map_snapshot

    approval = "202601151234567890123456"
    row = {
        "sourceId": "page-synth-csv",
        "properties": {
            "구분": "매입",
            "거래명": "synthetic",
            "거래처": "synthetic",
            "금액": 1000,
            "부가세": 100,
            "합계": 1100,
            "자연키": f"invoice|{approval}",
            "메모": f"홈택스 승인번호: {approval}",
            "실제 처리일": "2026-01-15",
        },
    }
    report = map_snapshot([row], {approval: (1000, 100, 1100)})
    assert len(report.loaded) == 1
    assert report.loaded[0].keys["hometax-approval"] == approval
    clash = map_snapshot([row], {approval: (900, 100, 1000)})
    assert clash.holds and "conflict" in clash.holds[0].reason

def test_negative_amended_amounts_preserved(tmp_path: Path):
    approval = "202601010000000000000077"
    (tmp_path / "hometax_purchase.csv").write_text(
        HEADER + _csv_row(approval, "-1,100", "-1,000", "-100"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert found[approval] == (-1000, -100, -1100)


def test_short_and_long_digit_approvals_are_separate(tmp_path: Path):
    short = "12345678901234567890123"
    exact = "123456789012345678901234"
    long = "1234567890123456789012345"
    (tmp_path / "BuyHomeTax.csv").write_text(
        HEADER
        + _csv_row(short, "11", "10", "1")
        + _csv_row(exact, "22", "20", "2")
        + _csv_row(long, "33", "30", "3"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert list(found) == [exact]
    assert found[exact] == (20, 2, 22)
    assert [item["approval_tail"] for item in separate] == ["0123", "2345"]
    assert [item["gross"] for item in separate] == ["11", "33"]
    matched = sum(item[2] for item in found.values())
    held = sum(int(item["gross"]) for item in separate)
    assert matched == 22
    assert held == 44
    assert matched + held == 66


def test_unrelated_csv_ignored_named_missing_column_fails(tmp_path: Path):
    approval = "202601010000000000000088"
    (tmp_path / "gt_export.csv").write_text("id,amount\n1,999\n", encoding="utf-8")
    (tmp_path / "notes.csv").write_text("memo\nnot a tax file\n", encoding="utf-8")
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found == {} and separate == []
    (tmp_path / "hometax_purchase_ok.csv").write_text(HEADER + _csv_row(approval, "110", "100", "10"), encoding="utf-8")
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert found[approval] == (100, 10, 110)
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "매입목록.csv").write_text("승인번호,합계금액\n1,2\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="missing columns"):
        S2.read_hometax_purchase_sources(tmp_path)


def test_reconciliation_lists_each_hold_and_splits_totals():
    approval = "202601010000000000000099"
    separate = [
        {
            "file": "purchases.csv",
            "row": "3",
            "approval_tail": "9795",
            "net": "10",
            "tax": "1",
            "gross": "11",
            "reason": "approval is not exactly 24 digits after hyphen removal; not coerced",
        }
    ]
    report = S2.MapReport()
    report.hometax_separate = separate
    text = S2.reconciliation(report, {approval: (100, 10, 110)}, None)
    assert "HOLD approval…9795" in text
    assert "file=purchases.csv" in text
    assert "gross=11" in text
    assert "net=10" in text
    assert "| hometax matched gross | 0 |" in text
    assert "| hometax separate gross | 11 |" in text
    assert "| hometax all-row gross | 121 |" in text


def test_core_ingest_does_not_read_hometax_sources():
    import price.notion_finance_ingest as core

    assert not hasattr(core, "read_hometax_purchase_sources")
    assert not hasattr(core, "read_hometax_purchases")
def test_sales_named_csv_not_summed_into_purchases(tmp_path: Path):
    purchase = "202601010000000000000101"
    sale = "202601010000000000000202"
    shared = "작성일자,승인번호,합계금액,공급가액,세액\n"
    (tmp_path / "SellHomeTax_2026.csv").write_text(
        shared + "2026-01-01," + sale + ",99000,90000,9000\n",
        encoding="utf-8",
    )
    (tmp_path / "매출전자세금계산서.csv").write_text(
        HEADER + _csv_row(sale, "99000", "90000", "9000"),
        encoding="utf-8",
    )
    (tmp_path / "BuyHomeTax.csv").write_text(
        HEADER + _csv_row(purchase, "1100", "1000", "100"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert list(found) == [purchase]
    assert sale not in found
    assert sum(item[2] for item in found.values()) == 1100




def test_held_row_blocks_pass_even_when_24_digit_partial_matches():
    approval = "202601010000000000000099"
    report = S2.MapReport()
    report.hometax_separate = [
        {
            "file": "purchases.csv",
            "row": "4",
            "approval_tail": "9795",
            "net": "10",
            "tax": "1",
            "gross": "11",
            "reason": "approval is not exactly 24 digits after hyphen removal; not coerced",
        }
    ]
    text = S2.reconciliation(report, {approval: (100, 10, 110)}, None)
    assert "| hometax target vs loaded | 110 vs 0 | FAIL gap=110 |" in text
    assert "PASS" not in text.split("hold_explains")[0]


def test_memo_does_not_store_truncated_or_letter_approval():
    from price.notion_finance_map import map_row

    exact = "202601151234567890123456"
    kept = map_row(
        {
            "sourceId": "page-exact",
            "properties": {
                "구분": "매입",
                "금액": 1000,
                "부가세": 100,
                "합계": 1100,
                "거래명": "synthetic | item | 2026-01-15",
                "자연키": f"invoice|{exact}",
                "메모": f"홈택스 승인번호: {exact}",
                "실제 처리일": "2026-01-15",
            },
        }
    )
    assert kept.keys["hometax-approval"] == exact
    letter = map_row(
        {
            "sourceId": "page-letter",
            "properties": {
                "구분": "매입",
                "금액": 1000,
                "부가세": 100,
                "합계": 1100,
                "거래명": "synthetic | item | 2026-01-15",
                "자연키": "invoice|2026101541000021b0009795",
                "메모": "홈택스 승인번호: 20261015-41000021-b0009795",
                "실제 처리일": "2026-01-15",
            },
        }
    )
    assert letter.held
    assert "hometax-approval" not in getattr(letter, "keys", {})
    short = map_row(
        {
            "sourceId": "page-short",
            "properties": {
                "구분": "매입",
                "금액": 1000,
                "부가세": 100,
                "합계": 1100,
                "거래명": "synthetic | item | 2026-01-15",
                "자연키": "bank|page-short",
                "메모": "홈택스 승인번호: 2026011512345678",
                "실제 처리일": "2026-01-15",
            },
        }
    )
    assert short.held
    hyphen_only = map_row(
        {
            "sourceId": "page-hyphen",
            "properties": {
                "구분": "매입",
                "금액": 1000,
                "부가세": 100,
                "합계": 1100,
                "거래명": "synthetic | item | 2026-01-15",
                "자연키": f"invoice|{exact}",
                "메모": "홈택스 승인번호: 20260115-12345678-90123456",
                "실제 처리일": "2026-01-15",
            },
        }
    )
    assert hyphen_only.held
    assert "90123456" not in str(getattr(hyphen_only, "reason", ""))


def test_blank_approval_with_amount_fails_empty_total_row_skipped(tmp_path: Path):
    approval = "202601010000000000000404"
    (tmp_path / "hometax_purchase.csv").write_text(
        HEADER + _csv_row("", "", "", "") + _csv_row(approval, "110", "100", "10"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert found[approval] == (100, 10, 110)
    (tmp_path / "매입-blank-amount.csv").write_text(
        HEADER + _csv_row("", "1100", "1000", "100"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="blank approval"):
        S2.read_hometax_purchase_sources(tmp_path)



def test_matched_count_is_intersection_not_hometax_size(tmp_path: Path):
    import importlib.util

    approval = "202601151234567890123456"
    other = "202601010000000000000505"
    snapshot = {
        "finance": [
            {
                "sourceId": "page-one",
                "lastEditedTime": "2026-09-30T00:00:00.000Z",
                "properties": {
                    "구분": "매입",
                    "금액": 1000,
                    "부가세": 100,
                    "합계": 1100,
                    "거래명": "synthetic | item | 2026-01-15",
                    "자연키": f"invoice|{approval}",
                    "메모": f"홈택스 승인번호: {approval}",
                    "실제 처리일": "2026-01-15",
                },
            }
        ]
    }
    (tmp_path / "snap.json").write_text(__import__("json").dumps(snapshot), encoding="utf-8")
    (tmp_path / "BuyHomeTax.csv").write_text(
        HEADER + _csv_row(approval, "1100", "1000", "100") + _csv_row(other, "2200", "2000", "200"),
        encoding="utf-8",
    )
    db = tmp_path / "scratch.sqlite3"
    spec = importlib.util.spec_from_file_location(
        "notion_finance_ingest_matched",
        Path(__file__).resolve().parents[1] / "scripts" / "notion_finance_ingest.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from price.db import Database
    from price.models import CompanyInput
    from price.service import Service

    opened = Database(db)
    Service(opened).auth.bootstrap(CompanyInput(name="scratch-match"), "scratch-match", "scratch-secret-match")
    buf = io.StringIO()
    argv = sys.argv
    sys.argv = [
        "notion_finance_ingest",
        "--snapshot",
        str(tmp_path / "snap.json"),
        "--db",
        str(db),
        "--hometax-dir",
        str(tmp_path),
    ]
    try:
        with contextlib.redirect_stdout(buf):
            module.main()
    finally:
        sys.argv = argv
    result = json.loads(buf.getvalue())
    assert result["hometax_numeric"] == 2
    assert result["hometax_matched"] == 1
    assert result["journals"] == result["journals_before"]


def test_overlapping_held_rows_listed_once(tmp_path: Path):
    raw = "20261015-41000021-b0009795"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    body = HEADER + _csv_row(raw, "384510", "349555", "34955")
    (tmp_path / "a" / "매입.csv").write_text(body, encoding="utf-8")
    (tmp_path / "b" / "BuyHomeTax.csv").write_text(body, encoding="utf-8")
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found == {}
    assert len(separate) == 1
    assert separate[0]["approval_tail"] == "9795"
    assert separate[0]["gross"] == "384510"


def test_malformed_amounts_rejected_valid_comma_and_negative_kept(tmp_path: Path):
    approval = "202601010000000000000606"
    (tmp_path / "매입-bad-comma.csv").write_text(
        HEADER + _csv_row(approval, "1,00", "100", "0"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="exact integer"):
        S2.read_hometax_purchase_sources(tmp_path)
    (tmp_path / "매입-bad-comma.csv").unlink()
    (tmp_path / "매입-bad-sign.csv").write_text(
        HEADER + _csv_row(approval, "-+100", "100", "0"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="exact integer"):
        S2.read_hometax_purchase_sources(tmp_path)
    (tmp_path / "매입-bad-sign.csv").unlink()
    (tmp_path / "hometax_purchase_ok.csv").write_text(
        HEADER
        + _csv_row(approval, "-1,100", "-1,000", "-100")
        + _csv_row("202601010000000000000607", "1,100", "1,000", "100"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert found[approval] == (-1000, -100, -1100)
    assert found["202601010000000000000607"] == (1000, 100, 1100)

def test_map_report_declares_separate_and_reconciliation_uses_it():
    report = S2.MapReport()
    assert "hometax_separate" in report.__dataclass_fields__
    approval = "202601010000000000000099"
    report.hometax_separate = [
        {
            "file": "BuyHomeTax.csv",
            "row": "2",
            "approval_tail": "9795",
            "net": "10",
            "tax": "1",
            "gross": "11",
            "reason": "approval is not exactly 24 digits after hyphen removal; not coerced",
        }
    ]
    text = S2.reconciliation(report, {approval: (100, 10, 110)}, None)
    assert "| hometax target vs loaded | 110 vs 0 | FAIL gap=110 |" in text
    assert "PASS" not in text.split("hold_explains")[0]

def test_unmarked_shared_columns_and_generic_hometax_are_rejected(tmp_path: Path):
    sale = "202601010000000000000303"
    shared = "작성일자,승인번호,합계금액,공급가액,세액\n"
    row = "2026-01-01," + sale + ",99000,90000,9000\n"
    (tmp_path / "export.csv").write_text(shared + row, encoding="utf-8")
    (tmp_path / "hometax.csv").write_text(shared + row, encoding="utf-8")
    (tmp_path / "hometax_etax.csv").write_text(shared + row, encoding="utf-8")
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found == {} and separate == []
    (tmp_path / "hometax_purchase_etax_demo.csv").write_text(
        HEADER + _csv_row("202601010000000000000304", "1100", "1000", "100"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert found["202601010000000000000304"] == (1000, 100, 1100)
    assert sale not in found


def test_same_tail_different_approvals_stay_separate(tmp_path: Path):
    left = "20261015-41000021-b0009795"
    right = "20261016-41000022-c0009795"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "매입.csv").write_text(
        HEADER + _csv_row(left, "11", "10", "1"),
        encoding="utf-8",
    )
    (tmp_path / "b" / "BuyHomeTax.csv").write_text(
        HEADER + _csv_row(right, "11", "10", "1"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found == {}
    assert len(separate) == 2
    assert separate[0]["approval_tail"] == separate[1]["approval_tail"] == "9795"
    assert separate[0]["approval_id"] != separate[1]["approval_id"]
    assert "approval_raw" not in separate[0]
    assert len(separate[0]["approval_id"]) == 64


def test_same_normalized_approval_across_files_listed_once(tmp_path: Path):
    raw = "20261015-41000021-b0009795"
    spaced = "20261015 41000021 b0009795"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "매입.csv").write_text(HEADER + _csv_row(raw, "11", "10", "1"), encoding="utf-8")
    (tmp_path / "b" / "BuyHomeTax.csv").write_text(HEADER + _csv_row(spaced, "11", "10", "1"), encoding="utf-8")
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found == {}
    assert len(separate) == 1
    assert separate[0]["approval_tail"] == "9795"


def test_grouped_thousands_accepted_and_irregular_comma_rejected(tmp_path: Path):
    approval = "202601010000000000000808"
    (tmp_path / "매입-grouped.csv").write_text(
        HEADER + _csv_row(approval, "1,234,567", "1,122,334", "112,233"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert found[approval] == (1122334, 112233, 1234567)
    (tmp_path / "매입-grouped.csv").unlink()
    (tmp_path / "매입-bad-group.csv").write_text(
        HEADER + _csv_row(approval, "1234,567", "100", "0"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="exact integer"):
        S2.read_hometax_purchase_sources(tmp_path)


def test_blank_approval_zero_comma_placeholder_does_not_bypass(tmp_path: Path):
    (tmp_path / "매입-zero.csv").write_text(
        HEADER + _csv_row("", "0,00", "0", "0"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="exact integer"):
        S2.read_hometax_purchase_sources(tmp_path)
    (tmp_path / "매입-zero.csv").unlink()
    (tmp_path / "매입-zero-ok.csv").write_text(
        HEADER + _csv_row("", "0", "+0", "-0"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found == {} and separate == []
    (tmp_path / "매입-zero-ok.csv").unlink()
    (tmp_path / "매입-zero-comma.csv").write_text(
        HEADER + _csv_row("", "0,000", "0", "0"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="exact integer"):
        S2.read_hometax_purchase_sources(tmp_path)


def test_labeled_bad_token_does_not_fall_through_to_later_digits():
    from price.notion_finance_map import map_row

    exact = "202601151234567890123456"
    other = "202601019999999999999999"
    held = map_row(
        {
            "sourceId": "page-bad-label",
            "properties": {
                "구분": "매입",
                "금액": 1000,
                "부가세": 100,
                "합계": 1100,
                "거래명": "synthetic | item | 2026-01-15",
                "자연키": "bank|page-bad-label",
                "메모": f"홈택스 승인번호: 20261015-41000021-b0009795 later {other}",
                "실제 처리일": "2026-01-15",
            },
        }
    )
    assert held.held
    assert "hometax-approval" not in getattr(held, "keys", {})
    assert other not in str(getattr(held, "reason", ""))
    bare = map_row(
        {
            "sourceId": "page-bare",
            "properties": {
                "구분": "매입",
                "금액": 1000,
                "부가세": 100,
                "합계": 1100,
                "거래명": "synthetic | item | 2026-01-15",
                "자연키": "bank|page-bare",
                "메모": f"reference {exact}",
                "실제 처리일": "2026-01-15",
            },
        }
    )
    assert "hometax-approval" not in getattr(bare, "keys", {})
    kept = map_row(
        {
            "sourceId": "page-kept-label",
            "properties": {
                "구분": "매입",
                "금액": 1000,
                "부가세": 100,
                "합계": 1100,
                "거래명": "synthetic | item | 2026-01-15",
                "자연키": "bank|page-kept-label",
                "메모": f"홈택스 승인번호: {exact}",
                "실제 처리일": "2026-01-15",
            },
        }
    )
    assert kept.keys["hometax-approval"] == exact


def test_operational_reconciliation_receives_separate_and_blocks_pass(tmp_path: Path):
    import importlib.util

    approval = "202601151234567890123456"
    held = "20261015-41000021-b0009795"
    snapshot = {
        "finance": [
            {
                "sourceId": "page-op",
                "lastEditedTime": "2026-09-30T00:00:00.000Z",
                "properties": {
                    "구분": "매입",
                    "금액": 1000,
                    "부가세": 100,
                    "합계": 1100,
                    "거래명": "synthetic | item | 2026-01-15",
                    "자연키": f"invoice|{approval}",
                    "메모": f"홈택스 승인번호: {approval}",
                    "실제 처리일": "2026-01-15",
                },
            }
        ]
    }
    (tmp_path / "snap.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (tmp_path / "BuyHomeTax.csv").write_text(
        HEADER + _csv_row(approval, "1100", "1000", "100") + _csv_row(held, "11", "10", "1"),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location(
        "notion_finance_s2_operational",
        Path(__file__).resolve().parents[1] / "scripts" / "notion_finance_s2.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = module.map_snapshot(snapshot["finance"], {})
    assert not hasattr(type(report), "__getattr__") or "hometax_separate" in report.__dataclass_fields__
    report.hometax_separate = []
    clean = module.reconciliation(report, {approval: (1000, 100, 1100)}, None)
    assert "| hometax target vs loaded | 1100 vs 1100 | PASS gap=0 |" in clean
    hometax, separate = module.read_hometax_purchase_sources(tmp_path)
    operated = module.map_snapshot(snapshot["finance"], hometax)
    operated.hometax_separate = separate
    text = module.reconciliation(operated, hometax, None)
    assert separate
    assert "| hometax target vs loaded | 1100 vs 1100 | FAIL gap=0 |" in text
    assert "PASS" not in text.split("hold_explains")[0]
    assert "| hometax matched approvals | 1 | purchase intersection only |" in text
    assert "| hometax matched gross | 1100 | purchase intersection only |" in text


def test_xlsx_short_digit_approval_is_separate_not_found(tmp_path: Path):
    short = "12345678"
    medium = "12345678901234567890123"
    exact = "123456789012345678901234"
    _xlsx(
        tmp_path / "TAX_demo_BuyHomeTax.xlsx",
        [
            (short, "10", "1", "11"),
            (medium, "20", "2", "22"),
            (exact, "100", "10", "110"),
        ],
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert list(found) == [exact]
    assert found[exact] == (100, 10, 110)
    assert short not in found
    assert medium not in found
    assert [item["approval_tail"] for item in separate] == ["5678", "0123"]
    assert [item["gross"] for item in separate] == ["11", "22"]
    purchases = S2.read_hometax_purchases(tmp_path)
    assert list(purchases) == [exact]


def test_supply_plus_tax_must_equal_gross_csv_and_xlsx(tmp_path: Path):
    approval = "202601010000000000000909"
    (tmp_path / "hometax_purchase.csv").write_text(
        HEADER + _csv_row(approval, "1100", "1000", "200"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="supply\\+tax!=gross"):
        S2.read_hometax_purchase_sources(tmp_path)
    (tmp_path / "hometax_purchase.csv").unlink()
    (tmp_path / "hometax_purchase_neg.csv").write_text(
        HEADER + _csv_row(approval, "-1,100", "-1,000", "-100"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert found[approval] == (-1000, -100, -1100)
    (tmp_path / "hometax_purchase_neg.csv").unlink()
    _xlsx(tmp_path / "TAX_bad_BuyHomeTax.xlsx", [(approval, "1000", "200", "1100")])
    with pytest.raises(SystemExit, match="supply\\+tax!=gross"):
        S2.read_hometax_purchase_sources(tmp_path)


def test_cp949_purchase_csv_uses_shared_decoder(tmp_path: Path):
    approval = "202601010000000000000808"
    text = HEADER + _csv_row(approval, "1100", "1000", "100")
    (tmp_path / "매입-cp949.csv").write_bytes(text.encode("cp949"))
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert found[approval] == (1000, 100, 1100)
    mixed = text.encode("utf-8") + "깨진".encode("cp949")
    (tmp_path / "매입-mixed.csv").write_bytes(mixed)
    with pytest.raises(SystemExit, match="encoding rejected"):
        S2.read_hometax_purchase_sources(tmp_path)


def test_sales_and_bank_holds_do_not_fail_purchase_match():
    from price.notion_finance_map import Hold

    approval = "202601010000000000000099"
    report = S2.MapReport()
    report.loaded.append(
        type("Row", (), {"classification": "매입", "approval": approval, "direction": "purchase", "gross": 110})()
    )
    report.holds.extend(
        [
            Hold("page-sale", "매출", "sale date missing", gross=99000),
            Hold("page-bank", "기타출금", "bank fact missing", gross=5000),
        ]
    )
    text = S2.reconciliation(report, {approval: (100, 10, 110)}, None)
    assert "| hometax target vs loaded | 110 vs 0 | PASS gap=110 |" not in text
    assert "| hometax target vs loaded | 110 vs 0 | FAIL" not in text
    assert "PASS" in text.split("hold_explains")[0]
    report.holds.append(Hold("page-buy", "매입", "purchase held", gross=11, approval=approval))
    blocked = S2.reconciliation(report, {approval: (100, 10, 110)}, None)
    assert "FAIL" in blocked.split("hold_explains")[0]
    assert "PASS" not in blocked.split("hold_explains")[0]


def test_ingest_matched_ignores_non_purchase_approvals(tmp_path: Path):
    sale = "202601010000000000000202"
    purchase = "202601151234567890123456"
    snapshot = {
        "finance": [
            {
                "sourceId": "page-sale",
                "lastEditedTime": "2026-09-30T00:00:00.000Z",
                "properties": {
                    "구분": "매출",
                    "금액": 90000,
                    "부가세": 9000,
                    "합계": 99000,
                    "거래명": "synthetic sale | 2026-01-15",
                    "자연키": f"invoice|{sale}",
                    "메모": f"홈택스 승인번호: {sale}",
                    "실제 처리일": "2026-01-15",
                },
            }
        ]
    }
    (tmp_path / "snap.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (tmp_path / "BuyHomeTax.csv").write_text(
        HEADER + _csv_row(sale, "99000", "90000", "9000") + _csv_row(purchase, "1100", "1000", "100"),
        encoding="utf-8",
    )
    db = tmp_path / "scratch-sale.sqlite3"
    spec = importlib.util.spec_from_file_location(
        "notion_finance_ingest_purchase_only",
        Path(__file__).resolve().parents[1] / "scripts" / "notion_finance_ingest.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from price.db import Database
    from price.models import CompanyInput
    from price.service import Service

    opened = Database(db)
    Service(opened).auth.bootstrap(CompanyInput(name="scratch-sale"), "scratch-sale", "scratch-secret-sale")
    buf = io.StringIO()
    argv = sys.argv
    sys.argv = [
        "notion_finance_ingest",
        "--snapshot",
        str(tmp_path / "snap.json"),
        "--db",
        str(db),
        "--hometax-dir",
        str(tmp_path),
    ]
    try:
        with contextlib.redirect_stdout(buf):
            module.main()
    finally:
        sys.argv = argv
    result = json.loads(buf.getvalue())
    assert result["hometax_numeric"] == 2
    assert result["hometax_matched"] == 0


def test_reconciliation_empty_purchase_intersection_reports_zero():
    report = S2.MapReport()
    text = S2.reconciliation(report, {"123456789012345678901234": (100, 10, 110)}, None)
    assert "| hometax matched approvals | 0 | purchase intersection only |" in text
    assert "| hometax matched gross | 0 | purchase intersection only |" in text

def test_header_xlsx_keeps_csv_and_skips_or_holds_by_approval(tmp_path: Path):
    exact = "202601010000000000000024"
    negative = "202601010000000000000025"
    short_bad = "12345678"
    short_hold = "87654321"
    _xlsx_with_header(
        tmp_path / "TAX_header_BuyHomeTax.xlsx",
        [
            (exact, "1000", "100", "1100"),
            (short_bad, "10.5", "1", "11"),
            (short_hold, "20", "2", "22"),
            (negative, "-1,000", "-100", "-1,100"),
        ],
    )
    bad24 = "202601010000000000000026"
    _xlsx(tmp_path / "TAX_bad24_BuyHomeTax.xlsx", [(bad24, "10.5", "1", "11")])
    with pytest.raises(SystemExit, match="exact integer"):
        S2.read_hometax_purchase_sources(tmp_path)
    (tmp_path / "TAX_bad24_BuyHomeTax.xlsx").unlink()
    csv_exact = "202601010000000000000027"
    (tmp_path / "hometax_purchase.csv").write_text(
        HEADER + _csv_row(csv_exact, "3300", "3000", "300"),
        encoding="utf-8",
    )
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert found[exact] == (1000, 100, 1100)
    assert found[negative] == (-1000, -100, -1100)
    assert found[csv_exact] == (3000, 300, 3300)
    assert short_bad not in found
    assert short_hold not in found
    assert [item["approval_tail"] for item in separate] == ["4321"]
    assert separate[0]["gross"] == "22"
    assert sum(item[0] for item in found.values()) + sum(int(item["net"]) for item in separate) == 3020


def test_short_xlsx_approval_with_inconsistent_integer_amounts_fails(tmp_path: Path):
    _xlsx_with_header(tmp_path / "TAX_bad_BuyHomeTax.xlsx", [("12345678", "100", "10", "999")])
    with pytest.raises(SystemExit, match="supply\\+tax!=gross"):
        S2.read_hometax_purchase_sources(tmp_path)
