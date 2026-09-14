"""
This script is for research and personal study purposes only.
Do not use for rebroadcasting or commercial purposes.

Scans index.m3u for channels whose stream URLs are all dead, searches public
GitHub code (playlists with the same channel name/tvg-id) for a working
replacement, validates the candidate, and swaps it in.

Replaced URLs are kept as commented-out lines directly below the new one, so
a fix can always be reviewed/reverted from the diff.
"""
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
WEB_QUERY_TEMPLATES = (
    '"{name}" live stream m3u8',
    '"{name}" m3u8 playlist',
    '"{name}" iptv link',
)
MAX_WEB_RESULTS = 15
MAX_WEB_URLS_PER_PAGE = 10

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
EXTINF_TVG_ID_RE = re.compile(r'tvg-id="([^"]*)"')
EXTINF_TVG_NAME_RE = re.compile(r'tvg-name="([^"]*)"')


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
                return True, 'status 403 (forbidden, assumed reachable)', b'', ''
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


def verify_playable(url, extra_headers=None, _is_variant_check=False):
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
    master playlist's first variant is fetched too, one level deep."""
    if url.lower().startswith('rtsp://'):
        return verify_rtsp(url)

    headers = {'User-Agent': 'Mozilla/5.0 (compatible; iptv-stream-fixer)'}
    if extra_headers:
        headers.update(extra_headers)

    ok, reason, body, content_type = _fetch(url, headers)
    if not ok:
        return False, reason
    if reason == 'status 403 (forbidden, assumed reachable)':
        return True, reason
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


def resolve_candidate_file(file_url, names, exclude_urls):
    """A discovered .m3u8/.mpd URL might already be the real stream, or it
    might be an M3U 'wrapper' file (common for personal per-channel mirror
    repos) that just lists one or more real links inside — in which case
    pointing our own playlist at the wrapper URL itself doesn't work for a
    real player even though it looks like valid M3U/HLS content. Fetch it,
    and if it parses as a list of #EXTINF entries, test the entries that
    match this channel by name (or the single entry, if the file only has
    one) rather than the wrapper URL itself."""
    try:
        resp = requests.get(file_url, timeout=10,
                             headers={'User-Agent': 'Mozilla/5.0 (compatible; iptv-stream-fixer)'})
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    content = resp.text

    inner_urls = None
    if content.lstrip().startswith('#EXTM3U'):
        entries = extract_channel_entries(content)
        if entries:
            matched = matching_urls(content, names)
            if matched:
                inner_urls = matched
            elif len(entries) == 1:
                inner_urls = [entries[0]['url']]
            else:
                # multiple entries, none naming this channel: too ambiguous
                # to guess which one is meant, so this file contributes
                # nothing (do NOT fall back to the wrapper URL itself)
                return None
    if inner_urls is None:
        # Not recognizable as an M3U wrapper list at all - the file URL
        # itself might be a direct manifest (e.g. a real .mpd found via web
        # search), so try it as-is.
        inner_urls = [file_url]

    for candidate_url in inner_urls:
        if candidate_url in exclude_urls or not is_direct_stream_url(candidate_url):
            continue
        ok, _ = verify_playable(candidate_url)
        if ok:
            return candidate_url
    return None


def find_replacement(names, exclude_urls):
    for raw_url in search_github_for_candidates(names):
        result = resolve_candidate_file(raw_url, names, exclude_urls)
        if result:
            return result, raw_url

    for file_url in search_web_for_candidates(names):
        if file_url in exclude_urls or not is_direct_stream_url(file_url):
            continue
        result = resolve_candidate_file(file_url, names, exclude_urls)
        if result:
            return result, 'web search'

    return None, None


def block_url_lines(block):
    """Return indices of active (non-commented) URL lines in a block."""
    return [i for i, l in enumerate(block) if URL_RE.match(l.strip())]


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
            ok, _ = verify_playable(url, extra_headers=headers)
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

        replacement, source = find_replacement(names, existing_urls)
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
            block[:] = new_block
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
