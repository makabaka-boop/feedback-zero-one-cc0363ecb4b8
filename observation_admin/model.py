"""Record parsing and validation (pure business-rule layer).

Rules enforced here:

* Identifiers are non-empty text, unique within their own record class.  A
  revision of an existing identifier wholly replaces it; identity persists but
  the revision identity is new.
* Instants are UTC integer seconds.  Booleans, floats and strings are refused;
  JSON has an unambiguous integer type, so no coercion is needed.
* Readings, references, tolerances, gains and offsets are *finite decimal*
  numbers, accepted as decimal strings, integers or ``Decimal`` objects and
  stored canonically.  Binary floats are refused: a float such as ``0.1`` is
  not the decimal it appears to be, so accepting one would make the promised
  exact arithmetic a lie.  Callers passing JSON should send decimal literals as
  strings (``"0.1"``) or integers.  (A JSON number like ``0.1`` is refused for
  the same reason; ``json.loads(..., parse_float=str)`` accepts them exactly.)
* Calibration intervals are non-empty and half-open, ``[start, end)``.
* Tolerances are non-negative.
"""

from . import exact
from .errors import AdminError


MAX_IDENTIFIER_LENGTH = 512
MAX_FIELDS_PER_RECORD = 16

KIND_FIELDS = {
    "calibration": ("id", "instrument", "start", "end", "gain", "offset"),
    # A quality-control record carries its own raw reading (converted through
    # the calibration at its instant) *in addition to* the reference value and
    # tolerance it is judged against.
    "qc": ("id", "instrument", "instant", "reading", "reference", "tolerance"),
    "observation": ("id", "instrument", "instant", "reading"),
}


def _reject(code, detail):
    raise AdminError(code, detail)


def parse_identifier(value, field="id"):
    if not isinstance(value, str):
        _reject("invalid_input", "%s must be a string" % field)
    if not value:
        _reject("invalid_input", "%s must not be empty" % field)
    if len(value) > MAX_IDENTIFIER_LENGTH:
        _reject("invalid_input", "%s is too long" % field)
    return value


def parse_instant(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        _reject("invalid_input", "%s must be an integer UTC second" % field)
    return value


def parse_decimal(value, field):
    try:
        return exact.canonical(value, field)
    except exact.DecimalInputError as exc:
        _reject("invalid_input", str(exc))


def build_record(kind, data):
    """Validate one raw record and return its canonical internal form."""
    if kind not in KIND_FIELDS:        _reject("invalid_input", "unknown record kind %r" % (kind,))
    if not isinstance(data, dict):
        _reject("invalid_input", "%s record must be an object" % kind)
    if len(data) > MAX_FIELDS_PER_RECORD:
        _reject("invalid_input", "%s record has too many fields" % kind)
    allowed = KIND_FIELDS[kind]
    extra = sorted(set(data) - set(allowed))
    if extra:
        _reject("invalid_input", "unexpected field(s) for %s: %s" % (kind, ", ".join(extra)))
    missing = [name for name in allowed if name not in data]
    if missing:
        _reject("invalid_input", "missing field(s) for %s: %s" % (kind, ", ".join(missing)))

    identifier = parse_identifier(data["id"])
    instrument = parse_identifier(data["instrument"], "instrument")

    if kind == "calibration":
        start = parse_instant(data["start"], "start")
        end = parse_instant(data["end"], "end")
        if not start < end:
            _reject("invalid_input", "calibration interval must be non-empty ([start, end))")
        return {
            "kind": "calibration",
            "id": identifier,
            "instrument": instrument,
            "start": start,
            "end": end,
            "gain": parse_decimal(data["gain"], "gain"),
            "offset": parse_decimal(data["offset"], "offset"),
        }

    instant = parse_instant(data["instant"], "instant")

    if kind == "qc":
        reference = parse_decimal(data["reference"], "reference")
        tolerance = parse_decimal(data["tolerance"], "tolerance")
        if exact.to_fraction(tolerance) < 0:
            _reject("invalid_input", "tolerance must be non-negative")
        return {
            "kind": "qc",
            "id": identifier,
            "instrument": instrument,
            "instant": instant,
            "reading": parse_decimal(data["reading"], "reading"),
            "reference": reference,
            "tolerance": tolerance,
        }

    return {
        "kind": "observation",
        "id": identifier,
        "instrument": instrument,
        "instant": instant,
        "reading": parse_decimal(data["reading"], "reading"),
    }


def build_batch(kind, records):
    """Validate a non-empty batch; duplicate identifiers inside it are refused."""
    if not isinstance(records, (list, tuple)):
        _reject("invalid_input", "batch records must be a list")
    if not records:
        _reject("invalid_input", "batch must not be empty")
    built = []
    seen = set()
    for index, raw in enumerate(records):
        try:
            record = build_record(kind, raw)
        except AdminError as exc:
            raise AdminError(exc.code, "record %d: %s" % (index, exc.detail or exc.code)) from None
        if record["id"] in seen:
            _reject("invalid_input", "duplicate id within batch: %r" % record["id"])
        seen.add(record["id"])
        built.append(record)
    return built
