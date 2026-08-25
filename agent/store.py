"""Case store: intake records in, escalation records out.

Phase B ships the JSON-file implementation the demo seed uses; Phase C adds
a DynamoDB implementation behind the same protocol (conditional-put
idempotency lives there). Parsing intake into engine types happens HERE, on
purpose: serialized strings become real enums and dates at the boundary, so
the frozen engine's fail-closed validation is the second line of defense,
not the first.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from engine.deadline import CaseInput
from engine.rules import ServiceMethod


@dataclass(frozen=True)
class IntakeRecord:
    """One clinic intake row, exactly as staff captured it. Synthetic in
    every demo: ``label`` is rendered wherever the record appears."""

    case_id: str
    jurisdiction_id: str
    service_date: str | None  # ISO date or None when unknown/disputed
    service_method: str  # "personal" | "notorious" | "tack_and_mail" | "unknown"
    posting_date: str | None = None
    mailing_date: str | None = None
    summons_stated_deadline: str | None = None
    amended_affidavit: bool = False
    answer_filed: bool = False
    notes: str = ""
    tenant_display_name: str = ""
    property_address: str = ""
    label: str = "EXAMPLE DATA"


class IntakeParseError(ValueError):
    """A record that cannot be converted safely. Never guessed past."""


def _parse_date(value: str | None, field_name: str, case_id: str) -> date | None:
    if value is None:
        return None
    # TypeError matters as much as ValueError: a JSON number or boolean in a
    # date field raises TypeError from fromisoformat, and IntakeRecord is a
    # plain dataclass with no runtime type validation, so this is the first
    # place a wrongly-typed value can be caught as a refusal instead of a
    # sweep-killing crash.
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise IntakeParseError(
            f"case {case_id}: {field_name} {value!r} is not an ISO date string"
        ) from exc


def validate_intake_types(record: IntakeRecord) -> None:
    """Exact-type validation for the fields the triage layer consumes
    directly (IntakeRecord is a plain dataclass; raw JSON reaches it with
    no runtime checks). A JSON string "false" in answer_filed is truthy and
    would silently HOLD an urgent case, which is the worst possible
    direction to fail; refuse instead."""
    for name in ("case_id", "jurisdiction_id", "service_method"):
        if type(getattr(record, name)) is not str:
            raise IntakeParseError(
                f"case {record.case_id!r}: {name} must be a string, got {getattr(record, name)!r}"
            )
    # The COMPLETE case-id contract, enforced where violations become
    # per-row refusals: a sixty-five-character id that only detonates in a
    # downstream Pydantic model (EscalationRationale caps at 64) would
    # abort the whole sweep instead of refusing one row.
    if not record.case_id or len(record.case_id) > 64:
        raise IntakeParseError(f"case {record.case_id[:80]!r}: case_id must be 1-64 characters")
    if any(not ch.isprintable() for ch in record.case_id):
        raise IntakeParseError(
            f"case {record.case_id!r}: case_id must contain only printable characters"
        )
    # ASCII only: a homoglyph twin ('26ED00101' with a Cyrillic capital
    # Ie in place of the E) passes every visual inspection, dodges
    # exact-codepoint duplicate detection,
    # and can occupy a capacity slot a genuinely distinct urgent case
    # needed. Fulton docket numbers are ASCII; anything else fails closed
    # (the same posture the model-text floor takes with mixed scripts).
    if not record.case_id.isascii():
        raise IntakeParseError(
            f"case {record.case_id!r}: case_id must contain only ASCII "
            "characters (a lookalike character cannot be distinguished "
            "from the docket id it imitates)"
        )
    # NO whitespace anywhere: padding created visually duplicate docket
    # identities ('26ED00101' vs '26ED00101 '), and round 17 showed
    # INTERIOR spaces do the same one channel over ('26ED 00101' vs
    # '26ED  00101' rendered identically, both swept, and their two
    # capacity slots held a genuinely distinct urgent case under a green
    # run). Fulton docket numbers contain no whitespace; fail closed.
    if any(ch.isspace() for ch in record.case_id):
        raise IntakeParseError(f"case {record.case_id!r}: case_id must contain no whitespace")
    for name in ("answer_filed", "amended_affidavit"):
        if type(getattr(record, name)) is not bool:
            raise IntakeParseError(
                f"case {record.case_id}: {name} must be a JSON boolean, "
                f"got {getattr(record, name)!r}"
            )
    if type(record.notes) is not str:
        raise IntakeParseError(
            f"case {record.case_id}: notes must be a string, got {record.notes!r}"
        )


def to_case_input(record: IntakeRecord) -> CaseInput:
    """Boundary conversion; raises IntakeParseError instead of guessing."""
    try:
        method = ServiceMethod(record.service_method)
    except ValueError as exc:
        raise IntakeParseError(
            f"case {record.case_id}: unknown service_method {record.service_method!r}"
        ) from exc
    return CaseInput(
        case_id=record.case_id,
        jurisdiction_id=record.jurisdiction_id,
        service_date=_parse_date(record.service_date, "service_date", record.case_id),
        service_method=method,
        posting_date=_parse_date(record.posting_date, "posting_date", record.case_id),
        mailing_date=_parse_date(record.mailing_date, "mailing_date", record.case_id),
        summons_stated_deadline=_parse_date(
            record.summons_stated_deadline, "summons_stated_deadline", record.case_id
        ),
        amended_affidavit=record.amended_affidavit,
    )


@dataclass(frozen=True)
class EscalationRecord:
    """A committed escalation: the ladder's decision plus the model's
    validated rationale, awaiting or reflecting an attorney's action."""

    case_id: str
    disposition: str
    rank: int
    factors: tuple[str, ...]
    rationale: str
    confidence: float
    run_id: str
    status: str = "pending_attorney"  # -> "approved" | "deferred"
    attorney_note: str = ""


