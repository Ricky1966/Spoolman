"""Helper functions for interacting with tag database objects.

Kept out of `spool.py`, which is already the largest module in the tree, but following
its conventions exactly: `AsyncSession` first, exceptions from `spoolman.exceptions`,
and the websocket event emitted after the commit.

This module is the ONE place a UID gets normalized, on the way in and on every lookup.
Doing it in a Pydantic validator would read better but would only cover callers that
arrive over HTTP -- and the unique constraint is worthless if any other path (an
importer, a migration backfill, a future tag codec) can write a differently-shaped UID.

It is also the one place that decides what a tag points at. A tag identifies a spool or a
filament today, and `models.Tag` can address more than that -- including things that are
not rows at all, such as a location -- so `target_type` is set from the thing being linked
and every read here says which kind it found rather than assuming. The database has no
CHECK enforcing "exactly one target": no migration in this tree uses one and MySQL below
8.0.16 silently ignores them, so a single enforced write path is worth more than a
constraint that is real on three databases out of four.
"""

import logging
import math
from datetime import datetime

import sqlalchemy
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from spoolman.api.v1.models import EventType
from spoolman.database import filament, models, spool, vendor
from spoolman.exceptions import ItemCreateError, ItemNotFoundError, TagConflictError
from spoolman.tags import TARGET_FILAMENT, TARGET_SPOOL, normalize_format, normalize_uid
from spoolman.tags import decode as tag_decode

logger = logging.getLogger(__name__)

# The row kinds a tag can be linked to. Both carry a `tags` collection and an `id`, which is
# all the shared link and unlink paths below need from them.
Target = models.Spool | models.Filament


async def _get_tag_by_uid(db: AsyncSession, uid: str) -> models.Tag | None:
    """Get the tag with this exact normalized UID, if any. Hits the unique index."""
    stmt = sqlalchemy.select(models.Tag).where(models.Tag.uid == uid)
    return (await db.execute(stmt)).scalar_one_or_none()


def _target_type(target: Target) -> str:
    return TARGET_SPOOL if isinstance(target, models.Spool) else TARGET_FILAMENT


def _holds(tag: models.Tag, target: Target) -> bool:
    """Whether `tag` is linked to exactly this target.

    Compared on the foreign key for the target's own kind, so spool 7 and filament 7 are never
    mistaken for each other: the other kind's column is null on the tag.
    """
    held = tag.spool_id if isinstance(target, models.Spool) else tag.filament_id
    return held == target.id


async def _changed(target: Target) -> None:
    """Emit the target's ordinary `updated` event; its tags are part of its payload."""
    if isinstance(target, models.Spool):
        await spool.spool_changed(target, EventType.UPDATED)
    else:
        await filament.filament_changed(target, EventType.UPDATED)


def _conflict(uid: str, existing: models.Tag) -> TagConflictError:
    """Describe a UID that is already spoken for.

    The holder's id is included when there is one, which is what lets a client offer "move it
    here". A kind addressed by value (a location) has no id, and the message says what holds
    the tag instead.
    """
    if existing.spool_id is not None:
        return TagConflictError(
            f"Tag {uid} is already linked to spool {existing.spool_id}.",
            spool_id=existing.spool_id,
        )
    if existing.filament_id is not None:
        return TagConflictError(
            f"Tag {uid} is already linked to filament {existing.filament_id}.",
            filament_id=existing.filament_id,
        )
    return TagConflictError(f"Tag {uid} is already linked to {existing.target_type} {existing.target_value}.")


