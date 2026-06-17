# TikTok MP4 Patcher

Self-hosted VPS tool that remuxes MP4 files and applies a binary structural
patch to bypass TikTok's aggressive compression on re-uploaded content.

## What it does

**Step 1 — Remux (stream copy)**  
Runs `ffmpeg -c copy -movflags +faststart` — no re-encoding, just moves the
`moov` atom to the front so the binary patch can reliably locate all boxes.

**Step 2 — ftyp patch**  
Rewrites the `ftyp` box: major brand → `mp42`, compatible brands →
`[isom, iso2, avc1, mp41]`. TikTok's ingest pipeline checks this field to
detect re-encoded/re-muxed content.

**Step 3 — mvhd flag clear**  
Clears the random-access flag bit in the `mvhd` (movie header) box.
TikTok uses this to identify files that have passed through a muxer.

## Requirements

- Python 3.10+
- `ffmpeg` on PATH (`apt install ffmpeg` on Debian/Ubuntu)
- Flask 3.x

## Quick start

```bash
git clone <repo> /opt/tiktok-patcher
cd /opt/tiktok-patcher
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py          # http://0.0.0.0:5000
```

## VPS deployment

1. Copy files to `/opt/tiktok-patcher`
2. Copy `tiktok-patcher.service` to `/etc/systemd/system/`
3. Copy `nginx.conf` to `/etc/nginx/sites-available/tiktok-patcher`
4. Enable and start:

```bash
systemctl enable --now tiktok-patcher
ln -s /etc/nginx/sites-available/tiktok-patcher /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

## File layout

```
app.py                   # Flask app + patcher logic
templates/index.html     # Single-page UI
uploads/                 # Temp upload dir (auto-cleaned)
outputs/                 # Patched files (served for download)
requirements.txt
tiktok-patcher.service   # systemd unit
nginx.conf               # Nginx reverse-proxy snippet
```