@dataclass(frozen=True)
class MalformedIntakeRow:
    """A raw intake row the loader could not even construct: not an object,
    unhashable or non-string case_id, unknown or missing fields. Indexed so
    the refusal names WHICH row when no usable case id exists."""

    row_key: str
    reason: str


@dataclass(frozen=True)
class IntakeLoadResult:
    """Per-row intake load: every constructible record, plus a refusal for
    every raw row that was not. One malformed row must never abort the
    sweep of the rows that parsed; the malformed rows fail the run as
    case-level refusals instead."""

    records: tuple[IntakeRecord, ...]
    malformed: tuple[MalformedIntakeRow, ...]


@dataclass(frozen=True)
class RunManifest:
    """The immutable identity of one scheduler invocation: what a stable
    run id PROMISES. The first commit attempt reserves it durably; every
    retry must present the identical manifest or fail closed, so two
    different sweeps (mutated intake, changed capacity, a shifted queue)
    can never merge under one run id."""

    run_id: str
    inputs_digest: str
    candidates: tuple[str, ...]
    run_date: str
    capacity: int


class CaseStore(Protocol):
    def load_intake(self) -> IntakeLoadResult: ...

    def record_escalation(self, escalation: EscalationRecord) -> None:
        """MUST be an atomic insert-if-absent keyed on (run_id, case_id):
        recording an already-recorded escalation is an idempotent no-op,
        and concurrent writers can never produce two rows for one case."""
        ...

    def list_escalations(self, run_id: str | None = None) -> list[EscalationRecord]: ...

    def reserve_run_manifest(self, manifest: RunManifest) -> RunManifest:
        """MUST be an atomic insert-if-absent keyed on run_id: the FIRST
        reservation records the manifest durably and returns it; every
        later call returns the ORIGINAL stored manifest unchanged. The
        caller compares the returned manifest with its own and fails
        closed on any difference. (The Phase C DynamoDB store makes this a
        conditional write.)"""
        ...


