from __future__ import annotations
import re
"""S2 scratch path: dry-run, optional /tmp load, hometax reconciliation.

Does not open the demo database or the company database.
"""

import hashlib
import csv
import io
import argparse
import json
import secrets
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from price.auth import Actor
from price.db import Database, digest
from price.models import CompanyInput
from price.notion_finance_map import MapReport, Mapped, map_snapshot, purchase_match_verdict
from price.ingest_safety import text_decode
from price.service import Service

FORBIDDEN = {
    Path("/home/jm/orca/projects/price/.data/demo.sqlite3").resolve(),
    Path("/home/jm/.local/share/price/company.sqlite3").resolve(),
}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
APPROVAL_DIGITS = 24
CSV_PURCHASE_NAME_MARKERS = ("매입", "buyhometax", "hometax_purchase")
CSV_SALES_NAME_MARKERS = ("매출", "sellhometax")
CSV_REQUIRED = ("승인번호", "합계금액", "공급가액", "세액")
_APPROVAL_COMPACT = re.compile(r"^[0-9]{24}$")
_KRW_EXACT = re.compile(r"^[+-]?(?:0|[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*)$")


def assert_scratch(path: Path) -> Path:
    resolved = path.resolve()
    if resolved in FORBIDDEN or resolved.name in {"demo.sqlite3", "company.sqlite3"}:
        raise SystemExit("refusing demo or company database")
    if not str(resolved).startswith("/tmp/"):
        raise SystemExit("scratch database must live under /tmp")
    return resolved


def _cell_ref(ref: str) -> tuple[int, int]:
    col = "".join(ch for ch in ref if ch.isalpha())
    row = int("".join(ch for ch in ref if ch.isdigit()))
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    return n, row


def _store_approval(
    found: dict[str, tuple[int, int, int]],
    approval: str,
    amounts: tuple[int, int, int],
) -> None:
    if approval in found and found[approval] != amounts:
        raise SystemExit("duplicate hometax approval with different amounts")
    found[approval] = amounts


def _exact_krw(raw: str, label: str) -> int:
    text = raw.strip()
    if text in {"", "-", "+"}:
        raise SystemExit(f"hometax {label} is empty")
    if not _KRW_EXACT.fullmatch(text):
        raise SystemExit(f"hometax {label} is not an exact integer")
    negative = text.startswith("-")
    digits = text[1:] if text[0] in "+-" else text
    value = int(digits.replace(",", ""))
    return -value if negative else value


def _decode_hometax_text(raw: bytes) -> str:
    """Reuse the shared decoder so sniff and body use one encoding."""
    try:
        text, _encoding = text_decode(raw)
    except ValueError as exc:
        raise SystemExit(f"hometax csv encoding rejected: {exc}") from exc
    return text


def _csv_header_fields(sample: bytes) -> list[str] | None:
    try:
        header = _decode_hometax_text(sample).splitlines()[0]
    except (SystemExit, IndexError):
        return None
    return next(csv.reader(io.StringIO(header)), [])


def _csv_is_hometax_purchase(path: Path, sample: bytes) -> bool:
    """True only for an explicitly marked purchase export.

    Shared amount columns, or a generic hometax name, are not a purchase
    marker. SellHomeTax/매출 is never a purchase source. A selected file
    whose header lacks 승인번호/합계금액/공급가액/세액 fails closed.
    """
    folded = path.name.casefold()
    if any(marker in folded for marker in CSV_SALES_NAME_MARKERS):
        return False
    if not any(marker in folded for marker in CSV_PURCHASE_NAME_MARKERS):
        return False
    return True


def _separate_non_digit_approval(
    path: Path,
    row: str,
    approval: str,
    net: int,
    tax: int,
    gross: int,
    separate: list[dict[str, str]],
) -> None:
    _append_separate(
        separate,
        {
            "file": path.name,
            "row": row,
            "approval_id": _approval_identity(approval),
            "approval_tail": approval[-4:],
            "net": str(net),
            "tax": str(tax),
            "gross": str(gross),
            "reason": "approval is not exactly 24 digits after hyphen removal; not coerced",
        },
    )


