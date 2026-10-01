"""Opt-in Phoenix OpenPrintTag writer. Browser never receives the bridge credential."""

import asyncio
import logging
import os
import secrets
import time
from http import HTTPStatus
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from spoolman.api.v1.models import Spool
from spoolman.database import spool, tag
from spoolman.database.database import get_db_session
from spoolman.database.models import Spool as DBSpool
from spoolman.openprinttag import CATALOG, make_image

router = APIRouter(prefix="/writer", tags=["OpenPrintTag writer"])
lock = asyncio.Lock()
# One configured reader, one process: preserves exclusive ownership through association.
state: dict[str, Any] = {}
MIN_TOKEN_LENGTH = 32
logger = logging.getLogger(__name__)
DB = Annotated[AsyncSession, Depends(get_db_session)]


class Prepare(BaseModel):
    session: str = Field(min_length=32, max_length=64)
    spool_id: int = Field(gt=0)
    main: dict[str, Any]
    aux: dict[str, Any] = Field(default_factory=dict)


class Commit(BaseModel):
    session: str = Field(min_length=32, max_length=64)
    replace: bool = False


def configured() -> bool:
    """Return whether the operator configured an authenticated bridge."""
    return bool(
        os.environ.get("SPOOLMAN_WRITER_URL") and len(os.environ.get("SPOOLMAN_WRITER_TOKEN", "")) >= MIN_TOKEN_LENGTH
    )


async def bridge(action: str, data: dict[str, Any]) -> dict[str, Any]:
    """Send one authenticated request to the fixed bridge; never retry mutations."""
    if not configured():
        raise HTTPException(503, "Writer not configured")
    url = os.environ["SPOOLMAN_WRITER_URL"].rstrip("/")
    # Operator configuration only; never an address supplied by the browser.
    if not url.startswith(("http://", "https://")):
        raise HTTPException(503, "Invalid writer URL")
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                url + "/writer/" + action,
                json=data,
                headers={"Authorization": "Bearer " + os.environ["SPOOLMAN_WRITER_TOKEN"]},
            )
        result = response.json()
        if response.status_code != HTTPStatus.OK:
            raise HTTPException(409, result.get("error", "Writer refused request"))
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Writer unreachable or invalid response; do not repeat a commit blindly") from exc
    return result


async def check_holder(db: AsyncSession, uid: str, spool_id: int) -> None:
    """Refuse a UID owned by another inventory item."""
    holder = await tag.find_by_uid(db, uid)
    # get_by_id loads the spool's tags, so compare by identity of the target model.

    if holder is not None and (not isinstance(holder, DBSpool) or holder.id != spool_id):
        raise HTTPException(409, "This tag belongs to another spool or filament. Unlink it explicitly first.")


@router.get("/schema")
async def schema() -> dict[str, Any]:
    """Expose the field catalog without bridge credentials."""
    return {"enabled": configured(), "reader": "Phoenix USB", "capacity": 320, "fields": CATALOG}


@router.get("/defaults/{spool_id}")
async def defaults(spool_id: int, db: DB) -> dict[str, Any]:
    """Prefill known fields from the saved spool."""
    raw = Spool.from_db(await spool.get_by_id(db, spool_id)).model_dump()
    f = raw["filament"]
    main = {"material_class": "FFF"}
    mappings = {
        "name": "material_name",
        "density": "density",
        "diameter": "filament_diameter",
        "weight": "nominal_netto_full_weight",
        "settings_extruder_temp": "min_print_temperature",
        "settings_bed_temp": "min_bed_temperature",
    }
    for key, dest in mappings.items():
        if f.get(key) is not None:
            main[dest] = f[key]
    if f.get("vendor", {}):
        main["brand_name"] = f["vendor"]["name"]
    if f.get("color_hex"):
        main["primary_color"] = "#" + f["color_hex"].lstrip("#")
    options = next(x["options"] for x in CATALOG["main"] if x["name"] == "material_type")
    if f.get("material") in [o["value"] for o in options]:
        main["material_type"] = f["material"]
    if raw.get("initial_weight") is not None:
        main["actual_netto_full_weight"] = raw["initial_weight"]
    if raw.get("spool_weight") is not None:
        main["empty_container_weight"] = raw["spool_weight"]
    return {"main": main, "aux": {"consumed_weight": raw.get("used_weight", 0)}}


class Open(BaseModel):
    spool_id: int = Field(gt=0)


@router.post("/open")
async def open_session(body: Open, db: DB) -> dict[str, Any]:
    """Pause automatic processing before the user places any tag on the reader."""
    async with lock:
        if state.get("session") is not None:
            raise HTTPException(409, "Writer session already open. Recover it before proceeding.")
        await spool.get_by_id(db, body.spool_id)
        info = await bridge("arm", {})
        sid = secrets.token_hex(24)
        state["session"] = {"id": sid, "bridge": info["session"], "spool_id": body.spool_id, "state": "editing"}
        return {"session": sid}


