"""Encoder/transaction regression tests; no running printer or database required."""

import io
import math
from unittest.mock import AsyncMock, MagicMock

import cbor2
import pytest
from fastapi import HTTPException

from spoolman.api.v1 import writer
from spoolman.openprinttag import cbor, make_image

UID = "E004010866073075"
MAIN = {
    "material_class": "FFF",
    "material_type": "PLA",
    "material_name": "Phoenix test",
    "brand_name": "ODIN",
    "density": 1.24,
    "filament_diameter": 1.75,
    "primary_color": "#C7BE5C",
    "nominal_netto_full_weight": 1000,
}


def test_image_roundtrip_regions_and_alignment():
    image, preview = make_image(MAIN, {"consumed_weight": 12.5}, UID)
    assert len(image) == 316
    assert image[:4] == bytes.fromhex("e1402701")
    assert int.from_bytes(image[6:8], "big") == 307
    assert image[-1] == 254
    mime_len = image[9]
    start = 8 + 6 + mime_len
    stream = io.BytesIO(image[start:-1])
    meta = cbor2.load(stream)
    main = cbor2.load(stream)
    assert (start + meta[2]) % 4 == 0
    assert main[8] == 0
    assert main[10] == "Phoenix test"
    assert cbor2.loads(image[start + meta[2] : -1])[0] == 12.5
    assert preview["main"]["instance_uuid"] == make_image(MAIN, {}, UID)[1]["main"]["instance_uuid"]
    assert preview["main"]["instance_uuid"] != make_image(MAIN, {}, "E004010866073076")[1]["main"]["instance_uuid"]


@pytest.mark.parametrize(
    "value", [0, -1, 23, 24, 255, 256, 65536, 2**63, True, False, 1.24, 1.75, "è", b"abc", [1, 2], {8: 0, 10: "PLA"}]
)
def test_cbor_independent_decoder(value: object):
    decoded = cbor2.loads(cbor(value))
    if isinstance(value, float):
        assert abs(decoded - value) < 0.001
    else:
        assert decoded == value


@pytest.mark.parametrize(
    "change",
    [
        {"density": float("nan")},
        {"filament_diameter": -1},
        {"material_class": "SLA"},
        {"unknown": 1},
        {"material_name": "x" * 64},
        {"primary_color": "#zzzzzz"},
        {"write_protection": "irreversible"},
        {"min_bed_temperature": 90, "max_bed_temperature": 40},
        {"gtin": True},
        {"country_of_origin": "Italia"},
        {"nominal_netto_full_weight": -1},
        {"max_print_temperature": 2**32},
        {"density": 1e40},
        {"brand_name": "a\0b"},
    ],
)
def test_reject_invalid(change: dict):
    with pytest.raises(ValueError, match=r".+"):
        make_image(dict(MAIN, **change), {}, UID)


def test_no_silent_truncation():
    huge = dict(
        MAIN,
        material_name="x" * 63,
        brand_name="b" * 63,
        brand_specific_instance_id="i" * 16,
        brand_specific_package_id="p" * 16,
        brand_specific_material_id="m" * 16,
        primary_color_ral="12345678",
    )
    # Multiple independent string fields are allowed but their sum must fit.
    huge.update(
        instance_uuid="00000000-0000-0000-0000-000000000001",
        material_uuid="00000000-0000-0000-0000-000000000002",
        brand_uuid="00000000-0000-0000-0000-000000000003",
        package_uuid="00000000-0000-0000-0000-000000000004",
    )
    with pytest.raises(ValueError, match=r".+"):
        make_image(huge, {}, UID)


