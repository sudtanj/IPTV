"""
This script is for research and personal study purposes only.
Do not use for rebroadcasting or commercial purposes.

Scans index.m3u for channels whose stream URLs are all dead, searches public
GitHub code (playlists with the same channel name/tvg-id) for a working
replacement, validates the candidate, and swaps it in.

Replaced URLs are kept as commented-out lines directly below the new one, so
a fix can always be reviewed/reverted from the diff.
"""
import base64
import os
import re
import sys
import time
import urllib.parse

import requests

from validate_streams import STREAM_TYPES

M3U_FILE = 'index.m3u'
GITHUB_API = 'https://api.github.com/search/code'
SEARCH_EXTENSIONS = ('m3u', 'm3u8')
# These are ceilings to keep a single channel's search from running forever,
# not targets: find_replacement() keeps trying every candidate from every
# query/page until one actually verifies, or all of them are exhausted.
MAX_CANDIDATES_PER_QUERY = 20
MAX_FILES_TO_INSPECT = 40
SEARCH_DELAY = 2.5  # stay under GitHub code search rate limits

DUCKDUCKGO_URL = 'https://html.duckduckgo.com/html/'
# The broadcaster's own live page is the most durable source there is, so
# it's checked before any third-party mirror. Keyed by normalized channel
# name (see normalize()); add more channels here as their pages are known.
OFFICIAL_SOURCE_PAGES = {
    'transtv': ('https://www.transtv.co.id/live',),
}
MAX_OFFICIAL_ASSETS = 6
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

WEB_QUERY_TEMPLATES = (
    '"{name}" live stream m3u8',
    '"{name}" m3u8 playlist',
    '"{name}" iptv link',
    '"{name}" m3u8 github',
    '"{name}" streaming url hls',
    '{name} live tv m3u8 2026',
)
MAX_WEB_RESULTS = 20
MAX_WEB_URLS_PER_PAGE = 15

VERIFY_TIMEOUT = 12
VERIFY_RETRIES = 2
VERIFY_RETRY_DELAY = 2
VERIFY_READ_BYTES = 8192

URL_RE = re.compile(r'^https?://\S+$')
STREAM_URL_RE = re.compile(r'https?://[^\s"\'<>]+?\.(?:m3u8|mpd)(?:\?[^\s"\'<>]*)?', re.IGNORECASE)
STREAM_INF_URI_RE = re.compile(r'#EXT-X-STREAM-INF:[^\n]*\n\s*([^\n#][^\n]*)')

# Code-hosting "view this file in the web UI" pages, not direct media
# endpoints. Our own requests.get() happily follows the redirect a
# '?raw=true'/'?raw=1' triggers and sees real playlist content, which is
# exactly the trap: a real IPTV player/app doesn't reliably follow that
# redirect (or gets served the HTML page instead), so the stream just
# doesn't play even though our validator says it's fine. Reject these
# outright rather than relying on validation to catch it.
NON_DIRECT_URL_RE = re.compile(
    r'^https?://(github\.com/[^/]+/[^/]+/blob/'
    r'|gitlab\.com/[^/]+/[^/]+/-/blob/'
    r'|bitbucket\.org/[^/]+/[^/]+/src/)',
    re.IGNORECASE,
)


def is_direct_stream_url(url):
    return not NON_DIRECT_URL_RE.match(url)


# Static file hosts. A playlist committed to one of these is a *file*, not a
# live streaming endpoint: it can't update as segments rotate, so pointing
# our playlist at it just adds an indirection that rots. These always get
# descended into for the real underlying stream URL, never used as-is.
STATIC_FILE_HOST_RE = re.compile(
    r'^https?://(raw\.githubusercontent\.com/'
    r'|gist\.githubusercontent\.com/'
    r'|[^/]+\.github\.io/'
    r'|gitlab\.com/.+/-/raw/'
    r'|raw\.githack\.com/'
    r'|cdn\.jsdelivr\.net/gh/)',
    re.IGNORECASE,
)

