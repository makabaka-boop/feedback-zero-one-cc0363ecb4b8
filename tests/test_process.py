"""Cross-process concurrency check.

Several separate OS processes open the same database file and mutate it at the
same time.  The result must be a serial order: every operation either commits
as a whole version, or is rejected without changing anything.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WORKER = r"""
import json
import sys
sys.path.insert(0, sys.argv[1])
from observation_admin import AdminError, Store

path, action, payload = sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
store = Store(path)
try:
    if action == "apply":
        print(json.dumps(store.apply(payload)))
    elif action == "approve":
        print(json.dumps(store.approve(payload["observation"], payload["credential"])))
    elif action == "query":
        print(json.dumps(store.query()))
except AdminError as exc:
    print(json.dumps({"error": exc.code, "detail": exc.detail}))
    sys.exit(3)
finally:
    store.close()
"""


class ProcessConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "admin.sqlite3")
        self.worker = os.path.join(self.directory, "worker.py")
        with open(self.worker, "w", encoding="utf-8") as handle:
            handle.write(WORKER)

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def run_worker(self, action, payload):
        completed = subprocess.run(
            [sys.executable, "-I", self.worker, ROOT, self.path, action,
             json.dumps(payload)],
            capture_output=True,
            text=True,
        )
        self.assertIn(completed.returncode, (0, 3), completed.stderr)
        return completed.returncode, json.loads(completed.stdout)

    def test_simultaneous_conflicting_writes_serialise(self):
        # q1 and q2 both claim instant 100; exactly one process may win.
        before = self.run_worker(
            "apply",
            {"qc": [{"id": "seed", "instrument": "A", "instant": 50,
                     "reading": "1", "reference": "1", "tolerance": "0"}]},
        )
        self.assertEqual(before[0], 0)
        processes = []
        for identifier in ("q1", "q2"):
            batch = {
                "qc": [
                    {"id": identifier, "instrument": "A", "instant": 100,
                     "reading": "1", "reference": "1", "tolerance": "0"}
                ]
            }
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-I", self.worker, ROOT, self.path,
                     "apply", json.dumps(batch)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        outcomes = []
        for process in processes:
            stdout, _stderr = process.communicate()
            outcomes.append((process.returncode, json.loads(stdout)))
        codes = sorted(code for code, _ in outcomes)
        self.assertEqual(codes, [0, 3])
        winner = next(body for code, body in outcomes if code == 0)
        loser = next(body for code, body in outcomes if code == 3)
        self.assertIn("error", loser)
        status, state = self.run_worker("query", None)
        self.assertEqual(status, 0)
        self.assertEqual(state["version"], winner["version"])
        self.assertEqual(state["counts"]["qcs"], 2)

    def test_simultaneous_approvals_all_commit(self):
        status, _ = self.run_worker(
            "apply",
            {
                "calibration": [{"id": "c1", "instrument": "A", "start": 0,
                                 "end": 1000, "gain": "1", "offset": "0"}],
                "qc": [
                    {"id": "q1", "instrument": "A", "instant": 100,
                     "reading": "1", "reference": "1", "tolerance": "0"},
                    {"id": "q2", "instrument": "A", "instant": 900,
                     "reading": "1", "reference": "1", "tolerance": "0"},
                ],
                "observation": [{"id": "o1", "instrument": "A", "instant": 500,
                                 "reading": "1"}],
            },
        )
        self.assertEqual(status, 0)
        status, state = self.run_worker("query", None)
        self.assertEqual(status, 0)
        credential = state["observations"][0]["credential"]
        processes = []
        for _ in range(4):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-I", self.worker, ROOT, self.path,
                     "approve", json.dumps({"observation": "o1",
                                            "credential": credential})],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        versions = []
        for process in processes:
            stdout, stderr = process.communicate()
            self.assertEqual(process.returncode, 0, stderr)
            versions.append(json.loads(stdout)["version"])
        self.assertEqual(sorted(versions), [2, 3, 4, 5])
        status, state = self.run_worker("query", None)
        self.assertEqual(len(state["approvals"]), 4)
        self.assertEqual(len(state["released"]), 1)


if __name__ == "__main__":
    unittest.main()