@pytest.mark.asyncio
async def test_commit_only_links_verified_and_never_replays(monkeypatch: pytest.MonkeyPatch):
    bridge = AsyncMock(return_value={"verified": True, "status": "verified", "uid": UID})
    link = AsyncMock()
    monkeypatch.setattr(writer, "bridge", bridge)
    monkeypatch.setattr(writer, "check_holder", AsyncMock())
    monkeypatch.setattr(writer.spool, "get_by_id", AsyncMock())
    monkeypatch.setattr(writer.tag, "link_spool", link)
    for verified in (False, True):
        writer.state["session"] = {
            "id": "a" * 48,
            "bridge": "b" * 48,
            "uid": UID,
            "spool_id": 1,
            "image": "00" * 316,
            "blank": True,
            "expires": math.inf,
            "state": "prepared",
        }
        bridge.return_value = {"verified": verified, "status": "verified" if verified else "verify_failed", "uid": UID}
        link.reset_mock()
        result = await writer.commit(writer.Commit(session="a" * 48), None)
        assert result["linked"] == verified
        assert link.await_count == int(verified)
        with pytest.raises(HTTPException):
            await writer.commit(writer.Commit(session="a" * 48), None)
    writer.state["session"] = None


@pytest.mark.asyncio
async def test_link_failure_reported_separately(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(writer, "bridge", AsyncMock(return_value={"verified": True, "status": "verified", "uid": UID}))
    monkeypatch.setattr(writer, "check_holder", AsyncMock())
    monkeypatch.setattr(writer.spool, "get_by_id", AsyncMock())
    monkeypatch.setattr(writer.tag, "link_spool", AsyncMock(side_effect=RuntimeError("db unavailable")))
    writer.state["session"] = {
        "id": "a" * 48,
        "bridge": "b" * 48,
        "uid": UID,
        "spool_id": 1,
        "image": "00" * 316,
        "blank": True,
        "expires": math.inf,
        "state": "prepared",
    }
    result = await writer.commit(writer.Commit(session="a" * 48), None)
    assert result["verified"]
    assert not result["linked"]
    assert writer.state["session"]["state"] == "link_failed"
    writer.state["session"] = None


@pytest.mark.asyncio
async def test_reserve_before_tag_and_validate_before_io(monkeypatch: pytest.MonkeyPatch):
    remote = AsyncMock(return_value={"session": "b" * 48})
    monkeypatch.setattr(writer, "state", {})
    monkeypatch.setattr(writer, "bridge", remote)
    monkeypatch.setattr(writer.spool, "get_by_id", AsyncMock())
    monkeypatch.setattr(writer, "check_holder", AsyncMock())
    session = (await writer.open_session(writer.Open(spool_id=1), None))["session"]
    remote.assert_awaited_once_with("arm", {})
    with pytest.raises(HTTPException):
        await writer.open_session(writer.Open(spool_id=2), None)
    remote.reset_mock()
    with pytest.raises(HTTPException):
        await writer.prepare(writer.Prepare(session=session, spool_id=1, main={"unknown": 1}), None)
    remote.assert_not_awaited()
    remote.return_value = {"uid": UID, "blank": True}
    result = await writer.prepare(writer.Prepare(session=session, spool_id=1, main=MAIN), None)
    assert result["uid"] == UID
    assert writer.state["session"]["state"] == "prepared"
    with pytest.raises(HTTPException):
        await writer.commit(writer.Commit(session="c" * 48), None)
    assert remote.await_count == 1
    await writer.cancel(writer.Commit(session=session))
    assert writer.state["session"] is None


@pytest.mark.asyncio
async def test_occupied_tag_requires_confirmation_before_bridge(monkeypatch: pytest.MonkeyPatch):
    remote = AsyncMock()
    monkeypatch.setattr(writer, "bridge", remote)
    monkeypatch.setattr(
        writer,
        "state",
        {
            "session": {
                "id": "a" * 48,
                "state": "prepared",
                "expires": math.inf,
                "blank": False,
            }
        },
    )
    with pytest.raises(HTTPException, match="Confirm replacement"):
        await writer.commit(writer.Commit(session="a" * 48), None)
    remote.assert_not_awaited()
    assert writer.state["session"]["state"] == "prepared"


@pytest.mark.asyncio
async def test_erase_writes_zeros_unlinks_only_after_verification(monkeypatch: pytest.MonkeyPatch):
    bridge = AsyncMock()
    unlink = AsyncMock()
    link = AsyncMock()
    monkeypatch.setattr(writer, "state", {})
    monkeypatch.setattr(writer, "bridge", bridge)
    monkeypatch.setattr(writer, "check_holder", AsyncMock())
    monkeypatch.setattr(writer.spool, "get_by_id", AsyncMock())
    monkeypatch.setattr(writer.tag, "unlink_spool", unlink)
    monkeypatch.setattr(writer.tag, "link_spool", link)
    holder = MagicMock(spec=writer.DBSpool)
    holder.id = 1
    monkeypatch.setattr(writer.tag, "find_by_uid", AsyncMock(return_value=holder))
    for verified in (False, True):
        unlink.reset_mock()
        writer.state["session"] = {"id": "a" * 48, "bridge": "b" * 48, "spool_id": 1, "state": "editing"}
        bridge.return_value = {"uid": UID, "blank": False}
        prepared = await writer.prepare_erase(writer.PrepareErase(session="a" * 48, spool_id=1), None)
        assert prepared["erase"] is True
        assert writer.state["session"]["image"] == "00" * 316  # the firmware's erase image
        bridge.return_value = {"verified": verified, "status": "verified" if verified else "verify_failed", "uid": UID}
        result = await writer.commit(writer.Commit(session="a" * 48, replace=True), None)
        assert unlink.await_count == int(verified)
        link.assert_not_awaited()
        if verified:
            assert result["erased"]
            assert result["unlinked"]
            assert writer.state["session"]["state"] == "erased"
        else:
            assert not result["verified"]
    writer.state["session"] = None


@pytest.mark.asyncio
async def test_erase_needs_replace_for_a_tag_with_data_and_refuses_foreign_tags(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(writer, "state", {})
    monkeypatch.setattr(writer, "bridge", AsyncMock(return_value={"uid": UID, "blank": False}))
    monkeypatch.setattr(writer.spool, "get_by_id", AsyncMock())
    monkeypatch.setattr(writer, "check_holder", AsyncMock())
    writer.state["session"] = {"id": "a" * 48, "bridge": "b" * 48, "spool_id": 1, "state": "editing"}
    await writer.prepare_erase(writer.PrepareErase(session="a" * 48, spool_id=1), None)
    with pytest.raises(HTTPException):
        await writer.commit(writer.Commit(session="a" * 48, replace=False), None)
    writer.state["session"] = {"id": "a" * 48, "bridge": "b" * 48, "spool_id": 1, "state": "editing"}
    monkeypatch.setattr(writer, "check_holder", AsyncMock(side_effect=HTTPException(409, "other spool")))
    with pytest.raises(HTTPException):
        await writer.prepare_erase(writer.PrepareErase(session="a" * 48, spool_id=1), None)
    assert writer.state["session"]["state"] == "editing"
    writer.state["session"] = None


@pytest.mark.asyncio
async def test_unlink_failure_after_erase_reported_separately(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(writer, "bridge", AsyncMock(return_value={"verified": True, "status": "verified", "uid": UID}))
    monkeypatch.setattr(writer, "check_holder", AsyncMock())
    monkeypatch.setattr(writer.spool, "get_by_id", AsyncMock())
    monkeypatch.setattr(writer.tag, "find_by_uid", AsyncMock(side_effect=RuntimeError("db unavailable")))
    writer.state["session"] = {
        "id": "a" * 48,
        "bridge": "b" * 48,
        "uid": UID,
        "spool_id": 1,
        "image": "00" * 316,
        "blank": False,
        "expires": math.inf,
        "state": "prepared",
        "erase": True,
    }
    result = await writer.commit(writer.Commit(session="a" * 48, replace=True), None)
    assert result["verified"]
    assert result["erased"]
    assert not result["unlinked"]
    assert writer.state["session"]["state"] == "unlink_failed"
    writer.state["session"] = None
