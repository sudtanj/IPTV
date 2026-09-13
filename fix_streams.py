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

import requests

from validate_streams import STREAM_TYPES

M3U_FILE = 'index.m3u'
GITHUB_API = 'https://api.github.com/search/code'
SEARCH_EXTENSIONS = ('m3u', 'm3u8')
MAX_CANDIDATES_PER_QUERY = 5
MAX_FILES_TO_INSPECT = 8
SEARCH_DELAY = 2.5  # stay under GitHub code search rate limits

VERIFY_TIMEOUT = 12
VERIFY_RETRIES = 2
VERIFY_RETRY_DELAY = 2
VERIFY_READ_BYTES = 4096

URL_RE = re.compile(r'^https?://\S+$')
EXTINF_TVG_ID_RE = re.compile(r'tvg-id="([^"]*)"')
EXTINF_TVG_NAME_RE = re.compile(r'tvg-name="([^"]*)"')


def verify_playable(url, extra_headers=None):
    """Actually fetch the stream and check its real content, not just headers:
    an HLS URL must return a body starting with '#EXTM3U', a DASH manifest
    must contain '<MPD', anything else falls back to a content-type check.
    A HEAD request (or a GET that only checks status/content-type) isn't
    enough — plenty of dead/expired URLs still answer 200 with a plausible
    content-type. `extra_headers` (e.g. a stream's own Referer/User-Agent
    from its #EXTVLCOPT lines) matters too: some CDNs 403/reject requests
    that don't send the referer a real player would send."""
    last_reason = 'unknown error'
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; iptv-stream-fixer)'}
    if extra_headers:
        headers.update(extra_headers)
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
                return True, 'status 403 (forbidden, assumed reachable)'
            if resp.status_code not in (200, 206):
                last_reason = f'status {resp.status_code}'
                time.sleep(VERIFY_RETRY_DELAY)
                continue

            body = b''
            try:
                for chunk in resp.iter_content(chunk_size=VERIFY_READ_BYTES):
                    body += chunk
                    if len(body) >= VERIFY_READ_BYTES:
                        break
            except Exception as e:
                last_reason = str(e)
                time.sleep(VERIFY_RETRY_DELAY)
                continue

            content_type = resp.headers.get('content-type', '').lower()
            text = body.decode('utf-8', errors='ignore').lstrip()
            lower_url = url.lower()

            if not body:
                last_reason = 'empty response body'
            elif '.m3u8' in lower_url or 'mpegurl' in content_type:
                if text.startswith('#EXTM3U'):
                    return True, None
                last_reason = 'not a valid HLS playlist (missing #EXTM3U)'
            elif '.mpd' in lower_url or 'dash+xml' in content_type:
                if '<mpd' in text.lower():
                    return True, None
                last_reason = 'not a valid DASH manifest (missing <MPD>)'
            elif any(t in content_type for t in STREAM_TYPES):
                return True, None
            else:
                last_reason = f'unrecognized content-type: {content_type}'
        finally:
            resp.close()
        time.sleep(VERIFY_RETRY_DELAY)
    return False, last_reason


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


def find_replacement(names, exclude_urls):
    raw_files = search_github_for_candidates(names)
    for raw_url in raw_files:
        try:
            resp = requests.get(raw_url, timeout=10)
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        for candidate_url in matching_urls(resp.text, names):
            if candidate_url in exclude_urls:
                continue
            ok, _ = verify_playable(candidate_url)
            if ok:
                return candidate_url, raw_url
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
