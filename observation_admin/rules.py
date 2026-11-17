"""Eligibility, blocking reasons, evidence bundles and credentials.

An evidence bundle records the *revision identity* of the observation, of both
of its neighbouring quality-control records, and of each calibration those
records rely on -- together with the complete content of all of them.  The
credential is the SHA-256 of the canonical bundle salted with the observation's
monotonic *basis epoch* (see :func:`basis_signature`).

Every re-submission of a record gets a fresh revision number, even when its
content is unchanged, so revision identity alone already distinguishes
"identical value, new revision" from "no change at all".  The epoch adds the
one thing revision identity cannot express: a basis that changes and later
returns to its previous shape.  Because the epoch only ever increases, a
credential issued before such an excursion can never become valid again.
"""

import hashlib
import json

from . import exact

EVIDENCE_SCHEMA = "observation-evidence/v1"


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def credential_of(evidence, epoch):
    """Credential binding a bundle to one monotonic basis epoch."""
    return hashlib.sha256(
        (str(epoch) + "\n" + canonical_json(evidence)).encode("utf-8")
    ).hexdigest()


def basis_signature(observation, calibrations, qcs):
    """Everything an observation's evidence depends on, without content.

    Two states with the same signature produce indistinguishable evidence, so
    the observation's basis is unchanged between them.  The signature includes
    the blocked reasons: moving from "blocked" to "unblocked" is a basis change
    even when the relied-on revisions happen to look familiar.
    """
    evaluation = evaluate(observation, calibrations, qcs, epoch=0)

    def side_basis(qc):
        if qc is None:
            return None
        return {
            "rev": qc["revision"],
            "calibration_rev": (
                None if qc["calibration"] is None else qc["calibration"]["revision"]
            ),
            "passed": qc["passed"],
        }

    return canonical_json(
        {
            "observation": observation["revision"],
            "earlier": side_basis(evaluation["earlier_qc"]),
            "later": side_basis(evaluation["later_qc"]),
            "conversion_rev": (
                None
                if evaluation["conversion"] is None
                else evaluation["conversion"]["revision"]
            ),
            "reasons": evaluation["reasons"],
        }
    )


def public_record(record):
    """Canonical public view of a stored record."""
    if record["kind"] == "calibration":
        return {
            "id": record["id"],
            "instrument": record["instrument"],
            "start": record["start"],
            "end": record["end"],
            "gain": record["gain"],
            "offset": record["offset"],
            "revision": record["revision"],
            "prev_revision": record.get("prev_revision"),
        }
    view = {
        "id": record["id"],
        "instrument": record["instrument"],
        "instant": record["instant"],
        "reading": record["reading"],
        "revision": record["revision"],
        "prev_revision": record.get("prev_revision"),
    }
    if record["kind"] == "qc":
        view["reference"] = record["reference"]
        view["tolerance"] = record["tolerance"]
    return view


def calibration_at(calibrations, instrument, instant):
    """The unique calibration whose half-open [start, end) holds ``instant``."""
    match = None
    for calibration in calibrations:
        if calibration["instrument"] != instrument:
            continue
        if calibration["start"] <= instant < calibration["end"]:
            if match is not None:  # pragma: no cover - writer validation excludes this
                raise AssertionError("overlapping calibrations reached evaluation")
            match = calibration
    return match


def convert(reading, calibration):
    return exact.convert(reading, calibration["gain"], calibration["offset"])


def calibration_basis(calibration, converted_value):
    return {
        "id": calibration["id"],
        "instrument": calibration["instrument"],
        "start": calibration["start"],
        "end": calibration["end"],
        "gain": calibration["gain"],
        "offset": calibration["offset"],
        "revision": calibration["revision"],
        "converted_value": converted_value,
    }


def qc_assessment(qc, calibration):
    """Assess a quality-control record against the calibration at its instant.

    When that instant has no calibration the record is still returned, with
    ``calibration``/``converted_value``/``deviation`` set to ``None`` and
    ``passed`` set to ``None``.  The distinction between "cannot be checked"
    and "checked and failed" is carried by ``passed is None`` versus ``False``,
    so a caller cannot accidentally treat an uncheckable record as passing, and
    the observation report can still show *which* record is at fault.
    """
    base = {
        "id": qc["id"],
        "instrument": qc["instrument"],
        "instant": qc["instant"],
        "reading": qc["reading"],
        "reference": qc["reference"],
        "tolerance": qc["tolerance"],
        "revision": qc["revision"],
    }
    if calibration is None:
        base.update(
            {
                "calibration": None,
                "converted_value": None,
                "deviation": None,
                "passed": None,
            }
        )
        return base
    converted = convert(qc["reading"], calibration)
    deviation = exact.format_fraction(exact.deviation(converted, qc["reference"]))
    base.update(
        {
            "calibration": calibration_basis(calibration, converted),
            "converted_value": converted,
            "deviation": deviation,
            "passed": exact.at_most(deviation, qc["tolerance"]),
        }
    )
    return base