class JsonFileCaseStore:
    """Seed-file intake + append-only escalation log. Demo/local backend."""

    def __init__(self, intake_path: Path, escalations_path: Path) -> None:
        self._intake_path = intake_path
        self._escalations_path = escalations_path
        self._manifests_path = escalations_path.with_name(escalations_path.name + ".manifests")

    def load_intake(self) -> IntakeLoadResult:
        # File-level corruption (unreadable file, invalid JSON, no
        # "records" key) stays a loud raise: there is no per-row degradation
        # when the file itself cannot be trusted.
        raw = json.loads(self._intake_path.read_text())
        rows = raw["records"]
        if not isinstance(rows, list):
            raise IntakeParseError("intake 'records' must be a JSON array")
        records: list[IntakeRecord] = []
        malformed: list[MalformedIntakeRow] = []
        # Real case ids carried by MALFORMED rows participate in duplicate
        # detection too: a staff correction that fails the schema must not
        # leave its stale sibling sweeping as the sole identity (previously
        # the stale row committed durably while the correction was refused,
        # under the loader's own every-row-refused promise).
        malformed_real_ids: list[str] = []
        for index, row in enumerate(rows, start=1):
            row_key = f"intake-row-{index}"
            has_real_id = (
                isinstance(row, dict) and type(row.get("case_id")) is str and bool(row["case_id"])
            )
            if has_real_id:
                row_key = row["case_id"]
            if not isinstance(row, dict):
                malformed.append(
                    MalformedIntakeRow(row_key, f"intake row {index} is not a JSON object")
                )
                continue
            try:
                record = IntakeRecord(**row)
            except TypeError as exc:
                # Unknown fields, missing required fields: the row does not
                # match the intake schema at all.
                malformed.append(
                    MalformedIntakeRow(
                        row_key,
                        f"intake row {index} does not match the intake schema: {str(exc)[:200]}",
                    )
                )
                if has_real_id:
                    malformed_real_ids.append(row_key)
                continue
            if type(record.case_id) is not str or not record.case_id:
                # A non-string case_id (a list, a number) constructs fine on
                # a plain dataclass but detonates as a dict key before any
                # per-case validation could refuse it; own it here.
                malformed.append(
                    MalformedIntakeRow(
                        row_key,
                        f"intake row {index} case_id {record.case_id!r} must be a non-empty string",
                    )
                )
                continue
            records.append(record)

        # Duplicate ids: identity is ambiguous for EVERY row carrying the
        # id, constructible or not, so all of them are refused and none is
        # swept; the rest of the intake still processes. Counting keys on a
        # NORMALIZED shadow (stripped, casefolded), so a padded or
        # case-variant sibling ('26ED00101 ', '26ed00101') contests the
        # identity instead of leaving its stale twin sweeping alone.
        def _identity_key(case_id: str) -> str:
            # Collapse ALL whitespace (interior-space variants are the same
            # visual identity) and fold the classic ASCII confusable pairs
            # (capital O for zero, lowercase l / uppercase I for one): a
            # hand-keyed '26EDO0101' beside '26ED00101' is a transposition
            # of ONE docket, and both rows must be contested, never swept
            # as two urgent cases holding two capacity slots. Dedup contest
            # only; stored ids are never rewritten.
            # o->0, l/i->1, s->5, b->8, z->2: the hand-keying and OCR
            # confusable family (round 19 reproduced the S-for-5 twin
            # taking a capacity slot exactly as the O-for-0 twin had).
            confusables = str.maketrans("olisbz", "011582")
            return "".join(case_id.split()).casefold().translate(confusables)

        counts: dict[str, int] = {}
        first_seen: dict[str, str] = {}
        for case_id in [r.case_id for r in records] + malformed_real_ids:
            key = _identity_key(case_id)
            counts[key] = counts.get(key, 0) + 1
            first_seen.setdefault(key, case_id)
        duplicated = {key for key, count in counts.items() if count > 1}
        if duplicated:
            for key in sorted(duplicated):
                original = first_seen[key]
                malformed.append(
                    MalformedIntakeRow(
                        original,
                        f"case_id {original!r} appears more than once in the intake "
                        "(padded, case-variant, or malformed siblings included); "
                        "identity is ambiguous and every row carrying it is refused",
                    )
                )
            records = [r for r in records if _identity_key(r.case_id) not in duplicated]
        if not records and not malformed:
            raise IntakeParseError("intake file contains zero records")
        return IntakeLoadResult(records=tuple(records), malformed=tuple(malformed))

    def record_escalation(self, escalation: EscalationRecord) -> None:
        """Atomic insert-if-absent on (run_id, case_id): the check and the
        append happen under one interprocess lock, so concurrent retries for
        the same run can never produce two rows for one case. A record that
        already exists is an idempotent no-op. Flush + fsync inside the
        lock: the row either exists durably or this raises into
        commit_escalations' loud partial-failure handler. (The Phase C
        DynamoDB store makes this a conditional write; this is the
        local-mode equivalent.)"""
        payload = asdict(escalation)
        payload["recorded_at"] = datetime.now().astimezone().isoformat()
        with self._escalations_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                for line in handle:
                    if not line.strip():
                        continue
                    existing = json.loads(line)
                    if (
                        existing.get("run_id") == escalation.run_id
                        and existing.get("case_id") == escalation.case_id
                    ):
                        # Idempotent ONLY for the identical payload: a
                        # duplicate key carrying different content means two
                        # writers disagree about what the attorney approved,
                        # and silently keeping either one would falsify the
                        # record. Conflict is loud.
                        stored = {k: v for k, v in existing.items() if k != "recorded_at"}
                        offered = {k: v for k, v in payload.items() if k != "recorded_at"}
                        offered = json.loads(json.dumps(offered))  # normalize types
                        if stored == offered:
                            return  # already durably recorded: idempotent no-op
                        raise ValueError(
                            f"idempotency conflict for run {escalation.run_id} "
                            f"case {escalation.case_id}: a row with the same key "
                            "but different content is already durably recorded; "
                            "staff must reconcile the escalation store"
                        )
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(payload) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _manifest_path(self, run_id: str) -> Path:
        # One crash-atomic file PER RUN (the filename is the run id's
        # digest, so an arbitrary run id can never traverse the path): a
        # torn write for one run must never wedge every other sweep the
        # way a shared appended sidecar would.
        return self._manifests_path / (hashlib.sha256(run_id.encode()).hexdigest() + ".json")

    def reserve_run_manifest(self, manifest: RunManifest) -> RunManifest:
        """Crash-atomic insert-if-absent: the manifest is written complete
        to a temporary file (fsynced), then installed with an atomic
        hard-link that fails if a manifest already exists, and the
        directory is fsynced. A reader can therefore never observe a
        partial manifest; a corrupt file is a loud, named error scoped to
        its one run."""
        if self._manifests_path.exists() and not self._manifests_path.is_dir():
            # A file at this name is the retired shared-sidecar layout,
            # replaced because one torn append wedged every run. Refuse
            # loudly with the fix rather than crashing on mkdir.
            raise ValueError(
                f"{self._manifests_path} is a file from the retired "
                "shared-sidecar manifest layout; remove it (after "
                "reconciling any in-flight runs) so per-run manifests can "
                "be stored in a directory of that name."
            )
        self._manifests_path.mkdir(parents=True, exist_ok=True)
        final = self._manifest_path(manifest.run_id)
        if not final.exists():
            temp = self._manifests_path / f".tmp-{os.getpid()}-{id(manifest):x}"
            with temp.open("w") as handle:
                json.dump(asdict(manifest), handle)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, final)  # atomic create-if-absent
            except FileExistsError:
                pass  # another writer reserved first; its manifest governs
            finally:
                temp.unlink(missing_ok=True)
            directory_fd = os.open(self._manifests_path, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        try:
            data = json.loads(final.read_text())
            return RunManifest(
                run_id=data["run_id"],
                inputs_digest=data["inputs_digest"],
                candidates=tuple(data["candidates"]),
                run_date=data["run_date"],
                capacity=data["capacity"],
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(
                f"run manifest {final} for run {manifest.run_id!r} is corrupt "
                "or has an invalid schema; staff must reconcile the "
                "escalation store and remove the manifest file before any "
                "retry of this run id. Other run ids are unaffected."
            ) from exc

    def list_escalations(self, run_id: str | None = None) -> list[EscalationRecord]:
        if not self._escalations_path.exists():
            return []
        out: list[EscalationRecord] = []
        for lineno, line in enumerate(self._escalations_path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"escalation store {self._escalations_path} is corrupt at "
                    f"line {lineno}; refusing to read a damaged store. Earlier "
                    "lines are intact JSONL and recoverable by hand."
                ) from exc
            data.pop("recorded_at", None)
            record = EscalationRecord(**{**data, "factors": tuple(data.get("factors", ()))})
            if run_id is None or record.run_id == run_id:
                out.append(record)
        return out
