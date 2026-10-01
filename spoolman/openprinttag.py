"""Full SLIX2 initialization from pinned official OpenPrintTag field definitions.

Region updates are excluded. No field is dropped to make the image fit.
"""

import json
import math
import struct
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

DATA = Path(__file__).with_name("openprinttag_data")
CATALOG = json.loads((DATA / "fields.json").read_text())
MIME = b"application/vnd.openprinttag"
CAPACITY = 316  # ICODE SLIX2 user memory: blocks 0-78; block 79 is the NXP counter
INLINE_LIMIT = 24
PRECISION = 0.001
LAB_COMPONENTS = 3
FIRMWARE_INT_MAX = 2**31 - 1
FIRMWARE_FLOAT_MAX = 3.4028234663852886e38
COUNTRY_LENGTH = 2


def _head(major: int, n: int) -> bytes:
    if n < INLINE_LIMIT:
        return bytes([major * 32 + n])
    for size, ai in ((1, 24), (2, 25), (4, 26), (8, 27)):
        if n < 1 << (size * 8):
            return bytes([major * 32 + ai]) + n.to_bytes(size, "big")
    raise ValueError("Integer exceeds CBOR uint64")


def _compact(value: float, fmt: str) -> bytes | None:
    try:
        packed = struct.pack(fmt, value)
    except (OverflowError, struct.error):
        return None
    return packed if abs(value - struct.unpack(fmt, packed)[0]) < PRECISION else None


def _float(value: float) -> bytes:
    if not math.isfinite(value):
        raise ValueError("Non-finite number")
    if value.is_integer():
        return cbor(int(value))
    for fmt, prefix in ((">e", b"\xf9"), (">f", b"\xfa")):
        packed = _compact(value, fmt)
        if packed is not None:
            return prefix + packed
    return b"\xfb" + struct.pack(">d", value)


def _map(value: dict) -> bytes:
    entries = sorted(((cbor(k), cbor(v)) for k, v in value.items()), key=lambda p: (len(p[0]), p[0]))
    return _head(5, len(entries)) + b"".join(k + v for k, v in entries)


def cbor(value: object) -> bytes:
    """Encode the bounded CBOR subset used by the official field types."""
    encoders: dict[type, Callable] = {
        bool: lambda v: b"\xf5" if v else b"\xf4",
        int: lambda v: _head(0, v) if v >= 0 else _head(1, -1 - v),
        float: _float,
        str: lambda v: _head(3, len(v.encode("utf-8"))) + v.encode("utf-8"),
        bytes: lambda v: _head(2, len(v)) + v,
        list: lambda v: _head(4, len(v)) + b"".join(cbor(i) for i in v),
        dict: _map,
    }
    encoder = encoders.get(type(value))
    if encoder is None:
        raise ValueError("Unsupported CBOR type")
    return encoder(value)


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _enum(field: dict, value: object) -> int:
    options = {o["value"]: o["key"] for o in field["options"]}
    if isinstance(value, str) and value in options:
        return options[value]
    if type(value) is int and value in options.values():
        return value
    raise ValueError("Unknown enum value")


def _enum_array(field: dict, value: object) -> list:
    _require(isinstance(value, list) and len(value) <= field["max_length"], "Invalid enum array")
    return [_enum(field, item) for item in value]


def _color(_field: dict, value: object) -> bytes:
    _require(isinstance(value, str) and value.startswith("#") and len(value) in (7, 9), "Expected #RRGGBB or #RRGGBBAA")
    return bytes.fromhex(value[1:])


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _lab(_field: dict, value: object) -> list:
    _require(
        isinstance(value, list) and len(value) == LAB_COMPONENTS and all(_number(v) for v in value),
        "Expected three finite numbers",
    )
    return value


def _field_value(field: dict, value: object) -> object:
    kind = field["type"]
    checks = {
        "int": lambda: type(value) is int,
        "timestamp": lambda: type(value) is int,
        "number": lambda: _number(value),
        "bool": lambda: type(value) is bool,
        "string": lambda: isinstance(value, str) and "\0" not in value and len(value.encode()) <= field["max_length"],
    }
    if kind in checks:
        _require(checks[kind](), "Invalid " + kind + " value or length")
        return value
    converters = {
        "enum": _enum,
        "enum_array": _enum_array,
        "color_rgba": _color,
        "color_lab": _lab,
        "uuid": lambda _f, v: uuid.UUID(v).bytes,
    }
    _require(kind in converters, "Unsupported field type")
    return converters[kind](field, value)


