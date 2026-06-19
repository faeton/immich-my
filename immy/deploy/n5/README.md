# immy on the N5 (Phase 6)

Run immy's enrichment **on the NAS** as a one-shot container that joins
Immich's docker network — so it can reach the ML container's internal-only
port (`immich_machine_learning:3003`) for CLIP, plus Postgres by the alias
`database`, and Ollama / the qwen-asr shim by container name (once they're
attached to the same network — see setup).

immy here is a **thin HTTP orchestrator**: no GPU, no weights, no ONNX/MLX in
the image. CLIP → Immich ML, captions → Ollama/gemma4, ASR → qwen-asr shim.

## Why this shape (and not the alternatives)

- **Not folded into the TrueNAS `ix-immich` stack** — TrueNAS regenerates that
  compose and would clobber edits.
- **Not a TrueNAS "custom app"** — re-introduces the managed-layer that can
  interfere on upgrades; buys only UI visibility/autostart, which a batch job
  doesn't need.
- **A standalone compose project invoked via `docker compose run --rm`** — joins
  `ix-immich_default` (external), gets container-name access to ML + PG, and
  leaves no idle daemon. Visibility comes from this dir being in git.

## One-time setup on the NAS

```sh
ssh n5
sudo mkdir -p /mnt/flash/immy/scratch

# Copy the deploy files + package source to the NAS (from the repo root):
#   rsync -a --delete immy/ n5:/mnt/flash/immy/src-immy/   # package (for build)
#   cp immy/deploy/n5/compose.yaml  /mnt/flash/immy/compose.yaml
#   cp immy/deploy/n5/run-batch.sh  /mnt/flash/immy/run-batch.sh && chmod +x ...
#   cp immy/deploy/n5/config.example.yml /mnt/flash/immy/config.yml   # then edit

# Put the real PG password in /mnt/flash/immy/config.yml (POSTGRES_PASSWORD of
# the immich_postgres container). This file is intentionally NOT in git.

# Attach Ollama + the qwen-asr shim to Immich's network so immy can reach them
# by container name. (The host.docker.internal/host-gateway path is unreliable:
# the published-port hairpin NAT resets HTTP responses back to a container on
# ix-immich_default — TCP connects, HTTP gets reset.) This is additive and
# reversible (`docker network disconnect`), but must be re-run if those
# containers are recreated (reboot/update) unless their run command adds the net.
sudo docker network connect ix-immich_default ollama
sudo docker network connect ix-immich_default qwen-asr-shim

# Build the image (compose build.context points at the package dir):
sudo docker compose -f /mnt/flash/immy/compose.yaml build
```

> Note: `compose.yaml`'s `build.context: ../..` assumes it sits at
> `<pkgdir>/deploy/n5/compose.yaml`. When deploying the compose file flat into
> `/mnt/flash/immy/`, either keep the `deploy/n5/` layout there too, or change
> `context:` to wherever the package source landed (e.g. `./src-immy`).

## Smoke test (no DB writes)

```sh
# 1. image runs + immy imports cleanly
sudo docker compose -f /mnt/flash/immy/compose.yaml run --rm immy --help

# 2. reachability from inside the network (ML + PG by name, host services via gateway)
sudo docker compose -f /mnt/flash/immy/compose.yaml run --rm --entrypoint sh immy -c '
  python -c "import urllib.request as u; print(\"ML:\", u.urlopen(\"http://immich_machine_learning:3003/ping\", timeout=5).read())" ;
  python -c "import socket; s=socket.create_connection((\"database\",5432),5); print(\"PG: ok\"); s.close()" ;
  python -c "import urllib.request as u; print(\"OLLAMA:\", u.urlopen(\"http://host.docker.internal:11434/api/version\", timeout=5).read())" ;
  python -c "import urllib.request as u; print(\"ASR:\", u.urlopen(\"http://host.docker.internal:8091/ping\", timeout=5).read())"
'

# 3. dry-run a real trip — reports would-insert rows, writes nothing
./run-batch.sh /originals/<trip> --dry-run
```

## Real runs

```sh
# CLIP + derivatives (faces forced off — see run-batch.sh):
./run-batch.sh /originals/<trip>

# add captions + transcripts:
./run-batch.sh /originals/<trip> --with-captions --with-transcripts
```

## First-run verification (do once)

- **originalPath anchoring** — confirm the `--dry-run` output's would-insert
  `originalPath` matches what an Immich library scan produces (anchored to the
  library `importPaths`, which immy reads from the DB). A prefix mismatch means
  the originals mount or the library import path is off.
- **CLIP dim guard** — immy checks the embedding width against
  `smart_search.embedding` typmod up front; a `clip_model` mismatch fails fast.

## Cron (optional)

`docker compose run --rm` is cron-friendly. Example — nightly enrichment of a
fixed trip root, captions+transcripts on:

```cron
30 2 * * *  /mnt/flash/immy/run-batch.sh /originals/incoming --with-captions --with-transcripts >> /mnt/flash/immy/cron.log 2>&1
```
