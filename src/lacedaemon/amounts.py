from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


ITEM_SCALE = 324
ITEM_ATOM_TOLERANCE = Decimal("0.000001")
ITEM_DISPLAY_PLACES = Decimal("0.000000001")


def decimal_amount(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Amount must be numeric") from exc
    if not amount.is_finite():
        raise ValueError("Amount must be finite")
    return amount


def amount_to_atoms(value: object, *, strict: bool = True) -> int:
    raw = decimal_amount(value) * ITEM_SCALE
    atoms = raw.to_integral_value(rounding=ROUND_HALF_UP)
    if strict and abs(raw - atoms) > ITEM_ATOM_TOLERANCE:
        raise ValueError(f"Item amount must align to 1/{ITEM_SCALE} of a base unit")
    return int(atoms)


def atoms_to_decimal(atoms: int) -> Decimal:
    return Decimal(int(atoms or 0)) / ITEM_SCALE


def atoms_to_float(atoms: int) -> float:
    return float(atoms_to_decimal(atoms))


def atoms_to_amount(atoms: int) -> int | float:
    amount = atoms_to_decimal(atoms)
    whole = amount.to_integral_value()
    if amount == whole:
        return int(whole)
    return float(amount.quantize(ITEM_DISPLAY_PLACES).normalize())
