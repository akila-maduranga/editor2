# TikTok MP4 Patcher

Self-hosted VPS tool that remuxes MP4 files and applies 7 binary structural
patches to bypass TikTok's re-upload compression detection.

## Patches applied

1. **Brand spoof** — `ftyp` major=`isom`, minor=`0x200`, compat=`[isom,iso2,avc1,mp41]`
2. **Date zeroing** — `mvhd`/`tkhd`/`mdhd` creation + modification timestamps → `0`
3. **Language spoof** — `mdhd` language → `und` (`0x55C4`)
4. **Frame inflate** — `stts` in-place rewrite: `sample_count=19690`, `delta=timescale//120` (forces 120 fps, no offset drift)
5. **Fake trailer** — invalid-size atom appended after `mdat` (triggers ExifTool warning)
6. **Encoder spoof** — `Lavf60.16.100` injected via ffmpeg during remux
7. **Comment inject** — custom comment field via ffmpeg `-metadata`
8. **moov at end** — no `+faststart`; `ftyp → mdat → moov` order

## Requirements

- Docker + Docker Compose (nothing else needed on the host)

## Deploy

```bash
# Clone / copy files to your VPS
git clone <repo> /opt/tiktok-patcher
cd /opt/tiktok-patcher

# Build and start (runs on port 80)
docker compose up -d --build

# View logs
docker compose logs -f app

# Stop
docker compose down
```

Open `http://YOUR_VPS_IP` in a browser.

## File layout

```
Dockerfile            # Python 3.12-slim + ffmpeg
docker-compose.yml    # app + nginx services
nginx.conf            # reverse proxy (SSE-safe, 2 GB upload limit)
app.py                # Flask app + all 7 patch functions
templates/index.html  # Single-page UI with live terminal log
requirements.txt      # flask only
```

## Volumes

Patched files are stored in a named Docker volume (`outputs`).
Uploads are ephemeral (cleaned after each job).