# Playlist files routinely keep alternative/backup sources as commented-out
# lines (our own index.m3u does it too). Those are a rich source of extra
# candidates for the same channel.
COMMENTED_URL_RE = re.compile(r'^[ \t]*#+[ \t]*(https?://\S+)[ \t]*$', re.MULTILINE)
IP_HOST_RE = re.compile(r'^https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:/|$)')


def is_static_file_host(url):
    return bool(STATIC_FILE_HOST_RE.match(url))


def candidate_priority(url):
    """Sort key (lower first) favouring sources likely to stay alive: a named
    host over a bare IP:port (unofficial restreams on raw IPs die fastest),
    and HTTPS over plain HTTP (many players refuse mixed content)."""
    score = 0
    if not url.lower().startswith('https://'):
        score += 1
    if IP_HOST_RE.match(url):
        score += 2
    return score


EXTINF_TVG_ID_RE = re.compile(r'tvg-id="([^"]*)"')
EXTINF_TVG_NAME_RE = re.compile(r'tvg-name="([^"]*)"')


# Signed/tokenised stream URLs carry their own expiry. Wowza base64-encodes
# it into a path segment; Akamai and friends put it in a query parameter.
WOWZA_TOKEN_RE = re.compile(r'_tk([A-Za-z0-9+/=_-]{16,})')
EXPIRY_PARAM_KEYS = ('expire', 'expires', 'exp', 'valid_until', 'wowzatokenendtime')


def expired_token_reason(url):
    """Return why a URL's embedded expiry has already passed, or None.

    A signed URL whose token has expired typically answers 403 - which used
    to be read as 'reachable' and promoted a long-dead stream into the
    playlist. Catching the expiry from the URL itself is cheaper and more
    honest than inferring it from the response."""
    now_s = time.time()

    def _check(key, raw):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        # These appear as seconds or milliseconds depending on the CDN.
        for scale in (1.0, 1000.0):
            seconds = value / scale
            # Only trust values landing in a plausible epoch-seconds range.
            if 1_000_000_000 < seconds < 4_000_000_000:
                if seconds < now_s:
                    when = time.strftime('%Y-%m-%d', time.gmtime(seconds))
                    return f'signed URL expired on {when} ({key})'
                return None
        return None

    m = WOWZA_TOKEN_RE.search(url)
    if m:
        blob = m.group(1)
        try:
            decoded = base64.b64decode(blob + '=' * (-len(blob) % 4)).decode('utf-8', 'ignore')
        except Exception:
            decoded = ''
        for key, raw in urllib.parse.parse_qsl(decoded):
            if key.lower() in EXPIRY_PARAM_KEYS:
                reason = _check(key, raw)
                if reason:
                    return reason

    parsed = urllib.parse.urlparse(url)
    for key, raw in urllib.parse.parse_qsl(parsed.query):
        if key.lower() in EXPIRY_PARAM_KEYS:
            reason = _check(key, raw)
            if reason:
                return reason
    # Some CDNs sign via a path segment like /expire/1748334379/
    for key in ('expire', 'expires'):
        m = re.search(rf'/{key}/(\d{{9,14}})(?:/|$)', parsed.path, re.IGNORECASE)
        if m:
            reason = _check(key, m.group(1))
            if reason:
                return reason
    return None