def _read_hometax_xlsx(
    path: Path,
    found: dict[str, tuple[int, int, int]],
    separate: list[dict[str, str]],
) -> None:
    with zipfile.ZipFile(path) as book:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            root = ET.fromstring(book.read("xl/sharedStrings.xml"))
            for node in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in node.findall(".//m:t", NS)))
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        rows: dict[int, dict[int, str]] = {}
        for cell in sheet.findall(".//m:c", NS):
            col, row = _cell_ref(cell.get("r"))
            value_node = cell.find("m:v", NS)
            value = value_node.text if value_node is not None else ""
            if cell.get("t") == "s" and value != "":
                value = shared[int(value)]
            rows.setdefault(row, {})[col] = value
    for row_no, cols in rows.items():
        approval = cols.get(2, "").strip()
        if not approval:
            continue
        net = int(cols[9])
        tax = int(cols[10])
        gross = int(cols[11])
        if net + tax != gross:
            raise SystemExit(f"hometax xlsx row {row_no} supply+tax!=gross")
        compact = approval.replace("-", "").replace(" ", "")
        if not _APPROVAL_COMPACT.fullmatch(compact):
            _separate_non_digit_approval(path, str(row_no), approval, net, tax, gross, separate)
            continue
        _store_approval(found, compact, (net, tax, gross))


def _approval_identity(raw: str) -> str:
    compact = raw.strip().replace("-", "").replace(" ", "").casefold()
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def _separate_key(item: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        item.get("approval_id", ""),
        item.get("net", ""),
        item.get("tax", ""),
        item.get("gross", ""),
        item.get("reason", ""),
    )


def _append_separate(separate: list[dict[str, str]], item: dict[str, str]) -> None:
    key = _separate_key(item)
    if any(_separate_key(existing) == key for existing in separate):
        return
    separate.append(item)


def _read_hometax_csv(path: Path, found: dict[str, tuple[int, int, int]], separate: list[dict[str, str]]) -> None:
    raw = path.read_bytes()
    text = _decode_hometax_text(raw)
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    required = ("승인번호", "합계금액", "공급가액", "세액")
    missing = [name for name in required if name not in fields]
    if missing:
        raise SystemExit("hometax csv missing columns: " + ",".join(missing))
    for index, row in enumerate(reader, start=2):
        approval_raw = (row.get("승인번호") or "").strip()
        gross_raw = row.get("합계금액") or ""
        net_raw = row.get("공급가액") or ""
        tax_raw = row.get("세액") or ""
        amount_blank = not any(part.strip() for part in (gross_raw, net_raw, tax_raw))
        zero_placeholder = all(
            (not part.strip()) or part.strip() in {"0", "+0", "-0"}
            for part in (gross_raw, net_raw, tax_raw)
        )
        if not approval_raw and (amount_blank or zero_placeholder):
            continue
        gross = _exact_krw(gross_raw, "합계금액")
        net = _exact_krw(net_raw, "공급가액")
        tax = _exact_krw(tax_raw, "세액")
        if net + tax != gross:
            raise SystemExit(f"hometax csv row {index} supply+tax!=gross")
        if not approval_raw:
            raise SystemExit(f"hometax csv row {index} has amounts but a blank approval")
        compact = approval_raw.replace("-", "").replace(" ", "")
        if not _APPROVAL_COMPACT.fullmatch(compact):
            _separate_non_digit_approval(path, str(index), approval_raw, net, tax, gross, separate)
            continue
        _store_approval(found, compact, (net, tax, gross))


def read_hometax_purchases(directory: Path) -> dict[str, tuple[int, int, int]]:
    found, _separate = read_hometax_purchase_sources(directory)
    return found


def read_hometax_purchase_sources(
    directory: Path,
) -> tuple[dict[str, tuple[int, int, int]], list[dict[str, str]]]:
    """Read xlsx (current dir only) and explicitly marked purchase CSV files.

    A CSV is read only when its name marks a purchase export
    (매입/BuyHomeTax/hometax_purchase). Generic hometax names and shared
    amount columns are not enough. SellHomeTax/매출 is excluded.
    A selected file missing one of 승인번호/합계금액/공급가액/세액 fails closed.
    Only approvals that are exactly 24 digits after hyphen removal are matched.
    8-23 digit and other non-24 values are returned individually and are not
    digit-coerced, for both CSV and XLSX. The same held approval+amounts
    from overlapping files is listed once.
    A blank approval on a row that still has amounts fails closed. A fully
    blank approval-and-amount row is skipped.
    Amounts must be exact integer won, optional sign, optional thousands commas.
    A row whose 공급가액+세액 does not equal 합계금액 fails closed. Negatives stay.
    Amounts are (net, tax, gross) = CSV 공급가액/세액/합계금액.
    UTF-8/CP949 decoding reuses price.ingest_safety.text_decode for the whole file.
    Identical approval+amounts across xlsx and CSV are stored once.
    """
    found: dict[str, tuple[int, int, int]] = {}
    separate: list[dict[str, str]] = []
    for path in sorted(directory.glob("TAX_*BuyHomeTax.xlsx")):
        _read_hometax_xlsx(path, found, separate)
    for path in sorted(directory.rglob("*.csv")):
        sample = path.read_bytes()[:4096]
        if not _csv_is_hometax_purchase(path, sample):
            continue
        _read_hometax_csv(path, found, separate)
    return found, separate