def encode_region(region: str, values: dict[str, Any]) -> bytes:
    """Validate named fields and encode their official integer keys."""
    _require(isinstance(values, dict), region + ": expected object")
    fields = {f["name"]: f for f in CATALOG[region]}
    unknown = set(values) - fields.keys()
    _require(not unknown, "Unknown fields: " + ", ".join(sorted(unknown)))
    encoded = {}
    for name, value in values.items():
        field = fields[name]
        try:
            encoded[field["key"]] = _field_value(field, value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"{region}.{name}: {exc}") from exc
    for field in fields.values():
        _require(field.get("required") is not True or field["name"] in values, f"Missing {region}.{field['name']}")
    return cbor(encoded)


def _validate_fff(main: dict, aux: dict) -> None:
    _require(main.get("material_class") in ("FFF", 0), "The Phoenix writer currently supports FFF only")
    _require(main.get("write_protection", "no") in ("no", 0), "Hardware write protection is not supported")
    for region, data in [("main", main), ("aux", aux)]:
        _require(
            not any(f.get("category") == "sla" and f["name"] in data for f in CATALOG[region]),
            "SLA-specific fields cannot be used on this FFF writer",
        )
    country = main.get("country_of_origin")
    _require(
        country is None
        or (isinstance(country, str) and len(country) == COUNTRY_LENGTH and country.isascii() and country.isalpha()),
        "country_of_origin must be a two-letter country code",
    )
    for name in (
        "nominal_netto_full_weight",
        "actual_netto_full_weight",
        "empty_container_weight",
        "density",
        "filament_diameter",
    ):
        _require(
            name not in main or (_number(main[name]) and 0 <= main[name] <= FIRMWARE_FLOAT_MAX),
            f"{name} must be a nonnegative finite float32",
        )
    for name in ("min_print_temperature", "max_print_temperature", "min_bed_temperature", "max_bed_temperature"):
        _require(
            name not in main or (type(main[name]) is int and 0 <= main[name] <= FIRMWARE_INT_MAX),
            f"{name} must be a nonnegative int32",
        )
    for name in ("filament_diameter", "density"):
        _require(name not in main or (_number(main[name]) and main[name] > 0), f"{name} must be positive")
    for lo, hi in [("min_print_temperature", "max_print_temperature"), ("min_bed_temperature", "max_bed_temperature")]:
        _require(lo not in main or hi not in main or main[lo] <= main[hi], f"{lo} exceeds {hi}")


def make_image(main: dict[str, Any], aux: dict[str, Any], uid: str) -> tuple[bytes, dict[str, Any]]:
    """Create a full image, with a block-aligned auxiliary region and stable tag UUID."""
    main = dict(main)
    # Stable identity for the physical tag, including across preview retries.
    main.setdefault("instance_uuid", str(uuid.uuid5(uuid.NAMESPACE_OID, "OpenPrintTag:" + uid)))
    _validate_fff(main, aux)
    main_bytes, aux_bytes = encode_region("main", main), encode_region("aux", aux)
    # Extended TLV + long MIME NDEF record, filling all 316 bytes. Aux starts
    # at absolute byte 280, a block boundary, with 35 bytes until the terminator.
    payload_start = 8 + 6 + len(MIME)
    payload_size = 315 - payload_start
    aux_offset = 280 - payload_start
    meta = cbor({2: aux_offset})
    if len(meta) + len(main_bytes) > aux_offset:
        raise ValueError(
            f"Main region too large: {len(main_bytes)} bytes, capacity {aux_offset - len(meta)}. "
            "Remove optional fields."
        )
    if len(aux_bytes) > payload_size - aux_offset:
        raise ValueError(f"Aux region too large: {len(aux_bytes)} bytes, capacity {payload_size - aux_offset}.")
    payload = bytearray(payload_size)
    payload[: len(meta)] = meta
    payload[len(meta) : len(meta) + len(main_bytes)] = main_bytes
    payload[aux_offset : aux_offset + len(aux_bytes)] = aux_bytes
    ndef = bytes([0xC2, len(MIME)]) + struct.pack(">I", len(payload)) + MIME + payload
    image = b"\xe1\x40\x27\x01\x03\xff" + struct.pack(">H", len(ndef)) + ndef + b"\xfe"
    _require(len(image) == CAPACITY, "Internal layout error")
    return image, {
        "main": main,
        "aux": aux,
        "main_bytes": len(main_bytes),
        "main_capacity": aux_offset - len(meta),
        "aux_bytes": len(aux_bytes),
        "aux_capacity": payload_size - aux_offset,
    }
