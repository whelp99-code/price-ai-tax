"""S2 scratch path: dry-run, optional /tmp load, hometax reconciliation.

Does not open the demo database or the company database.
"""
from __future__ import annotations

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


def read_hometax_purchases(directory: Path) -> dict[str, tuple[int, int, int]]:
    found: dict[str, tuple[int, int, int]] = {}
    for path in sorted(directory.glob("TAX_*BuyHomeTax.xlsx")):
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
        for row, cols in rows.items():
            approval = cols.get(2, "")
            if not approval.isdigit():
                continue
            amounts = (int(cols[9]), int(cols[10]), int(cols[11]))
            if approval in found and found[approval] != amounts:
                raise SystemExit("duplicate hometax approval with different amounts")
            found[approval] = amounts
    return found


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
        f"| hometax purchase rows | {len(hometax)} | xlsx |",
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
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--hometax-dir", type=Path, required=True)
    parser.add_argument("--load", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.snapshot.read_text())
    hometax = read_hometax_purchases(args.hometax_dir)
    report = map_snapshot(payload["finance"], hometax)
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
