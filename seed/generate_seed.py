"""Deterministic synthetic-intake generator.

Every record is fabricated: placeholder tenant labels, fabricated case
numbers, synthetic addresses, and an explicit EXAMPLE DATA label rendered
wherever a record appears. The statutory rules the demo runs against are
real; the people are not, by design (the demo-evidence rule and the
hackathon's own data guidance).

The layout is engineered around a demo run date so one unattended sweep
exercises: overdue/today/tomorrow interrupts, the capacity gate, weekend
rolls, tack-and-mail (including split and mismatched component dates),
summons conflicts, missing service dates, an amended affidavit, filed
answers, and free-text notes with extractable signals.

Regenerate with:  uv run python seed/generate_seed.py
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

DEMO_RUN_DATE = date(2026, 9, 9)  # Wednesday

NOTES = [
    "Tenant says papers were taped to the front door last week; no mail copy seen yet.",
    "Tenant already mailed an answer, has the certified mail receipt in hand.",
    "Neighbor accepted the papers; tenant unsure which day they actually arrived.",
    "Tenant says the hearing date on the letter looks different from the court text message.",
    "Landlord texted that the case is dropped, but nothing in writing.",
    "Tenant found the summons in the mailbox, envelope postmarked three days after the date on it.",
    "Papers may belong to the unit next door; names do not match exactly.",
    "Tenant was in the hospital when papers were posted; daughter took photos of the door.",
    "Second set of papers arrived this week that looks almost the same as the first set.",
    "Tenant says the amount on the affidavit includes fees the lease never mentions.",
    "Interpreter needed; tenant primarily speaks Spanish and is unsure what the deadline is.",
    "Tenant plans to pay everything owed this week and wants to know if that ends the case.",
]


def _record(
    index: int,
    service: date | None,
    method: str = "personal",
    **overrides: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "case_id": f"26ED{100 + index:05d}",
        "jurisdiction_id": "GA-FULTON",
        "service_date": service.isoformat() if service else None,
        "service_method": method,
        "posting_date": None,
        "mailing_date": None,
        "summons_stated_deadline": None,
        "amended_affidavit": False,
        "answer_filed": False,
        "notes": "",
        "tenant_display_name": f"Tenant {index:03d} (EXAMPLE)",
        "property_address": f"{100 + index} Example Way SW, Atlanta, GA (SYNTHETIC)",
        "label": "EXAMPLE DATA",
    }
    row.update(overrides)
    return row


def build_records() -> list[dict[str, object]]:
    r = DEMO_RUN_DATE
    records: list[dict[str, object]] = []
    i = 0

    def nxt() -> int:
        nonlocal i
        i += 1
        return i

    # Interrupt band: overdue, due today, due tomorrow.
    records.append(_record(nxt(), r - timedelta(days=8)))  # deadline yesterday
    records.append(_record(nxt(), r - timedelta(days=7)))  # deadline today
    records.append(
        _record(
            nxt(),
            r - timedelta(days=6),
            method="tack_and_mail",
            posting_date=(r - timedelta(days=6)).isoformat(),
            mailing_date=(r - timedelta(days=6)).isoformat(),
            notes=NOTES[0],
        )
    )  # deadline tomorrow + service risk
    # Surface-today band (2-3 days out).
    records.append(_record(nxt(), r - timedelta(days=5), notes=NOTES[2]))
    records.append(
        _record(
            nxt(),
            r - timedelta(days=5),
            summons_stated_deadline=(r + timedelta(days=3)).isoformat(),
            notes=NOTES[3],
        )
    )  # summons conflict raises
    # Tack-and-mail split and mismatch cases.
    records.append(
        _record(
            nxt(),
            r - timedelta(days=4),
            method="tack_and_mail",
            posting_date=(r - timedelta(days=4)).isoformat(),
            mailing_date=(r - timedelta(days=3)).isoformat(),
            notes=NOTES[5],
        )
    )
    records.append(
        _record(
            nxt(),
            r - timedelta(days=3),
            method="tack_and_mail",
            posting_date=(r - timedelta(days=5)).isoformat(),
            mailing_date=(r - timedelta(days=5)).isoformat(),
            notes=NOTES[7],
        )
    )
    # Missing service dates (one summons-only, one nothing).
    records.append(
        _record(
            nxt(),
            None,
            method="unknown",
            summons_stated_deadline=(r + timedelta(days=1)).isoformat(),
            notes=NOTES[6],
        )
    )
    records.append(_record(nxt(), None, method="unknown", notes=NOTES[10]))
    # Amended affidavit.
    records.append(_record(nxt(), r - timedelta(days=2), amended_affidavit=True, notes=NOTES[8]))
    # Answers already filed (explicit holds).
    records.append(_record(nxt(), r - timedelta(days=6), answer_filed=True, notes=NOTES[1]))
    records.append(_record(nxt(), r - timedelta(days=3), answer_filed=True))
    # Tender-curious note (boundary showcase for the analyst).
    records.append(_record(nxt(), r - timedelta(days=2), notes=NOTES[11]))
    # Monitor band: spread over the coming week, weekend rolls included.
    for offset_days, note_index in [
        (1, None),
        (1, 4),
        (2, None),
        (2, 9),
        (0, None),  # served today
        (1, None),
        (2, None),
    ]:
        note = NOTES[note_index] if note_index is not None else ""
        records.append(_record(nxt(), r - timedelta(days=offset_days), notes=note))
    # Bulk realistic monitor tail, mixed methods (roughly 70/30 personal/tack).
    for k in range(28):
        service = r - timedelta(days=(k % 3))
        if k % 10 in (3, 7, 9):
            records.append(
                _record(
                    nxt(),
                    service,
                    method="tack_and_mail",
                    posting_date=service.isoformat(),
                    mailing_date=service.isoformat(),
                )
            )
        else:
            records.append(_record(nxt(), service))
    return records


def main() -> None:
    records = build_records()
    payload = {
        "label": "EXAMPLE DATA: every record in this file is synthetic",
        "demo_run_date": DEMO_RUN_DATE.isoformat(),
        "records": records,
    }
    out = Path(__file__).parent / "synthetic_intake.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {len(records)} synthetic records to {out}")


if __name__ == "__main__":
    main()