def _neighbours(observation, qcs):
    earlier = None
    later = None
    for qc in qcs:
        if qc["instrument"] != observation["instrument"]:
            continue
        if qc["instant"] < observation["instant"]:
            if earlier is None or qc["instant"] > earlier["instant"]:
                earlier = qc
        elif qc["instant"] > observation["instant"]:
            if later is None or qc["instant"] < later["instant"]:
                later = qc
    return earlier, later


def evaluate(observation, calibrations, qcs, epoch=0):
    """Full eligibility report for one observation.

    ``reasons`` lists every actual blocker.  Codes:

    * ``missing_calibration`` with ``scope`` ``observation`` -- the observation
      itself cannot be converted;
    * ``missing_calibration`` with ``scope`` ``quality_control`` and ``side`` --
      that neighbouring quality-control record cannot be converted;
    * ``missing_neighbor`` with ``side`` -- that side has no quality-control
      record at all;
    * ``qc_failed`` with ``side`` -- that side's record exists and exceeds its
      tolerance.

    A missing side produces only ``missing_neighbor``: no ``qc_failed`` is
    invented for a check that cannot be performed.  ``epoch`` is folded into the
    credential so that it also changes when a basis changes and later returns
    to an earlier shape.
    """
    conversion_calibration = calibration_at(
        calibrations, observation["instrument"], observation["instant"]
    )
    reasons = []
    converted = None
    conversion = None

    if conversion_calibration is None:
        reasons.append({"reason": "missing_calibration", "scope": "observation"})
    else:
        converted = convert(observation["reading"], conversion_calibration)
        conversion = calibration_basis(conversion_calibration, converted)

    earlier, later = _neighbours(observation, qcs)
    sides = {}
    for side, qc in (("earlier", earlier), ("later", later)):
        if qc is None:
            sides[side] = None
            reasons.append({"reason": "missing_neighbor", "side": side})
            continue
        calibration = calibration_at(calibrations, qc["instrument"], qc["instant"])
        assessment = qc_assessment(qc, calibration)
        sides[side] = assessment
        if assessment["passed"] is None:
            reasons.append(
                {
                    "reason": "missing_calibration",
                    "scope": "quality_control",
                    "side": side,
                    "qc_id": qc["id"],
                }
            )
        elif not assessment["passed"]:
            reasons.append(
                {
                    "reason": "qc_failed",
                    "side": side,
                    "qc_id": qc["id"],
                    "deviation": assessment["deviation"],
                    "tolerance": qc["tolerance"],
                }
            )

    evidence = None
    credential = None
    if not reasons:
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "observation": {
                "id": observation["id"],
                "instrument": observation["instrument"],
                "instant": observation["instant"],
                "reading": observation["reading"],
                "revision": observation["revision"],
            },
            "conversion": conversion,
            "earlier_qc": sides["earlier"],
            "later_qc": sides["later"],
            "value": converted,
        }
        credential = credential_of(evidence, epoch)

    return {
        "observation": public_record(observation),
        "conversion": conversion,
        "earlier_qc": sides["earlier"],
        "later_qc": sides["later"],
        "reasons": reasons,
        "eligible": not reasons,
        "evidence": evidence,
        "credential": credential,
    }


def qc_public(qc, calibration):
    """Quality-control record plus its own assessment, for listings."""
    view = public_record(qc)
    assessment = qc_assessment(qc, calibration)
    if assessment["passed"] is None:
        view.update(
            {
                "status": "missing_calibration",
                "passed": None,
                "converted_value": None,
                "deviation": None,
                "calibration_revision": None,
            }
        )
        return view
    view.update(
        {
            "status": "pass" if assessment["passed"] else "fail",
            "passed": assessment["passed"],
            "converted_value": assessment["converted_value"],
            "deviation": assessment["deviation"],
            "calibration_revision": assessment["calibration"]["revision"],
        }
    )
    return view