def _fetch(url, headers, read_bytes=VERIFY_READ_BYTES):
    """GET with retries, returning (ok, reason, body_bytes, content_type)."""
    last_reason = 'unknown error'
    for attempt in range(VERIFY_RETRIES):
        try:
            resp = requests.get(url, timeout=VERIFY_TIMEOUT, stream=True,
                                 allow_redirects=True, headers=headers)
        except Exception as e:
            last_reason = str(e)
            time.sleep(VERIFY_RETRY_DELAY)
            continue
        try:
            if resp.status_code == 403:
                return False, 'status 403 (forbidden)', b'', ''
            if resp.status_code not in (200, 206):
                last_reason = f'status {resp.status_code}'
                time.sleep(VERIFY_RETRY_DELAY)
                continue
            body = b''
            try:
                for chunk in resp.iter_content(chunk_size=read_bytes):
                    body += chunk
                    if len(body) >= read_bytes:
                        break
            except Exception as e:
                last_reason = str(e)
                time.sleep(VERIFY_RETRY_DELAY)
                continue
            return True, None, body, resp.headers.get('content-type', '').lower()
        finally:
            resp.close()
        time.sleep(VERIFY_RETRY_DELAY)
    return False, last_reason, b'', ''


def _has_hls_content(text):
    """A playlist that's just '#EXTM3U' with no segments/variants is not
    actually sending any stream data (e.g. a channel that went offline but
    whose endpoint still answers with an empty shell playlist)."""
    if '#EXTINF' in text or '#EXT-X-STREAM-INF' in text:
        return True
    return any(l.strip() and not l.strip().startswith('#') for l in text.splitlines())


RTSP_TIMEOUT = 8


def verify_rtsp(url):
    """RTSP has no HTTP semantics, so this sends a real DESCRIBE request over
    a raw socket and requires both a 200 reply AND an SDP body (a 200 with
    no SDP means the server answered but isn't actually offering a stream)."""
    import socket
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port or 554
    if not host:
        return False, 'invalid RTSP URL (no host)'
    try:
        with socket.create_connection((host, port), timeout=RTSP_TIMEOUT) as sock:
            sock.settimeout(RTSP_TIMEOUT)
            request = (
                f'DESCRIBE {url} RTSP/1.0\r\n'
                'CSeq: 1\r\n'
                'Accept: application/sdp\r\n'
                'User-Agent: iptv-stream-fixer\r\n\r\n'
            )
            sock.sendall(request.encode())
            data = b''
            try:
                while len(data) < 8192:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
    except Exception as e:
        return False, str(e)

    if not data:
        return False, 'no data received from RTSP server'
    text = data.decode('latin-1', errors='ignore')
    status_line = text.splitlines()[0] if text else ''
    if 'RTSP/1.0 200' not in status_line:
        return False, f'RTSP server did not return 200: {status_line or "no response"}'
    if 'v=0' not in text:
        return False, 'RTSP DESCRIBE returned 200 but no SDP body (no real stream data)'
    return True, None