async def _link(db: AsyncSession, target: Target, uid: str, tag_format: str | None) -> models.Tag:
    """Link an already-normalized UID to a loaded spool or filament. See `link_spool`."""
    existing = await _get_tag_by_uid(db, uid)
    if existing is not None:
        if not _holds(existing, target):
            raise _conflict(uid, existing)
        if tag_format is not None and existing.format != tag_format:
            existing.format = tag_format
            await db.commit()
            await _changed(target)
        return existing

    tag = models.Tag(
        uid=uid,
        target_type=_target_type(target),
        format=tag_format,
        added=datetime.utcnow().replace(microsecond=0),
    )
    target.tags.append(tag)
    try:
        await db.commit()
    except IntegrityError:
        # Two clients linked the same UID at the same time and the unique index caught
        # the loser. The database, not the read above, is what makes "one tag, one thing"
        # true; report the winner the same way a sequential conflict is reported.
        await db.rollback()
        winner = await _get_tag_by_uid(db, uid)
        if winner is None:
            raise
        raise _conflict(uid, winner) from None

    await _changed(target)
    return tag


async def _unlink(db: AsyncSession, target: Target, uid: str) -> None:
    """Unlink an already-normalized UID from a loaded spool or filament. See `unlink_spool`."""
    for tag in target.tags:
        if tag.uid == uid:
            # delete-orphan on the relationship turns this into the DELETE.
            target.tags.remove(tag)
            break
    else:
        raise ItemNotFoundError(f"{_target_type(target).capitalize()} {target.id} has no tag with UID {uid}.")

    # Commit before notifying so the change is durable and visible to subsequent
    # requests; post-commit notification must be the last, infallible step.
    await db.commit()
    await _changed(target)


async def link_spool(
    *,
    db: AsyncSession,
    spool_id: int,
    uid: str,
    tag_format: str | None = None,
) -> models.Tag:
    """Link a physical tag to a spool.

    Re-linking a UID to the spool that already holds it is idempotent; if the request
    carries a format and the stored row has a different one, the stored one is refined,
    since a later scan generally knows more about the tag than the first one did.

    Args:
        db: Database session.
        spool_id: The spool to link the tag to.
        uid: The tag UID in any shape; normalized here.
        tag_format: Optional tag format name, e.g. "ntag".

    Returns:
        models.Tag: The linked tag.

    Raises:
        ItemNotFoundError: If no spool with that ID exists.
        TagConflictError: If the UID is already linked to anything else, spool or filament.
        ValueError: If the UID or format is not valid.

    """
    uid = normalize_uid(uid)
    tag_format = normalize_format(tag_format)
    return await _link(db, await spool.get_by_id(db, spool_id), uid, tag_format)


async def link_filament(
    *,
    db: AsyncSession,
    filament_id: int,
    uid: str,
    tag_format: str | None = None,
) -> models.Tag:
    """Link a physical tag to a filament. Same rules as `link_spool`, across both kinds.

    Raises:
        ItemNotFoundError: If no filament with that ID exists.
        TagConflictError: If the UID is already linked to anything else, spool or filament.
        ValueError: If the UID or format is not valid.

    """
    uid = normalize_uid(uid)
    tag_format = normalize_format(tag_format)
    return await _link(db, await filament.get_by_id(db, filament_id), uid, tag_format)


async def unlink_spool(*, db: AsyncSession, spool_id: int, uid: str) -> None:
    """Unlink a tag from a spool.

    Args:
        db: Database session.
        spool_id: The spool the tag is linked to.
        uid: The tag UID in any shape; normalized here.

    Raises:
        ItemNotFoundError: If the spool does not exist, or does not hold that tag.
        ValueError: If the UID is not valid.

    """
    uid = normalize_uid(uid)
    await _unlink(db, await spool.get_by_id(db, spool_id), uid)


async def unlink_filament(*, db: AsyncSession, filament_id: int, uid: str) -> None:
    """Unlink a tag from a filament. Same rules as `unlink_spool`.

    Raises:
        ItemNotFoundError: If the filament does not exist, or does not hold that tag.
        ValueError: If the UID is not valid.

    """
    uid = normalize_uid(uid)
    await _unlink(db, await filament.get_by_id(db, filament_id), uid)


