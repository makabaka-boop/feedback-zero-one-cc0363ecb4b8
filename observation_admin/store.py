"""Versioned SQLite store: revisions, approvals, publication and history.

Durability and concurrency
--------------------------
All committed state lives in one SQLite database.  Every successful change is a
single ``BEGIN IMMEDIATE`` transaction that (a) reads and validates the
committed state, (b) writes the new records, their revision identities and any
approval, and (c) appends an immutable snapshot holding the *complete query
result* of the resulting version.  A commit is therefore atomic across data,
eligibility, approvals and the release set, and a failure or crash leaves the
previous version exactly as it was.

Reads run in a ``BEGIN DEFERRED`` transaction, so a read snapshot is fixed at
its first statement.  SQLite admits one writer at a time and a writer's commit
waits for active readers, so every query observes one whole version.  Together
with the in-process re-entrant lock this makes concurrent use serial-equivalent.
Cross-process ordering and atomicity come from SQLite plus ``busy_timeout``.

Each version's query result is stored once and never rewritten, so the release
published by a historical version can be reproduced exactly, and restart
preserves every committed version.
"""

import json
import sqlite3
import threading
import uuid

from . import model, rules
from .errors import AdminError

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

-- Exactly one row per live identifier: its current revision.
CREATE TABLE IF NOT EXISTS records (
    kind       TEXT    NOT NULL CHECK (kind IN ('calibration','qc','observation')),
    id         TEXT    NOT NULL,
    revision   INTEGER NOT NULL,
    instrument TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    PRIMARY KEY (kind, id)
) WITHOUT ROWID;

-- Permanent ledger of every revision identity ever created.
CREATE TABLE IF NOT EXISTS revisions (
    revision INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL,
    id       TEXT NOT NULL,
    payload  TEXT NOT NULL
);

-- Every approval ever granted.  Rows are never deleted.  ``epoch`` is the
-- observation basis epoch at the moment of approval; an approval is current
-- only while the observation's basis epoch and signature are unchanged, which
-- is monotonic and therefore cannot be revived by restoring old values.
CREATE TABLE IF NOT EXISTS approvals (
    approval_id          TEXT PRIMARY KEY,
    observation_id       TEXT    NOT NULL,
    credential           TEXT    NOT NULL,
    observation_revision INTEGER NOT NULL,
    approved_version     INTEGER NOT NULL,
    epoch                INTEGER NOT NULL,
    signature            TEXT    NOT NULL
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS approvals_by_observation
    ON approvals (observation_id);

-- Monotonic basis state per observation.  ``signature`` captures everything an
-- observation's evidence depends on (its own revision, both neighbours'
-- revisions, the calibrations those use, and the blocking reasons).  When a
-- commit changes the signature, ``epoch`` is incremented and never decreases,
-- so a credential burned by a basis change cannot come back even if the basis
-- returns to an earlier-looking shape.
CREATE TABLE IF NOT EXISTS observation_basis (
    observation_id TEXT    PRIMARY KEY,
    epoch          INTEGER NOT NULL,
    signature      TEXT    NOT NULL
) WITHOUT ROWID;

