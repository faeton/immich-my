# Immich v2.7.5 Ingest Pipeline — Reference for Phase Y (Sidecar Pre-Ingest)

> Working reference for `immy` direct-write Y.1–Y.5. All citations are to
> `immich-app/immich@v2.7.5` on GitHub, and to the local `immich-accelerator`
> 1.4.8 brew package at `/opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/`.
>
> Source reading date: 2026-04-19. Anything flagged **GAP** was not pinned down
> and needs verification against a live install before we rely on it.

## Abstract — Path Forward

- **External-library asset rows are cheap to forge.** `checksum = sha1("path:" + absPath)`, `originalPath = absolute path as-seen-by-server`, `isExternal = true`, `libraryId` set, `checksumAlgorithm = 'sha1-path'`. No file-content hashing required, and uniqueness is `(ownerId, libraryId, checksum)`, so the path-hash IS the dedupe key. See §1, §2.
- **The pipeline is fully job-chained off `JobService.onDone()`**, not event-driven. `SidecarCheck → AssetExtractMetadata → AssetGenerateThumbnails → (SmartSearch + AssetDetectFaces + Ocr + [AssetEncodeVideo]) → FacialRecognition + AssetDetectDuplicates`. If we write rows *and* the derivative files *and* the `asset_job_status` timestamps, nothing re-runs unless the user clicks "Missing" in Jobs UI. See §4, §5.
- **`filterNewExternalAssetPaths` is the scan's only dedupe check** — it queries `asset.originalPath = $1 AND libraryId = $2 AND isExternal = true`. If we pre-insert an asset row with the exact `originalPath`, a subsequent scan skips it and queues **zero** jobs. That's our no-op guarantee. See §5.
- **Derivative paths are deterministic from `assetId` + `userId` + `MEDIA_LOCATION`**: `<MEDIA>/thumbs/<userId>/<id[0:2]>/<id[2:4]>/<id>_preview.jpeg`, `..._thumbnail.webp`, `<MEDIA>/encoded-video/<userId>/.../<id>.mp4`. We generate the UUIDv4 client-side, then write the DB row and the files under that id. See §3.
- **The edge that changes fastest is `asset_file` and ML tables** (9 migrations touching them in the last 3 months of 2.7.x); the `asset` / `asset_exif` core is stable. We pin to v2.7.5 and re-audit on every minor bump. `smart_search.embedding` dimension is **model-dependent and dynamically altered** when the CLIP model changes — do not hardcode 512. See §7.

---

## §1 — Schema reference for a new asset

For `/mnt/external/originals/trip/DSC_4182.JPG` (external library, 2,000 × 3,000 JPEG with GPS + faces), a fully-processed asset lands in these tables, in this FK order:

### 1.1 Insert order

```
1. asset                 (parent; generates id)
2. asset_exif            (FK -> asset.id, PK = assetId)
3. asset_file  x N       (FK -> asset.id, one row per derivative: preview, thumbnail, [fullsize])
4. smart_search          (FK -> asset.id, PK = assetId, holds CLIP embedding)
5. asset_face  x M       (FK -> asset.id, one per detected face, PK = UUID)
6. face_search x M       (FK -> asset_face.id, PK = faceId, holds arcface embedding)
7. person      x K       (FK -> users.id, created during clustering, PK = UUID)
   + UPDATE asset_face   (SET personId on clustered faces)
8. asset_job_status      (FK -> asset.id, PK = assetId; timestamps for each stage)
   (stacks / asset_stack — not part of automatic ingest, user-driven)
```

### 1.2 `asset` table