async def find_by_uid(db: AsyncSession, uid: str) -> Target | None:
    """Find the spool or filament a tag UID is linked to, or None if the tag is unknown.

    Args:
        db: Database session.
        uid: The tag UID in any shape; normalized here.

    Returns:
        models.Spool | models.Filament | None: Whatever holds that tag.

    Raises:
        ValueError: If the UID is not valid.

    """
    tag = await _get_tag_by_uid(db, normalize_uid(uid))
    if tag is None:
        return None
    if tag.spool_id is not None:
        return await spool.get_by_id(db, tag.spool_id)
    if tag.filament_id is not None:
        return await filament.get_by_id(db, tag.filament_id)
    # A kind addressed by value (a location) resolves to no row, and nothing reads those yet.
    return None


async def _find_vendor_by_exact_name(db: AsyncSession, name: str) -> models.Vendor | None:
    """Find a vendor whose name matches exactly.

    `vendor.find`'s name filter is a substring match (it backs the vendor list's search
    box), which is the wrong tool for "does this vendor already exist" -- a substring hit on
    an unrelated, longer vendor name would silently attach an auto-created filament to it.
    """
    matches, _total = await vendor.find(db=db, name=name)
    for candidate in matches:
        if candidate.name == name:
            return candidate
    return None


def _check_finite(*values: float | None) -> None:
    """Refuse NaN and infinity: they are not storable, and not JSON either."""
    if any(v is not None and not math.isfinite(v) for v in values):
        raise ItemCreateError("Decoded tag contains a non-finite number.")


# Sane physical ranges for a decoded tag. A value outside them is a bad tag, and a huge or tiny one
# breaks the length math when the spool is read back: an infinite length made GET /spool fail for
# everyone.
_MIN_DIAMETER_MM = 0.5
_MAX_DIAMETER_MM = 5.0
_MIN_DENSITY_G_CM3 = 0.1
_MAX_DENSITY_G_CM3 = 20.0
_MAX_WEIGHT_G = 100_000.0  # nominal and measured net weight of a roll
_MAX_SPOOL_WEIGHT_G = 10_000.0  # the empty spool itself


def _check_range(name: str, value: float | None, low: float, high: float) -> None:
    """Refuse a value outside its physical range, naming the field in the error."""
    if value is not None and not low <= value <= high:
        raise ItemCreateError(f"Decoded {name} {value} is outside the supported range.")


def _same_measure(a: float | None, b: float | None) -> bool:
    """Compare two measurements with a tolerance.

    MySQL and MariaDB store these as single precision floats, so a 2.85 mm diameter read back from
    the database is not exactly 2.85, and an exact comparison would never match there.
    """
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-6)


def _same_color(a: str | None, b: str | None) -> bool:
    return (a or "").lower() == (b or "").lower()


async def _find_filament_for_product(
    db: AsyncSession,
    decoded: tag_decode.DecodedTag,
    vendor_item: models.Vendor | None,
    diameter: float,
) -> models.Filament | None:
    """Find the filament this product already has, so a new roll reuses it.

    A filament is one product: vendor, name, material, diameter, nominal weight and colour together.
    The tag's instance UUID identifies the roll and is deliberately not used here. Without a name or
    a material there is no product to match, so nothing is reused. Diameter and weight are compared
    with a tolerance, and colour case-insensitively, after the database has narrowed the candidates.
    """
    if decoded.name is None or decoded.material is None:
        return None
    stmt = sqlalchemy.select(models.Filament).where(
        models.Filament.name == decoded.name,
        models.Filament.material == decoded.material,
        models.Filament.vendor_id == (vendor_item.id if vendor_item is not None else None),
    )
    for candidate in (await db.execute(stmt)).scalars().all():
        if (
            _same_measure(candidate.diameter, diameter)
            and _same_measure(candidate.weight, decoded.weight)
            and _same_color(candidate.color_hex, decoded.color_hex)
        ):
            return candidate
    return None