def tail(value: str) -> str:
    return value[-4:] if value else ""


def dry_run_text(report: MapReport, hometax: dict[str, tuple[int, int, int]]) -> str:
    lines = [
        f"rows {report.rows}",
        f"class {json.dumps(report.by_class, ensure_ascii=False)}",
        f"purchase_notion_gross {report.purchase_notion_gross()}",
        f"purchase_count {report.by_class.get('매입', 0)}",
        f"sale_count {report.by_class.get('매출', 0)}",
        f"other {report.by_class.get('기타입금', 0) + report.by_class.get('기타출금', 0)}",
        f"holds {len(report.holds)}",
        f"loadable {len(report.loaded)}",
        f"loadable_purchase_gross {report.purchase_loaded_gross()}",
        f"hometax_purchase_count {len(hometax)}",
        f"hometax_purchase_gross {sum(item[2] for item in hometax.values())}",
        f"hometax_matched_gross {sum(item[2] for item in hometax.values())}",
        f"hometax_separate_count {len(getattr(report, 'hometax_separate', []))}",
        f"hometax_separate_gross {sum(int(item.get('gross') or 0) for item in getattr(report, 'hometax_separate', []))}",
        f"hometax_separate_non_digit {len(getattr(report, 'hometax_separate', []))}",
    ]
    for hold in report.holds:
        lines.append(
            "HOLD "
            + " ".join(
                [
                    f"page…{tail(hold.page_id)}",
                    hold.classification,
                    f"gross={hold.gross}",
                    f"approval…{hold.approval_tail}",
                    hold.reason,
                ]
            )
        )
    return "\n".join(lines)


def load_twice(path: Path, report: MapReport) -> dict:
    path = assert_scratch(path)
    if path.exists():
        path.unlink()
    db = Database(path)
    svc = Service(db)
    company, actor = svc.auth.bootstrap(
        CompanyInput(name="scratch-s2"),
        "scratch-s2",
        secrets.token_urlsafe(18),
    )
    assert str(db.path.resolve()) == str(path)
    first = _insert_all(svc, db, actor, company, report.loaded)
    second = _insert_all(svc, db, actor, company, report.loaded)
    with db.connect() as c:
        gross = c.execute(
            "SELECT coalesce(sum(json_extract(body,'$.gross')),0) FROM objects WHERE company_id=? AND kind='transaction' AND json_extract(body,'$.direction')='purchase'",
            (company,),
        ).fetchone()[0]
        count = c.execute(
            "SELECT count(*) FROM objects WHERE company_id=? AND kind='transaction'",
            (company,),
        ).fetchone()[0]
    return {
        "db": str(path),
        "first_inserted": first["inserted"],
        "first_deduplicated": first["deduplicated"],
        "second_inserted": second["inserted"],
        "second_deduplicated": second["deduplicated"],
        "purchase_gross": int(gross),
        "transactions": int(count),
    }


def _insert_all(svc: Service, db: Database, actor: Actor, company: str, rows: list[Mapped]) -> dict:
    inserted = deduplicated = 0
    for item in rows:
        rec = svc.create_transaction(actor, company, item.draft)
        if rec["deduplicated"]:
            deduplicated += 1
        else:
            inserted += 1
        _link_approval(db, company, item, rec["id"])
    return {"inserted": inserted, "deduplicated": deduplicated}


def _link_approval(db: Database, company: str, item: Mapped, object_id: str) -> None:
    approval = item.keys.get("hometax-approval")
    if not approval:
        return
    body_hash = digest(item.draft.model_dump(mode="json"))
    namespace = "hometax-approval"
    with db.transaction() as c:
        old = c.execute(
            "SELECT object_id, body_hash FROM external_ids WHERE company_id=? AND namespace=? AND external_id=?",
            (company, namespace, approval),
        ).fetchone()
        if old:
            if old["object_id"] != object_id or old["body_hash"] != body_hash:
                raise SystemExit("hometax approval key conflict; not overwritten")
            return
        c.execute(
            "INSERT INTO external_ids VALUES(?,?,?,?,?)",
            (company, namespace, approval, body_hash, object_id),
        )


