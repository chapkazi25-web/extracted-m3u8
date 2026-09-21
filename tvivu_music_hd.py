#!/usr/bin/env python3
"""
tvivu_music_hd.py — Extract HD/FHD music-channel streams from tvivu.com
and generate a VLC / Tivimate compatible M3U playlist.

Sources:
  Channels : https://tvivu.com/categories/music (paginated ?page=N)
  Watch    : https://tvivu.com/watch/<slug>  (embedded stream JSON)
  Logos    : tv-logo/tv-logos, K-yzu/Logos, Wikimedia Commons, fallback tvivu CDN

Usage:
  pip install requests
  python tvivu_music_hd.py --output tvivu_music_hd.m3u
  python tvivu_music_hd.py --output out.m3u --max-channels 20 --workers 8
  python tvivu_music_hd.py --output out.m3u --include-unknown --no-logo-resolve

Playlist output is `#EXTM3U` + `#EXTINF` entries with
tvg-id / tvg-logo / tvg-country / group-title="Music" plus
EXTVLCOPT user-agent/referrer hints (ignored by Tivimate, used by VLC).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html as htmlmod
import json
import re
import sys
import time
import urllib.parse

try:
    import requests
except ImportError:  # pragma: no cover
    print("ERROR: the 'requests' package is required. Install with: pip install requests",
          file=sys.stderr)
    sys.exit(1)

BASE = "https://tvivu.com"
CATEGORY_URL = f"{BASE}/categories/music"
WATCH_URL = f"{BASE}/watch"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------- quality

HD_QUALITIES = {"720p", "720p+", "1080p", "1080i", "hd", "fhd",
                "1280x720", "1920x1080", "720", "1080"}

RE_HEIGHT_P = re.compile(r"(\d{3,4})\s*p\+?", re.IGNORECASE)
RE_WxH = re.compile(r"(\d{3,4})\s*x\s*(\d{3,4})", re.IGNORECASE)


def quality_height(quality: str | None) -> int | None:
    """Return vertical resolution in px for a quality label, else None."""
    if not quality:
        return None
    q = quality.strip().lower()
    if q in ("null", "none", "unknown", "auto", ""):
        return None
    m = RE_WxH.search(q)
    if m:
        try:
            return int(m.group(2))
        except ValueError:
            pass
    m = RE_HEIGHT_P.search(q)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    if q in HD_QUALITIES:
        return 720 if q in ("720p", "720p+", "hd", "720", "1280x720") else 1080
    return None


def is_hd_quality(quality: str | None, min_height: int = 720) -> bool:
    h = quality_height(quality)
    return h is not None and h >= min_height


def quality_rank(quality: str | None) -> int:
    h = quality_height(quality)
    return h or 0

# ------------------------------------------------------------ logo sources

TV_LOGOS_BASE = "https://raw.githubusercontent.com/tv-logo/tv-logos/main"
KYZU_BASE = "https://raw.githubusercontent.com/K-yzu/Logos/main"

# ISO code -> tv-logo/tv-logos countries/<folder>
COUNTRY_FOLDER = {
    "AL": "albania", "AR": "argentina", "AU": "australia", "AT": "austria",
    "AZ": "azerbaijan", "BE": "belgium", "BR": "brazil", "BG": "bulgaria",
    "CA": "canada", "CL": "chile", "CR": "costa-rica", "HR": "croatia",
    "CZ": "czech-republic", "DK": "nordic/denmark", "FI": "nordic/finland",
    "FR": "france", "DE": "germany", "GR": "greece", "HK": "hong-kong",
    "HU": "hungary", "IS": "nordic/iceland", "IN": "india", "ID": "indonesia",
    "IE": "ireland", "IL": "israel", "IT": "italy", "LB": "lebanon",
    "LT": "lithuania", "LU": "luxembourg", "MY": "malaysia", "MT": "malta",
    "MX": "mexico", "NL": "netherlands", "NZ": "new-zealand", "NO": "nordic/norway",
    "PH": "philippines", "PL": "poland", "PT": "portugal", "RO": "romania",
    "RU": "russia", "RS": "serbia", "SG": "singapore", "SK": "slovakia",
    "SI": "slovenia", "ZA": "south-africa", "ES": "spain", "SE": "nordic/sweden",
    "CH": "switzerland", "TR": "turkey", "UA": "ukraine", "AE": "united-arab-emirates",
    "GB": "united-kingdom", "UK": "united-kingdom", "US": "united-states",
}

# ISO code -> K-yzu folder
KYZU_FOLDER = {
    "US": "TV:US", "GB": "TV:UK", "UK": "TV:UK", "CA": "TV:CA",
    "AU": "TV:AU", "FR": "TV:FR", "NZ": "TV:NZ",
}

# Hand-checked overrides: (Channel Name lower, CC upper) -> direct logo URL.
# Add entries here when auto-resolution picks a wrong/missing logo.
LOGO_OVERRIDES: dict[tuple[str, str], str] = {
    # e.g.: ("mtv classic", "US"): "https://raw.githubusercontent.com/.../mtv-classic-us.png",
}

_logo_cache: dict[tuple[str, str], str] = {}


def slugify(name: str) -> str:
    s = name.lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return re.sub(r"-+", "-", s)


def url_ok(session: requests.Session, url: str, timeout: float = 6.0) -> bool:
    try:
        r = session.head(url, timeout=timeout, allow_redirects=True,
                         headers=HEADERS)
        if r.status_code == 200:
            ct = r.headers.get("Content-Type", "")
            return "image" in ct or "octet-stream" in ct or ct == ""
        # Some hosts (wikimedia/raw) dislike HEAD -> try ranged GET
        if r.status_code in (403, 405):
            g = session.get(url, timeout=timeout, stream=True,
                            headers={**HEADERS, "Range": "bytes=0-0"})
            ok = g.status_code in (200, 206)
            g.close()
            return ok
        return False
    except requests.RequestException:
        return False


def wikimedia_logo(session: requests.Session, channel: str,
                   timeout: float = 8.0) -> str | None:
    """Search Wikimedia Commons for '<channel> logo' and return a thumb URL."""
    try:
        api = "https://commons.wikimedia.org/w/api.php"
        params = {
            "action": "query", "format": "json",
            "generator": "search", "gsrsearch": f"{channel} logo",
            "gsrnamespace": 6, "gsrlimit": 10,
            "prop": "imageinfo", "iiprop": "url|size|mime",
            "iiurlwidth": 512,
        }
        r = session.get(api, params=params, timeout=timeout, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        pages = (data.get("query") or {}).get("pages") or {}
        best = None
        for p in pages.values():
            infos = p.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            mime = info.get("mime", "")
            if not mime.startswith("image/"):
                continue
            url = info.get("thumburl") or info.get("url")
            if not url:
                continue
            url = url.split("?")[0]  # drop Commons utm tracking params
            # prefer files whose title mentions the channel
            title = p.get("title", "").lower()
            score = 1 if channel.lower().split()[0] in title else 0
            if best is None or score > best[0]:
                best = (score, url)
        return best[1] if best else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def resolve_logo(session: requests.Session, channel: str, country: str,
                 fallback: str = "", check: bool = True) -> str:
    """Pick best logo URL: overrides -> tv-logos -> K-yzu -> wikimedia -> tvivu."""
    key = (channel.lower(), (country or "").upper())
    if key in _logo_cache:
        return _logo_cache[key]
    if key in LOGO_OVERRIDES:
        _logo_cache[key] = LOGO_OVERRIDES[key]
        return LOGO_OVERRIDES[key]

    cc = (country or "").upper()
    candidates: list[str] = []

    folder = COUNTRY_FOLDER.get(cc)
    if folder:
        # tv-logo naming: <slug>-<cc-lower>.png  (e.g. mtv-classic-us.png)
        fname = f"{slugify(channel)}-{cc.lower()}.png"
        candidates.append(f"{TV_LOGOS_BASE}/countries/{folder}/{fname}")

    kfolder = KYZU_FOLDER.get(cc)
    if kfolder:
        # K-yzu naming: <Channel Name>.png (spaces kept, URL-encoded)
        fname = urllib.parse.quote(f"{channel}.png")
        candidates.append(f"{KYZU_BASE}/{urllib.parse.quote(kfolder)}/{fname}")
    else:
        fname = urllib.parse.quote(f"{channel}.png")
        candidates.append(f"{KYZU_BASE}/MISC/{fname}")

    if check:
        for url in candidates:
            if url_ok(session, url):
                _logo_cache[key] = url
                return url
        wiki = wikimedia_logo(session, channel)
        if wiki:
            _logo_cache[key] = wiki
            return wiki
    else:
        # no network verification: return first candidate, else fallback
        if candidates:
            _logo_cache[key] = candidates[0]
            return candidates[0]

    _logo_cache[key] = fallback or ""
    return fallback or ""

# ------------------------------------------------------- tvivu extraction

def normalize_escapes(text: str) -> str:
    """Collapse the JS-string escapes tvivu embeds in HTML.

    Watch/category pages embed JSON with single-backslash escaping, e.g.
    ``{\\"slug\\":\\"zo\\'r-tv\\",\\"url\\":\\"https://...m3u8\\u0026exp=..\\"}``.
    Decoding \\uXXXX / \\/ / \\' up front keeps the field regexes simple
    (only \\" then remains as a delimiter).
    """
    text = re.sub(r"\\u([0-9a-fA-F]{4})",
                  lambda m: chr(int(m.group(1), 16)), text)
    return text.replace("\\/", "/").replace("\\'", "'")


# channel card in category HTML (single-backslash escaped JSON):
# {\"slug\":\"mtv-classic-3\",\"name\":\"MTV Classic\",\"logoUrl\":\"...\",\"countryCode\":\"US\",...}
RE_CARD = re.compile(
    r'\\"slug\\":\\"([a-z0-9][a-z0-9\-]*)\\",'
    r'\\"name\\":\\"([^\"\\]*)\\",'
    r'\\"logoUrl\\":\\"([^\"\\]*)\\",'
    r'\\"countryCode\\":\\"([A-Z]{2})\\"'
)

# stream object in watch HTML:
# {\"id\":\"...\",\"url\":\"https://...\",\"streamType\":\"hls\",\"quality\":\"720p\",
#  \"userAgent\":... ,\"referrer\":...}
RE_STREAM = re.compile(
    r'\\"url\\":\\"((?:https?:)?[^\"\\]+?)\\",'
    r'\\"streamType\\":\\"([^\"\\]*)\\",'
    r'\\"quality\\":(?:\\"([^\"\\]*)\\"|null)'
    r'(?:,\\"userAgent\\":(?:\\"([^\"\\]*)\\"|null))?'
    r'(?:,\\"referrer\\":(?:\\"([^\"\\]*)\\"|null))?'
)

RE_CH_META = re.compile(
    r'\\"channel\\":\{\\"id\\":\\"[^\"\\]*\\",'
    r'\\"slug\\":\\"([a-z0-9][a-z0-9\-]*)\\",'
    r'\\"name\\":\\"([^\"\\]*)\\",'
    r'\\"logoUrl\\":\\"([^\"\\]*)\\",'
    r'\\"countryCode\\":\\"([A-Z]{2})\\".*?'
    r'\\"streamUrl\\":\\"([^\"\\]*)\\"'
)


def unescape(s: str) -> str:
    return (s.replace("\\u0026", "&").replace("\\/", "/")
             .replace('\\"', '"').replace("\\'", "'"))


def unwrap_proxy(url: str) -> str:
    """Unwrap tvivu proxy URLs (srv*.pxfy.dev/?url=<encoded>&exp=&sig=)."""
    try:
        p = urllib.parse.urlparse(url)
        if p.netloc.endswith("pxfy.dev") or "url=" in (p.query or ""):
            qs = urllib.parse.parse_qs(p.query)
            if "url" in qs and qs["url"]:
                inner = urllib.parse.unquote(qs["url"][0])
                if inner.startswith("http"):
                    return inner
    except Exception:
        pass
    return url


def fetch_text(session: requests.Session, url: str, timeout: float,
               retries: int = 5) -> str:
    last = None
    for i in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 429:  # rate limited: back off and retry
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else 3.0 * (i + 1)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise last  # type: ignore[misc]


def get_category_channels(session: requests.Session, timeout: float,
                          max_pages: int = 30, delay: float = 0.3,
                          verbose: bool = True) -> list[dict]:
    """Paginate /categories/music?page=N and collect channel cards."""
    channels: dict[str, dict] = {}
    for page in range(1, max_pages + 1):
        url = CATEGORY_URL if page == 1 else f"{CATEGORY_URL}?page={page}"
        html_text = normalize_escapes(fetch_text(session, url, timeout))
        found = 0
        for m in RE_CARD.finditer(html_text):
            slug, raw_name, logo, cc = m.groups()
            if slug == "music" or slug in channels:
                continue
            name = unescape(raw_name)
            channels[slug] = {"slug": slug, "name": name,
                              "logoUrl": logo, "countryCode": cc}
            found += 1
        if verbose:
            print(f"[category] page {page}: +{found} new "
                  f"(total {len(channels)})")
        time.sleep(delay)
        if found == 0:
            break  # last page reached
    return list(channels.values())


def get_channel_streams(session: requests.Session, slug: str,
                        timeout: float) -> dict:
    """Fetch a watch page and return meta + stream list."""
    html_text = normalize_escapes(
        fetch_text(session, f"{WATCH_URL}/{slug}", timeout))
    meta = {"slug": slug, "name": slug, "logoUrl": "",
            "countryCode": "", "streamUrl": ""}
    m = RE_CH_META.search(html_text)
    if m:
        meta = {"slug": m.group(1), "name": unescape(m.group(2)),
                "logoUrl": m.group(3), "countryCode": m.group(4),
                "streamUrl": unescape(m.group(5))}
    streams: list[dict] = []
    for sm in RE_STREAM.finditer(html_text):
        raw_url, stype, quality, ua, ref = sm.groups()
        url = unescape(raw_url)
        if not url.startswith("http"):
            url = "https:" + url if url.startswith("//") else url
        if not url.startswith("http"):
            continue
        streams.append({
            "url": unwrap_proxy(url),
            "raw_url": url,
            "streamType": stype or "hls",
            "quality": quality,
            "userAgent": unescape(ua) if ua else None,
            "referrer": unescape(ref) if ref else None,
        })
    # dedupe by direct URL, keep best quality label
    seen: dict[str, dict] = {}
    for s in streams:
        prev = seen.get(s["url"])
        if prev is None or quality_rank(s["quality"]) > quality_rank(prev["quality"]):
            seen[s["url"]] = s
    return {"meta": meta, "streams": list(seen.values())}


def pick_hd_streams(streams: list[dict], min_height: int,
                    include_unknown: bool = False) -> list[dict]:
    out = []
    for s in streams:
        q = s.get("quality")
        if is_hd_quality(q, min_height):
            out.append(s)
        elif include_unknown and quality_height(q) is None and q in (None, "null"):
            out.append({**s, "quality": "unknown"})
    out.sort(key=lambda s: quality_rank(s.get("quality")), reverse=True)
    return out

# ------------------------------------------------------------------- m3u

SAFE_TVG_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def tvg_id(name: str, country: str) -> str:
    base = re.sub(r"\s+", "", name)
    base = SAFE_TVG_ID_RE.sub("", base)
    return f"{base}.{country.lower()}" if country else base


def m3u_entry(name: str, country: str, logo: str, url: str,
              quality: str | None, user_agent: str | None,
              referrer: str | None) -> str:
    q = (quality or "").strip()
    h = quality_height(q)
    if h is not None and h >= 1080:
        qlabel = "FHD"
    elif h is not None and h >= 720:
        qlabel = "HD"
    elif q and q.lower() != "unknown":
        qlabel = q
    else:
        qlabel = ""
    label = f"{name} ({qlabel})" if qlabel else name
    lines = [f'#EXTINF:-1 tvg-id="{tvg_id(name, country)}" '
             f'tvg-logo="{logo}" tvg-country="{country}" '
             f'group-title="Music",{label}']
    if user_agent:
        lines.append(f"#EXTVLCOPT:http-user-agent={user_agent}")
    if referrer:
        lines.append(f"#EXTVLCOPT:http-referrer={referrer}")
    lines.append(url)
    return "\n".join(lines)


def write_playlist(path: str, entries: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U x-tvg-url=\"\"\n")
        for e in entries:
            f.write(e + "\n")

# ------------------------------------------------------------------- main

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Extract HD/FHD music channels from tvivu.com into an M3U playlist.")
    ap.add_argument("--output", "-o", default="tvivu_music_hd.m3u",
                    help="Output .m3u file (default: tvivu_music_hd.m3u)")
    ap.add_argument("--max-channels", type=int, default=0,
                    help="Limit number of category channels (0 = all)")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel watch-page fetchers (default: 4)")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="HTTP timeout seconds (default: 20)")
    ap.add_argument("--delay", type=float, default=0.25,
                    help="Delay between category page fetches (default: 0.25)")
    ap.add_argument("--min-height", type=int, default=720,
                    help="Minimum height px to count as HD (default: 720)")
    ap.add_argument("--include-unknown", action="store_true",
                    help="Also keep streams with unknown quality")
    ap.add_argument("--no-logo-resolve", action="store_true",
                    help="Skip tv-logos/K-yzu/wikimedia lookup, use tvivu logos")
    ap.add_argument("--dump-json", default="",
                    help="Optional path to dump extracted channel JSON")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session = requests.Session()
    session.headers.update(HEADERS)

    print(f"Fetching music category: {CATEGORY_URL}")
    channels = get_category_channels(session, args.timeout,
                                     delay=args.delay)
    print(f"Found {len(channels)} music channels.")
    if args.max_channels and args.max_channels > 0:
        channels = channels[:args.max_channels]
        print(f"Limited to first {len(channels)} channels.")

    # ---- fetch watch pages in parallel
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(get_channel_streams, requests.Session(),
                          ch["slug"], args.timeout): ch for ch in channels}
        # share default headers with worker sessions
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            ch = futs[fut]
            done += 1
            try:
                data = fut.result()
                # merge category card meta as fallback
                for k in ("name", "logoUrl", "countryCode"):
                    if not data["meta"].get(k) and ch.get(k):
                        data["meta"][k] = ch[k]
                data["meta"].setdefault("slug", ch["slug"])
                results.append(data)
            except Exception as e:  # noqa: BLE001 - report and continue
                print(f"[warn] {ch['slug']}: {e}")
            if done % 25 == 0 or done == len(channels):
                print(f"[watch] {done}/{len(channels)} pages fetched")

    # ---- filter HD/FHD + resolve logos + build entries
    entries: list[str] = []
    kept = skipped = 0
    for data in results:
        meta = data["meta"]
        hd = pick_hd_streams(data["streams"], args.min_height,
                             args.include_unknown)
        if not hd:
            skipped += 1
            continue
        kept += 1
        best = hd[0]  # highest quality stream for this channel
        logo = meta.get("logoUrl", "")
        if not args.no_logo_resolve:
            logo = resolve_logo(session, meta["name"],
                                meta.get("countryCode", ""), logo)
        entries.append(m3u_entry(meta["name"], meta.get("countryCode", ""),
                                 logo, best["url"], best.get("quality"),
                                 best.get("userAgent"), best.get("referrer")))

    entries.sort(key=str.lower)
    write_playlist(args.output, entries)
    print(f"Channels with HD/FHD streams: {kept} | without: {skipped}")
    print(f"Wrote {len(entries)} entries -> {args.output}")

    if args.dump_json:
        dump = [{"slug": d["meta"].get("slug"), "name": d["meta"].get("name"),
                 "country": d["meta"].get("countryCode"),
                 "streams": d["streams"]} for d in results]
        with open(args.dump_json, "w", encoding="utf-8") as f:
            json.dump(dump, f, indent=2, ensure_ascii=False)
        print(f"Dumped raw data -> {args.dump_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
