"""Command-line interface.

Usage::

    python3 -m observation_admin --db PATH apply FILE
    python3 -m observation_admin --db PATH query [--version N]
    python3 -m observation_admin --db PATH approve OBSERVATION_ID CREDENTIAL
    python3 -m observation_admin --db PATH proof OBSERVATION_ID
    python3 -m observation_admin --db PATH versions
    python3 -m observation_admin --db PATH revisions [--kind KIND] [--id ID]

Results are written to standard output as one compact JSON object.  A rejected
operation writes ``{"error": ..., "detail": ...}`` to standard error, exits
with status 3, and does not modify the database.

``apply`` reads a JSON object keyed by record kind.  JSON numbers are parsed as
decimal strings so that values stay exact::

    {
      "calibration": [{"id": "c1", "instrument": "A", "start": 0, "end": 100,
                       "gain": "2", "offset": "1"}],
      "qc": [{"id": "q1", "instrument": "A", "instant": 10, "reading": "5",
              "reference": "11", "tolerance": "0"}],
      "observation": [{"id": "o1", "instrument": "A", "instant": 20,
                       "reading": "3"}]
    }

Inside that file, ``gain`` may also be written as the JSON number ``2`` or
``2.5``; both are read exactly.
"""

import argparse
import json
import sys

from . import Store
from .errors import AdminError
from .model import KIND_FIELDS


def _emit(value, stream=sys.stdout):
    stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()


def _load_batch(path):
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
    except OSError as exc:
        raise AdminError("invalid_input", "cannot read batch file: %s" % exc) from None
    try:
        # parse_float=str keeps decimal literals exact.
        return json.loads(text, parse_float=str)
    except ValueError as exc:
        raise AdminError("invalid_input", "batch file is not valid JSON: %s" % exc) from None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="observation_admin",
        description="Observation administration backend (SQLite).",
    )
    parser.add_argument("--db", required=True, help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    apply_parser = sub.add_parser("apply", help="apply one maintenance batch")
    apply_parser.add_argument("file", help="JSON batch file, or - for stdin")

    query_parser = sub.add_parser("query", help="print a complete version snapshot")
    query_parser.add_argument("--version", type=int, default=None)

    approve_parser = sub.add_parser("approve", help="approve an observation")
    approve_parser.add_argument("observation_id")
    approve_parser.add_argument("credential")

    proof_parser = sub.add_parser("proof", help="print all evidence for an observation")
    proof_parser.add_argument("observation_id")

    sub.add_parser("versions", help="list committed versions")

    revisions_parser = sub.add_parser(
        "revisions", help="list revision identities ever created"
    )
    revisions_parser.add_argument("--kind", choices=sorted(KIND_FIELDS), default=None)
    revisions_parser.add_argument("--id", default=None)
    return parser


def run(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        store = Store(args.db)
    except AdminError as exc:
        _emit(exc.to_dict(), sys.stderr)
        return 3
    try:
        if args.command == "apply":
            result = store.apply(_load_batch(args.file))
        elif args.command == "query":
            result = store.query(args.version)
        elif args.command == "approve":
            result = store.approve(args.observation_id, args.credential)
        elif args.command == "proof":
            result = store.proof(args.observation_id)
        elif args.command == "versions":
            result = store.versions()
        elif args.command == "revisions":  # pragma: no cover - parser enforces set
            result = store.revisions(kind=args.kind, id=args.id)
        else:  # pragma: no cover
            raise AdminError("invalid_input", "unknown command %r" % args.command)
    except AdminError as exc:
        _emit(exc.to_dict(), sys.stderr)
        return 3
    finally:
        store.close()
    _emit(result)
    return 0


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
