"""Independent acceptance check, deliberately written against the *public API
only* and in a style different from the unit tests.

Run with ``python3 accept.py``.  It exercises each business rule end to end and
prints PASS/FAIL per check; the exit status is non-zero if anything fails.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from observation_admin import AdminError, Store

results = []


def check(name, condition, extra=""):
    results.append(bool(condition))
    print(("PASS " if condition else "FAIL ") + name + (("  [%s]" % extra) if extra and not condition else ""))


def cred(store, observation_id):
    return store.proof(observation_id)["credential"]


def reasons(store, observation_id):
    entry = next(
        item
        for item in store.query()["observations"]
        if item["observation"]["id"] == observation_id
    )
    return [item["reason"] for item in entry["reasons"]], entry


def cal(i, inst, a, b, gain, offset):
    return {"id": i, "instrument": inst, "start": a, "end": b, "gain": gain, "offset": offset}


def qc(i, inst, t, reading, ref, tol):
    return {"id": i, "instrument": inst, "instant": t, "reading": reading,
            "reference": ref, "tolerance": tol}


def ob(i, inst, t, reading):
    return {"id": i, "instrument": inst, "instant": t, "reading": reading}


def main():
    workdir = tempfile.mkdtemp(prefix="accept-")
    path = os.path.join(workdir, "admin.sqlite3")
    try:
        run(path)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("\n%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


def run(path):
    store = Store(path)

    empty = store.query()
    check("initial release empty", empty["released"] == [])
    check("initial version 0 readable", store.query(0) == empty)

    store.apply(
        {
            "calibration": [cal("C", "M", 0, 1000, "2", "1")],
            "qc": [qc("E", "M", 100, "5", "11", "0"), qc("L", "M", 900, "6", "13", "0")],
            "observation": [ob("O", "M", 500, "3")],
        }
    )
    check("mixed batch is one version", store.query()["version"] == 1)
    r, entry = reasons(store, "O")
    check("convertible and neighbours found", r == ["unapproved"], r)
    check("gain*reading+offset exactly", entry["conversion"]["converted_value"] == "7")
    check("evidence names both qcs",
          {entry["earlier_qc"]["id"], entry["later_qc"]["id"]} == {"E", "L"})

    store.approve("O", cred(store, "O"))
    rel = store.query()["released"]
    check("approved observation is released", len(rel) == 1)
    check("release carries full basis",
          rel and rel[0]["earlier_qc"]["calibration"]["id"] == "C"
          and rel[0]["later_qc"]["calibration"]["id"] == "C"
          and rel[0]["converted_value"] == "7")
    check("repeat approval with same credential",
          store.approve("O", cred(store, "O"))["version"] == 3)
    check("still exactly one release", len(store.query()["released"]) == 1)

    # Unrelated revisions leave it released.
    store.apply(
        {
            "calibration": [cal("CZ", "Z", 0, 10, "1", "0")],
            "qc": [qc("QZ1", "Z", 1, "1", "1", "0"), qc("QZ2", "Z", 9, "1", "1", "0")],
            "observation": [ob("OZ", "Z", 5, "1")],
        }
    )
    check("unrelated revision keeps approval", len(store.query()["released"]) == 1)

    # Identical re-submission of a relied-on record: new identity, approval dies.
    good = cred(store, "O")
    store.apply({"qc": [qc("L", "M", 900, "6", "13", "0")]})
    r, entry = reasons(store, "O")
    check("identical re-submission expires approval", r == ["approval_expired"], r)
    check("no release after expiry", store.query()["released"] == [])
    try:
        store.approve("O", good)
        check("burnt credential refused", False)
    except AdminError as exc:
        check("burnt credential refused", exc.code == "invalid_credential", exc.code)

    # Neighbour excursion, then restore the original layout.
    fresh = cred(store, "O")
    store.approve("O", fresh)
    check("fresh credential releases", len(store.query()["released"]) == 1)
    store.apply({"qc": [qc("MID", "M", 600, "3.5", "8", "0")]})
    check("new neighbour expires approval", reasons(store, "O")[0] == ["approval_expired"])
    store.apply({"qc": [qc("MID", "M", 950, "6", "13", "0")]})
    r, entry = reasons(store, "O")
    check("restored adjacency still expired", r == ["approval_expired"], r)
    check("restored neighbours are E and L",
          {entry["earlier_qc"]["id"], entry["later_qc"]["id"]} == {"E", "L"})
    try:
        store.approve("O", fresh)
        check("old fresh credential refused after excursion", False)
    except AdminError as exc:
        check("old fresh credential refused after excursion",
              exc.code == "invalid_credential", exc.code)

    # Re-acquire and approve again.
    store.approve("O", cred(store, "O"))
    check("re-acquired credential releases", len(store.query()["released"]) == 1)

    # Blocking reasons are complete and distinguish the cases.
    store2 = Store()
    store2.apply(
        {
            "calibration": [cal("C", "M", 0, 100, "1", "0"),
                            cal("C2", "M", 200, 400, "1", "0")],
            "qc": [qc("BAD", "M", 50, "1", "999", "0")],
            "observation": [ob("X", "M", 500, "1"), ob("Y", "M", 300, "1")],
        }
    )
    rx, ex = reasons(store2, "X")
    check("missing calibration reported for observation", "missing_calibration" in rx, rx)
    check("missing neighbour reported", "missing_neighbor" in rx, rx)
    ry, ey = reasons(store2, "Y")
    check("failed qc reported when present",
          any(item["reason"] == "qc_failed" for item in ey["reasons"]), ry)
    check("missing later side is not a qc failure",
          [item for item in ey["reasons"] if item["reason"] == "missing_neighbor"
           and item["side"] == "later"] != [])
    store2.close()

    # Validation and atomicity.
    before = store.query()
    for batch, code in (
        ({"observation": [ob("", "M", 1, "1")]}, "invalid_input"),
        ({"observation": [ob("N", "M", 1, "1"), ob("N", "M", 2, "1")]}, "invalid_input"),
        ({"calibration": [cal("CU", "M", 900, 1200, "1", "0")]}, "conflict"),
        ({"observation": [ob("N", "M", 100, "1")]}, "conflict"),
    ):
        try:
            store.apply(batch)
            check("rejected %s" % code, False)
        except AdminError as exc:
            check("rejected %s" % code, exc.code == code, exc.code)
    check("rejections changed nothing", store.query() == before)

    # Historical publication is replayable.
    snapshots = {v["version"]: store.query(v["version"]) for v in store.versions()}
    store.close()
    reopened = Store(path)
    check("restart preserves current result", reopened.query() == snapshots[max(snapshots)])
    check("restart preserves every version",
          all(reopened.query(v) == snapshots[v] for v in snapshots))
    try:
        reopened.query(9999)
        check("unknown version rejected", False)
    except AdminError as exc:
        check("unknown version rejected", exc.code == "unknown_version", exc.code)
    reopened.close()


if __name__ == "__main__":
    sys.exit(main())