-- Immutable per-version snapshot: the complete query result of that version.
CREATE TABLE IF NOT EXISTS versions (
    version   INTEGER PRIMARY KEY,
    operation TEXT NOT NULL,
    summary   TEXT NOT NULL,
    snapshot  TEXT NOT NULL
);
"""


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def loads(text):
    return json.loads(text)


class _Transaction:
    """Acquire the process lock and open exactly one SQLite transaction."""

    def __init__(self, store, begin):
        self.store = store
        self.begin = begin

    def __enter__(self):
        store = self.store
        store._lock.acquire()
        try:
            store._conn.execute(self.begin)
        except sqlite3.OperationalError as exc:
            store._lock.release()
            raise AdminError("busy", str(exc)) from None
        return store

    def __exit__(self, exc_type, exc, tb):
        try:
            try:
                if exc_type is None:
                    self.store._conn.execute("COMMIT")
                else:
                    self.store._conn.execute("ROLLBACK")
            except sqlite3.OperationalError as commit_error:
                # A COMMIT can fail (for example "database is locked" after the
                # busy timeout).  Roll back so no partial transaction survives,
                # and report it as a rejection rather than leaking SQLite text.
                try:
                    self.store._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                if exc_type is None:
                    raise AdminError("busy", str(commit_error)) from None
                raise
        finally:
            self.store._lock.release()
        return False


def _empty_state(version=0):
    return {
        "version": version,
        "calibrations": {},
        "qcs": {},
        "observations": {},
        "approvals": [],
        "basis": {},
        "next_revision": 1,
    }


class Store:
    """Observation-administration database.

    ``path`` is ``":memory:"`` or a filesystem path.  A file database keeps
    every committed version across restarts.  One instance is safe to share
    between threads; separate processes coordinate through SQLite.
    """

    def __init__(self, path=":memory:"):
        self._lock = threading.RLock()
        self._closed = False
        self._path = path
        try:
            self._conn = sqlite3.connect(
                path, isolation_level=None, timeout=30.0, check_same_thread=False
            )
        except sqlite3.OperationalError as exc:
            raise AdminError("unavailable", str(exc)) from None
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA busy_timeout=30000")
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._lock.acquire()
            try:
                self._conn.executescript(_SCHEMA)
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    raise AdminError("busy", str(exc)) from None
                try:
                    self._initialise()
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                self._conn.execute("COMMIT")
            finally:
                self._lock.release()
        except sqlite3.OperationalError as exc:
            self._conn.close()
            raise AdminError("unavailable", str(exc)) from None

    # ------------------------------------------------------------- lifecycle

    def _initialise(self):
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
        else:
            if row["value"] != SCHEMA_VERSION:
                raise AdminError(
                    "incompatible_database",
                    "schema version %s, expected %s" % (row["value"], SCHEMA_VERSION),
                )
        if self._conn.execute("SELECT 1 FROM meta WHERE key='version'").fetchone() is None:
            self._conn.execute("INSERT INTO meta(key, value) VALUES ('version', '0')")
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('next_revision', '1')"
            )
            empty = _empty_state(0)
            self._conn.execute(
                "INSERT INTO versions(version, operation, summary, snapshot) "
                "VALUES (?,?,?,?)",
                (0, "init", dumps({"version": 0, "empty": True}), dumps(self._full_query(empty))),
            )

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _check_open(self):
        if self._closed:
            raise AdminError("closed", "store is closed")

    def _write(self):
        self._check_open()
        return _Transaction(self, "BEGIN IMMEDIATE")

    def _read(self):
        self._check_open()
        return _Transaction(self, "BEGIN DEFERRED")

    # ---------------------------------------------------------- state access

    def _read_state(self):
        tables = {"calibration": {}, "qc": {}, "observation": {}}
        for row in self._conn.execute(
            "SELECT kind, id, revision, payload FROM records ORDER BY kind, id"
        ):
            record = loads(row["payload"])
            record["kind"] = row["kind"]
            record["id"] = row["id"]
            record["revision"] = row["revision"]
            tables[row["kind"]][row["id"]] = record
        approvals = [
            {
                "approval_id": row["approval_id"],
                "observation_id": row["observation_id"],
                "credential": row["credential"],
                "observation_revision": row["observation_revision"],
                "approved_version": row["approved_version"],
                "epoch": row["epoch"],
                "signature": row["signature"],
            }
            for row in self._conn.execute(
                "SELECT approval_id, observation_id, credential, observation_revision, "
                "approved_version, epoch, signature FROM approvals ORDER BY approval_id"
            )
        ]
        basis = {
            row["observation_id"]: {
                "epoch": row["epoch"],
                "signature": row["signature"],
            }
            for row in self._conn.execute(
                "SELECT observation_id, epoch, signature FROM observation_basis"
            )
        }
        return {
            "version": self._meta_int("version"),
            "calibrations": tables["calibration"],
            "qcs": tables["qc"],
            "observations": tables["observation"],
            "approvals": approvals,
            "basis": basis,
            "next_revision": self._meta_int("next_revision"),
        }

    def _meta_int(self, key):
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            raise AdminError("corrupt_database", "meta key %r is missing" % key)
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            raise AdminError("corrupt_database", "meta key %r is not an integer" % key) from None

    @staticmethod
    def _ordered(state):
        calibrations = sorted(
            state["calibrations"].values(),
            key=lambda r: (r["instrument"], r["start"], r["end"], r["revision"], r["id"]),
        )
        qcs = sorted(
            state["qcs"].values(),
            key=lambda r: (r["instrument"], r["instant"], r["revision"], r["id"]),
        )
        observations = sorted(
            state["observations"].values(),
            key=lambda r: (r["instrument"], r["instant"], r["id"]),
        )
        return calibrations, qcs, observations

    # ------------------------------------------------------------- derivation

    def _derive(self, state):
        """``(released, observation_reports)`` for a state snapshot."""
        calibrations, qcs, observations = self._ordered(state)
        approvals_by_observation = {}
        for approval in state["approvals"]:
            approvals_by_observation.setdefault(approval["observation_id"], []).append(
                approval
            )
        basis = state.get("basis", {})

        released = []
        reports = []
        for observation in observations:
            signature = rules.basis_signature(observation, calibrations, qcs)
            epoch = basis.get(observation["id"], {"epoch": 0})["epoch"]
            evaluation = rules.evaluate(observation, calibrations, qcs, epoch=epoch)
            reasons = [dict(reason) for reason in evaluation["reasons"]]
            approvals = approvals_by_observation.get(observation["id"], [])
            # An approval is current only if it was granted for this basis
            # signature, at this epoch, with this exact credential.  Epochs are
            # monotonic, so a signature that returns to an earlier shape comes
            # back with a higher epoch and no longer matches.
            valid_approvals = [
                approval
                for approval in approvals
                if approval["epoch"] == epoch
                and approval["signature"] == signature
                and approval["credential"] == evaluation["credential"]
            ]
            if evaluation["eligible"] and not valid_approvals:
                # Approval only gates observations whose basis is sound.  When
                # the basis itself is broken, that is the blocking reason and an
                # approval could not have been granted anyway.
                if approvals:
                    reasons.append(
                        {
                            "reason": "approval_expired",
                            "approval_ids": [a["approval_id"] for a in approvals],
                        }
                    )
                else:
                    reasons.append({"reason": "unapproved"})

            report = {
                "observation": evaluation["observation"],
                "eligible": evaluation["eligible"],
                "conversion": evaluation["conversion"],
                "earlier_qc": evaluation["earlier_qc"],
                "later_qc": evaluation["later_qc"],
                "credential": evaluation["credential"],
                "evidence": evaluation["evidence"],
                "reasons": reasons,
                "approval": {
                    "status": (
                        "valid"
                        if valid_approvals
                        else ("expired" if approvals else "none")
                    ),
                    "approval_ids": [a["approval_id"] for a in approvals],
                    "valid_approval_ids": [
                        a["approval_id"] for a in valid_approvals
                    ],
                },
                "released": False,
            }

            if valid_approvals and evaluation["eligible"]:
                chosen = max(
                    valid_approvals,
                    key=lambda a: (a["approved_version"], a["approval_id"]),
                )
                report["released"] = True
                report["approval"]["approval_id"] = chosen["approval_id"]
                report["reasons"] = []
                released.append(
                    {
                        "observation": evaluation["observation"],
                        "converted_value": evaluation["evidence"]["value"],
                        "conversion": evaluation["conversion"],
                        "earlier_qc": evaluation["earlier_qc"],
                        "later_qc": evaluation["later_qc"],
                        "credential": evaluation["credential"],
                        "approval_id": chosen["approval_id"],
                        "approved_version": chosen["approved_version"],
                        "evidence": evaluation["evidence"],
                    }
                )
            reports.append(report)
        return released, reports

    def _full_query(self, state):
        calibrations, qcs, observations = self._ordered(state)
        released, reports = self._derive(state)
        approvals = sorted(state["approvals"], key=lambda a: a["approval_id"])
        return {
            "version": state["version"],
            "released": released,
            "observations": reports,
            "calibrations": [rules.public_record(record) for record in calibrations],
            "qcs": [
                rules.qc_public(
                    qc,
                    rules.calibration_at(calibrations, qc["instrument"], qc["instant"]),
                )
                for qc in qcs
            ],
            "approvals": approvals,
            "counts": {
                "calibrations": len(calibrations),
                "qcs": len(qcs),
                "observations": len(observations),
                "released": len(released),
                "approvals": len(approvals),
            },
        }

    # ----------------------------------------------------------- validation

    def _stage(self, state, batches):
        """Stage parsed batches and validate their whole final state.

        ``batches`` maps a kind to a list of parsed records.  All records are
        given new revision identities and placed in the state before any
        cross-record rule is checked, so a batch is validated by its final
        state rather than record by record.
        """
        tables = {
            "calibration": state["calibrations"],
            "qc": state["qcs"],
            "observation": state["observations"],
        }
        staged = []
        for kind in ("calibration", "qc", "observation"):
            for record in batches.get(kind, ()):
                table = tables[kind]
                original = table.get(record["id"])
                new_record = dict(record)
                new_record["revision"] = state["next_revision"]
                new_record["prev_revision"] = (
                    original["revision"] if original is not None else None
                )
                state["next_revision"] += 1
                table[record["id"]] = new_record
                staged.append(new_record)

        self._check_calibration_layout(state)
        self._check_unique_qc_instants(state)
        self._check_observation_qc_disjointness(state)
        return staged

    def _check_calibration_layout(self, state):
        grouped = {}
        for calibration in state["calibrations"].values():
            grouped.setdefault(calibration["instrument"], []).append(calibration)
        for instrument, intervals in grouped.items():
            intervals.sort(key=lambda r: (r["start"], r["end"], r["revision"]))
            for left, right in zip(intervals, intervals[1:]):
                if right["start"] < left["end"]:
                    raise AdminError(
                        "conflict",
                        "calibrations %r [%d,%d) and %r [%d,%d) overlap on instrument %r"
                        % (
                            left["id"],
                            left["start"],
                            left["end"],
                            right["id"],
                            right["start"],
                            right["end"],
                            instrument,
                        ),
                    )

    def _check_unique_qc_instants(self, state):
        seen = {}
        for qc in state["qcs"].values():
            other = seen.get(qc["instant"])
            if other is not None:
                raise AdminError(
                    "conflict",
                    "quality-control instant %d is used by both %r and %r"
                    % (qc["instant"], other, qc["id"]),
                )
            seen[qc["instant"]] = qc["id"]

    def _check_observation_qc_disjointness(self, state):
        qc_instants = {qc["instant"] for qc in state["qcs"].values()}
        for observation in state["observations"].values():
            if observation["instant"] in qc_instants:
                raise AdminError(
                    "conflict",
                    "observation %r shares instant %d with a quality-control record"
                    % (observation["id"], observation["instant"]),
                )

    # --------------------------------------------------------------- commits

    def _refresh_basis(self, state):
        """Advance the epoch of every observation whose basis changed.

        The epoch is monotonic: it increases whenever an observation's basis
        signature differs from the last committed one, and it is never lowered.
        This is what makes an expired approval unrecoverable even if a later
        revision restores an earlier-looking neighbourhood.
        """
        calibrations, qcs, _ = self._ordered(state)
        previous = state.get("basis", {})
        current = {}
        for observation in state["observations"].values():
            signature = rules.basis_signature(observation, calibrations, qcs)
            old = previous.get(observation["id"])
            if old is None:
                # New observation, or one migrated from a database without
                # basis rows: epoch 0 with the live signature is the baseline.
                current[observation["id"]] = {"epoch": 0, "signature": signature}
            elif old["signature"] == signature:
                current[observation["id"]] = dict(old)
            else:
                current[observation["id"]] = {
                    "epoch": old["epoch"] + 1,
                    "signature": signature,
                }
        state["basis"] = current
        return current

    def _store_basis(self, state):
        for observation_id, basis_state in state["basis"].items():
            self._conn.execute(
                "INSERT OR REPLACE INTO observation_basis(observation_id, epoch, signature) "
                "VALUES (?,?,?)",
                (observation_id, basis_state["epoch"], basis_state["signature"]),
            )

    def _store_record(self, record):
        payload = dumps(record)
        self._conn.execute(
            "INSERT OR REPLACE INTO records(kind, id, revision, instrument, payload) "
            "VALUES (?,?,?,?,?)",
            (
                record["kind"],
                record["id"],
                record["revision"],
                record["instrument"],
                payload,
            ),
        )
        self._conn.execute(
            "INSERT INTO revisions(revision, kind, id, payload) VALUES (?,?,?,?)",
            (record["revision"], record["kind"], record["id"], payload),
        )

    def _commit_version(self, state, operation, summary):
        version = state["version"] + 1
        state["version"] = version
        # Callers refresh the basis before deriving approval validity; refreshing
        # here as well is idempotent (the signature does not depend on approvals)
        # and guarantees a persisted basis for every observation.
        self._refresh_basis(state)
        self._conn.execute(
            "UPDATE meta SET value=? WHERE key='version'", (str(version),)
        )
        self._conn.execute(
            "UPDATE meta SET value=? WHERE key='next_revision'",
            (str(state["next_revision"]),),
        )
        self._store_basis(state)
        snapshot = self._full_query(state)
        self._conn.execute(
            "INSERT INTO versions(version, operation, summary, snapshot) "
            "VALUES (?,?,?,?)",
            (version, operation, dumps(summary), dumps(snapshot)),
        )
        return version

    # ------------------------------------------------------------- public API

    def query(self, version=None):
        """The complete query result of ``version`` (default: the current one).

        The result is the exact snapshot recorded when that version was
        committed, so historical publication can be reproduced.
        """
        with self._read() as store:
            if version is None:
                version = store._meta_int("version")
            else:
                if isinstance(version, bool) or not isinstance(version, int):
                    raise AdminError("unknown_version", "version must be an integer")
                if version < 0:
                    raise AdminError(
                        "unknown_version", "version %d does not exist" % version
                    )
            row = store._conn.execute(
                "SELECT snapshot FROM versions WHERE version=?", (version,)
            ).fetchone()
            if row is None:
                raise AdminError(
                    "unknown_version", "version %d does not exist" % version
                )
            return loads(row["snapshot"])

    def versions(self):
        """Metadata of every committed version, oldest first."""
        with self._read() as store:
            rows = store._conn.execute(
                "SELECT version, operation, summary FROM versions ORDER BY version"
            ).fetchall()
            return [
                {
                    "version": row["version"],
                    "operation": row["operation"],
                    "summary": loads(row["summary"]),
                }
                for row in rows
            ]

    def revisions(self, kind=None, id=None):
        """Every revision identity ever created, optionally filtered."""
        with self._read() as store:
            clauses = []
            params = []
            if kind is not None:
                if kind not in model.KIND_FIELDS:
                    raise AdminError("invalid_input", "unknown record kind %r" % (kind,))
                clauses.append("kind=?")
                params.append(kind)
            if id is not None:
                if not isinstance(id, str):
                    raise AdminError("invalid_input", "id must be a string")
                clauses.append("id=?")
                params.append(id)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = store._conn.execute(
                "SELECT revision, kind, id, payload FROM revisions"
                + where
                + " ORDER BY revision",
                params,
            ).fetchall()
            result = []
            for row in rows:
                record = loads(row["payload"])
                record["kind"] = row["kind"]
                record["id"] = row["id"]
                record["revision"] = row["revision"]
                result.append(rules.public_record(record))
            return result

    def apply(self, batch):
        """Apply one non-empty maintenance batch atomically.

        ``batch`` maps record kind to a list of records, e.g.::

            {"calibration": [...], "qc": [...], "observation": [...]}

        Each record is added, or wholly replaces the record with the same
        identifier in the same class.  The whole final state is validated and
        every accepted record gets a fresh revision identity.  Rejections do not
        change any state.

        Returns ``{"version": ..., "records": [...]}``.
        """
        if not isinstance(batch, dict):
            raise AdminError("invalid_input", "batch must be an object keyed by record kind")
        if not batch:
            raise AdminError("invalid_input", "batch must not be empty")
        parsed = {}
        for kind, records in batch.items():
            if kind not in model.KIND_FIELDS:
                raise AdminError("invalid_input", "unknown record kind %r" % (kind,))
            if not isinstance(records, (list, tuple)):
                raise AdminError("invalid_input", "%s records must be a list" % kind)
            # An empty sub-list is allowed inside a mixed batch, but a batch
            # with no records at all is refused below.
            parsed[kind] = model.build_batch(kind, records) if records else []
        if not any(parsed.values()):
            raise AdminError("invalid_input", "batch must contain at least one record")

        with self._write() as store:
            state = store._read_state()
            staged = store._stage(state, parsed)
            for record in staged:
                store._store_record(record)
            summary = {
                "operation": "maintenance",
                "records": [
                    {"kind": record["kind"], "id": record["id"],
                     "revision": record["revision"]}
                    for record in staged
                ],
            }
            version = store._commit_version(state, "maintenance", summary)
            return {
                "version": version,
                "records": [rules.public_record(record) for record in staged],
            }

    def upsert(self, kind, records):
        """Convenience wrapper for a single-class maintenance batch."""
        if kind not in model.KIND_FIELDS:
            raise AdminError("invalid_input", "unknown record kind %r" % (kind,))
        return self.apply({kind: records})

    def approve(self, observation_id, credential):
        """Record an approval if ``credential`` is valid at this instant.

        An observation may be approved repeatedly with the same valid
        credential; every successful approval is a new version.
        """
        if not isinstance(observation_id, str) or not observation_id:
            raise AdminError("invalid_input", "observation id must be a non-empty string")
        if not isinstance(credential, str) or not credential:
            raise AdminError(
                "invalid_input", "credential must be a non-empty string"
            )
        with self._write() as store:
            state = store._read_state()
            observation = state["observations"].get(observation_id)
            if observation is None:
                raise AdminError(
                    "not_found", "observation %r does not exist" % observation_id
                )
            calibrations, qcs, _ = store._ordered(state)
            # Bring the basis up to date first: the approval must bind to the
            # epoch that the resulting version will publish.
            store._refresh_basis(state)
            basis_state = state["basis"][observation_id]
            signature = basis_state["signature"]
            epoch = basis_state["epoch"]
            evaluation = rules.evaluate(
                observation, calibrations, qcs, epoch=epoch
            )
            if not evaluation["eligible"]:
                raise AdminError(
                    "ineligible",
                    "observation %r is not eligible: %s"
                    % (
                        observation_id,
                        ", ".join(reason["reason"] for reason in evaluation["reasons"]),
                    ),
                )
            if evaluation["credential"] != credential:
                raise AdminError(
                    "invalid_credential",
                    "credential is not the current credential of observation %r"
                    % observation_id,
                )
            approval_id = uuid.uuid4().hex
            store._conn.execute(
                "INSERT INTO approvals(approval_id, observation_id, credential, "
                "observation_revision, approved_version, epoch, signature) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    approval_id,
                    observation_id,
                    credential,
                    observation["revision"],
                    state["version"],
                    epoch,
                    signature,
                ),
            )
            state["approvals"].append(
                {
                    "approval_id": approval_id,
                    "observation_id": observation_id,
                    "credential": credential,
                    "observation_revision": observation["revision"],
                    "approved_version": state["version"],
                    "epoch": epoch,
                    "signature": signature,
                }
            )
            state["approvals"].sort(key=lambda a: a["approval_id"])
            version = store._commit_version(
                state,
                "approval",
                {
                    "operation": "approval",
                    "observation": observation_id,
                    "approval_id": approval_id,
                },
            )
            return {
                "version": version,
                "approval_id": approval_id,
                "observation_id": observation_id,
                "credential": credential,
                "observation_revision": observation["revision"],
                "basis_epoch": epoch,
                # The approval was granted against the state of the version
                # before this commit; the new version is the one that publishes
                # it.
                "approved_version": version - 1,
                "version": version,
            }

    def proof(self, observation_id):
        """Everything used to judge one observation in the current version."""
        if not isinstance(observation_id, str) or not observation_id:
            raise AdminError("invalid_input", "observation id must be a non-empty string")
        with self._read() as store:
            state = store._read_state()
            observation = state["observations"].get(observation_id)
            if observation is None:
                raise AdminError(
                    "not_found", "observation %r does not exist" % observation_id
                )
            calibrations, qcs, _ = store._ordered(state)
            released, reports = store._derive(state)
            report = next(
                entry
                for entry in reports
                if entry["observation"]["id"] == observation_id
            )
            basis_state = state["basis"].get(
                observation_id, {"epoch": 0, "signature": None}
            )
            signature = rules.basis_signature(observation, calibrations, qcs)
            current_epoch = (
                basis_state["epoch"] if basis_state["signature"] == signature else 0
            )
            evaluation = rules.evaluate(
                observation, calibrations, qcs, epoch=current_epoch
            )
            approvals = [
                {
                    "approval_id": approval["approval_id"],
                    "approved_version": approval["approved_version"],
                    "observation_revision": approval["observation_revision"],
                    "epoch": approval["epoch"],
                    "valid": (
                        approval["epoch"] == current_epoch
                        and approval["signature"] == signature
                        and evaluation["credential"] is not None
                        and approval["credential"] == evaluation["credential"]
                    ),
                }
                for approval in state["approvals"]
                if approval["observation_id"] == observation_id
            ]
            return {
                "version": state["version"],
                "basis_epoch": current_epoch,
                "eligible": evaluation["eligible"],
                "credential": evaluation["credential"],
                "reasons": report["reasons"],
                "observation": evaluation["observation"],
                "conversion": evaluation["conversion"],
                "earlier_qc": evaluation["earlier_qc"],
                "later_qc": evaluation["later_qc"],
                "evidence": evaluation["evidence"],
                "approvals": approvals,
                "released": report["released"],
            }
