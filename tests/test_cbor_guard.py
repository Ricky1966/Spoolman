"""Tests for the CBOR guard that runs before cbor2 sees tag memory.

The guard has no dependency on cbor2, so these build the CBOR by hand from hex.
"""

import pytest

from spoolman.tags.cbor_guard import MAX_CBOR_DEPTH, cbor_item_length


def shared_reference_bomb(levels: int) -> bytes:
    """Build a flat list of shareable arrays that each refer twice to the one before.

    This is the shape that expands exponentially at constant nesting depth (RFC 8746, tags 28 and
    29), ending in a malformed tag 260 whose error message cbor2 builds from `repr(value)`. It is
    only ever fed to the guard, never to cbor2: the point is that it is refused first.
    """
    steps = "".join(f"d81c82d81d{k - 1:02x}d81d{k - 1:02x}" for k in range(1, levels))
    return bytes.fromhex("9f" + "d81c8100" + steps + f"d9010481d81d{levels - 1:02x}" + "ff")


@pytest.mark.parametrize(
    ("hex_item", "length"),
    [
        pytest.param("a10001", 3, id="map"),
        pytest.param("a1000102", 3, id="only-the-first-item-is-measured"),
        pytest.param("a0", 1, id="empty-map"),
        pytest.param("bf0001ff", 4, id="indefinite-map"),
        pytest.param("9f0102ff", 4, id="indefinite-array"),
        pytest.param("7f61616162ff", 6, id="indefinite-text-two-chunks"),
        pytest.param("5f4101ff", 4, id="indefinite-bytes"),
        pytest.param("a2008301020301a0", 8, id="nested-array-and-empty-map"),
        pytest.param("f94200", 3, id="half-float"),
        pytest.param("fb3ff0000000000000", 9, id="double-float"),
        pytest.param("1b0000000000000001", 9, id="eight-byte-integer"),
        pytest.param("5818" + "00" * 24, 26, id="byte-string-with-one-byte-length"),
    ],
)
def test_untagged_cbor_is_measured(hex_item: str, length: int) -> None:
    assert cbor_item_length(bytes.fromhex(hex_item)) == length


@pytest.mark.parametrize(
    "hex_item",
    [
        pytest.param("d81c8101", id="shareable-tag-28"),
        pytest.param("a100d81c8101", id="tag-28-inside-a-map"),
        pytest.param("a100d81d00", id="shared-reference-tag-29"),
        pytest.param("9fd81d00ff", id="tag-29-inside-an-indefinite-array"),
        pytest.param("d9010446000000000000", id="tag-260"),
        pytest.param("c074323031332d30332d32315432303a30343a30305a", id="tag-0-date-string"),
        pytest.param("c100", id="tag-1-epoch"),
        pytest.param("d81c", id="tag-with-nothing-after-it"),
    ],
)
def test_semantic_tags_are_refused_wherever_they_appear(hex_item: str) -> None:
    with pytest.raises(ValueError, match="semantic tags"):
        cbor_item_length(bytes.fromhex(hex_item))


@pytest.mark.parametrize(
    "hex_item",
    [
        pytest.param("", id="empty"),
        pytest.param("a1", id="map-without-items"),
        pytest.param("a100", id="map-missing-its-value"),
        pytest.param("8201", id="array-missing-an-item"),
        pytest.param("ff", id="stray-break"),
        pytest.param("5f4101", id="indefinite-bytes-without-break"),
        pytest.param("7f4101ff", id="text-chunk-of-the-wrong-type"),
        pytest.param("7f7f6161ffff", id="nested-indefinite-string"),
        pytest.param("1c", id="reserved-additional-information"),
        pytest.param("a1ff", id="break-inside-a-definite-map"),
        pytest.param("5901", id="truncated-length"),
        pytest.param("59ffff00", id="byte-string-longer-than-the-data"),
        pytest.param("9bffffffffffffffff00", id="array-count-far-beyond-the-data"),
    ],
)
def test_truncated_or_malformed_cbor_raises_value_error(hex_item: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 -- any message; the contract is the type
        cbor_item_length(bytes.fromhex(hex_item))


def test_nesting_up_to_the_limit_is_accepted_and_deeper_is_not() -> None:
    assert cbor_item_length(bytes.fromhex("81" * MAX_CBOR_DEPTH + "00")) == MAX_CBOR_DEPTH + 1
    with pytest.raises(ValueError, match="deeply"):
        cbor_item_length(bytes.fromhex("81" * (MAX_CBOR_DEPTH + 1) + "00"))


@pytest.mark.parametrize("levels", [4, 14, 20])
def test_shared_reference_bomb_is_refused_immediately(levels: int) -> None:
    with pytest.raises(ValueError, match="semantic tags"):
        cbor_item_length(shared_reference_bomb(levels))
