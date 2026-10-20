"""Synthetic Hometax purchase CSV/xlsx fixtures. No real company text."""
from __future__ import annotations

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
    (tmp_path / "nested" / "purchases.csv").write_text(
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
    (tmp_path / "purchases.csv").write_text(body, encoding="utf-8")
    found, separate = S2.read_hometax_purchase_sources(tmp_path)
    assert separate == []
    assert found["202601010000000000000000"] == (0, 0, 0)
    assert found["202601010000000000000001"] == (1000, 100, 1100)


def test_missing_amount_column_fails_closed(tmp_path: Path):
    (tmp_path / "hometax-bad.csv").write_text("승인번호,합계금액\n1,2\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="missing columns"):
        S2.read_hometax_purchase_sources(tmp_path)


def test_float_text_rejected(tmp_path: Path):
    (tmp_path / "bad.csv").write_text(
        HEADER + _csv_row("202601010000000000000009", "1100.0", "1000", "100"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="exact integer"):
        S2.read_hometax_purchase_sources(tmp_path)


def test_empty_amount_fails_closed(tmp_path: Path):
    (tmp_path / "empty.csv").write_text(
        HEADER + _csv_row("202601010000000000000008", "", "1000", "100"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="empty"):
        S2.read_hometax_purchase_sources(tmp_path)


def test_xlsx_and_csv_same_approval_not_double_counted(tmp_path: Path):
    approval = "202601151234567890123456"
    _xlsx(tmp_path / "TAX_demo_BuyHomeTax.xlsx", [(approval, "1000", "100", "1100")])
    (tmp_path / "child").mkdir()
    (tmp_path / "child" / "same.csv").write_text(
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
    (tmp_path / "other.csv").write_text(
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
    (tmp_path / "purchases.csv").write_text(
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
    (tmp_path / "ok.csv").write_text(HEADER + _csv_row(approval, "110", "100", "10"), encoding="utf-8")
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
    assert "| hometax matched gross | 110 |" in text
    assert "| hometax separate gross | 11 |" in text
    assert "| hometax all-row gross | 121 |" in text


def test_core_ingest_does_not_read_hometax_sources():
    import price.notion_finance_ingest as core

    assert not hasattr(core, "read_hometax_purchase_sources")
    assert not hasattr(core, "read_hometax_purchases")