def _purchase_hold(item) -> bool:
    return getattr(item, "classification", "") == "매입"


def reconciliation(report: MapReport, hometax: dict[str, tuple[int, int, int]], load: dict | None) -> str:
    hometax_gross = sum(item[2] for item in hometax.values())
    held_gross = sum(item.gross or 0 for item in report.holds if _purchase_hold(item))
    loaded_gross = report.purchase_loaded_gross()
    verdict = purchase_match_verdict(loaded_gross, hometax_gross, report.holds)
    separate = report.hometax_separate
    purchase_holds = [item for item in report.holds if _purchase_hold(item)]
    if purchase_holds or separate:
        verdict = {
            "status": "FAIL",
            "gap": verdict["gap"],
            "hold_explains": verdict["hold_explains"],
        }
    lines = [
        "| item | value | status |",
        "|---|---:|---|",
        f"| notion purchase rows | {report.by_class.get('매입', 0)} | counted |",
        f"| notion purchase gross | {report.purchase_notion_gross()} | source sum, includes hold |",
        f"| hold purchase gross | {held_gross} | excluded from load; not fixed |",
        f"| loadable purchase gross | {loaded_gross} | notion gross minus hold |",
        f"| hometax purchase rows | {len(hometax)} | numeric approvals; letters listed separately |",
        f"| hometax purchase gross | {hometax_gross} | target |",
        "| hometax target vs loaded | "
        + f"{hometax_gross} vs {loaded_gross} | {verdict['status']} gap={verdict['gap']} |",
        f"| hold_explains |  | {verdict['hold_explains']} |",
        "| sales external match | "
        + f"{report.by_class.get('매출', 0)} rows | NOT_RUN |",
        "| core5 8-row OUT 96536000 | not used | not the purchase total |",
        "| bank 60/67 | cited from RECON-20260930 | NOT_RUN |",
    ]
    if load:
        lines.append(f"| scratch second inserts | {load['second_inserted']} | {'ok' if load['second_inserted']==0 else 'FAIL'} |")
        lines.append(f"| scratch purchase gross | {load['purchase_gross']} | scratch only |")
        lines.append(f"| scratch path | {load['db']} | not demo, not company |")
    lines.append("")
    lines.append("Holds:")
    for hold in report.holds:
        lines.append(f"- page…{tail(hold.page_id)} {hold.classification} gross={hold.gross} approval…{hold.approval_tail} {hold.reason}")
    lines.append("")
    lines.append("Draft only. Not a filed return. Sales per-invoice files were not compared.")
    separate = getattr(report, "hometax_separate", [])
    separate_gross = sum(int(item.get("gross") or 0) for item in separate)
    matched = {
        item.approval
        for item in (*report.loaded, *report.holds)
        if getattr(item, "classification", "") == "매입" and getattr(item, "approval", None)
    } & set(hometax)
    matched_gross = sum(hometax[key][2] for key in matched)
    lines.append(f"| hometax matched approvals | {len(matched)} | purchase intersection only |")
    lines.append(f"| hometax matched gross | {matched_gross} | purchase intersection only |")
    lines.append(f"| hometax separate approvals | {len(separate)} | not coerced |")
    lines.append(f"| hometax separate gross | {separate_gross} | excluded from match target |")
    lines.append(f"| hometax all-row gross | {hometax_gross + separate_gross} | matched plus separate |")
    for item in separate:
        lines.append(
            "- HOLD approval…"
            + f"{item.get('approval_tail', '')} file={item.get('file', '')} "
            + f"row={item.get('row', '')} net={item.get('net', '')} "
            + f"tax={item.get('tax', '')} gross={item.get('gross', '')} {item.get('reason', '')}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--hometax-dir", type=Path, required=True)
    parser.add_argument("--load", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.snapshot.read_text())
    hometax, separate = read_hometax_purchase_sources(args.hometax_dir)
    report = map_snapshot(payload["finance"], hometax)
    report.hometax_separate = separate
    text = dry_run_text(report, hometax)
    print(text)
    load = load_twice(args.load, report) if args.load else None
    if load:
        print(json.dumps({k: v for k, v in load.items()}, ensure_ascii=False))
    table = reconciliation(report, hometax, load)
    print(table)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n" + (json.dumps(load, ensure_ascii=False) if load else "") + "\n" + table + "\n")


if __name__ == "__main__":
    main()
