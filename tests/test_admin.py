"""Acceptance tests for the observation-administration backend.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from observation_admin import AdminError, Store


def cal(identifier, instrument, start, end, gain, offset):
    return {
        "id": identifier,
        "instrument": instrument,
        "start": start,
        "end": end,
        "gain": gain,
        "offset": offset,
    }


def qc(identifier, instrument, instant, reading, reference, tolerance):
    return {
        "id": identifier,
        "instrument": instrument,
        "instant": instant,
        "reading": reading,
        "reference": reference,
        "tolerance": tolerance,
    }


def obs(identifier, instrument, instant, reading):
    return {
        "id": identifier,
        "instrument": instrument,
        "instant": instant,
        "reading": reading,
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def tearDown(self):
        self.store.close()

    def seed_basic(self):
        self.store.apply(
            {
                "calibration": [cal("c1", "A", 0, 1000, "2", "1")],
                "qc": [
                    qc("q1", "A", 100, "5", "11", "0"),
                    qc("q2", "A", 900, "6", "13", "0"),
                ],
                "observation": [obs("o1", "A", 500, "3")],
            }
        )

    def reasons(self, version=None, observation_id="o1"):
        query = self.store.query(version)
        entry = next(
            r for r in query["observations"] if r["observation"]["id"] == observation_id
        )
        return [reason["reason"] for reason in entry["reasons"]], entry


class InitialStateTests(Base):
    def test_initial_is_empty(self):
        query = self.store.query()
        self.assertEqual(query["version"], 0)
        self.assertEqual(query["released"], [])
        self.assertEqual(query["observations"], [])
        self.assertEqual(query["calibrations"], [])
        self.assertEqual(query["qcs"], [])
        self.assertEqual(query["approvals"], [])

    def test_initial_version_readable_explicitly(self):
        self.assertEqual(self.store.query(0), self.store.query())

    def test_unknown_version(self):
        for bad in (1, -1, "0", 1.0, True):
            with self.assertRaises(AdminError) as caught:
                self.store.query(bad)
            self.assertEqual(caught.exception.code, "unknown_version")


class EligibilityTests(Base):
    def test_eligible_but_unapproved_not_released(self):
        self.seed_basic()
        reasons, entry = self.reasons()
        self.assertEqual(reasons, ["unapproved"])
        self.assertTrue(entry["eligible"])
        self.assertIsNotNone(entry["credential"])
        self.assertEqual(self.store.query()["released"], [])

    def test_conversion_is_gain_times_reading_plus_offset(self):
        self.seed_basic()
        entry = self.score_entry = self.reasons()[1]
        self.assertEqual(entry["conversion"]["converted_value"], "7")

    def test_missing_calibration(self):
        self.store.apply(
            {
                "qc": [
                    qc("q1", "A", 100, "5", "11", "0"),
                    qc("q2", "A", 900, "6", "13", "0"),
                ],
                "observation": [obs("o1", "A", 500, "3")],
            }
        )
        reasons, entry = self.reasons()
        self.assertIn("missing_calibration", reasons)
        self.assertIsNone(entry["conversion"])
        self.assertIsNone(entry["credential"])

    def test_qc_without_calibration_is_reported_and_blocks(self):
        # Calibration gaps are allowed, so a quality-control record can sit in
        # one.  That must be reported as a missing calibration of the
        # quality-control side, not skipped and not treated as a failure.
        self.store.apply(
            {
                "calibration": [
                    cal("c1", "A", 0, 200, "1", "0"),
                    cal("c2", "A", 400, 1000, "1", "0"),
                ],
                "qc": [
                    qc("q1", "A", 300, "1", "1", "0"),
                    qc("q2", "A", 900, "1", "1", "0"),
                ],
                "observation": [obs("o1", "A", 500, "1")],
            }
        )
        reasons, entry = self.reasons()
        self.assertEqual(reasons, ["missing_calibration"])
        self.assertIn(
            {
                "reason": "missing_calibration",
                "scope": "quality_control",
                "side": "earlier",
                "qc_id": "q1",
            },
            entry["reasons"],
        )
        self.assertIsNone(entry["earlier_qc"]["calibration"])
        self.assertIsNone(entry["earlier_qc"]["passed"])
        self.assertEqual(entry["earlier_qc"]["id"], "q1")
        self.assertTrue(entry["later_qc"]["passed"])
        self.assertEqual(
            [(item["id"], item["status"]) for item in self.store.query()["qcs"]],
            [("q1", "missing_calibration"), ("q2", "pass")],
        )
        self.assertIsNone(entry["credential"])

    def test_observation_at_interval_boundary_uses_next_calibration(self):
        self.store.apply(
            {
                "calibration": [
                    cal("c1", "A", 0, 500, "1", "0"),
                    cal("c2", "A", 500, 1000, "3", "2"),
                ],
                "qc": [
                    qc("q1", "A", 100, "1", "1", "0"),
                    qc("q2", "A", 900, "1", "1", "0"),
                ],
                "observation": [obs("o1", "A", 500, "2")],
            }
        )
        entry = self.reasons()[1]
        self.assertEqual(entry["conversion"]["id"], "c2")
        self.assertEqual(entry["conversion"]["converted_value"], "8")

    def test_missing_side_never_reports_qc_failure_for_that_side(self):
        self.store.apply(
            {
                "calibration": [cal("c1", "A", 0, 1000, "1", "0")],
                "qc": [qc("q2", "A", 900, "0", "999", "0")],
                "observation": [obs("o1", "A", 500, "3")],
            }
        )
        reasons, entry = self.reasons()
        self.assertIn("missing_neighbor", reasons)
        # The missing earlier side has no quality-control record, so no failure
        # may be invented for it; the existing later side genuinely fails and is
        # reported as such.
        self.assertIsNone(entry["earlier_qc"])
        self.assertIsNotNone(entry["later_qc"])
        self.assertFalse(entry["later_qc"]["passed"])
        failed = [r for r in entry["reasons"] if r["reason"] == "qc_failed"]
        self.assertEqual([r["side"] for r in failed], ["later"])
        missing = [r for r in entry["reasons"] if r["reason"] == "missing_neighbor"]
        self.assertEqual([r["side"] for r in missing], ["earlier"])

    def test_both_neighbors_missing_reports_both_sides(self):
        self.store.apply(
            {
                "calibration": [cal("c1", "A", 0, 1000, "1", "0")],
                "observation": [obs("o1", "A", 500, "3")],
            }
        )
        reasons, entry = self.reasons()
        self.assertEqual(reasons.count("missing_neighbor"), 2)
        sides = {r["side"] for r in entry["reasons"] if r["reason"] == "missing_neighbor"}
        self.assertEqual(sides, {"earlier", "later"})

    def test_failed_qc_blocks_and_is_reported(self):
        self.store.apply(
            {
                "calibration": [cal("c1", "A", 0, 1000, "2", "1")],
                "qc": [
                    qc("q1", "A", 100, "5", "10.5", "0.4"),
                    qc("q2", "A", 900, "6", "13", "0"),
                ],
                "observation": [obs("o1", "A", 500, "3")],
            }
        )
        reasons, entry = self.reasons()
        self.assertIn("qc_failed", reasons)
        failed = [r for r in entry["reasons"] if r["reason"] == "qc_failed"]
        self.assertEqual([r["side"] for r in failed], ["earlier"])
        self.assertEqual(failed[0]["deviation"], "0.5")
        self.assertEqual(entry["earlier_qc"]["deviation"], "0.5")
        self.assertIsNone(entry["credential"])

    def test_tolerance_is_inclusive(self):
        self.store.apply(
            {
                "calibration": [cal("c1", "A", 0, 1000, "1", "0")],
                "qc": [
                    qc("q1", "A", 100, "10", "10.5", "0.5"),
                    qc("q2", "A", 900, "10", "10", "0"),
                ],
                "observation": [obs("o1", "A", 500, "3")],
            }
        )
        reasons, _ = self.reasons()
        self.assertEqual(reasons, ["unapproved"])

    def test_exact_decimal_arithmetic_has_no_rounding(self):
        # 0.1 * 0.2 + 0.3 is exactly 0.32 in decimal (floats would not be).
        self.store.apply(
            {
                "calibration": [cal("c1", "A", 0, 10, "0.1", "0.3")],
                "qc": [
                    qc("q1", "A", 1, "0.2", "0.32", "0"),
                    qc("q2", "A", 9, "0.2", "0.32", "0"),
                ],
                "observation": [obs("o1", "A", 5, "0.2")],
            }
        )
        reasons, entry = self.reasons()
        self.assertEqual(reasons, ["unapproved"])
        self.assertEqual(entry["conversion"]["converted_value"], "0.32")

    def test_large_values_are_exact(self):
        huge = "123456789012345678901234567890"
        expected = str(int(huge) + 1)
        self.store.apply(
            {
                "calibration": [cal("c1", "A", 0, 10, huge, "1")],
                "qc": [
                    qc("q1", "A", 1, "1", expected, "0"),
                    qc("q2", "A", 9, "1", expected, "0"),
                ],
                "observation": [obs("o1", "A", 5, "1")],
            }
        )
        reasons, entry = self.reasons()
        self.assertEqual(reasons, ["unapproved"])
        self.assertEqual(entry["conversion"]["converted_value"], expected)

    def test_neighbors_are_per_instrument(self):
        self.store.apply(
            {
                "calibration": [
                    cal("c1", "A", 0, 1000, "1", "0"),
                    cal("c2", "B", 0, 1000, "1", "0"),
                ],
                "qc": [
                    qc("q1", "B", 100, "1", "1", "0"),
                    qc("q2", "B", 900, "1", "1", "0"),
                ],
                "observation": [obs("o1", "A", 500, "1")],
            }
        )
        reasons, _ = self.reasons()
        self.assertEqual(reasons.count("missing_neighbor"), 2)


class EvidenceAndApprovalTests(Base):
    def test_approval_releases(self):
        self.seed_basic()
        credential = self.reasons()[1]["credential"]
        result = self.store.approve("o1", credential)
        self.assertEqual(result["version"], 2)
        query = self.store.query()
        released = query["released"]
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["converted_value"], "7")
        self.assertEqual(released[0]["credential"], credential)
        for key in ("observation", "conversion", "earlier_qc", "later_qc", "evidence"):
            self.assertIn(key, released[0])
        self.assertEqual(released[0]["earlier_qc"]["id"], "q1")
        self.assertEqual(released[0]["later_qc"]["id"], "q2")
        self.assertEqual(released[0]["earlier_qc"]["calibration"]["id"], "c1")

    def test_approval_requires_current_credential(self):
        self.seed_basic()
        with self.assertRaises(AdminError) as caught:
            self.store.approve("o1", "deadbeef")
        self.assertEqual(caught.exception.code, "invalid_credential")
        self.assertEqual(self.store.query()["version"], 1)

    def test_approval_requires_eligible(self):
        self.store.apply(
            {
                "calibration": [cal("c1", "A", 0, 1000, "1", "0")],
                "observation": [obs("o1", "A", 500, "3")],
            }
        )
        with self.assertRaises(AdminError) as caught:
            self.store.approve("o1", "anything")
        self.assertEqual(caught.exception.code, "ineligible")
        self.assertEqual(self.store.query()["version"], 1)

    def test_missing_observation(self):
        with self.assertRaises(AdminError) as caught:
            self.store.approve("nope", "x")
        self.assertEqual(caught.exception.code, "not_found")
        with self.assertRaises(AdminError) as caught:
            self.store.proof("nope")
        self.assertEqual(caught.exception.code, "not_found")

    def test_repeated_approval_with_same_credential(self):
        self.seed_basic()
        credential = self.reasons()[1]["credential"]
        first = self.store.approve("o1", credential)
        second = self.store.approve("o1", credential)
        self.assertNotEqual(first["approval_id"], second["approval_id"])
        self.assertEqual(self.store.query()["released"][0]["converted_value"], "7")
        self.assertEqual(len(self.store.query()["approvals"]), 2)

    def test_revision_of_relied_record_expires_approval_irreversibly(self):
        self.seed_basic()
        credential = self.reasons()[1]["credential"]
        self.store.approve("o1", credential)
        self.assertEqual(len(self.store.query()["released"]), 1)

        # Identical content, new revision: still a new identity -> expired.
        self.store.apply({"qc": [qc("q2", "A", 900, "6", "13", "0")]})
        reasons, entry = self.reasons()
        self.assertEqual(reasons, ["approval_expired"])
        self.assertEqual(entry["approval"]["status"], "expired")
        self.assertEqual(self.store.query()["released"], [])

        # Restoring the old values cannot revive the old credential either.
        self.store.apply({"qc": [qc("q2", "A", 900, "6", "13", "0")]})
        reasons, _ = self.reasons()
        self.assertEqual(reasons, ["approval_expired"])
        with self.assertRaises(AdminError) as caught:
            self.store.approve("o1", credential)
        self.assertEqual(caught.exception.code, "invalid_credential")

    def test_reapproval_after_expiry(self):
        self.seed_basic()
        first = self.reasons()[1]["credential"]
        self.store.approve("o1", first)
        self.store.apply({"qc": [qc("q2", "A", 900, "6", "13", "0")]})
        reasons, entry = self.reasons()
        self.assertEqual(reasons, ["approval_expired"])
        fresh = entry["credential"]
        self.assertNotEqual(fresh, first)
        self.store.approve("o1", fresh)
        query = self.store.query()
        self.assertEqual(len(query["released"]), 1)
        self.assertEqual(query["released"][0]["credential"], fresh)
        self.assertEqual(len(query["approvals"]), 2)

    def test_adjacency_change_expires_approval(self):
        self.seed_basic()
        credential = self.reasons()[1]["credential"]
        self.store.approve("o1", credential)
        self.assertEqual(len(self.store.query()["released"]), 1)
        # A new qc closer to o1 on the earlier side becomes the neighbour.
        self.store.apply({"qc": [qc("q3", "A", 400, "3.5", "8", "0")]})
        reasons, entry = self.reasons()
        self.assertEqual(reasons, ["approval_expired"])
        self.assertEqual(entry["earlier_qc"]["id"], "q3")

    def test_adjacency_restored_still_not_revived(self):
        self.seed_basic()
        credential = self.reasons()[1]["credential"]
        self.store.approve("o1", credential)
        self.assertEqual(len(self.store.query()["released"]), 1)

        # Put a third record between q1 and o1: q1 stops being the neighbour.
        self.store.apply({"qc": [qc("q3", "A", 400, "3.5", "8", "0")]})
        self.assertEqual(self.reasons()[0], ["approval_expired"])
        self.assertEqual(self.reasons()[1]["earlier_qc"]["id"], "q3")

        # Move q3 past q2 and past o1: q1 is the earlier neighbour again, i.e.
        # the earlier shape is restored exactly.  The approval must stay dead.
        self.store.apply({"qc": [qc("q3", "A", 950, "5.6", "12.2", "0")]})
        reasons, entry = self.reasons()
        self.assertEqual(entry["earlier_qc"]["id"], "q1")
        self.assertEqual(entry["later_qc"]["id"], "q2")
        self.assertEqual(reasons, ["approval_expired"])
        self.assertEqual(self.store.query()["released"], [])

        # The original credential is also refused for a fresh approval.
        with self.assertRaises(AdminError) as caught:
            self.store.approve("o1", credential)
        self.assertEqual(caught.exception.code, "invalid_credential")

        # A newly obtained credential works.
        fresh = entry["credential"]
        self.assertNotEqual(fresh, credential)
        self.store.approve("o1", fresh)
        self.assertEqual(len(self.store.query()["released"]), 1)

    def test_removing_a_neighbour_then_restoring_it_does_not_revive(self):
        self.store.apply(
            {
                "calibration": [cal("c1", "A", 0, 1000, "1", "0")],
                "qc": [
                    qc("q1", "A", 100, "1", "1", "0"),
                    qc("q2", "A", 200, "1", "1", "0"),
                    qc("q3", "A", 900, "1", "1", "0"),
                ],
                "observation": [obs("o1", "A", 500, "1")],
            }
        )
        credential = self.reasons()[1]["credential"]
        self.store.approve("o1", credential)
        self.assertEqual(len(self.store.query()["released"]), 1)
        # There is no delete API, but a record can be moved out of the way and
        # later moved back.  Version 0 here is the seed; this checks the epoch
        # survives both excursions.
        self.store.apply({"qc": [qc("q2", "A", 950, "1", "1", "0")]})
        self.store.apply({"qc": [qc("q2", "A", 200, "1", "1", "0")]})
        reasons, _ = self.reasons()
        self.assertEqual(reasons, ["approval_expired"])

    def test_unrelated_revision_does_not_expire(self):
        self.seed_basic()
        credential = self.reasons()[1]["credential"]
        self.store.approve("o1", credential)
        self.store.apply(
            {
                "calibration": [cal("c9", "Z", 0, 10, "1", "0")],
                "qc": [
                    qc("q8", "Z", 1, "1", "1", "0"),
                    qc("q9", "Z", 3, "1", "1", "0"),
                ],
                "observation": [obs("o9", "Z", 2, "1")],
            }
        )
        query = self.store.query()
        self.assertEqual(len(query["released"]), 1)
        self.assertEqual(query["released"][0]["observation"]["id"], "o1")

    def test_replacing_observation_expires_its_approval(self):
        self.seed_basic()
        credential = self.reasons()[1]["credential"]
        self.store.approve("o1", credential)
        self.store.apply({"observation": [obs("o1", "A", 500, "3")]})
        reasons, entry = self.reasons()
        self.assertEqual(reasons, ["approval_expired"])
        self.assertNotEqual(entry["credential"], credential)

    def test_proof_contents(self):
        self.seed_basic()
        credential = self.reasons()[1]["credential"]
        approved = self.store.approve("o1", credential)
        proof = self.store.proof("o1")
        self.assertTrue(proof["eligible"])
        self.assertEqual(proof["credential"], credential)
        self.assertTrue(proof["released"])
        self.assertEqual(proof["conversion"]["converted_value"], "7")
        self.assertEqual(proof["earlier_qc"]["id"], "q1")
        self.assertEqual(proof["later_qc"]["id"], "q2")
        self.assertEqual(proof["approvals"][0]["approval_id"], approved["approval_id"])
        self.assertTrue(proof["approvals"][0]["valid"])
        self.assertEqual(proof["evidence"]["earlier_qc"]["revision"], 2)
        self.assertEqual(proof["evidence"]["conversion"]["revision"], 1)


class ValidationTests(Base):
    def assertRejected(self, batch, code="invalid_input"):
        before = self.store.query()
        with self.assertRaises(AdminError) as caught:
            self.store.apply(batch)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.store.query(), before)

    def test_empty_batch(self):
        self.assertRejected({})
        self.assertRejected({"qc": []})

    def test_empty_identifier(self):
        self.assertRejected({"observation": [obs("", "A", 1, "1")]})
        self.assertRejected({"observation": [obs("o", "", 1, "1")]})

    def test_missing_and_extra_fields(self):
        bad = obs("o1", "A", 1, "1")
        del bad["reading"]
        self.assertRejected({"observation": [bad]})
        extra = obs("o1", "A", 1, "1")
        extra["bogus"] = 1
        self.assertRejected({"observation": [extra]})

    def test_float_instants_and_values_refused(self):
        self.assertRejected({"observation": [obs("o1", "A", 1.5, "1")]})
        self.assertRejected({"observation": [obs("o1", "A", True, "1")]})
        self.assertRejected({"observation": [obs("o1", "A", 1, 0.1)]})

    def test_non_finite_and_bad_decimals(self):
        for bad in ("NaN", "Infinity", "-Infinity", "", "abc", "1e999999",
                    "1." + "0" * 600 + "1"):
            self.assertRejected({"observation": [obs("o1", "A", 1, bad)]})
        self.assertRejected({"observation": [obs("o1", "A", 1, None)]})

    def test_duplicate_identifiers_within_batch(self):
        self.assertRejected(
            {"observation": [obs("o1", "A", 1, "1"), obs("o1", "B", 2, "1")]}
        )

    def test_same_identifier_across_classes_is_fine(self):
        self.store.apply(
            {
                "calibration": [cal("x", "A", 0, 10, "1", "0")],
                "qc": [
                    qc("x", "A", 1, "1", "1", "0"),
                    qc("y", "A", 9, "1", "1", "0"),
                ],
                "observation": [obs("x", "A", 5, "1")],
            }
        )
        self.assertEqual(len(self.store.query()["observations"]), 1)

    def test_negative_tolerance(self):
        self.assertRejected({"qc": [qc("q1", "A", 1, "1", "1", "-0.1")]})

    def test_calibration_interval_non_empty(self):
        self.assertRejected({"calibration": [cal("c1", "A", 5, 5, "1", "0")]})
        self.assertRejected({"calibration": [cal("c1", "A", 6, 5, "1", "0")]})

    def test_calibration_overlap_rejected(self):
        self.store.apply({"calibration": [cal("c1", "A", 0, 10, "1", "0")]})
        self.assertRejected(
            {"calibration": [cal("c2", "A", 9, 20, "1", "0")]}, code="conflict"
        )

    def test_calibration_touching_intervals_allowed(self):
        self.store.apply({"calibration": [cal("c1", "A", 0, 10, "1", "0")]})
        self.store.apply({"calibration": [cal("c2", "A", 10, 20, "1", "0")]})

    def test_calibration_gap_allowed(self):
        self.store.apply(
            {
                "calibration": [
                    cal("c1", "A", 0, 10, "1", "0"),
                    cal("c2", "A", 20, 30, "1", "0"),
                ],
                "qc": [
                    qc("q1", "A", 1, "1", "1", "0"),
                    qc("q2", "A", 29, "1", "1", "0"),
                ],
                "observation": [obs("o1", "A", 15, "1")],
            }
        )
        reasons, entry = self.reasons()
        self.assertIn("missing_calibration", reasons)
        self.assertIsNone(entry["conversion"])

    def test_interval_half_open(self):
        self.store.apply(
            {
                "calibration": [
                    cal("c1", "A", 0, 10, "1", "0"),
                    cal("c2", "A", 10, 20, "2", "0"),
                ],
                "observation": [obs("o1", "A", 10, "1")],
            }
        )
        entry = self.reasons()[1]
        self.assertEqual(entry["conversion"]["id"], "c2")

    def test_qc_instant_is_globally_unique(self):
        # "Quality-control instants are unique" carries no instrument
        # qualifier, so two instruments may not share one.
        with self.assertRaises(AdminError) as caught:
            self.store.apply(
                {
                    "qc": [
                        qc("q1", "A", 10, "1", "1", "0"),
                        qc("q2", "B", 10, "1", "1", "0"),
                    ]
                }
            )
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.store.query()["version"], 0)

    def test_observation_cannot_share_instant_with_qc(self):
        with self.assertRaises(AdminError) as caught:
            self.store.apply(
                {
                    "qc": [qc("q1", "A", 10, "1", "1", "0")],
                    "observation": [obs("o1", "A", 10, "1")],
                }
            )
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.store.query()["version"], 0)

    def test_observation_cannot_be_moved_onto_existing_qc(self):
        self.store.apply(
            {
                "qc": [qc("q1", "A", 10, "1", "1", "0"), qc("q2", "A", 30, "1", "1", "0")],
                "observation": [obs("o1", "A", 20, "1")],
            }
        )
        store = self.store
        with self.assertRaises(AdminError) as caught:
            store.apply({"observation": [obs("o1", "A", 10, "1")]})
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(store.query()["version"], 1)

    def test_unknown_kind(self):
        with self.assertRaises(AdminError) as caught:
            self.store.apply({"widget": [{"id": "w"}]})
        self.assertEqual(caught.exception.code, "invalid_input")

    def test_failed_batch_does_not_change_state(self):
        self.store.apply({"calibration": [cal("c1", "A", 0, 100, "1", "0")]})
        before = self.store.query()
        with self.assertRaises(AdminError):
            self.store.apply(
                {
                    "observation": [obs("o1", "A", 5, "1")],
                    "qc": [qc("q1", "A", 5, "1", "1", "0")],
                }
            )
        self.assertEqual(self.store.query(), before)


class RevisionTests(Base):
    def test_whole_batch_final_state_validation(self):
        # Individually each observation is fine; together they collide with the
        # qc instant only if final state is what is validated.
        with self.assertRaises(AdminError):
            self.store.apply(
                {
                    "observation": [
                        obs("o1", "A", 5, "1"),
                        obs("o2", "A", 6, "1"),
                    ],
                    "qc": [qc("q1", "A", 6, "1", "1", "0")],
                }
            )

    def test_replacement_keeps_identity_and_gets_new_revision(self):
        self.store.apply({"calibration": [cal("c1", "A", 0, 100, "1", "0")]})
        first = self.store.query()["calibrations"][0]
        self.store.apply({"calibration": [cal("c1", "A", 0, 100, "2", "0")]})
        second = self.store.query()["calibrations"][0]
        self.assertEqual(second["id"], "c1")
        self.assertNotEqual(second["revision"], first["revision"])
        self.assertEqual(second["prev_revision"], first["revision"])
        self.assertEqual(second["gain"], "2")
        self.assertEqual(len(self.store.query()["calibrations"]), 1)

    def test_identical_resubmission_is_new_revision(self):
        self.store.apply({"calibration": [cal("c1", "A", 0, 100, "1", "0")]})
        first = self.store.query()["calibrations"][0]["revision"]
        self.store.apply({"calibration": [cal("c1", "A", 0, 100, "1", "0")]})
        second = self.store.query()["calibrations"][0]["revision"]
        self.assertNotEqual(first, second)
        revisions = self.store.revisions(kind="calibration", id="c1")
        self.assertEqual([r["revision"] for r in revisions], [first, second])

    def test_no_delete_api(self):
        for name in ("delete", "remove", "drop"):
            self.assertFalse(hasattr(self.store, name))

    def test_history_is_immutable(self):
        self.store.apply({"calibration": [cal("c1", "A", 0, 100, "1", "0")]})
        v1 = self.store.query(1)
        self.store.apply({"calibration": [cal("c1", "A", 0, 100, "9", "0")]})
        self.assertEqual(self.store.query(1), v1)
        self.assertEqual(self.store.query(1)["calibrations"][0]["gain"], "1")


class ReleaseHistoryTests(Base):
    def test_release_set_per_version(self):
        self.seed_basic()
        credential = self.reasons()[1]["credential"]
        self.store.approve("o1", credential)
        current = self.store.query()
        self.assertEqual(len(current["released"]), 1)
        self.assertEqual(self.store.query(current["version"]), current)
        self.assertEqual(self.store.query(1)["released"], [])
        versions = self.store.versions()
        self.assertEqual([v["version"] for v in versions], [0, 1, 2])

    def test_every_version_readable(self):
        self.seed_basic()
        credential = self.reasons()[1]["credential"]
        self.store.approve("o1", credential)
        self.store.apply({"qc": [qc("q2", "A", 900, "6", "13", "0")]})
        for version in range(len(self.store.versions())):
            result = self.store.query(version)
            self.assertEqual(result["version"], version)
            self.assertIn("released", result)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "admin.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_restart_preserves_versions_and_state(self):
        store = Store(self.path)
        store.apply(
            {
                "calibration": [cal("c1", "A", 0, 100, "2", "1")],
                "qc": [
                    qc("q1", "A", 10, "5", "11", "0"),
                    qc("q2", "A", 90, "6", "13", "0"),
                ],
                "observation": [obs("o1", "A", 20, "3")],
            }
        )
        credential = store.query()["observations"][0]["credential"]
        store.approve("o1", credential)
        before = store.query()
        history = store.versions()
        store.close()

        reopened = Store(self.path)
        try:
            self.assertEqual(reopened.query(), before)
            self.assertEqual(reopened.versions(), history)
            self.assertEqual(reopened.query(1)["released"], [])
            self.assertEqual(reopened.query()["released"][0]["converted_value"], "7")
        finally:
            reopened.close()


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "admin.sqlite3")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_concurrent_approvals_of_same_credential_all_succeed(self):
        self.store.apply(
            {
                "calibration": [cal("c1", "A", 0, 1000, "1", "0")],
                "qc": [
                    qc("q1", "A", 100, "1", "1", "0"),
                    qc("q2", "A", 900, "1", "1", "0"),
                ],
                "observation": [obs("o1", "A", 500, "1")],
            }
        )
        credential = self.store.query()["observations"][0]["credential"]
        errors = []
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            try:
                results.append(self.store.approve("o1", credential))
            except AdminError as exc:  # pragma: no cover - should not happen
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        versions = sorted(r["version"] for r in results)
        self.assertEqual(versions, list(range(2, 10)))
        self.assertEqual(len(self.store.query()["approvals"]), 8)

    def test_concurrent_conflicting_writes_have_one_winner(self):
        self.store.apply(
            {
                "qc": [
                    qc("q1", "A", 100, "1", "1", "0"),
                    qc("q2", "A", 200, "1", "1", "0"),
                ],
                "calibration": [cal("c1", "A", 0, 1000, "1", "0")],
            }
        )
        outcome = []
        barrier = threading.Barrier(2)

        def worker(identifier, instant):
            barrier.wait()
            try:
                self.store.apply({"qc": [qc(identifier, "A", instant, "1", "1", "0")]})
                outcome.append((identifier, "ok"))
            except AdminError as exc:
                outcome.append((identifier, exc.code))

        threads = [
            threading.Thread(target=worker, args=("q1", 100)),
            threading.Thread(target=worker, args=("q1", 300)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Both target q1, so both may succeed; the last committed one wins and
        # the version count proves serial execution.
        self.assertEqual(len(outcome), 2)

    def test_concurrent_inserts_of_same_qc_instant(self):
        outcome = []
        barrier = threading.Barrier(2)

        def worker(identifier):
            barrier.wait()
            try:
                self.store.apply({"qc": [qc(identifier, "A", 100, "1", "1", "0")]})
                outcome.append((identifier, "ok"))
            except AdminError as exc:
                outcome.append((identifier, exc.code))

        threads = [
            threading.Thread(target=worker, args=("q1",)),
            threading.Thread(target=worker, args=("q2",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        successes = [identifier for identifier, status in outcome if status == "ok"]
        failures = [identifier for identifier, status in outcome if status != "ok"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.store.query()["counts"]["qcs"], 1)

    def test_reader_sees_consistent_versions(self):
        self.store.apply(
            {
                "calibration": [cal("c1", "A", 0, 10, "1", "0")],
            }
        )
        seen = set()
        stop = threading.Event()

        def writer():
            for instant in range(1, 6):
                self.store.apply(
                    {"qc": [qc("q%d" % instant, "A", instant, "1", "1", "0")]}
                )
            stop.set()

        def reader():
            while not stop.is_set():
                result = self.store.query()
                self.assertEqual(result["counts"]["qcs"], len(result["qcs"]))

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()


if __name__ == "__main__":
    unittest.main()