async def _add_tag_for_spool(
    db: AsyncSession,
    spool_item: models.Spool,
    uid: str,
    tag_format: str | None,
) -> None:
    """Link a UID to a new spool without committing: the create commits it together with the rest.

    A UID that is already linked is a conflict. Two requests racing for the same UID are caught by
    the unique index at flush time; the caller turns that into the same conflict.
    """
    existing = await _get_tag_by_uid(db, uid)
    if existing is not None:
        raise _conflict(uid, existing)
    tag = models.Tag(
        uid=uid,
        target_type=_target_type(spool_item),
        format=tag_format,
        added=datetime.utcnow().replace(microsecond=0),
    )
    spool_item.tags.append(tag)
    await db.flush()


def _validate_decoded(decoded: tag_decode.DecodedTag, density: float, diameter: float) -> None:
    """Refuse everything the database must never see, before the first write.

    That is non-finite numbers, values outside their physical range, and values the response models
    would reject: a value the codec accepts but the API cannot serve would break GET /filament and
    GET /spool for everyone until someone removed it by hand.
    """
    _check_finite(density, diameter, decoded.weight, decoded.initial_weight, decoded.spool_weight)
    _check_range("diameter", diameter, _MIN_DIAMETER_MM, _MAX_DIAMETER_MM)
    _check_range("density", density, _MIN_DENSITY_G_CM3, _MAX_DENSITY_G_CM3)
    _check_range("weight", decoded.weight, 0.0, _MAX_WEIGHT_G)
    _check_range("measured weight", decoded.initial_weight, 0.0, _MAX_WEIGHT_G)
    _check_range("spool weight", decoded.spool_weight, 0.0, _MAX_SPOOL_WEIGHT_G)

    try:
        # Local imports: the API modules import this one, so importing them at module level
        # would be circular.
        from spoolman.api.v1.filament import FilamentParameters  # noqa: PLC0415
        from spoolman.api.v1.spool import SpoolParameters  # noqa: PLC0415
        from spoolman.api.v1.vendor import VendorParameters  # noqa: PLC0415

        if decoded.vendor_name:
            VendorParameters(name=decoded.vendor_name)
        FilamentParameters(
            density=density,
            diameter=diameter,
            material=decoded.material,
            name=decoded.name,
            weight=decoded.weight,
            spool_weight=decoded.spool_weight,
            color_hex=decoded.color_hex,
            article_number=decoded.article_number,
            settings_extruder_temp=decoded.extruder_temp,
            settings_bed_temp=decoded.bed_temp,
        )
        SpoolParameters(filament_id=1, initial_weight=decoded.initial_weight, spool_weight=decoded.spool_weight)
    except ValidationError as e:
        raise ItemCreateError(f"Decoded tag values are not storable: {e}") from e


async def _write_spool_and_tag(
    db: AsyncSession,
    decoded: tag_decode.DecodedTag,
    uid: str,
    tag_format: str | None,
    density: float,
    diameter: float,
) -> tuple[models.Spool, models.Vendor | None, models.Filament | None]:
    """Write vendor, filament, spool and tag link in the current transaction, and commit them together.

    Returns the spool and the vendor and filament this call created, if it created them, so the caller
    can announce them once the commit has gone through.
    """
    vendor_item = None
    new_vendor = None
    if decoded.vendor_name:
        vendor_item = await _find_vendor_by_exact_name(db, decoded.vendor_name)
        if vendor_item is None:
            vendor_item = await vendor.create(db=db, name=decoded.vendor_name, commit=False)
            new_vendor = vendor_item

    filament_item = await _find_filament_for_product(db, decoded, vendor_item, diameter)
    new_filament = None
    if filament_item is None:
        filament_item = await filament.create(
            db=db,
            density=density,
            diameter=diameter,
            material=decoded.material,
            name=decoded.name,
            vendor_id=vendor_item.id if vendor_item is not None else None,
            weight=decoded.weight,
            spool_weight=decoded.spool_weight,
            article_number=decoded.article_number,
            color_hex=decoded.color_hex,
            settings_extruder_temp=decoded.extruder_temp,
            settings_bed_temp=decoded.bed_temp,
            commit=False,
        )
        new_filament = filament_item

    spool_item = await spool.create(
        db=db,
        filament_id=filament_item.id,
        initial_weight=decoded.initial_weight,
        spool_weight=decoded.spool_weight,
        commit=False,
    )
    await _add_tag_for_spool(db, spool_item, uid, tag_format)
    await db.commit()
    return spool_item, new_vendor, new_filament