def verify_playable(url, extra_headers=None, _is_variant_check=False,
                    accept_unverifiable=False):
    """Actually fetch the stream and check its real content, not just headers:
    an HLS URL must return a body starting with '#EXTM3U' AND contain actual
    segments/variants, a DASH manifest must contain '<MPD', an RTSP URL must
    complete a real DESCRIBE handshake with an SDP body, anything else falls
    back to a content-type check. A HEAD request (or a GET that only checks
    status/content-type) isn't enough — plenty of dead/expired URLs still
    answer 200 with a plausible content-type but no real data.
    `extra_headers` (e.g. a stream's own Referer/User-Agent from its
    #EXTVLCOPT lines) matters too: some CDNs 403/reject requests that don't
    send the referer a real player would send.

    For an HLS *master* playlist, a #EXTM3U prefix alone isn't proof the
    stream is live: some CDNs (YouTube included) keep serving a
    structurally valid master playlist long after the underlying broadcast
    is over, and only the referenced variant playlist actually fails. So a
    master playlist's first variant is fetched too, one level deep.

    `accept_unverifiable` sets the bar differently depending on the question
    being asked. Deciding whether an entry we already ship is dead, a 403
    means "couldn't check" — some CDNs block our requests but serve real
    players — so we leave that entry alone rather than churn a channel that
    works for viewers. Deciding whether to *promote* a new URL into the
    playlist, a 403 is a rejection: we never shipped a URL we couldn't
    actually read. That asymmetry is deliberate — the bar for replacing has
    to be higher than the bar for keeping."""
    if url.lower().startswith('rtsp://'):
        return verify_rtsp(url)

    expiry_reason = expired_token_reason(url)
    if expiry_reason:
        return False, expiry_reason

    headers = {'User-Agent': 'Mozilla/5.0 (compatible; iptv-stream-fixer)'}
    if extra_headers:
        headers.update(extra_headers)

    ok, reason, body, content_type = _fetch(url, headers)
    if not ok:
        if accept_unverifiable and reason == 'status 403 (forbidden)':
            return True, 'status 403 (could not verify; leaving entry as-is)'
        return False, reason
    if not body:
        return False, 'empty response body'

    text = body.decode('utf-8', errors='ignore').lstrip()
    lower_url = url.lower()

    if '.m3u8' in lower_url or 'mpegurl' in content_type:
        if not text.startswith('#EXTM3U'):
            return False, 'not a valid HLS playlist (missing #EXTM3U)'
        if _is_variant_check:
            if not _has_hls_content(text):
                return False, 'HLS variant playlist has no segments (no real data)'
            return True, None
        m = STREAM_INF_URI_RE.search(text)
        if m:
            variant_url = urllib.parse.urljoin(url, m.group(1).strip())
            child_ok, child_reason = verify_playable(variant_url, extra_headers, _is_variant_check=True)
            if not child_ok:
                return False, f'master playlist variant unreachable: {child_reason}'
            return True, None
        if not _has_hls_content(text):
            return False, 'HLS playlist has no segments (no real data)'
        return True, None
    elif '.mpd' in lower_url or 'dash+xml' in content_type:
        if '<mpd' in text.lower():
            return True, None
        return False, 'not a valid DASH manifest (missing <MPD>)'
    elif any(t in content_type for t in STREAM_TYPES):
        return True, None
    else:
        return False, f'unrecognized content-type: {content_type}'


