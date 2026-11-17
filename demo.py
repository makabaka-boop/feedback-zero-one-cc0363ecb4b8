"""End-to-end demonstration of the observation-administration backend.

Run with ``python3 demo.py``.  Everything happens in an in-memory database.
"""

import json

from observation_admin import AdminError, Store


def show(title, value):
    print("\n== %s ==" % title)
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def main():
    store = Store()
    print("initial version:", store.query()["version"],
          "| released:", len(store.query()["released"]))

    store.apply(
        {
            "calibration": [
                {"id": "cal-A-1", "instrument": "A", "start": 0, "end": 1000,
                 "gain": "2", "offset": "1"},
            ],
            "qc": [
                {"id": "qc-A-early", "instrument": "A", "instant": 100,
                 "reading": "5", "reference": "11", "tolerance": "0"},
                {"id": "qc-A-late", "instrument": "A", "instant": 900,
                 "reading": "6", "reference": "13", "tolerance": "0"},
            ],
            "observation": [
                {"id": "obs-A-500", "instrument": "A", "instant": 500,
                 "reading": "3"},
            ],
        }
    )
    entry = store.query()["observations"][0]
    show("observation before approval", {
        "version": store.query()["version"],
        "eligible": entry["eligible"],
        "conversion": entry["conversion"],
        "reasons": entry["reasons"],
        "credential": entry["credential"],
    })

    credential = entry["credential"]
    approved = store.approve("obs-A-500", credential)
    show("approval", approved)
    released = store.query()["released"]
    show("released set", {
        "version": store.query()["version"],
        "converted_value": released[0]["converted_value"],
        "earlier_qc": released[0]["earlier_qc"]["id"],
        "later_qc": released[0]["later_qc"]["id"],
    })

    # Unrelated revision: another instrument's data.
    store.apply(
        {
            "calibration": [
                {"id": "cal-B-1", "instrument": "B", "start": 0, "end": 10,
                 "gain": "1", "offset": "0"},
            ],
            "qc": [
                {"id": "qc-B-1", "instrument": "B", "instant": 1,
                 "reading": "1", "reference": "1", "tolerance": "0"},
                {"id": "qc-B-2", "instrument": "B", "instant": 9,
                 "reading": "1", "reference": "1", "tolerance": "0"},
            ],
        }
    )
    print("\nunrelated revision -> still released:",
          len(store.query()["released"]) == 1)

    # Re-submit the later QC with identical content: new revision identity.
    store.apply(
        {
            "qc": [
                {"id": "qc-A-late", "instrument": "A", "instant": 900,
                 "reading": "6", "reference": "13", "tolerance": "0"},
            ]
        }
    )
    entry = store.query()["observations"][0]
    show("after identical re-submission of a relied-on QC", {
        "version": store.query()["version"],
        "released": store.query()["released"],
        "reasons": entry["reasons"],
        "approval": entry["approval"],
    })

    # Push the neighbour out of the way, then restore the original layout.
    store.apply(
        {
            "qc": [
                {"id": "qc-A-mid", "instrument": "A", "instant": 600,
                 "reading": "3.5", "reference": "8", "tolerance": "0"},
            ]
        }
    )
    store.apply(
        {
            "qc": [
                {"id": "qc-A-mid", "instrument": "A", "instant": 950,
                 "reading": "6", "reference": "13", "tolerance": "0"},
            ]
        }
    )
    entry = store.query()["observations"][0]
    show("neighbouring identities restored, approval still dead", {
        "version": store.query()["version"],
        "earlier_qc": entry["earlier_qc"]["id"],
        "later_qc": entry["later_qc"]["id"],
        "reasons": entry["reasons"],
    })
    try:
        store.approve("obs-A-500", credential)
    except AdminError as exc:
        print("re-approval with old credential ->", exc.to_dict())

    fresh = store.query()["observations"][0]["credential"]
    store.approve("obs-A-500", fresh)
    show("released after fresh approval", {
        "version": store.query()["version"],
        "released": len(store.query()["released"]),
    })

    show("version history", store.versions())
    show("historical release at version 2",
         [entry["observation"]["id"] for entry in store.query(2)["released"]])
    show("historical release at version 5",
         [entry["observation"]["id"] for entry in store.query(5)["released"]])
    store.close()


if __name__ == "__main__":
    main()