Source: `server/src/schema/tables/asset.table.ts` (see
[v2.7.5/server/src/schema/tables/asset.table.ts](https://github.com/immich-app/immich/blob/v2.7.5/server/src/schema/tables/asset.table.ts)).

| Column                | Type                       | NOT NULL | Default    | For our row                           |
|-----------------------|----------------------------|----------|------------|---------------------------------------|
| `id`                  | uuid (generated)           | ✓        | gen_uuid   | generate client-side (v4)             |
| `deviceAssetId`       | string                     | ✓        | —          | `basename(path).replaceAll(/\s+/, '')` |
| `ownerId`             | uuid (FK users)            | ✓        | —          | primary user UUID                      |
| `deviceId`            | string                     | ✓        | —          | `'Library Import'`                    |
| `type`                | `asset_type_enum`          | ✓        | —          | `'IMAGE'` or `'VIDEO'`                 |
| `originalPath`        | string                     | ✓        | —          | `/mnt/external/originals/trip/DSC_4182.JPG` (normalized) |
| `fileCreatedAt`       | timestamptz                | ✓        | —          | `stat.mtime` initially, overwritten by EXIF dateTimeOriginal |
| `fileModifiedAt`      | timestamptz                | ✓        | —          | `stat.mtime`                           |
| `isFavorite`          | bool                       | ✓        | false      | false                                  |
| `duration`            | varchar                    | —        | —          | `null` for images, `"HH:MM:SS.sss"` for videos |
| `checksum`            | bytea                      | ✓        | —          | SHA1 of `"path:" + originalPath` — **20 bytes, not hex/base64** |
| `checksumAlgorithm`   | `asset_checksum_algorithm_enum` | ✓  | —          | `'sha1-path'` for external, `'sha1'` for upload |
| `livePhotoVideoId`    | uuid (FK asset SET NULL)   | —        | —          | null                                   |
| `updatedAt`           | timestamptz                | ✓        | now()      | auto                                   |
| `createdAt`           | timestamptz                | ✓        | now()      | auto                                   |
| `originalFileName`    | string                     | ✓        | —          | `'DSC_4182.JPG'` (basename)            |
| `thumbhash`           | bytea                      | —        | null       | 25-byte thumbhash, set by thumb step   |
| `isOffline`           | bool                       | ✓        | false      | false                                  |
| `libraryId`           | uuid (FK library SET NULL) | —        | null       | `2dc3b5bd-...` for external            |
| `isExternal`          | bool                       | ✓        | false      | **true** for external library          |
| `deletedAt`           | timestamptz                | —        | null       | null                                   |
| `localDateTime`       | timestamptz                | ✓        | —          | `stat.mtime` initially, then EXIF      |
| `stackId`             | uuid (FK stack SET NULL)   | —        | null       | null unless user stacks                |
| `duplicateId`         | uuid                       | —        | null       | set by `AssetDetectDuplicates`         |
| `status`              | `asset_status_enum`        | ✓        | `active`   | `'active'`                             |
| `updateId`            | uuid (generated)           | ✓        | gen_uuid   | auto                                   |
| `visibility`          | `asset_visibility_enum`    | ✓        | `timeline` | `'timeline'`                           |
| `width`               | integer                    | —        | null       | set by thumb step (`fullsizeDimensions.width`) |
| `height`              | integer                    | —        | null       | set by thumb step                       |
| `isEdited`            | bool                       | ✓        | false      | false                                   |

**Uniqueness** (from lines 34–45 of the table def):

```typescript
// When libraryId IS NULL (upload assets):
@Index({ columns: ['ownerId','checksum'], unique: true, where: '("libraryId" IS NULL)' })

// When libraryId IS NOT NULL (external library assets):
@Index({ columns: ['ownerId','libraryId','checksum'], unique: true, where: '("libraryId" IS NOT NULL)' })

// Non-unique index on (originalPath, libraryId)
@Index({ columns: ['originalPath','libraryId'] })
```

That is: **for external-library assets, `(ownerId, libraryId, checksum)` is the uniqueness tuple.** Since we set `checksum = sha1("path:"+path)`, inserting the same path twice collides, and Immich's own scan computes the same value — so our pre-insert idempotently matches what the scanner would produce.

### 1.3 `asset_exif` table

Source: `server/src/schema/tables/asset-exif.table.ts`
([v2.7.5](https://github.com/immich-app/immich/blob/v2.7.5/server/src/schema/tables/asset-exif.table.ts)).

Table name: `asset_exif`. Primary key & FK: `assetId` (CASCADE on asset delete). One row per asset.

| Column            | Type          | NOT NULL | Notes                             |
|-------------------|---------------|----------|-----------------------------------|
| `assetId`         | uuid          | ✓        | PK, FK -> asset.id CASCADE         |
| `make`            | varchar       | —        |                                   |
| `model`           | varchar       | —        |                                   |
| `exifImageWidth`  | integer       | —        | original pixel width               |
| `exifImageHeight` | integer       | —        |                                   |
| `fileSizeInByte`  | int8          | —        |                                   |
| `orientation`     | varchar       | —        | EXIF orientation code ("1".."8") or textual |
| `dateTimeOriginal`| timestamptz   | —        |                                   |
| `modifyDate`      | timestamptz   | —        |                                   |
| `lensModel`       | varchar       | —        |                                   |
| `fNumber`         | double        | —        |                                   |
| `focalLength`     | double        | —        |                                   |
| `iso`             | integer       | —        |                                   |
| `latitude`        | double        | —        |                                   |
| `longitude`       | double        | —        |                                   |
| `city`            | varchar       | —        | indexed                            |
| `state`           | varchar       | —        |                                   |
| `country`         | varchar       | —        |                                   |
| `description`     | text          | ✓        | default `''`                       |
| `fps`             | double        | —        | video only                         |
| `exposureTime`    | varchar       | —        |                                   |
| `livePhotoCID`    | varchar       | —        | indexed, used to pair photo+video  |
| `timeZone`        | varchar       | —        |                                   |
| `projectionType`  | varchar       | —        | `'EQUIRECTANGULAR'` for 360 photos |
| `profileDescription` | varchar    | —        | ICC profile                        |
| `colorspace`      | varchar       | —        | `'sRGB'`, `'Display P3'`, etc.    |
| `bitsPerSample`   | integer       | —        |                                   |
| `autoStackId`     | varchar       | —        | indexed, grouping hint             |
| `rating`          | integer       | —        |                                   |
| `tags`            | varchar[]     | —        |                                   |
| `updatedAt`       | timestamptz   | ✓        | `clock_timestamp()` default        |
| `updateId`        | uuid          | ✓        | generated                          |
| `lockedProperties`| enum[]        | —        | per-column lock to prevent overwrite |

There's a GiST index on `ll_to_earth_public(latitude, longitude)` for geo queries.

**Minimum insert:** `{ assetId, description: '' }` is enough to satisfy NOT NULL. Everything else nullable. For a useful row we also want `dateTimeOriginal`, `timeZone`, `exifImageWidth/Height`, `fileSizeInByte`, `latitude/longitude`, `make/model`, and `orientation`.

### 1.4 `asset_file` table

Source: `server/src/schema/tables/asset-file.table.ts`
([v2.7.5](https://github.com/immich-app/immich/blob/v2.7.5/server/src/schema/tables/asset-file.table.ts)).

Table name: `asset_file`. One row per derivative. Unique composite: `(assetId, type, isEdited)`.

| Column            | Type              | NOT NULL | Notes                        |
|-------------------|-------------------|----------|------------------------------|
| `id`              | uuid (generated)  | ✓        | PK                           |
| `assetId`         | uuid              | ✓        | FK -> asset.id CASCADE       |
| `type`            | `AssetFileType` (`'fullsize'`,`'preview'`,`'thumbnail'`,`'sidecar'`,`'encoded_video'`) | ✓ | — |
| `path`            | string            | ✓        | absolute FS path              |
| `createdAt`       | timestamptz       | ✓        | generated                     |
| `updatedAt`       | timestamptz       | ✓        | generated                     |
| `updateId`        | uuid              | ✓        | generated, indexed            |
| `isEdited`        | bool              | ✓        | false                         |
| `isProgressive`   | bool              | ✓        | false                         |
| `isTransparent`   | bool              | ✓        | false                         |

**Note:** `encoded_video` as an `AssetFileType` is new in v2.7.5 (`1773242919341-EncodedVideoAssetFiles.ts`). Pre-2.7.5 used `asset.encodedVideoPath` column. The column is **gone** in 2.7.5 — transcode path is now `asset_file(type='encoded_video')`. Same for `sidecar` (`1764698859174-SidecarInAssetFile.ts`).

For our JPEG: **2 rows** — `preview` (JPEG 1440px) and `thumbnail` (WebP 250px). No `fullsize` unless user enabled it (default off). For a HEIC we'd add a `fullsize` JPEG. For a video, an `encoded_video` row points at the transcoded mp4 (only inserted when transcode actually runs — see §4.5).

### 1.5 `smart_search` table

Source: `server/src/schema/tables/smart-search.table.ts`
([v2.7.5](https://github.com/immich-app/immich/blob/v2.7.5/server/src/schema/tables/smart-search.table.ts)).

| Column     | Type          | NOT NULL | Notes                                   |
|------------|---------------|----------|-----------------------------------------|
| `assetId`  | uuid          | ✓        | PK, FK -> asset.id CASCADE               |
| `embedding`| `vector(N)`   | ✓        | N = dimension of configured CLIP model   |

HNSW index `clip_index` using `vector_cosine_ops`, `ef_construction = 300, m = 16`.

**Dimension is not fixed at 512.** The default model `ViT-B-32__openai` is 512. `ViT-L-14__openai` is 768. `ViT-SO400M-16-SigLIP2-384__webli` is 1152. `SmartInfoService.onConfigUpdate` reads `getDimensionSize('smart_search')` and calls `setDimensionSize(dim)` on mismatch, which `ALTER TABLE smart_search ALTER COLUMN embedding TYPE vector(N)` + re-indexes.

**For immy:** the accelerator defaults to `ViT-B-32__openai` (see local [ml/src/config.py:25](file:///opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/ml/src/config.py#L25)) → 512-dim. We should **read the current column type from Postgres at startup**, not hardcode. The MLX fallback in the accelerator also normalizes to 512 via ViT-B-32. **GAP**: verify `pgvecto-rs` accepts the literal string `'[0.1,0.2,...]'` on INSERT — it does for `pgvector`, and the compose file pins `tensorchord/pgvecto-rs:pg14-v0.2.0`.

### 1.6 `asset_face` and `face_search`

Source: `server/src/schema/tables/asset-face.table.ts`
([v2.7.5](https://github.com/immich-app/immich/blob/v2.7.5/server/src/schema/tables/asset-face.table.ts))
and `face-search.table.ts`
([v2.7.5](https://github.com/immich-app/immich/blob/v2.7.5/server/src/schema/tables/face-search.table.ts)).

`asset_face` columns:

| Column           | Type          | NOT NULL | Default               |
|------------------|---------------|----------|-----------------------|
| `id`             | uuid          | ✓        | generated             |
| `assetId`        | uuid          | ✓        | FK -> asset.id CASCADE |
| `personId`       | uuid          | —        | FK -> person.id SET NULL |
| `imageWidth`     | integer       | ✓        | 0                     |
| `imageHeight`    | integer       | ✓        | 0                     |
| `boundingBoxX1`  | integer       | ✓        | 0                     |
| `boundingBoxY1`  | integer       | ✓        | 0                     |
| `boundingBoxX2`  | integer       | ✓        | 0                     |
| `boundingBoxY2`  | integer       | ✓        | 0                     |
| `sourceType`     | `SourceType`  | ✓        | `'machine-learning'`  |
| `deletedAt`      | timestamptz   | —        |                       |
| `updatedAt`      | timestamptz   | ✓        |                       |
| `updateId`       | uuid          | ✓        |                       |
| `isVisible`      | bool          | ✓        | true                  |

Indexes: `(assetId, personId)` and `(personId, assetId)` WHERE `deletedAt IS NULL AND isVisible IS TRUE`.

`face_search`:

| Column      | Type          | NOT NULL | Notes                              |
|-------------|---------------|----------|------------------------------------|
| `faceId`    | uuid          | ✓        | PK, FK -> asset_face.id CASCADE     |
| `embedding` | `vector(512)` | ✓        | ArcFace 512-dim, HNSW `face_index`  |

ArcFace embedding dim is **fixed at 512** regardless of buffalo_s/m/l (all are 512-dim ArcFace variants). See local [ml/src/models/face_embed.py:22](file:///opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/ml/src/models/face_embed.py#L22): `ARCFACE_EMBEDDING_DIM = 512`.

### 1.7 `person` table

| Column         | Type          | NOT NULL | Default   |
|----------------|---------------|----------|-----------|
| `id`           | uuid          | ✓        | generated |
| `createdAt`    | timestamptz   | ✓        |           |
| `updatedAt`    | timestamptz   | ✓        |           |
| `ownerId`      | uuid          | ✓        | FK users CASCADE |
| `name`         | varchar       | ✓        | `''`      |
| `thumbnailPath`| varchar       | ✓        | `''`      |
| `isHidden`     | bool          | ✓        | false     |
| `birthDate`    | date          | —        | null      |
| `faceAssetId`  | uuid          | —        | FK asset_face.id SET NULL |
| `isFavorite`   | bool          | ✓        | false     |
| `color`        | varchar       | —        |           |
| `updateId`     | uuid          | ✓        | generated |

Created during `FacialRecognition` job — **not during detection**. Immich's clustering is cosine-distance + threshold (maxDistance 0.5, minFaces 3; see §4.4) — one `person` per cluster, `faceAssetId` points at one face for thumbnail.

### 1.8 `asset_job_status`

Source: `server/src/schema/tables/asset-job-status.table.ts`. PK + FK on `assetId`.

| Column                 | Type        | NOT NULL |
|------------------------|-------------|----------|
| `assetId`              | uuid        | ✓ (PK)   |
| `metadataExtractedAt`  | timestamptz | —        |
| `facesRecognizedAt`    | timestamptz | —        |
| `duplicatesDetectedAt` | timestamptz | —        |
| `ocrAt`                | timestamptz | —        |

**Critical for idempotency.** If any of these is null, the corresponding "Missing" queue in Jobs UI will re-queue the job. For a fully-processed no-op, set all four to `now()`.

**GAP**: There is no `previewAt`/`thumbnailAt` column here — the "Missing thumbnails" scan checks the presence of rows in `asset_file` with types `preview` and `thumbnail`. Confirm by running the Missing queue against a row with file rows present and seeing if it skips.

### 1.9 Stacks — user-driven, skip for Y

`stack` table has `id, createdAt, updatedAt, updateId, primaryAssetId (UNIQUE, FK asset), ownerId (FK users)`. **There is no `asset_stack` join table in 2.7.5.** Stack membership lives on `asset.stackId`. We don't need to touch this during ingest.

---

## §2 — Checksum + asset identity

### 2.1 Two algorithms

Source: `server/src/enum.ts` lines 54-57
([v2.7.5/server/src/enum.ts](https://github.com/immich-app/immich/blob/v2.7.5/server/src/enum.ts)):

```typescript
export enum ChecksumAlgorithm {
  sha1File = 'sha1',
  sha1Path = 'sha1-path',
}
```

**External library** (`LibraryService.processEntity`, lines 451-474 of
[library.service.ts](https://github.com/immich-app/immich/blob/v2.7.5/server/src/services/library.service.ts)):

```typescript
checksum: this.cryptoRepository.hashSha1(`path:${assetPath}`),
checksumAlgorithm: ChecksumAlgorithm.sha1Path,
```

**Upload** (`AssetMediaService.create`, around line 281):

```typescript
checksum: file.checksum,              // pre-computed from streamed SHA1
checksumAlgorithm: ChecksumAlgorithm.sha1File,
```

### 2.2 `hashSha1` returns raw binary (Buffer)

Source: `server/src/repositories/crypto.repository.ts` lines 47-49
([v2.7.5](https://github.com/immich-app/immich/blob/v2.7.5/server/src/repositories/crypto.repository.ts)):

```typescript
hashSha1(value: string | Buffer): Buffer {
  return createHash('sha1').update(value).digest();
}
```

**`.digest()` with no argument returns a Buffer of 20 raw bytes.** The Postgres column is `bytea` — so we insert the bytes directly, not hex or base64.

For our example path `/mnt/external/originals/trip/DSC_4182.JPG`:

```
shasum -a 1 <(printf 'path:%s' '/mnt/external/originals/trip/DSC_4182.JPG') | cut -c1-40
```

gives a 40-hex-char digest; we insert that as `decode('...', 'hex')` or pass Buffer over the wire.

`hashFile` (line 51) streams the file in chunks and returns a 20-byte Buffer — used for uploaded assets, not for external-library ingest.

### 2.3 Duplicate behaviour

- **External scan before any Immich logic** — `LibraryService.handleQueueSyncFiles` calls `filterNewExternalAssetPaths(libraryId, pathBatch)` which does:
  ```sql
  SELECT path FROM unnest($1::text[]) AS path
  WHERE NOT EXISTS (
    SELECT 1 FROM asset
    WHERE asset."originalPath" = path
      AND "libraryId" = $2::uuid
      AND "isExternal" = true
  )
  ```
  Lines 1067-1083 of [asset.repository.ts](https://github.com/immich-app/immich/blob/v2.7.5/server/src/repositories/asset.repository.ts). Paths already present are **silently dropped** from the batch.

- **Upload-path duplicate** — `AssetMediaService` catches the `ASSET_CHECKSUM_CONSTRAINT` index violation around line 228 and returns HTTP 200 with `{status: 'DUPLICATE', id: existingId}` (via `getUploadAssetIdByChecksum`).

- **Direct INSERT with duplicate checksum (our pre-insert scenario)** — the unique index `(ownerId, libraryId, checksum) WHERE libraryId IS NOT NULL` raises `23505 unique_violation` at the Postgres level. We must handle this as "already ingested, skip."

### 2.4 Recommendation for immy

For any external-library asset we're pre-ingesting:

```
checksum = sha1_raw("path:" + absolute_path_as_immich_sees_it)
```

Do **not** use SHA1 of file content (that's only for uploads). The server-side path matters — if Immich runs in Docker with `/mnt/external/originals` bind-mounted to `/ext/originals`, Immich stores and hashes `/ext/originals/...`, not the host path. See §6.

---

## §3 — Derivative file paths

Source: `server/src/cores/storage.core.ts`
([v2.7.5](https://github.com/immich-app/immich/blob/v2.7.5/server/src/cores/storage.core.ts)),
helper methods `getImagePath` (lines 103-107), `getEncodedVideoPath` (109-110),
`getNestedFolder` (273), `getNestedPath` (277), `getBaseFolder` (96),
`getFolderLocation` (88).

### 3.1 Folder layout

Root: `$IMMICH_MEDIA_LOCATION` (in docker-compose.yml → `/path/to/upload`
= `/usr/src/app/upload` inside the container by default).

```
<MEDIA>/
├── thumbs/<userId>/<id[0:2]>/<id[2:4]>/…         # thumbnails + previews + person thumbs
├── encoded-video/<userId>/<id[0:2]>/<id[2:4]>/…  # transcoded mp4
├── library/<userId>/<id[0:2]>/<id[2:4]>/…        # uploaded originals (NOT used for external)
├── upload/<userId>/<id[0:2]>/<id[2:4]>/…         # staging during upload
├── profile/<userId>/…                             # user avatars
└── backups/…
```

`StorageFolder` enum (`server/src/enum.ts` lines 418-424):

```typescript
enum StorageFolder {
  EncodedVideo = 'encoded-video',
  Library      = 'library',
  Upload       = 'upload',
  Profile      = 'profile',
  Thumbnails   = 'thumbs',
  Backups      = 'backups',
}
```

### 3.2 Thumbnail (grid view)

- Filename: `<assetId>_thumbnail.webp`
- Full path: `<MEDIA>/thumbs/<userId>/<id[0:2]>/<id[2:4]>/<assetId>_thumbnail.webp`
- Default size: **250 px on longest edge**, format **WebP**, quality **80**, `fit: 'inside'`, `withoutEnlargement: true`.
- Source: [`config.ts` lines 272-277](https://github.com/immich-app/immich/blob/v2.7.5/server/src/config.ts) + [`media.repository.ts` lines 140-148](https://github.com/immich-app/immich/blob/v2.7.5/server/src/repositories/media.repository.ts).

### 3.3 Preview (detail view)

- Filename: `<assetId>_preview.jpeg` (default) or `..._preview.webp`.
- Full path: `<MEDIA>/thumbs/<userId>/<id[0:2]>/<id[2:4]>/<assetId>_preview.jpeg`
- Default size: **1440 px on longest edge**, format **JPEG**, quality **80**, progressive `true` (JPEG only).
- Source: `config.ts` lines 278-283.

### 3.4 Fullsize (optional, default off)

- Filename: `<assetId>_fullsize.jpeg` — only generated when `image.fullsize.enabled = true` OR when the original is a RAW with an embedded JPEG preview that exceeds `preview.size`.
- Format **JPEG** by default, quality 80.
- For HEIC conversion: the extracted embedded JPEG is written to the fullsize path without transcode (`generateImageThumbnails` lines ~325 of media.service.ts — `createOrOverwriteFile(fullsizeFile.path, extracted.buffer)` then `writeExif` to copy orientation/colorspace).

### 3.5 Encoded video

- Filename: `<assetId>.mp4`
- Full path: `<MEDIA>/encoded-video/<userId>/<id[0:2]>/<id[2:4]>/<assetId>.mp4`
- FFmpeg defaults (config.ts lines 134-161): `targetVideoCodec=H264`, `targetAudioCodec=AAC`, `targetResolution=720`, `crf=23`, `preset=ultrafast`, `twoPass=false`, `accel=disabled`, `acceptedContainers=['mov','ogg','webm']`.
- The `StorageCore.getEncodedVideoPath(asset)`:
  ```ts
  return StorageCore.getNestedPath(StorageFolder.EncodedVideo, asset.ownerId, asset.id + '.mp4');
  ```

### 3.6 Person thumbnail (face crop)

- Filename: `<personId>.jpeg`
- Full path: `<MEDIA>/thumbs/<userId>/<personId[0:2]>/<personId[2:4]>/<personId>.jpeg`
- Generated by `JobName.PersonGenerateThumbnail` from the face's bounding box with extra padding; JPEG quality per default config.

### 3.7 Path construction helpers

```typescript
// storage.core.ts ~line 96
static getBaseFolder(folder: StorageFolder) {
  return join(StorageCore.getMediaLocation(), folder);
}

// ~line 88
static getFolderLocation(folder: StorageFolder, userId: string) {
  return join(StorageCore.getBaseFolder(folder), userId);
}

// ~line 273
static getNestedFolder(folder, userId, filename) {
  return join(
    StorageCore.getFolderLocation(folder, userId),
    filename.slice(0, 2),
    filename.slice(2, 4),
  );
}

// ~line 277
static getNestedPath(folder, userId, filename) {
  return join(StorageCore.getNestedFolder(folder, userId, filename), filename);
}

// ~line 103 — the canonical thumbnail/preview path
static getImagePath(asset, { fileType, format, isEdited, ... }) {
  const suffix = isEdited ? '_edited' : '';
  return StorageCore.getNestedPath(
    StorageFolder.Thumbnails,
    asset.ownerId,
    `${asset.id}_${fileType}${suffix}.${format}`,
  );
}
```

**For our JPEG with assetId `abc12345-...`:**
```
/usr/src/app/upload/thumbs/<userId>/ab/c1/abc12345-..._preview.jpeg
/usr/src/app/upload/thumbs/<userId>/ab/c1/abc12345-..._thumbnail.webp
```

---

## §4 — Processors: ML pipeline

Job orchestration lives in `JobService.onDone` (`server/src/services/job.service.ts` lines 58-148, [v2.7.5](https://github.com/immich-app/immich/blob/v2.7.5/server/src/services/job.service.ts)). On successful or skipped completion of each job, the switch queues the next stage:

```
SidecarCheck          → AssetExtractMetadata
SidecarWrite          → AssetExtractMetadata
StorageTemplateMigrationSingle → AssetGenerateThumbnails
AssetGenerateThumbnails → SmartSearch + AssetDetectFaces + Ocr (+ AssetEncodeVideo if video)
SmartSearch           → AssetDetectDuplicates
PersonGenerateThumbnail → (WebSocket ping only)
AssetEditThumbnailGeneration → (WebSocket ping only)
```

The external library scan queues `SidecarCheck` (`LibraryService.queuePostSyncJobs` line 476-487) which is the head of the chain — **metadata extraction does NOT queue thumbs directly**; the JobService onDone dispatch does that for us.

### 4.1 `metadata-extraction` (`JobName.AssetExtractMetadata`)

- **Tool:** `exiftool-vendored` (Perl exiftool bundled as npm package). Calls `metadataRepository.readTags(path)` and `extractBinaryTag(path, 'MPImage2')` for motion-photo extraction.
- **Version:** whatever `exiftool-vendored` pin is in `server/package.json` at the v2.7.5 commit (**GAP** — check `pnpm-lock.yaml`).
- **Invocation:** library call, not a subprocess we can match directly. For immy, we should use `exiftool-vendored` via Bun/Node OR run the local `exiftool` CLI and parse JSON output.
- **Writes** (via `MetadataService.handleMetadataExtraction` lines 275-373 of
  [metadata.service.ts](https://github.com/immich-app/immich/blob/v2.7.5/server/src/services/metadata.service.ts)):
  - `asset_exif`: UPSERT all 25+ columns listed in §1.3.
  - `asset`: UPDATE `duration`, `localDateTime`, `fileCreatedAt` (when `dateTimeOriginal` exists), `fileModifiedAt`, `width`, `height` (only when not edited).
  - `asset_job_status`: UPSERT `metadataExtractedAt = now()`.
- **Side effects:**
  - Motion-photo extraction: writes the embedded MP4 to disk as a new asset (`assetRepository.create`, new asset for the .mov), queues `AssetEncodeVideo` and optionally `AssetDelete` for the old linked video.
  - `linkLivePhotos()`: pairs photos + videos that share `livePhotoCID` via `asset.livePhotoVideoId`.
  - Tagged-face application: if face tags exist in XMP, queues `PersonGenerateThumbnail`.

### 4.2 `generate-thumbnail` (`JobName.AssetGenerateThumbnails`)

- **Tool:** `sharp` (npm, wraps libvips). Version pinned in package.json — accelerator notes `sharp@0.34.5` as known-good (see [`immich_accelerator/__main__.py:44`](file:///opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/immich_accelerator/__main__.py#L44)).
- **Invocation shape** (from `media.repository.ts` lines 140-148):
  ```typescript
  const pipeline = await this.getImageDecodingPipeline(input, options); // sharp(buffer).rotate().resize(...)
  await pipeline
    .toFormat(options.format, {
      quality: options.quality,
      chromaSubsampling: options.quality >= 80 ? '4:4:4' : '4:2:0',
      progressive: options.progressive,
    })
    .toFile(output);
  ```
- **Inputs:** decoded & rotated original buffer (respecting EXIF orientation and ICC colorspace).
- **Outputs:**
  - Preview file at `<MEDIA>/thumbs/<u>/<i1>/<i2>/<id>_preview.jpeg` (default).
  - Thumbnail file at `<MEDIA>/thumbs/<u>/<i1>/<i2>/<id>_thumbnail.webp`.
  - Optional fullsize at `..._fullsize.jpeg`.
  - `asset_file` UPSERT rows: `(assetId, type, isEdited, path, isProgressive, isTransparent)` with `onConflict (assetId,type,isEdited) DO UPDATE`.
  - `asset.thumbhash` = 25-byte Buffer (thumbhash lib's `rgbaToThumbHash` on a 100×100 RGBA downsample).
  - `asset.width` / `asset.height` = `fullsizeDimensions` (original decoded, pre-edit) if null.
- **Thumbhash:** `pipeline.resize(100,100,{fit:'inside', withoutEnlargement:true}).raw().ensureAlpha()` → `rgbaToThumbHash(w,h,data)`.

### 4.3 `smart-search` (`JobName.SmartSearch`) — CLIP

- **Model (default):** `ViT-B-32__openai`, 512-dim (config.ts line 210-213). Configurable.
- **Tool (Docker):** `immich-machine-learning` Python service, uses open_clip / PyTorch.
- **Tool (Mac accelerator):** the FastAPI service at `ml/src/main.py`, routes to MLX (preferred) or open_clip (fallback). See local [`ml/src/models/clip.py:279`](file:///opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/ml/src/models/clip.py#L279) — `get_clip_model` loads `mlx-community/clip-vit-base-patch32`.
- **Input resolution:** ViT-B-32 accepts 224×224, processor handles resize. Image is the **preview** file (`asset.files[0].path` on `getForClipEncoding`, which selects the preview row per `SmartInfoService.handleEncodeClip` lines 75-98).
- **Output:** normalized float32 1-D vector (length = model dim). Serialized by ML service as `str(embedding.tolist())`, transported as string.
- **Request body** (via `MachineLearningRepository.predict` lines 106-131 + `getFormData` 194-206 of [machine-learning.repository.ts](https://github.com/immich-app/immich/blob/v2.7.5/server/src/repositories/machine-learning.repository.ts)):
  ```
  POST /predict
  Content-Type: multipart/form-data
  
  entries = {"clip":{"visual":{"modelName":"ViT-B-32__openai"}}}
  image   = <blob of preview file>
  ```
- **Writes** (`SearchRepository.upsert` lines 224-230 of search.repository.ts):
  ```typescript
  this.db.insertInto('smart_search')
    .values({ assetId, embedding })  // embedding is the raw string "[0.012, -0.034, ...]"
    .onConflict((oc) => oc.column('assetId').doUpdateSet({ embedding: eb.ref('excluded.embedding') }))
  ```
  Kysely's template interpolation handles the `vector` cast.
- **Quantization:** none in 2.7.5. Full float32. HNSW index uses cosine distance.

### 4.4 `face-detection` + `face-recognition`

**Detection (`JobName.AssetDetectFaces`)** — `PersonService.handleDetectFaces`:

- **Model (default):** `buffalo_l` (config.ts 214-219). `minScore = 0.7`, `maxDistance = 0.5`, `minFaces = 3`.
- **Library (Docker):** InsightFace + onnxruntime, stock `buffalo_l` model pack: detection `det_10g.onnx` (RetinaFace 640×640), recognition `w600k_r50.onnx` (ArcFace R50).
- **Library (Mac accelerator):** Apple's **Vision framework** for detection (`VNDetectFaceLandmarksRequest`, runs on ANE) — see [`ml/src/models/face_detect.py:57`](file:///opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/ml/src/models/face_detect.py#L57). For embeddings: InsightFace's ArcFace ONNX via onnxruntime **CoreML provider** — see [`ml/src/models/face_embed.py:155`](file:///opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/ml/src/models/face_embed.py#L155).
- **Input:** the **preview** file (same as CLIP).
- **Request body:**
  ```
  entries = {"facial-recognition":{"detection":{"modelName":"buffalo_l","options":{"minScore":0.7}},"recognition":{"modelName":"buffalo_l"}}}
  image   = <blob of preview file>
  ```
- **Response shape** (from local [`ml/src/main.py:337-346`](file:///opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/ml/src/main.py#L337)):
  ```json
  {
    "imageHeight": 1440,
    "imageWidth": 1080,
    "facial-recognition": [
      { "boundingBox": {"x1":100,"y1":200,"x2":300,"y2":400},
        "embedding": "[0.01, -0.03, ...]",
        "score": 0.99 }
    ]
  }
  ```
- **Writes:** `PersonService.handleDetectFaces` lines 340-372:
  - `asset_face` rows (one per face): `{id: newUuid(), assetId, imageHeight, imageWidth, boundingBoxX1..Y2}`. `sourceType='machine-learning'`, `isVisible=true`.
  - `face_search` rows: `{faceId, embedding}` — 512-dim normalized float32.
  - `asset_job_status.facesRecognizedAt = now()` (upsert).
  - Queues one `FacialRecognition` job **per new face** plus a `FacialRecognitionQueueAll`.
- **Thumbnail frame choice**: Images use `preview`; videos are first decoded into a preview via thumbnail pipeline step, then the preview is used.

**Recognition (`JobName.FacialRecognition`)** — `PersonService.handleRecognizeFaces`:

- Queries `face_search` via `searchRepository.searchFaces` (cosine distance on HNSW index) with `maxDistance=0.5`, `minFaces=3`.
- If face qualifies as "core" (>= 3 matches and visibility=timeline) and no existing person matches: `personRepository.create({ownerId, faceAssetId: face.id})` → new `person` row.
- Otherwise: `reassignFaces({faceIds:[id], newPersonId: matchedPersonId})` → sets `asset_face.personId`.
- Queues `PersonGenerateThumbnail` for the new person (face-crop JPEG at `thumbs/<userId>/<personId[0:2]>/<personId[2:4]>/<personId>.jpeg`).

**For immy:** we replicate the inference locally on the Mac and can write **`asset_face` + `face_search`** ourselves. `person` and `asset_face.personId` we can either leave null (Immich's next-bump `FacialRecognition` queue clusters them) OR do our own clustering and pre-populate. **Recommend: skip the `person` insert for Y.1 — just write detection rows + embeddings + timestamps, and let Immich's FacialRecognition cluster. It's cheap because each job is a HNSW search + a small UPDATE, no re-inference.** Wait — if we set `facesRecognizedAt`, does Immich ever run `FacialRecognition`? **GAP**: confirm whether `FacialRecognition` is gated on `facesRecognizedAt` or is a separate timestamp. Looking at `asset_job_status` columns (§1.8) there's no `facesClusteredAt` — so `FacialRecognition` queueing is only triggered by the completion of `AssetDetectFaces`, not by a Missing scan. Safe path: set `facesRecognizedAt = now()` AND manually queue `FacialRecognition` jobs via a Redis RPUSH onto the `facialRecognition` BullMQ queue. Or, simpler, insert faces and leave `facesRecognizedAt = NULL` so the Missing scan re-queues `AssetDetectFaces` — but that re-runs inference. Better: insert with timestamps, and accept that un-clustered faces show as "unknown" until the user clicks "refresh."

### 4.5 `video-conversion` (`JobName.AssetEncodeVideo`)

- **Tool:** `jellyfin-ffmpeg` (accelerator bundles this — see `__main__.py:1153`). Docker image also uses jellyfin-ffmpeg for tonemapx support.
- **Invocation:** abstracted through `mediaRepository.transcode`. Fallback chain on error: hw-decode-off → hw-off → fail.
- **Defaults:** H264 (`libx264`), AAC, 720p target, CRF 23, preset ultrafast, single-pass, `-movflags faststart`.
- **Output path:** `<MEDIA>/encoded-video/<userId>/<id[0:2]>/<id[2:4]>/<assetId>.mp4`.
- **Writes:** `asset_file(type='encoded_video', path=<out>)` upsert. **Note (2.7.5 change):** no longer writes `asset.encodedVideoPath` — that column was removed by migration `1773242919341-EncodedVideoAssetFiles.ts`.

**Policies** (config — `TranscodePolicy`): Disabled / All / Required / Optimal / Bitrate. Default is **Required**. "Required" means: transcode only if codec/container/audio not already web-accepted. For a .mp4/h264/aac source, no transcode happens and no `asset_file` row is written. **immy should check the source's codec and only emit the encoded-video row when transcoding would have actually run.**

### 4.6 OCR (`JobName.Ocr`)

- Queued alongside smart-search and face-detection after thumbnails complete.
- Mac accelerator uses Apple's Vision VNRecognizeTextRequest (`ml/src/models/ocr.py`).
- Writes to a separate `ocr` table (not audited here — out of scope for Phase Y.1).
- Sets `asset_job_status.ocrAt = now()`.

---

## §5 — External library scan vs direct insert

### 5.1 Scan job chain

Trigger: `JobName.LibrarySyncFilesQueueAll` (from admin "Scan Library" or cron).

`LibraryService.handleQueueSyncFiles` (lines 468-510 of [library.service.ts](https://github.com/immich-app/immich/blob/v2.7.5/server/src/services/library.service.ts)):

```typescript
const pathsOnDisk = this.storageRepository.walk({
  pathsToCrawl: validImportPaths,
  includeHidden: false,
  exclusionPatterns: library.exclusionPatterns,
  take: JOBS_LIBRARY_PAGINATION_SIZE,
});

for await (const pathBatch of pathsOnDisk) {
  const paths = await this.assetRepository.filterNewExternalAssetPaths(library.id, pathBatch);
  if (paths.length > 0) {
    await this.jobRepository.queue({
      name: JobName.LibrarySyncFiles,
      data: { libraryId: library.id, paths },
    });
  }
}
```

Then `LibraryService.handleSyncFiles` (lines 183-211) creates assets in bulk:

```typescript
await Promise.all(
  job.paths.map((path) =>
    this.processEntity(path, library.ownerId, job.libraryId)
      .then((asset) => assetImports.push(asset))
  )
);
const assetIds = await this.assetRepository.createAll(assetImports);
await this.queuePostSyncJobs(assetIds);
```

`processEntity` (lines 451-474) returns exactly the `asset` row we need to replicate; `createAll` batches the INSERT (`server/src/repositories/asset.repository.ts` lines 383-387):

```typescript
@ChunkedArray({ chunkSize: 4000 })
async createAll(assets) {
  const ids = await this.db.insertInto('asset').values(assets).returning('id').execute();
  return ids.map(({ id }) => id);
}
```

`queuePostSyncJobs` (lines 476-487) fires `SidecarCheck` per new asset — which via JobService.onDone cascades into the full pipeline.

### 5.2 Dedupe key: `originalPath`

`filterNewExternalAssetPaths` (lines 1067-1083 of asset.repository.ts):

```typescript
await this.db
  .selectFrom(unnest(paths).as('path'))
  .select('path')
  .where((eb) => eb.not(eb.exists(
    this.db.selectFrom('asset')
      .select('originalPath')
      .whereRef('asset.originalPath', '=', eb.ref('path'))
      .where('libraryId', '=', asUuid(libraryId))
      .where('isExternal', '=', true),
  )))
  .execute();
```

**This is the only dedupe the scanner does.** If we pre-insert a row with:
- `originalPath = path.normalize('/mnt/external/originals/trip/DSC_4182.JPG')` (exactly matching what Immich's scan would see)
- `libraryId = '2dc3b5bd-30aa-49f1-bf78-1b91ccafc8be'`
- `isExternal = true`

…then `filterNewExternalAssetPaths` excludes the path from the `SyncFiles` batch. Zero jobs get queued for it. **This is our no-op guarantee.**

### 5.3 What happens if we miss and two paths collide

The unique index `(ownerId, libraryId, checksum) WHERE libraryId IS NOT NULL` kicks in when both our row and the scanner's would have the same `sha1("path:"+path)`. Since the hash IS a pure function of the path, if `originalPath` matches ours, the checksum matches ours, and the scanner's INSERT errors with `23505` — but the scanner uses `.values(...).returning('id')` without `ON CONFLICT`, so the whole batch throws. **Therefore, the `originalPath` filter is the actually critical gate**, not the checksum unique index. Get that right and the scan never tries to insert.

### 5.4 File-offline detection (`LibrarySyncAssets`)

`JobName.LibrarySyncAssetsQueueAll` walks assets already in DB for the library and marks `isOffline=true` if the file has vanished. If the file reappears, it flips back. Paths don't change; only the offline flag does. **Our ingest doesn't interact with this** — as long as the file exists at `originalPath`, assets stay online.

### 5.5 Minimum handshake for "scan is a no-op"

```
REQUIRED (scan won't double-insert):
  - asset row exists with matching (libraryId, isExternal=true, originalPath=normalized abs path).

REQUIRED (offline check doesn't flag):
  - File actually exists on disk at that path from Immich's PoV.

REQUIRED (UI shows the asset):
  - asset.visibility = 'timeline' (default).
  - asset.status = 'active' (default).
  - asset.deletedAt IS NULL.
  - asset.fileCreatedAt, fileModifiedAt, localDateTime are NOT NULL (must populate).
```

---

## §6 — Ownership, library IDs, and path anchoring

### 6.1 What Immich stores

`originalPath` is stored **exactly as the server sees it**, after `path.normalize()` (which collapses `//`, `/./`, `/..` and normalizes separators — but doesn't resolve symlinks or absolutize). In Docker, the server walks the `importPaths` configured on the `library` row; those paths are as-seen-from-inside-the-container.

For our setup:

| Host path                     | Container path            | What's in `asset.originalPath` |
|-------------------------------|---------------------------|--------------------------------|
| `/Volumes/NAS/ext/originals/trip/DSC_4182.JPG` | `/mnt/external/originals/trip/DSC_4182.JPG` (volume mount) | `/mnt/external/originals/trip/DSC_4182.JPG` |

The host path is never stored. The `library.importPaths` value (set by admin UI or API) IS the container-absolute anchor. `validateImportPath` refuses non-absolute or non-existent paths (lines 322-357 of library.service.ts).

### 6.2 Consequences for immy running on the Mac

The Mac worker runs Immich's Node workers with `IMMICH_MEDIA_LOCATION` set to the host path of the upload volume — see [`immich_accelerator/__main__.py:2423`](file:///opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/immich_accelerator/__main__.py#L2423):
```python
worker_env["IMMICH_MEDIA_LOCATION"] = config["upload_mount"]
```
The Mac worker reads/writes `thumbs/`, `encoded-video/` etc. under `upload_mount`.

**Path mismatch is fatal.** The accelerator refuses to start if Docker's container-side `IMMICH_MEDIA_LOCATION` doesn't equal the Mac-side `upload_mount` (same string) — see [`_warn_on_path_mismatch` line 1480](file:///opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/immich_accelerator/__main__.py#L1480). That logic is already in place: if the upload library in Immich has `originalPath='/path/to/upload/upload/<uid>/<year>/<file>'` and the Mac mount is `/Volumes/data`, they'd diverge. Same rule applies to **external** libraries: the Mac must see `/mnt/external/originals/...` at the same absolute path that Immich wrote into `asset.originalPath`. If Mac sees `/Volumes/NAS/ext/originals`, we must resolve a mapping (config option: `external_library_mounts: [{container: '/mnt/external/originals', host: '/Volumes/NAS/ext/originals'}]`) and:

1. Walk files using the **host path**.
2. Compute `checksum = sha1("path:" + CONTAINER_path)`.
3. Store `originalPath = CONTAINER_path`.
4. Also write derivative files under host `upload_mount/thumbs/...` (same string as container).

### 6.3 `library_id`, `owner_id`

- `library.id` is a UUID generated by Immich at library creation; for our NAS: `2dc3b5bd-30aa-49f1-bf78-1b91ccafc8be`.
- `library.importPaths` is a `text[]` of absolute container paths. External libraries can have multiple import paths.
- `library.ownerId` is a single user UUID (CASCADE on user delete). Every asset in the library inherits that ownerId (see `processEntity`: `ownerId` passed to the insert = `library.ownerId`).
- `library.exclusionPatterns` is `text[]` of minimatch-style globs (e.g. `**/.DS_Store`).

For our ingest: read library row once, cache `(id, ownerId, importPaths)`.

### 6.4 Path transformation summary

```
host fs walk                  →  host path      e.g. /Volumes/NAS/ext/originals/trip/DSC_4182.JPG
apply host→container mapping  →  container path e.g. /mnt/external/originals/trip/DSC_4182.JPG
path.normalize()              →  same string in ASCII/UTF-8 filesystems (no trailing-slash issues for files)
store in asset.originalPath   →  exact string above
sha1("path:"+that)            →  checksum bytea
```

**No URL-encoding, no case changes.** On macOS HFS+ with path normalization quirks, watch for NFD vs NFC Unicode — Immich's Linux container sees bytes as-delivered by the filesystem driver. Our Bun implementation should `normalize('NFC')` to match what the Linux kernel would hand back on a ZFS/SMB share.

---

## §7 — What Immich upgrades typically change

Migrations directory at tag: [server/src/schema/migrations](https://github.com/immich-app/immich/tree/v2.7.5/server/src/schema/migrations).

### 7.1 Churn rate

v2.7.5 tag contains **68 migrations** total in the 2.7.x series; they span timestamps from early 2025 through April 2026.

Rate: ~5-10 migrations per month during active 2.7.x development, clustered around minor bumps.

### 7.2 What's stable vs what moves

**Stable core (rarely changed since 2.5):**
- `asset` primary columns (`id, ownerId, type, originalPath, fileCreatedAt, fileModifiedAt, checksum, localDateTime, originalFileName, libraryId, isExternal, visibility, status`).
- `asset_exif` (EXIF columns added incrementally but existing ones don't rename/drop).
- `person` (name, thumbnailPath, ownerId stable).

**Edge that moves:**
- `asset_file`: new `type` values added (`encoded_video` in 2.7.5, `sidecar` added before that via migration `1764698859174`).
- `asset` column drift: `width`/`height` added 2.7.x (`1768336661963-AddAssetWidthHeight.ts`), `encodedVideoPath` removed 2.7.5 (`1773242919341-EncodedVideoAssetFiles.ts`), `isEdited` added 2.7.x (`1768757482271-SwitchToIsEdited.ts`).
- `asset_exif`: `tags` added (`1768847456553-AddTagsToExif.ts`), `lockedProperties` added (`1764957138636-AddLockedPropertiesToAssetExif.ts`), GiST Earth-coord index added (`1772121424533-AddAssetExifGistEarthcoord.ts`).
- `checksumAlgorithm` column added late in 2.7.x (`1774548649115-AddChecksumAlgorithm.ts.ts`) — pre-2.7.5 installs had no way to distinguish sha1File from sha1Path. **immy must tolerate both column-present and column-absent Immich versions** if we ever support < 2.7.5.
- `smart_search.embedding` dimension mutates dynamically on model change (see §1.5).
- Face recognition tables: `asset_face_sync` migrations (`1774393726320-AssetFaceSyncReset.ts`) — reset touches all rows.

**Recent (2.7.5-era) migration themes:**
1. **Asset editing** (~10 migrations around `AddAssetWidthHeight`, `CreateAssetEditTable`, `AddEditCountToAsset`, `SwitchToIsEdited`, `AddIsEditedToAssetFile`) — new feature lane.
2. **Asset files consolidation** — moving `asset.sidecarPath` and `asset.encodedVideoPath` into `asset_file` rows with specific `type`.
3. **Transparency/progressive flags** on `asset_file`.
4. **OCR + plugin scaffolding** (`AddPluginAndWorkflowTables`, `OCRBigramsForCJK`).
5. **Checksum algorithm bookkeeping** (the `sha1` vs `sha1-path` distinction).

### 7.3 Risk lens for immy

- **Pin to v2.7.5 exactly.** Read the entire `schema/migrations` directory at that tag into a static "schema snapshot" and validate against live Postgres at immy startup.
- **Refuse to run if the migrations table doesn't end at `1775165531374-AddPersonNameTrigramIndex.ts`** (the last 2.7.5 migration). On an upgrade to 2.7.6+, fail loudly and require a new immy release.
- Tables that need version gating:
  - `asset_file`: check for `isTransparent`/`isProgressive`/`isEdited` columns — all present in 2.7.5.
  - `asset`: check no `encodedVideoPath`, `sidecarPath`, `thumbhash` present (thumbhash present, paths removed).
  - `asset_exif`: check `tags`, `lockedProperties` columns exist.
- The `asset_job_status` table's column set is the most fragile — at minimum we need `metadataExtractedAt`, `facesRecognizedAt`, `duplicatesDetectedAt`, `ocrAt`. **GAP**: if 2.7.6 adds `thumbnailsGeneratedAt`, our inserts will work but the Missing scan may re-queue. Re-audit on every minor.

### 7.4 Upgrade-on-the-fly ruled out

We should **not** auto-detect and adapt. The surface area is too big. Lock to one Immich version per immy release; support exactly that one.

---

## §8 — Minimum viable insert

For a new external-library JPEG to appear fully processed in the UI — visible in timeline, with thumbnail, with search, with faces — here is the minimal sequence. Items marked **REQUIRED** must be present; **nice-to-have** improve fidelity but aren't blockers.

### 8.1 Transaction order

```
BEGIN;

-- REQUIRED (1 row)
INSERT INTO asset (
  id, ownerId, libraryId,
  deviceAssetId, deviceId,
  type, originalPath, originalFileName,
  checksum, checksumAlgorithm,
  fileCreatedAt, fileModifiedAt, localDateTime,
  isExternal, isFavorite, isOffline,
  status, visibility,
  thumbhash, width, height
) VALUES (
  $id,
  $ownerId,
  $libraryId,
  replace($basename, ' ', ''),
  'Library Import',
  'IMAGE',
  $originalPath,
  $basename,
  decode($sha1_path_hex, 'hex'),
  'sha1-path',
  $fileCreatedAt,
  $fileModifiedAt,
  $localDateTime,
  true, false, false,
  'active', 'timeline',
  decode($thumbhash_hex, 'hex'),  -- 25-byte buffer
  $width, $height
);

-- REQUIRED (1 row — description NOT NULL default '')
INSERT INTO asset_exif (assetId, description) VALUES ($id, '');
-- nice-to-have: all the camera/gps/date columns from §1.3

-- REQUIRED (2 rows — preview and thumbnail files must exist on disk first)
INSERT INTO asset_file (assetId, type, path, isEdited, isProgressive, isTransparent)
  VALUES ($id, 'preview',   $preview_path,   false, true,  false),
         ($id, 'thumbnail', $thumbnail_path, false, false, false);

-- NICE-TO-HAVE (but if omitted, smart search fails silently for this asset)
INSERT INTO smart_search (assetId, embedding) VALUES ($id, $clip_embedding_vector);

-- NICE-TO-HAVE (faces are optional; omit for no-faces asset)
INSERT INTO asset_face (id, assetId, imageWidth, imageHeight, boundingBoxX1, boundingBoxY1, boundingBoxX2, boundingBoxY2, sourceType, isVisible)
  VALUES (...);
INSERT INTO face_search (faceId, embedding) VALUES (...);

-- REQUIRED for "no re-run" behaviour — set all four to now()
INSERT INTO asset_job_status (assetId, metadataExtractedAt, facesRecognizedAt, duplicatesDetectedAt, ocrAt)
  VALUES ($id, now(), now(), now(), now());

COMMIT;
```

### 8.2 Filesystem side-effects (must happen before commit to be safe)

```
write <MEDIA>/thumbs/<userId>/<id[0:2]>/<id[2:4]>/<id>_preview.jpeg     -- 1440px JPEG q80 progressive
write <MEDIA>/thumbs/<userId>/<id[0:2]>/<id[2:4]>/<id>_thumbnail.webp   -- 250px WebP q80
(optional) write <MEDIA>/encoded-video/<userId>/<id[0:2]>/<id[2:4]>/<id>.mp4
(optional) write <MEDIA>/thumbs/<userId>/<pid[0:2]>/<pid[2:4]>/<pid>.jpeg  -- person thumbnails
```

`mkdir -p` the bucket dirs. Immich's `ensureFolders(previewFile.path)` does this at generation time.

### 8.3 Required vs nice-to-have breakdown

| Row / file                               | Required for UI to show? | Required for scan no-op? | Required for search? | Required for faces? |
|------------------------------------------|---------------------------|---------------------------|----------------------|---------------------|
| `asset` row                              | ✅                        | ✅                        | ✅                   | ✅                  |
| `asset_exif(assetId, description='')`    | ✅ (NOT NULL FK from UI)  | ✅                        | ✅                   | ✅                  |
| `asset_file preview`                     | ✅ (detail view 404 w/o)  | ✅                        | -                    | -                   |
| `asset_file thumbnail`                   | ✅ (grid view 404 w/o)    | ✅                        | -                    | -                   |
| `preview.jpeg on disk`                   | ✅                        | -                         | -                    | -                   |
| `thumbnail.webp on disk`                 | ✅                        | -                         | -                    | -                   |
| `asset.thumbhash`                        | nice (blur placeholder)   | -                         | -                    | -                   |
| `asset.width/height`                     | nice (layout sizing)      | -                         | -                    | -                   |
| `asset_exif` full EXIF                   | nice (detail panel)       | -                         | timezone/date only   | -                   |
| `smart_search` row                       | -                         | -                         | ✅                   | -                   |
| `asset_face` rows                        | nice (people view)        | -                         | -                    | ✅                  |
| `face_search` rows                       | -                         | -                         | -                    | ✅                  |
| `person` rows + `asset_face.personId`    | nice (named clusters)     | -                         | -                    | nice                |
| `asset_job_status` all timestamps        | -                         | Missing scan no-op        | -                    | -                   |

### 8.4 Watch-outs

- **`asset_exif` insert is mandatory-ish.** Not strictly FK-required at INSERT time (it's a child table), but the Immich UI's asset detail panel issues a JOIN and renders `null` EXIF fields fine — EXCEPT for `description` which is declared `NOT NULL DEFAULT ''` and queries assume its presence. Always insert the row.
- **`fileCreatedAt` NOT NULL.** Use `stat.mtime` if EXIF dateTimeOriginal missing; matching `LibraryService.processEntity` behaviour.
- **`checksum` must be binary 20 bytes**, not hex string. `bytea`-over-wire in most Postgres drivers is `\x<hex>` or direct Buffer in Bun/Node.
- **`asset_file` unique is `(assetId, type, isEdited)`** — two previews with different `isEdited` are allowed. For pure ingest we always use `isEdited=false`.
- **HEIC / RAW / WEBP sources:** Set `type='IMAGE'` — the thumbnail pipeline handles the decode. If we transcode the fullsize ourselves, also add an `asset_file(type='fullsize', format=JPEG, isEdited=false)` row.
- **Video-specific:** set `asset.type='VIDEO'`, populate `asset.duration` (the `ffprobe` format.duration formatted as `"HH:MM:SS.sss"`). Preview is a single-frame JPEG extracted via ffmpeg.
- **Live photos:** pair `.HEIC` + `.MOV` via matching `livePhotoCID` in EXIF; after inserting both, set `asset.livePhotoVideoId = <video_id>` on the image and `asset.visibility = 'hidden'` on the video. The `linkLivePhotos` logic is in `metadata.service.ts`; easier to replicate its query than rebuild the heuristic.
- **Thumbhash encoding:** the thumbhash npm library emits a `Uint8Array` of length typically 18-25 bytes; `asset.thumbhash` is `bytea`. See the `thumbhash` npm package or Bun port; must match decoder the UI uses (`thumbhash-utils`).

---

## Appendix A — Job names (v2.7.5 enum, selected)

From `server/src/enum.ts` lines ~1227-1297:

```
// Library
LibraryScanQueueAll, LibrarySyncFilesQueueAll, LibrarySyncFiles,
LibrarySyncAssetsQueueAll, LibrarySyncAssets,
LibraryDeleteCheck, LibraryDelete, LibraryRemoveAsset

// Per-asset pipeline
SidecarQueueAll, SidecarCheck, SidecarWrite
AssetExtractMetadataQueueAll, AssetExtractMetadata
AssetGenerateThumbnailsQueueAll, AssetGenerateThumbnails
AssetEditThumbnailGeneration
SmartSearchQueueAll, SmartSearch
AssetDetectFacesQueueAll, AssetDetectFaces
FacialRecognitionQueueAll, FacialRecognition
PersonGenerateThumbnail
AssetEncodeVideoQueueAll, AssetEncodeVideo
AssetDetectDuplicates
Ocr (+ queueall)
AssetDelete, AssetFileMigration, FileMigrationQueueAll
StorageTemplateMigrationSingle
```

Corresponding BullMQ `QueueName` values (used as Redis keys): `library`, `sidecar`, `metadataExtraction`, `thumbnailGeneration`, `smartSearch`, `faceDetection`, `facialRecognition`, `videoConversion`, `migration`, `backgroundTask`, `editor`.

If we ever want to RPUSH directly onto a BullMQ queue to trigger a handler, the key pattern is `bull:<queueName>:wait` or `bull:<queueName>:<jobId>` — **GAP**: verify against BullMQ version at v2.7.5. Avoid if possible; prefer the REST API or direct DB writes.

---

## Appendix B — ML predict protocol summary

Single endpoint: `POST <ml-url>/predict`, `Content-Type: multipart/form-data`.

- `entries` (form field, required): JSON string, keys are ModelTask values.
- `image` (form field, one of): binary blob of image file (preview).
- `text` (form field, one of): UTF-8 text (for CLIP textual).

ModelTask = `'clip' | 'facial-recognition' | 'ocr'` (see `machine-learning.repository.ts` lines 14-17).
ModelType = `'visual' | 'textual' | 'detection' | 'recognition' | 'pipeline' | 'ocr'`.

**Examples (exact strings as Immich sends):**

```
# CLIP visual embedding
entries = {"clip":{"visual":{"modelName":"ViT-B-32__openai"}}}

# CLIP text embedding
entries = {"clip":{"textual":{"modelName":"ViT-B-32__openai","options":{"language":null}}}}

# Face detect+recognize in one call
entries = {"facial-recognition":{"detection":{"modelName":"buffalo_l","options":{"minScore":0.7}},"recognition":{"modelName":"buffalo_l"}}}

# OCR
entries = {"ocr":{"detection":{"modelName":"","options":{"minScore":0.5,"maxResolution":1024}},"recognition":{"modelName":"","options":{"minScore":0.5}}}}
```

**Response shape** (validated by `PredictResponse` in [`ml/src/main.py:205-212`](file:///opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/ml/src/main.py#L205)):

```jsonc
{
  "imageHeight": 1440,
  "imageWidth": 1080,
  "clip": "[0.012,-0.034,...]",          // stringified float list, length = model dim
  "facial-recognition": [
    { "boundingBox": {"x1":100,"y1":200,"x2":300,"y2":400},
      "embedding": "[0.01,-0.03,...]",    // stringified float list, length 512
      "score": 0.99 }
  ],
  "ocr": {
    "text": ["...","..."],
    "box": [0,0,100,0, ...],              // flat quadrilateral coords, 8 ints per text box
    "boxScore": [0.95, 0.92],
    "textScore": [0.98, 0.96]
  }
}
```

Immich parses `response["clip"]` as a string and stores it in `smart_search.embedding` (the Kysely driver passes the string and pgvector casts). Same for face embeddings → `face_search.embedding`. **So immy's inserts should pass the embedding as a string `"[...]"` to Kysely / bun:sql, not as `float[]`** — let the vector cast happen on the DB side.

---

## Appendix C — Citation index (mixed local + GitHub)

**Local (immich-accelerator 1.4.8):**

- Orchestration: `/opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/immich_accelerator/__main__.py` (3156 lines; key spots: `_warn_on_path_mismatch` 1480-1528, worker env 2423, ffmpeg 1120-1199).
- ML service: `/opt/homebrew/Cellar/immich-accelerator/1.4.8/libexec/ml/src/main.py` (705 lines; `/predict` 420-672, response schema 205-212).
- CLIP: `.../ml/src/models/clip.py` (326 lines; MODEL_MAP 21-47, MLXClip class 52-270).
- Face detect: `.../ml/src/models/face_detect.py` (255 lines; Vision framework 48-106, 5-point landmarks 108-220).
- Face embed: `.../ml/src/models/face_embed.py` (483 lines; ArcFace buffalo_l 31-213, batch inference 282-376).
- Config: `.../ml/src/config.py` (96 lines; defaults 12-86).
- Requirements: `.../ml/requirements.txt` — pinned `mlx-clip@f56e3ec`, `insightface>=0.7.3`, `onnxruntime>=1.18.0`, `pyobjc-framework-Vision>=10.0`.
- Compose reference: `.../docker/docker-compose.yml` — `tensorchord/pgvecto-rs:pg14-v0.2.0`, `redis:6.2-alpine`, `IMMICH_MACHINE_LEARNING_URL=http://host.internal:3003`.

**GitHub (immich-app/immich @ v2.7.5):**

- `server/src/services/library.service.ts` — `processEntity` 451-474, `queuePostSyncJobs` 476-487, `handleSyncFiles` 183-211, `handleQueueSyncFiles` 468-510, `validateImportPath` 322-357.
- `server/src/services/asset-media.service.ts` — `create` ~281-323, duplicate 228-245.
- `server/src/services/metadata.service.ts` — `handleMetadataExtraction` 275-373, `applyMotionPhotos` ~600-630, `handleSidecarCheck` 428-475.
- `server/src/services/media.service.ts` — `handleGenerateThumbnails` 184-222, `generateImageThumbnails` 264-326, `getImageFile` 743-751.
- `server/src/services/smart-info.service.ts` — `handleEncodeClip` 75-98, `onConfigUpdate` 42-56.
- `server/src/services/person.service.ts` — `handleDetectFaces` ~330-380, `handleRecognizeFaces` ~400-440.
- `server/src/services/job.service.ts` — `onDone` 58-148 (the job chain switch).
- `server/src/repositories/asset.repository.ts` — `createAll` 383-387, `filterNewExternalAssetPaths` 1067-1083, `getUploadAssetIdByChecksum` 513-523, `upsertExif` 106-162, `upsertFiles` 1138-1157.
- `server/src/repositories/crypto.repository.ts` — `hashSha1` 47-49, `hashFile` 51-59.
- `server/src/repositories/machine-learning.repository.ts` — `predict` 106-131, `getFormData` 194-206, `encodeImage` 134-138, `detectFaces` 127-138.
- `server/src/repositories/media.repository.ts` — `generateThumbnail` 140-148, `generateThumbhash` 150-165.
- `server/src/repositories/search.repository.ts` — `upsert` 224-230, `searchFaces` 195-221.
- `server/src/cores/storage.core.ts` — `getImagePath` 103-107, `getEncodedVideoPath` 109-110, `getNestedPath` 277.
- `server/src/config.ts` — image defaults 272-289, ML defaults 210-219, FFmpeg defaults 134-161.
- `server/src/enum.ts` — `AssetType` 48-52, `ChecksumAlgorithm` 54-57, `AssetFileType` 61-68, `ImageFormat` 1089-1092, `StorageFolder` 418-424, `JobName` 1227-1297.
- Schema tables (all under `server/src/schema/tables/`): `asset.table.ts`, `asset-exif.table.ts`, `asset-file.table.ts`, `asset-face.table.ts`, `face-search.table.ts`, `smart-search.table.ts`, `person.table.ts`, `stack.table.ts`, `library.table.ts`, `asset-job-status.table.ts`.
- Migrations: `server/src/schema/migrations/` — 68 total in tag, 10 most recent listed in §7.2.

---

## GAPs (flagged for follow-up)

1. **pgvecto-rs vs pgvector embedding literal format.** `tensorchord/pgvecto-rs:pg14-v0.2.0` is pinned in the accelerator compose. Embedding string literal `"[0.1,0.2,...]"` works for pgvector; confirm it does for pgvecto-rs too, or whether we need a different operator/cast. Test with a small INSERT before committing to the path.
2. **`exiftool-vendored` exact pinned version** at v2.7.5 — need to read `server/pnpm-lock.yaml` at that tag to pin immy's local exiftool to the same major.
3. **Missing-thumbnail detection mechanism.** The Jobs UI "Missing" button for thumbnail/preview — is it driven by absence of `asset_file` rows, absence of files on disk, or both? If our ingest writes the rows but the files are missing, will the button catch it? Read `MediaService.handleQueueGenerateThumbnails` when `force=false`.
4. **`FacialRecognition` queueing gate.** After we insert `asset_face` rows with `personId=NULL` and set `facesRecognizedAt=now()`, do clustered people get built automatically? Or does `FacialRecognition` only run from `AssetDetectFaces.onDone`? If the latter, we either (a) replicate clustering ourselves, (b) leave faces un-clustered, or (c) RPUSH `FacialRecognition` jobs into Redis directly.
5. **BullMQ Redis key format at v2.7.5.** If we ever want to trigger specific jobs (e.g., `FacialRecognition`) without UI: know the exact `bull:<queue>:<jobId>` pattern and how BullMQ expects the payload serialized. Prefer the REST admin API.
6. **Unicode normalization on filenames.** macOS HFS+ vs APFS vs mounted SMB vs ZFS present filenames as NFD vs NFC. Immich/Linux passes bytes through. Walk the filesystem, read bytes, and normalize to match what the Immich container will see from its own walk — probably `normalize('NFC')` for anything going into Postgres.
7. **Upload-path live-photo case.** We don't support uploads in Phase Y.1, but if later: the `livePhotoCID` pairing uses a cross-asset search. Replicate the logic from `metadata.service.ts linkLivePhotos()` carefully.

Sources:
- [Immich Jobs and Workers docs](https://docs.immich.app/administration/jobs-workers/)
- [DeepWiki Background Job System](https://deepwiki.com/immich-app/immich/3.2-people-and-face-recognition)
- [Immich v2.7.5 release notes](https://github.com/immich-app/immich/releases/tag/v2.7.5)