def gh_headers():
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    headers = {
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'iptv-stream-fixer',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def parse_blocks(lines):
    """Split the file into blank-line-separated blocks, keeping line content."""
    blocks = []
    current = []
    for line in lines:
        if line.strip() == '':
            if current:
                blocks.append(current)
                current = []
            blocks.append([line])  # preserve blank line as its own "block"
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


EXTVLCOPT_UA_RE = re.compile(r'#EXTVLCOPT:http-user-agent=(.+)$')
EXTVLCOPT_REFERRER_RE = re.compile(r'#EXTVLCOPT:http-referrer=(.+)$')


def block_headers(block_text):
    """Pull this entry's own http-user-agent/http-referrer (from its
    #EXTVLCOPT lines) so we probe it the way a real player would."""
    headers = {}
    for line in block_text:
        stripped = line.strip()
        m = EXTVLCOPT_UA_RE.match(stripped)
        if m:
            headers['User-Agent'] = m.group(1).strip()
        m = EXTVLCOPT_REFERRER_RE.match(stripped)
        if m:
            headers['Referer'] = m.group(1).strip()
    return headers


def channel_names(block_text):
    tvg_id = ''
    tvg_name = ''
    display_name = ''
    for line in block_text:
        if line.strip().startswith('#EXTINF'):
            m = EXTINF_TVG_ID_RE.search(line)
            if m:
                tvg_id = m.group(1)
            m = EXTINF_TVG_NAME_RE.search(line)
            if m:
                tvg_name = m.group(1)
            display_name = line.rsplit(',', 1)[-1].strip()
    names = []
    for n in (tvg_id, tvg_name, display_name):
        n = n.strip()
        if n and n not in names and n not in ('__', '-1'):
            names.append(n)
    return names


def search_github_for_candidates(names):
    """Search public GitHub code for playlists mentioning any of `names`."""
    candidates = []  # list of (owner/repo, raw_url)
    seen_files = set()
    for name in names:
        if not name:
            continue
        for ext in SEARCH_EXTENSIONS:
            query = f'"{name}" extension:{ext}'
            try:
                resp = requests.get(
                    GITHUB_API,
                    params={'q': query, 'per_page': MAX_CANDIDATES_PER_QUERY},
                    headers=gh_headers(),
                    timeout=15,
                )
            except Exception as e:
                print(f'  search error for {query!r}: {e}')
                time.sleep(SEARCH_DELAY)
                continue
            time.sleep(SEARCH_DELAY)
            if resp.status_code != 200:
                print(f'  search failed ({resp.status_code}) for {query!r}: {resp.text[:200]}')
                continue
            for item in resp.json().get('items', []):
                html_url = item.get('html_url', '')
                if '/blob/' not in html_url:
                    continue
                raw_url = html_url.replace('github.com', 'raw.githubusercontent.com').replace('/blob/', '/')
                if raw_url in seen_files:
                    continue
                seen_files.add(raw_url)
                candidates.append(raw_url)
                if len(candidates) >= MAX_FILES_TO_INSPECT:
                    return candidates
    return candidates


def normalize(name):
    """Fold a channel name to bare alphanumerics for tolerant-but-exact matching
    (e.g. 'TRANS 7' and 'Trans7' normalize equal; 'RTV' and 'RTV Sellingen' do not)."""
    return re.sub(r'[^a-z0-9]', '', name.lower())


def extract_channel_entries(content):
    """Parse a raw playlist into (tvg_id, tvg_name, display_name, url) entries,
    each url anchored to the #EXTINF line that actually precedes it."""
    entries = []
    current = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith('#EXTINF'):
            m_id = EXTINF_TVG_ID_RE.search(stripped)
            m_name = EXTINF_TVG_NAME_RE.search(stripped)
            current = {
                'tvg_id': m_id.group(1) if m_id else '',
                'tvg_name': m_name.group(1) if m_name else '',
                'display_name': stripped.rsplit(',', 1)[-1].strip(),
            }
        elif current is not None and URL_RE.match(stripped):
            current['url'] = stripped
            entries.append(current)
            current = None
    return entries


def matching_urls(content, names):
    """Return candidate URLs whose owning #EXTINF entry names the same channel
    as one of `names`, via exact match after normalization (not substring)."""
    target_norms = {normalize(n) for n in names if n and len(normalize(n)) >= 3}
    if not target_norms:
        return []
    found = []
    for entry in extract_channel_entries(content):
        entry_norms = {
            normalize(entry.get(f, ''))
            for f in ('tvg_id', 'tvg_name', 'display_name')
        }
        entry_norms.discard('')
        if entry_norms & target_norms:
            found.append(entry['url'])
    return found


def duckduckgo_search(query, max_results=MAX_WEB_RESULTS):
    """Keyless web search via DuckDuckGo's HTML endpoint (no API key needed).
    Best-effort: DuckDuckGo may rate-limit or change markup, so failures here
    just mean this source contributes nothing, not a hard error."""
    try:
        resp = requests.get(
            DUCKDUCKGO_URL,
            params={'q': query},
            headers={'User-Agent': 'Mozilla/5.0 (compatible; iptv-stream-fixer)'},
            timeout=15,
        )
    except Exception as e:
        print(f'  web search error for {query!r}: {e}')
        return []
    if resp.status_code != 200:
        print(f'  web search failed ({resp.status_code}) for {query!r}')
        return []
    results = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"', resp.text):
        target = m.group(1)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(target).query)
        real_url = qs.get('uddg', [target])[0]
        real_url = urllib.parse.unquote(real_url)
        if real_url not in results:
            results.append(real_url)
        if len(results) >= max_results:
            break
    return results


