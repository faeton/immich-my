# TODO

Explicit backlog for work that is **not shipped yet**.

Use this file as the quick "what's next / what still matters" list.
Use [PLAN.md](./PLAN.md) for the longer phased build narrative and acceptance
criteria.

## Active

### Phase 2c residuals

- Improve bloat/transcode UX beyond the current CLI flow.
- Add better sampling / before-after review for candidate transcodes.
- Tighten catalog identity guarantees after `--apply` on real libraries.

### Phase 3 — Proxy-first AI enrichment

Not shipped yet.

- Whisper transcripts for video proxies
  - run on the Mac
  - write `.srt` sidecars
  - optionally append transcript excerpts into searchable metadata
- Captioner worker
  - `moondream2` or BLIP-based caption generation
  - append `AI:` descriptions without overwriting human-written text
- Transcript / caption search integration
  - searchable without needing the original file online
- Job queue + resumability for enrichment workers
  - keyed by `(checksum, worker, version)`
  - safe to resume after crash / sleep / disconnect

### Phase 4 — Event clustering

Not shipped yet.

- Nightly clustering on `(time, lat, lon)`
- Album naming from reverse geocoding
- Idempotent album create/update flow

## Planned

### Phase 5 — Metadata gap-fill UI

- Small sidecar web UI for missing GPS / timestamp groups
- Group-level apply flow instead of per-asset edits
- Keep XMP sidecars and Immich metadata in sync

### Phase 6 — Ghost assets

- Keep offline originals searchable
- Friendly offline/original-unavailable state
- Automatic resurrection on remount

### Quality of life

- Cross-device near-duplicate reporting
- Export-to-edit workflows
- Backup automation
- Apple Photos people-name seeding

## Notes

- "Shipped" means implemented in `immy` and tested.
- If something is only described in architecture docs but missing here, add it.
- If something lands, remove it from here and keep the implementation detail in
  [PLAN.md](./PLAN.md) and the relevant code/docs.
