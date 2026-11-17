"""Exact decimal arithmetic.

Readings, references, tolerances, gains and offsets are finite decimal numbers
and must be compared exactly.  ``decimal.Decimal`` multiplication and addition
obey the *ambient context precision*, which silently rounds once a value grows
past it, so this module never relies on a Decimal context: values are parsed to
``Decimal`` only for validation and canonicalisation, and every arithmetic
operation is performed on ``fractions.Fraction``, where it is exact.

Values are represented internally as canonical decimal strings.  Canonical form
depends only on the numeric value (``"1.50"``, ``"1.5"`` and ``Decimal("1.5")``
all become ``"1.5"``), so two equal numbers always compare and serialise
identically.

Input is accepted as a decimal string, an integer, or a ``Decimal``.  A float
is refused: a binary float is not the decimal it prints as, so silently
round-tripping it through ``repr`` would make "exact" arithmetic approximate
for inputs the caller believes are exact.  JSON callers should send decimals as
strings (``"0.1"``) and can use ``json.loads(..., parse_float=str)``.

Limits below are input guards, not business rules.  Inputs are limited so that
the largest results of ``gain * reading + offset`` still fit comfortably; they
are far beyond any physical measurement.
"""

from decimal import Decimal
from fractions import Fraction

INPUT_MAX_DIGITS = 500
INPUT_MAX_ADJUSTED_EXPONENT = 1000
RESULT_MAX_DIGITS = 4000
RESULT_MAX_ADJUSTED_EXPONENT = 10000


class DecimalInputError(ValueError):
    """The supplied value is not an acceptable finite decimal."""


def _as_decimal(value, field):
    if isinstance(value, bool):
        raise DecimalInputError("%s must be a decimal number" % field)
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        raise DecimalInputError(
            "%s must be an exact decimal, not a binary float; pass a string" % field
        )
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise DecimalInputError("%s must not be empty" % field)
        try:
            result = Decimal(text)
        except ArithmeticError:
            raise DecimalInputError("%s is not a decimal number" % field) from None
    else:
        raise DecimalInputError("%s must be a decimal number" % field)
    if not result.is_finite():
        raise DecimalInputError("%s must be a finite decimal number" % field)
    return result


def _check_limits(decimal_value, field, max_digits, max_adjusted):
    _sign, digits, exponent = decimal_value.as_tuple()
    if len(digits) > max_digits:
        raise DecimalInputError("%s has too many significant digits" % field)
    if not isinstance(exponent, int):  # pragma: no cover - only finite values reach here
        raise DecimalInputError("%s must be a finite decimal number" % field)
    if all(digit == 0 for digit in digits):
        return
    adjusted = exponent + len(digits) - 1
    if abs(adjusted) > max_adjusted:
        raise DecimalInputError("%s has an out-of-range exponent" % field)


def _format(sign, digits, exponent):
    """Render a decimal without consulting any context.

    ``digits`` is a list of ints with no leading zero beyond a lone zero, and
    ``exponent`` is the power of ten applied to the digit string.  Trailing
    zeros of the digit string are removed first so the form is canonical.
    """
    if not digits or all(digit == 0 for digit in digits):
        return "0"
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    text = "".join(str(digit) for digit in digits)
    adjusted = exponent + len(text) - 1
    if -30 <= adjusted <= 30:
        if exponent >= 0:
            body = text + "0" * exponent
        else:
            point = len(text) + exponent
            if point > 0:
                body = text[:point] + "." + text[point:]
            else:
                body = "0." + "0" * (-point) + text
    else:
        mantissa = text[0] + ("." + text[1:] if len(text) > 1 else "")
        body = mantissa + "E" + ("+" if adjusted >= 0 else "") + str(adjusted)
    return ("-" if sign else "") + body


def canonical(value, field="value"):
    """Validate ``value`` and return its canonical decimal string."""
    decimal_value = _as_decimal(value, field)
    _check_limits(decimal_value, field, INPUT_MAX_DIGITS, INPUT_MAX_ADJUSTED_EXPONENT)
    sign, digits, exponent = decimal_value.as_tuple()
    return _format(sign, list(digits), exponent)


def to_fraction(canonical_text):
    """Exact rational for a canonical decimal string."""
    numerator, denominator = Decimal(canonical_text).as_integer_ratio()
    return Fraction(numerator, denominator)


def format_fraction(value):
    """Exact canonical decimal string for a rational with a 2-and-5 denominator."""
    if value == 0:
        return "0"
    sign = value < 0
    numerator = abs(value.numerator)
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:  # pragma: no cover - decimals are closed under +, -, *
        raise ArithmeticError("value is not a finite decimal")
    scale = max(twos, fives)
    coefficient = numerator * (2 ** (scale - twos)) * (5 ** (scale - fives))
    digits = [int(char) for char in str(coefficient)]
    text = _format(sign, digits, -scale)
    _check_limits(Decimal(text), "result", RESULT_MAX_DIGITS, RESULT_MAX_ADJUSTED_EXPONENT)
    return text


def convert(reading, gain, offset):
    """``gain * reading + offset``, exact, as a canonical decimal string."""
    return format_fraction(
        to_fraction(gain) * to_fraction(reading) + to_fraction(offset)
    )


def deviation(value, reference):
    """``|value - reference|`` as an exact rational."""
    return abs(to_fraction(value) - to_fraction(reference))


def at_most(left, right):
    """Exact ``left <= right`` for canonical decimal strings."""
    return to_fraction(left) <= to_fraction(right)