def search_web_for_candidates(names):
    """Broaden discovery beyond GitHub code search: look up the channel name
    on the open web (trying several query phrasings, not just one) and pull
    any .m3u8/.mpd URLs out of pages that actually mention the channel by
    (close to) its full name."""
    searchable = [n for n in names if len(normalize(n)) >= 4]
    if not searchable:
        return []
    query_name = searchable[0]
    name_patterns = [re.compile(r'\b' + re.escape(n) + r'\b', re.IGNORECASE) for n in searchable]
    candidates = []
    seen_urls = set()
    seen_pages = set()

    for template in WEB_QUERY_TEMPLATES:
        if len(candidates) >= MAX_FILES_TO_INSPECT:
            break
        pages = duckduckgo_search(template.format(name=query_name))
        time.sleep(SEARCH_DELAY)

        for page_url in pages:
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            try:
                resp = requests.get(page_url, timeout=10,
                                     headers={'User-Agent': 'Mozilla/5.0 (compatible; iptv-stream-fixer)'})
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            text = resp.text
            if not any(p.search(text) for p in name_patterns):
                continue  # page doesn't actually mention this channel by name
            found_here = 0
            for m in STREAM_URL_RE.finditer(text):
                url = m.group(0)
                if url in seen_urls or not is_direct_stream_url(url):
                    continue
                seen_urls.add(url)
                candidates.append(url)
                found_here += 1
                if found_here >= MAX_WEB_URLS_PER_PAGE:
                    break
            if len(candidates) >= MAX_FILES_TO_INSPECT:
                break
    return candidates


