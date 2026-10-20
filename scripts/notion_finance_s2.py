"""S2 scratch path: dry-run, optional /tmp load, hometax reconciliation.

Does not open the demo database or the company database.
"""
from __future__ import annotations

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
from price.service import Service

FORBIDDEN = {
    Path("/home/jm/orca/projects/price/.data/demo.sqlite3").resolve(),
    Path("/home/jm/.local/share/price/company.sqlite3").resolve(),
}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
APPROVAL_DIGITS = 24
CSV_NAME_MARKERS = ("매입", "buyhometax", "hometax")
CSV_REQUIRED = ("승인번호", "합계금액", "공급가액", "세액")


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
    text = raw.strip().replace(",", "").replace(" ", "")
    if text in {"", "-", "+"}:
        raise SystemExit(f"hometax {label} is empty")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if text.startswith("+"):
        text = text[1:]
    if any(ch in text for ch in ".eE"):
        raise SystemExit(f"hometax {label} is not an exact integer")
    if not text.isdigit():
        raise SystemExit(f"hometax {label} is not an exact integer")
    value = int(text)
    return -value if negative else value


def _csv_is_hometax_purchase(path: Path, sample: bytes) -> bool:
    """True for a named Hometax export or a file whose header is the purchase set.

    Unrelated CSV files are ignored. A named candidate with a missing column
    still fails closed inside the reader.
    """
    folded = path.name.casefold()
    if any(marker in folded for marker in CSV_NAME_MARKERS):
        return True
    if sample.startswith(b"\xef\xbb\xbf"):
        sample = sample[3:]
    try:
        header = sample.decode("utf-8-sig").splitlines()[0]
    except (UnicodeDecodeError, IndexError):
        return False
    fields = next(csv.reader(io.StringIO(header)), [])
    return all(name in fields for name in CSV_REQUIRED)


def _read_hometax_xlsx(path: Path, found: dict[str, tuple[int, int, int]]) -> None:
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
    for cols in rows.values():
        approval = cols.get(2, "")
        if not approval.isdigit():
            continue
        amounts = (int(cols[9]), int(cols[10]), int(cols[11]))
        _store_approval(found, approval, amounts)


def _read_hometax_csv(path: Path, found: dict[str, tuple[int, int, int]], separate: list[dict[str, str]]) -> None:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    required = ("승인번호", "합계금액", "공급가액", "세액")
    missing = [name for name in required if name not in fields]
    if missing:
        raise SystemExit("hometax csv missing columns: " + ",".join(missing))
    for index, row in enumerate(reader, start=2):
        approval_raw = (row.get("승인번호") or "").strip()
        if not approval_raw:
            continue
        compact = approval_raw.replace("-", "").replace(" ", "")
        gross = _exact_krw(row.get("합계금액") or "", "합계금액")
        net = _exact_krw(row.get("공급가액") or "", "공급가액")
        tax = _exact_krw(row.get("세액") or "", "세액")
        if not (compact.isdigit() and len(compact) == APPROVAL_DIGITS):
            separate.append(
                {
                    "file": path.name,
                    "row": str(index),
                    "approval_tail": approval_raw[-4:],
                    "net": str(net),
                    "tax": str(tax),
                    "gross": str(gross),
                    "reason": "approval is not exactly 24 digits after hyphen removal; not coerced",
                }
            )
            continue
        _store_approval(found, compact, (net, tax, gross))


def read_hometax_purchases(directory: Path) -> dict[str, tuple[int, int, int]]:
    found, _separate = read_hometax_purchase_sources(directory)
    return found


def read_hometax_purchase_sources(
    directory: Path,
) -> tuple[dict[str, tuple[int, int, int]], list[dict[str, str]]]:
    """Read xlsx (current dir only) and identified purchase CSV files.

    A CSV is read only when its name marks a Hometax purchase export or its
    header contains 승인번호/합계금액/공급가액/세액. Other CSV files are ignored.
    A selected file missing one of those columns fails closed.
    Only approvals that are exactly 24 digits after hyphen removal are matched.
    Other approval values are returned individually and are not digit-coerced.
    Amounts are (net, tax, gross) = CSV 공급가액/세액/합계금액, including negatives.
    Identical approval+amounts across xlsx and CSV are stored once.
    """
    found: dict[str, tuple[int, int, int]] = {}
    separate: list[dict[str, str]] = []
    for path in sorted(directory.glob("TAX_*BuyHomeTax.xlsx")):
        _read_hometax_xlsx(path, found)
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


def reconciliation(report: MapReport, hometax: dict[str, tuple[int, int, int]], load: dict | None) -> str:
    hometax_gross = sum(item[2] for item in hometax.values())
    held_gross = sum(item.gross or 0 for item in report.holds if item.classification == "매입")
    loaded_gross = report.purchase_loaded_gross()
    verdict = purchase_match_verdict(loaded_gross, hometax_gross, report.holds)
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
    lines.append(f"| hometax matched approvals | {len(hometax)} | 24-digit only |")
    lines.append(f"| hometax matched gross | {hometax_gross} | match target |")
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