@router.post("/prepare")
async def prepare(body: Prepare, db: DB) -> dict[str, Any]:
    """Bind a validated image to the reserved reader's snapshot."""
    async with lock:
        current = require_session(body.session)
        if current["state"] != "editing" or current["spool_id"] != body.spool_id:
            raise HTTPException(409, "Invalid session for this spool")
        await spool.get_by_id(db, body.spool_id)
        try:
            make_image(body.main, body.aux, "E004010000000000")
        except (ValueError, TypeError, OverflowError) as exc:
            raise HTTPException(422, str(exc)) from exc
        info = await bridge("prepare", {"session": current["bridge"]})
        uid = info["uid"]
        await check_holder(db, uid, body.spool_id)
        image, preview = make_image(body.main, body.aux, uid)
        current.update(
            uid=uid,
            image=image.hex(),
            expires=time.monotonic() + 50,
            state="prepared",
            blank=info["blank"],
            preview=preview,
        )
        return {"session": current["id"], "uid": uid, "blank": info["blank"], "expires_seconds": 50, "preview": preview}


@router.post("/commit")
async def commit(body: Commit, db: DB) -> dict[str, Any]:
    """Write once and associate only after full readback verification."""
    async with lock:
        current = require_session(body.session)
        if current["state"] != "prepared" or time.monotonic() > current["expires"]:
            raise HTTPException(409, "Preview expired or already consumed; cancel and inspect again")
        if not current["blank"] and not body.replace:
            raise HTTPException(409, "Confirm replacement of all existing tag data")
        await spool.get_by_id(db, current["spool_id"])
        await check_holder(db, current["uid"], current["spool_id"])
        current["state"] = "writing"  # consumed before external I/O
        try:
            result = await bridge(
                "commit", {"session": current["bridge"], "replace": body.replace, "image_hex": current["image"]}
            )
            if (
                result.get("verified") is not True
                or result.get("status") not in ("verified", "unchanged")
                or result.get("uid") != current["uid"]
            ):
                current["state"] = "failed"
                current["result"] = {"verified": False, "linked": False, "reader_result": result}
                return current["result"]
            # NFC cannot participate in a DB transaction. Report partial success explicitly.
            try:
                await tag.link_spool(db=db, spool_id=current["spool_id"], uid=current["uid"], tag_format="openprinttag")
            except Exception:
                logger.exception("Tag verified but database association failed")
                current["state"] = "link_failed"
                current["result"] = {
                    "verified": True,
                    "linked": False,
                    "uid": current["uid"],
                    "message": "Tag verified but association failed. Keep the tag aside and link this UID manually.",
                    "backup": result.get("backup"),
                }
                return current["result"]
            current["state"] = "done"
            current["result"] = {
                "verified": True,
                "linked": True,
                "uid": current["uid"],
                "spool_id": current["spool_id"],
                "backup": result.get("backup"),
            }
            return current["result"]
        except HTTPException:
            current["state"] = "unknown"
            current["result"] = {
                "verified": False,
                "linked": False,
                "message": "Outcome unknown. Inspect the tag before another write.",
            }
            raise


def require_session(sid: str) -> dict[str, Any]:
    """Resolve an opaque, single-session capability."""
    session = state.get("session")
    if session is None or not secrets.compare_digest(sid, session["id"]):
        raise HTTPException(404, "Unknown writer session")
    return session


@router.get("/session/{sid}")
async def status(sid: str) -> dict[str, Any]:
    """Recover a result after a lost HTTP response without rewriting."""
    current = require_session(sid)
    return {"state": current["state"], "result": current.get("result")}


@router.post("/cancel")
async def cancel(body: Commit) -> dict[str, Any]:
    """Release the reader after the browser acknowledges the result."""
    async with lock:
        current = require_session(body.session)
        await bridge("finish" if current["state"] == "done" else "cancel", {"session": current["bridge"]})
        state["session"] = None
        return {"status": "closed", "remove_tag": True}


class Reset(BaseModel):
    confirm: bool


@router.post("/reset")
async def reset(body: Reset) -> dict[str, Any]:
    """Explicit recovery after a lost page/server restart. Never interrupts an active commit."""
    if not body.confirm:
        raise HTTPException(400, "Explicit confirmation required")
    if lock.locked():
        raise HTTPException(409, "Operation in progress; wait for completion")
    async with lock:
        await bridge("reset", {})
        state["session"] = None
        return {"status": "closed", "remove_tag": True}