SCRIPT_SRC_RE = re.compile(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', re.IGNORECASE)


def extract_stream_urls(text):
    """Stream URLs on a broadcaster page are usually embedded in JSON or JS
    rather than sitting in plain HTML, so undo the usual manglings (escaped
    slashes, HTML entities) before looking for them."""
    unescaped = (text.replace('\\/', '/')
                     .replace('&#x2F;', '/').replace('&#47;', '/')
                     .replace('\\u0026', '&').replace('&amp;', '&'))
    found = []
    for m in STREAM_URL_RE.finditer(unescaped):
        url = m.group(0)
        if url not in found:
            found.append(url)
    return found


def search_official_page_for_candidates(names):
    """Scrape the broadcaster's own live page for its stream URL. Returns
    (url, headers) pairs: such a stream usually only plays when the request
    carries the broadcaster's page as Referer, so the headers that made it
    verify need to travel with it into the playlist entry."""
    pages = []
    for name in names:
        for page in OFFICIAL_SOURCE_PAGES.get(normalize(name), ()):
            if page not in pages:
                pages.append(page)
    if not pages:
        return []

    origin = None
    results = []
    for page_url in pages:
        parsed = urllib.parse.urlparse(page_url)
        origin = f'{parsed.scheme}://{parsed.netloc}/'
        headers = {'User-Agent': BROWSER_UA, 'Referer': origin}
        try:
            resp = requests.get(page_url, timeout=15, headers=headers)
        except Exception as e:
            print(f'  official page error for {page_url}: {e}')
            continue
        if resp.status_code != 200:
            print(f'  official page failed ({resp.status_code}): {page_url}')
            continue

        found = extract_stream_urls(resp.text)
        if not found:
            # A modern site loads the stream from a JS bundle rather than
            # inlining it, so follow a few of the page's own scripts.
            for i, m in enumerate(SCRIPT_SRC_RE.finditer(resp.text)):
                if i >= MAX_OFFICIAL_ASSETS:
                    break
                asset_url = urllib.parse.urljoin(page_url, m.group(1))
                if urllib.parse.urlparse(asset_url).netloc != parsed.netloc:
                    continue  # third-party script, not the player config
                try:
                    asset = requests.get(asset_url, timeout=10, headers=headers)
                except Exception:
                    continue
                if asset.status_code == 200:
                    found.extend(u for u in extract_stream_urls(asset.text) if u not in found)

        for url in found:
            results.append((url, {'User-Agent': BROWSER_UA, 'Referer': origin}))
        if found:
            print(f'  official page {page_url}: found {len(found)} stream url(s)')
    return results


def collect_inner_urls(content, names, base_url):
    """Pull every plausible stream URL *out of* a playlist file: entries that
    name this channel, the variants of an HLS master playlist, and the
    commented-out backup sources these files usually carry. Returns None if
    the file is a multi-channel list with nothing naming this channel (too
    ambiguous to guess from)."""
    urls = []
    entries = extract_channel_entries(content)
    if entries:
        matched = matching_urls(content, names)
        if matched:
            urls.extend(matched)
        elif len(entries) == 1:
            urls.append(entries[0]['url'])
        else:
            return None

    # An HLS master playlist points at variant playlists rather than naming
    # channels, so #EXTINF parsing finds nothing in it.
    for m in STREAM_INF_URI_RE.finditer(content):
        urls.append(urllib.parse.urljoin(base_url, m.group(1).strip()))

    for m in COMMENTED_URL_RE.finditer(content):
        urls.append(m.group(1).strip())

    deduped = []
    for u in urls:
        if u not in deduped:
            deduped.append(u)
    return deduped


def resolve_candidate_file(file_url, names, exclude_urls, extra_headers=None):
    """A discovered .m3u8/.mpd URL might already be the real live stream, or
    it might be a *file* that merely points at one — a per-channel mirror
    committed to a GitHub repo, say. Pointing our playlist at such a file
    doesn't work for a real player (and can't keep working, since a static
    file can't update as live segments rotate), even though fetching it
    returns perfectly valid playlist content that fools a naive check.

    So: for a file on a static host, never use the file URL itself — descend
    and find the real stream it references. For a URL on a normal host, try
    it directly first (a genuine CDN master playlist is exactly what we
    want), then fall back to descending into it."""
    try:
        resp = requests.get(file_url, timeout=10,
                             headers={'User-Agent': 'Mozilla/5.0 (compatible; iptv-stream-fixer)',
                                      **(extra_headers or {})})
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    content = resp.text

    static_host = is_static_file_host(file_url)
    is_playlist = content.lstrip().startswith('#EXTM3U')

    if not static_host and not is_playlist:
        # Not a playlist we can read (e.g. a DASH .mpd) - try it as-is.
        candidates = [file_url]
    else:
        inner = collect_inner_urls(content, names, file_url) if is_playlist else []
        if inner is None:
            return None  # ambiguous multi-channel list
        inner.sort(key=candidate_priority)
        # A real host's own URL is a legitimate endpoint, so try it first;
        # a static-host file URL is never usable, so it isn't a candidate.
        candidates = inner if static_host else [file_url] + inner

    for candidate_url in candidates:
        if candidate_url in exclude_urls or not is_direct_stream_url(candidate_url):
            continue
        if is_static_file_host(candidate_url):
            continue  # another static file: not a usable endpoint either
        ok, _ = verify_playable(candidate_url, extra_headers=extra_headers)
        if ok:
            return candidate_url
    return None


def find_replacement(names, exclude_urls):
    """Returns (url, source, headers) — `headers` are the request headers the
    replacement needs to play (None when it needs nothing special), so the
    caller can write them onto the playlist entry."""
    # The broadcaster's own page first: nothing else is as durable.
    for url, headers in search_official_page_for_candidates(names):
        if url in exclude_urls or not is_direct_stream_url(url) or is_static_file_host(url):
            continue
        result = resolve_candidate_file(url, names, exclude_urls, extra_headers=headers)
        if result:
            return result, 'official site', headers

    for raw_url in search_github_for_candidates(names):
        result = resolve_candidate_file(raw_url, names, exclude_urls)
        if result:
            return result, raw_url, None

    for file_url in search_web_for_candidates(names):
        if file_url in exclude_urls or not is_direct_stream_url(file_url):
            continue
        result = resolve_candidate_file(file_url, names, exclude_urls)
        if result:
            return result, 'web search', None

    return None, None, None


def block_url_lines(block):
    """Return indices of active (non-commented) URL lines in a block."""
    return [i for i, l in enumerate(block) if URL_RE.match(l.strip())]


def apply_entry_headers(block, headers):
    """Rewrite the entry's #EXTVLCOPT lines to the headers the new stream
    needs. The old entry's referrer belongs to the old source, so leaving it
    in place would stop the replacement playing on a CDN that checks it."""
    if not headers:
        return block
    wanted = []
    if headers.get('User-Agent'):
        wanted.append(f"#EXTVLCOPT:http-user-agent={headers['User-Agent']}\n")
    if headers.get('Referer'):
        wanted.append(f"#EXTVLCOPT:http-referrer={headers['Referer']}\n")

    stripped = [l for l in block
                if not (EXTVLCOPT_UA_RE.match(l.strip())
                        or EXTVLCOPT_REFERRER_RE.match(l.strip()))]
    for i, line in enumerate(stripped):
        if line.strip().startswith('#EXTINF'):
            return stripped[:i + 1] + wanted + stripped[i + 1:]
    return wanted + stripped


def main():
    with open(M3U_FILE, encoding='utf-8') as f:
        lines = f.readlines()

    blocks = parse_blocks(lines)
    replaced = []
    still_broken = []

    for block in blocks:
        if not any(l.strip().startswith('#EXTINF') for l in block):
            continue

        url_idxs = block_url_lines(block)
        if not url_idxs:
            continue

        names = channel_names(block)
        display = names[-1] if names else '(unknown channel)'
        headers = block_headers(block)

        any_alive = False
        dead_urls = []
        for idx in url_idxs:
            url = block[idx].strip()
            # Lenient here: only replace an entry we can actually prove dead.
            ok, _ = verify_playable(url, extra_headers=headers, accept_unverifiable=True)
            if ok:
                any_alive = True
                break
            dead_urls.append(url)

        if any_alive:
            continue

        print(f'Dead channel: {display} ({len(dead_urls)} url(s) checked)')
        existing_urls = set(dead_urls)
        for l in block:
            if l.strip().startswith('#http'):
                existing_urls.add(l.strip().lstrip('#').strip())

        replacement, source, needed_headers = find_replacement(names, existing_urls)
        if replacement:
            print(f'  -> replacement found: {replacement} (from {source})')
            new_line = replacement + '\n'
            commented_old = [f'#{block[i].rstrip(chr(10))}\n' for i in url_idxs]
            new_block = []
            inserted = False
            for i, l in enumerate(block):
                if i in url_idxs:
                    if not inserted:
                        new_block.append(new_line)
                        new_block.extend(commented_old)
                        inserted = True
                    continue
                new_block.append(l)
            block[:] = apply_entry_headers(new_block, needed_headers)
            replaced.append((display, replacement, source))
        else:
            print('  -> no working replacement found')
            still_broken.append(display)

    if replaced:
        out_lines = []
        for block in blocks:
            out_lines.extend(block)
        with open(M3U_FILE, 'w', encoding='utf-8') as f:
            f.writelines(out_lines)

    summary_lines = ['## Stream fix summary', '']
    if replaced:
        summary_lines.append(f'### Replaced ({len(replaced)})')
        for name, url, source in replaced:
            summary_lines.append(f'- **{name}**: `{url}` (found via {source})')
    else:
        summary_lines.append('### Replaced (0)')
        summary_lines.append('No dead channels had a working replacement found.')
    if still_broken:
        summary_lines.append('')
        summary_lines.append(f'### Still broken, no replacement found ({len(still_broken)})')
        for name in still_broken:
            summary_lines.append(f'- {name}')

    summary = '\n'.join(summary_lines)
    print('\n' + summary)

    step_summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if step_summary:
        with open(step_summary, 'a', encoding='utf-8') as f:
            f.write(summary + '\n')


if __name__ == '__main__':
    main()