async def create_spool_from_decoded_tag(
    *,
    db: AsyncSession,
    uid: str,
    tag_format: str | None,
    decoded: tag_decode.DecodedTag,
) -> models.Spool:
    """Create a spool from a decoded tag and link the tag to it, reusing the filament if it exists.

    Only ever called for a UID that `find_spool_by_uid` just reported as unknown, from an
    opt-in path (see spoolman/api/v1/tag.py) -- this function does not check that itself, so
    a caller that skips the check can create a duplicate spool for an already-linked tag.

    The vendor, filament, spool and tag link are written in one transaction: they are flushed as
    they are created and committed once at the end, so either all of them exist or none do. The
    websocket events are sent after that commit.

    The nominal weight belongs to the filament, the measured weight of this roll to the spool's
    initial weight, and the weight the tag says is already used is shown but never applied.

    Args:
        db: Database session.
        uid: The tag UID in any shape; normalized here.
        tag_format: The tag format that was decoded, e.g. "openprinttag".
        decoded: The decoded tag contents.

    Returns:
        models.Spool: The newly created spool, linked to the tag.

    Raises:
        ItemCreateError: If the decoded values are not storable. Nothing is written in that case.
        TagConflictError: If another request linked this UID first. Nothing this call wrote is
            kept, and callers should fall back to `find_spool_by_uid`.

    """
    uid = normalize_uid(uid)

    # Fall back to a default only when the tag does not carry the field. An explicit zero is not
    # a missing value: validation refuses it.
    density = decoded.density if decoded.density is not None else tag_decode.density_or_fallback(decoded.material)
    diameter = decoded.diameter if decoded.diameter is not None else 1.75
    _validate_decoded(decoded, density, diameter)

    try:
        spool_item, new_vendor, new_filament = await _write_spool_and_tag(
            db, decoded, uid, tag_format, density, diameter
        )
    except IntegrityError:
        # Another request linked this UID between our check and our flush. Nothing of ours was
        # committed, so the rollback is the whole cleanup.
        await db.rollback()
        winner = await _get_tag_by_uid(db, uid)
        if winner is None:
            raise
        raise _conflict(uid, winner) from None
    except BaseException:
        await db.rollback()
        raise

    # Only once the whole create is committed do websocket clients hear about it.
    if new_vendor is not None:
        await vendor.vendor_changed(new_vendor, EventType.ADDED)
    if new_filament is not None:
        await filament.filament_changed(new_filament, EventType.ADDED)
    await spool.spool_changed(spool_item, EventType.ADDED)
    return spool_item


async def find_spool_by_uid(db: AsyncSession, uid: str) -> models.Spool | None:
    """Find the spool a tag UID is linked to, or None if it's unknown or linked to a filament instead.

    A thin, correctly-typed wrapper over `find_by_uid` for callers that only ever deal in
    spools -- the auto-create flow above, which never links a tag to a filament.

    Args:
        db: Database session.
        uid: The tag UID in any shape; normalized here.

    Returns:
        models.Spool | None: The spool this tag is linked to, if any.

    Raises:
        ValueError: If the UID is not valid.

    """
    target = await find_by_uid(db, uid)
    return target if isinstance(target, models.Spool) else None
