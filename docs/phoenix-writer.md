# Phoenix OpenPrintTag writer (experimental)

## ⚠️ Disclaimer: use at your own risk

This writer is an experimental hobby project, provided **"as is"**, without warranty of any kind. It means flashing firmware, wiring electronics, writing to NFC tags, sending commands to a printer's firmware (Klipper/Moonraker) and installing software on your computers. Mistakes, bugs or incompatibilities can cause failed prints, damage to your printer(s), your computer(s), the ESP32/PN5180 module, the tags or any other hardware or software, loss of data, or other harm.

By building, installing, running or modifying anything here you accept that you do so **entirely at your own risk**. The author and the contributors are **not responsible or liable** for any damage or loss of any kind: printers, PCs, hardware, software, data or anything else. Test carefully, never leave a printer unattended, back up your configuration before changing it, and read the code before you run it. The warranty disclaimer of the AGPL-3.0 licence (sections 15 and 16) applies as well.

*In italiano: progetto sperimentale da usare **a proprio rischio e pericolo**. L'autore non risponde di alcun danno a stampanti, computer, hardware, software o dati.*

This fork adds **Scrivi tag** to the Tags section of a saved spool in the Svelte
client. Create/save the spool first. The legacy React client is unchanged.

## What is included

- A form driven by the pinned official field catalog in
  `spoolman/openprinttag_data/fields.json` (source revision and MIT license alongside it).
  Initial support is FFF. SLA fields and hardware write protection are not supported.
- Defaults from the saved spool, including manufacturer, material, diameter,
  density, color, full weights and consumed weight. Optional fields remain empty
  unless supplied. Editing the form changes the tag payload, not the saved spool.
- All supported official field types: numbers, integers, booleans, strings, enums,
  enum arrays, UUID, UTC Unix timestamps, RGB/RGBA and LAB. Array fields accept JSON.
  The physical memory is finite: including every optional field at once is not
  possible. Oversize regions are rejected; fields are never silently discarded.
- A 316-byte NFC-V image (SLIX2 blocks 0-78; block 79 is a read-only counter): CC, NDEF MIME record, metadata, main region and a
  block-aligned auxiliary region. The main region has 234 bytes and aux 35 bytes.
  A deterministic UUIDv5 based on the physical UID is used when instance_uuid is
  blank. Explicit UUIDs are accepted; do not copy a package identity to another spool.
- A reader reservation made **before placing a tag**, which pauses the daemon's
  normal USB/WiFi scan processing. The paired browser reader setting is for scan
  navigation, not hardware write routing: this first version has ONE operator-
  configured Phoenix USB writer. The modal suppresses automatic browser navigation.
- UID preview, memory usage, a 50-second confirmation window and explicit full
  replacement consent for occupied tags. The bridge saves their previous bytes
  before committing. Only firmware-verified results are associated with the spool.
- Lost/failed write responses are never automatically retried. Database association
  failure after a successful physical write is reported separately. A tag already
  linked to another inventory item must be explicitly unlinked before writing.

This operation is **full initialization/replacement**, not an auxiliary-region
update. Existing unknown data may be replaced only with explicit consent. Normal
reading does not write weights back to the tag. NFC and the database cannot be one
atomic transaction: on an unknown result keep the tag aside and inspect it before
another write. On an association error, manually link the reported UID after
checking the verified tag. Do not re-scan it into automatic spool creation first.

## Dependencies and deployment for the first hardware test

Firmware from Phoenix-OpenTag PR #3 (including the fix that limits writing to SLIX2
user blocks 0-78, since block 79 is a read-only counter) has been tested on ESP32 +
PN5180 for writing, verification and interrupted-write recovery. This UI requires
also the **new host bridge**, branch `codex/spoolman-write-bridge` in
`Ricky1966/phoenix-opentag`. Copy all three host files together:

- `phoenix_opentag_daemon.py`
- `phoenix_opentag_write.py`
- `phoenix_writer_bridge.py`

The existing `--serial` by-id argument remains necessary. Do not run the manual
writer/serial monitor while the daemon owns the port. USB is used for writing even
when normal tag delivery is configured as WiFi. The ESP32 must run firmware that includes the 316-byte (blocks 0-78) fix;
older writer firmware fails at block 79.

Generate a long random credential locally (never commit it). Set the same value as:

- `PHOENIX_WRITER_TOKEN` in the Phoenix daemon's systemd environment;
- `SPOOLMAN_WRITER_TOKEN` in the Spoolman container environment.

Also configure:

- `SPOOLMAN_WRITER_URL=http://192.168.1.36:7913` (use the address reachable FROM the
  container; `localhost` inside a container is not Phoenix);
- optional daemon `PHOENIX_TAG_BACKUP_DIR`, default `~/phoenix-tag-backups`, writable
  by `biqu`. Backups are outside the Spoolman container and survive its replacement.

