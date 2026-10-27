"""Refuse CBOR that uses semantic tags, before the bytes reach cbor2.

Tag memory is untrusted input. CBOR semantic tags (major type 6) let a few hundred bytes describe
an enormous object: RFC 8746 shared references (tags 28 and 29) expand into an exponential object
graph at constant nesting depth, so a depth cap never trips, and cbor2 formats `repr(value)` of
that graph into the error message for a malformed tag such as 260. That string is built inside
`CBORDecoder.decode()`, before any validation of ours can run, and it does the same in the C
extension that the Docker image ships. Hooks, decoder subclasses, a text length cap and a payload
size cap cannot stop it: the payload is small, and the C path resolves tags 28, 29 and 260
internally.

Real OpenPrintTag payloads use no semantic tags (both Prusa sample dumps in
tests/fixtures/openprinttag decode to zero), so the whole class is refused. The check walks the
CBOR item heads itself, with no recursion and no cbor2, and stops at the end of the first item:
the caller then hands cbor2 only the bytes that were checked.

The check lives here, not in a codec, so every CBOR-based format inherits it.
"""

from dataclasses import dataclass

# Same limit the codec has always applied to nesting. Kept here too so the walk stays bounded on
# its own, whatever the decoder does with it.
MAX_CBOR_DEPTH = 8

_BREAK = 0xFF
_MAJOR_BYTES = 2
_MAJOR_TEXT = 3
_MAJOR_ARRAY = 4
_MAJOR_MAP = 5
_MAJOR_TAG = 6
_INFO_ONE_BYTE = 24  # additional-information values 24..27 announce a 1, 2, 4 or 8 byte argument
_INFO_EIGHT_BYTES = 27
_INFO_INDEFINITE = 31
_INDEFINITE = -1


@dataclass(slots=True)
class _Frame:
    """An open container: how many items it still needs, or _INDEFINITE until its break byte."""

    remaining: int
    string_major: int | None = None  # set for an indefinite-length string: its chunks must match


def _read_argument(data: bytes, pos: int, info: int) -> tuple[int, int]:
    """Return (argument, position after it) for a head whose additional information is not 31."""
    if info < _INFO_ONE_BYTE:
        return info, pos
    if info > _INFO_EIGHT_BYTES:
        raise ValueError("Invalid CBOR additional information")
    width = 1 << (info - _INFO_ONE_BYTE)
    if pos + width > len(data):
        raise ValueError("Truncated CBOR item")
    return int.from_bytes(data[pos : pos + width], "big"), pos + width


def _read_item(data: bytes, pos: int, head: int, stack: list[_Frame]) -> tuple[int, bool]:
    """Read the rest of one item head. Return (new position, whether it opened a container)."""
    major, info = head >> 5, head & 0x1F
    if major == _MAJOR_TAG:
        raise ValueError("CBOR semantic tags are not accepted")

    string_major = stack[-1].string_major if stack else None
    if string_major is not None and (major != string_major or info == _INFO_INDEFINITE):
        raise ValueError("Invalid chunk in an indefinite-length CBOR string")

    if info == _INFO_INDEFINITE:
        if not _MAJOR_BYTES <= major <= _MAJOR_MAP:
            raise ValueError("Invalid CBOR additional information")
        stack.append(_Frame(_INDEFINITE, major if major <= _MAJOR_TEXT else None))
        return pos, True

    argument, pos = _read_argument(data, pos, info)
    if major in (_MAJOR_BYTES, _MAJOR_TEXT):
        if pos + argument > len(data):
            raise ValueError("Truncated CBOR item")
        return pos + argument, False
    if major in (_MAJOR_ARRAY, _MAJOR_MAP):
        count = argument * 2 if major == _MAJOR_MAP else argument
        if count:
            stack.append(_Frame(count))
            return pos, True
    # Integers, simple values, floats and empty containers are complete once the head is read.
    return pos, False


def _finish_item(stack: list[_Frame]) -> bool:
    """Count a finished item towards its parents. Return True once the top-level item is done.

    A parent that has all its items is itself a finished item. An indefinite-length parent only
    ends at its break byte.
    """
    while stack and stack[-1].remaining != _INDEFINITE:
        stack[-1].remaining -= 1
        if stack[-1].remaining:
            return False
        stack.pop()
    return not stack


def cbor_item_length(data: bytes, max_depth: int = MAX_CBOR_DEPTH) -> int:
    """Return the length in bytes of the first CBOR item in `data`, refusing anything unsafe.

    Raises:
        ValueError: If the item is truncated or malformed, nests deeper than `max_depth`, or
            uses a semantic tag (major type 6) anywhere inside it.

    """
    pos = 0
    stack: list[_Frame] = []

    while True:
        if pos >= len(data):
            raise ValueError("Truncated CBOR item")
        head = data[pos]
        pos += 1

        if head == _BREAK:
            if not stack or stack[-1].remaining != _INDEFINITE:
                raise ValueError("Unexpected CBOR break")
            stack.pop()
            finished = True
        else:
            pos, opened = _read_item(data, pos, head, stack)
            if len(stack) > max_depth:
                raise ValueError("CBOR nested too deeply")
            finished = not opened

        if finished and _finish_item(stack):
            return pos
