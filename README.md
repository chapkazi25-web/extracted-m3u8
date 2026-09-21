# tvivu Music HD Playlist

Extract HD/FHD music-channel streams from [tvivu.com](https://tvivu.com/categories/music)
and generate an M3U playlist playable in **VLC** and **Tivimate**.

## Files

- `tvivu_music_hd.py` — extractor / playlist generator
- `tvivu_music_hd.m3u` — generated playlist (408 HD/FHD entries)

## Requirements

- Python 3.9+
- `requests` (`pip install requests`)

## Usage

```bash
# Full regeneration (takes several minutes for ~870 channels)
python tvivu_music_hd.py --output tvivu_music_hd.m3u

# Quick test on first N channels
python tvivu_music_hd.py --output test.m3u --max-channels 20

# Also keep streams with unknown quality
python tvivu_music_hd.py --output out.m3u --include-unknown

# Skip external logo lookup (use tvivu CDN logos only, faster)
python tvivu_music_hd.py --output out.m3u --no-logo-resolve

# Dump raw extracted data as JSON for inspection
python tvivu_music_hd.py --output out.m3u --dump-json data.json
```

| Option | Default | Description |
|---|---|---|
| `--output`, `-o` | `tvivu_music_hd.m3u` | Output playlist file |
| `--max-channels` | `0` (all) | Limit category channels processed |
| `--workers` | `4` | Parallel watch-page fetchers |
| `--timeout` | `20` | HTTP timeout, seconds |
| `--min-height` | `720` | Min. height (px) counted as HD |
| `--include-unknown` | off | Keep streams with unknown quality |
| `--no-logo-resolve` | off | Skip tv-logos / K-yzu / Wikimedia lookup |
| `--dump-json` | — | Write raw channel/stream JSON to path |

## How it works

1. **Discovery** — paginates `https://tvivu.com/categories/music?page=N`,
   parsing embedded channel cards (slug, name, logo, country).
2. **Extraction** — fetches each `https://tvivu.com/watch/<slug>` page,
   parses the embedded stream list (URL, quality, user-agent, referrer),
   unwraps tvivu's expiring `srv*.pxfy.dev/?url=…` proxy links to stable
   direct `.m3u8` URLs, and dedupes them.
3. **HD/FHD filter** — keeps only streams ≥ `--min-height` px
   (`720p` → `HD`, `1080p`/`1080i` → `FHD`); best stream per channel wins.
4. **Logos** — per channel, first verified hit wins:
   [tv-logo/tv-logos](https://github.com/tv-logo/tv-logos) →
   [K-yzu/Logos](https://github.com/K-yzu/Logos) →
   Wikimedia Commons → tvivu CDN fallback.
   Add manual fixes to `LOGO_OVERRIDES` in the script.
5. **Playlist** — writes `#EXTM3U` with `tvg-id`, `tvg-logo`,
   `tvg-country`, `group-title="Music"` plus `#EXTVLCOPT`
   user-agent/referrer hints (used by VLC, ignored by Tivimate).

## Loading the playlist

- **VLC:** Media → Open File (or Open Network Stream → paste file path/URL).
- **Tivimate:** Add playlist → M3U playlist → select the file
  (or host it and add by URL); EPG is not included.