The feature is disabled without URL and a token of at least 32 characters.
Credentials stay server-side. The bridge rejects browser Origin headers and checks
Bearer authorization. Spoolman's existing same-origin/CSRF guards remain in place;
Spoolman itself relies on a trusted network or an authenticated reverse proxy.
Do not expose either service directly to the Internet. Use TLS between machines
on an untrusted network; no redirects are followed by the bridge client.

Use a **single Spoolman worker/process** (the image default). Reservations are in
memory, so multiple workers or replicas are not supported. If a page or service
restarts, remove the tag and use “Recupera una sessione rimasta aperta” → “Libera il
lettore”. This only releases the reservation, never confirms, restores or retries
an earlier write. A reset is rejected while a request is in progress.

Build the fork image locally from branch `codex/openprinttag-write-ui`:

```sh
# Node 22.12+ and npm are required to build both clients first.
npm --prefix client ci
npm --prefix client run build
npm --prefix client_v2 ci
npm --prefix client_v2 run build
docker build -t phoenix-spoolman:opentag .
```

Back up the existing Spoolman data and compose configuration before changing its
image to `phoenix-spoolman:opentag`. Keep its existing data volume and port mapping.
Add the environment variables above. This feature adds no database migration.
Do not replace the deployed container until the reviewed build and configuration
are ready. This development work does not deploy or restart anything on Phoenix.

## First test through the web UI

1. Use a test spool and a spare ICODE SLIX2 tag, not a production spool.
2. Open its detail panel → Tags → **Scrivi tag**. Wait for “Lettore riservato”.
3. Check the prefilled values; add optional fields. Place one tag on the reader.
4. Click “Leggi tag e prepara anteprima”; check UID and memory use.
5. For an occupied tag tick the explicit replacement checkbox; then confirm.
6. Keep the tag still during writing and verification. Wait for BOTH verified
   writing and successful association. Remove the tag, then close the dialog.
7. Re-scan to check that the existing spool is selected rather than duplicated.

## HTTP contract

Spoolman `/api/v1/writer`: `GET schema`, `GET defaults/{spool_id}`, `POST open`
(`spool_id`), `POST prepare` (`session`, `spool_id`, `main`, `aux`), `POST commit`
(`session`, `replace`), `GET session/{id}`, `POST cancel` (`session`), `POST reset`
(`confirm: true`). A session is consumed before the first commit I/O.

The daemon `/writer/{arm,prepare,commit,finish,cancel,reset}` requires the shared
Bearer token. Spoolman passes the full encoded image only server-to-server.
The serial worker is the only owner of the device and services a bounded command
queue; no second serial descriptor is opened by HTTP requests.

## Validation

- Backend unit tests cover CBOR decoding with independent cbor2, official field
  types, size limits and invalid values, no association on unverified writes,
  association failure and duplicate commit rejection.
- Host bridge tests cover reservation, explicit replacement, backup, disconnected
  serial writes, session identity and no automatic retries.
- Generated images are accepted by the existing firmware parser in a native test.
- Svelte type-check, lint and production build; browser end-to-end test with SQLite,
  the real HTTP bridge and an emulated serial firmware. Physical UI-to-Phoenix
  validation passed on 1 Oct 2026: write from the Spoolman UI, firmware verification,
  association to the spool and re-scan without a duplicate.

## Blank tag on the reader (offer to write)

Needs the `claude/blank-tag-flow` changes of Phoenix-OpenTag (firmware, daemon and bridge:
`PHOENIX-BLANK`, `/blank`, `write-resume`) and a configured writer (`SPOOLMAN_WRITER_URL` and
token).

When an empty ICODE SLIX2 tag is placed on the reader and the printer is idle, the daemon sends
Spoolman a scan with `format: "openprinttag-blank"` through the existing `/api/v1/tag/scan`
relay. Any open browser then shows **Tag vuoto rilevato**:

1. The spools without a tag are listed. If there is exactly one it is preselected; with several the
   user picks the spool the tag was stuck on; with none the dialog explains that the spool must be
   created first. A UID already linked to a spool (a wiped tag) offers only that spool.
2. **Scrivi su questa bobina** opens the normal write dialog with the reader already reserved and the
   preview prepared (no second tag placement). Nothing is written until **Conferma e scrivi tag**.
3. If the tag on the reader is not the one the prompt offered, confirmation is disabled.
4. After a verified write and association, closing the dialog makes the reader read the tag again
   (`write-resume`): the normal lookup runs and Moonraker activates the spool, with no need to
   remove the tag.

The daemon ignores the blank tag (no prompt) while Moonraker reports `printing`/`paused`,
`idle_timeout` `Printing` (G-code running: macros, heating, filament load/unload), a write session
is open, or Moonraker is unreachable. Only blank tags are offered: an occupied tag is never replaced
automatically.
