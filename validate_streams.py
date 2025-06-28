"""
This script is for research and personal study purposes only.
Do not use for rebroadcasting or commercial purposes.
"""
import re
import requests
import time

M3U_FILE = 'index.m3u'
MAX_RETRIES = 3
DELAY = 2
TIMEOUT = 10

STREAM_TYPES = [
    'mpegurl', 'dash+xml', 'mp2t', 'video/', 'application/octet-stream', 'vnd.apple.mpegurl'
]

def check_stream(url):
    tries = 0
    while tries < MAX_RETRIES:
        try:
            r = requests.head(url, timeout=TIMEOUT, allow_redirects=True)
            if r.status_code in (200, 206):
                ct = r.headers.get('content-type', '').lower()
                if any(x in ct for x in STREAM_TYPES):
                    return True, None
                else:
                    return True, f'Unusual content-type: {ct}'
            elif r.status_code == 404:
                tries += 1
                if tries < MAX_RETRIES:
                    time.sleep(DELAY)
                    continue
                else:
                    return False, f'Status 404 after {MAX_RETRIES} tries'
            elif r.status_code == 403:
                return True, 'Status 403 (forbidden)'
            elif r.status_code >= 400:
                return False, f'Status {r.status_code}'
            return False, f'Status {r.status_code}'
        except Exception as e:
            if tries < MAX_RETRIES - 1:
                time.sleep(DELAY)
                tries += 1
                continue
            return False, str(e)
    return False, 'Max retries reached'

def main():
    with open(M3U_FILE, encoding='utf-8') as f:
        lines = f.readlines()
    url_pattern = re.compile(r'^(https?://[^\s]+)$', re.MULTILINE)
    urls = []
    widevine_urls = set()
    # Track if previous lines contain Widevine license type
    for i, line in enumerate(lines):
        if url_pattern.match(line.strip()):
            url = line.strip()
            # Look back a few lines for license type
            is_widevine = False
            for j in range(max(0, i-3), i):
                if 'inputstream.adaptive.license_type=com.widevine.alpha' in lines[j]:
                    is_widevine = True
                    break
            if is_widevine:
                widevine_urls.add(url)
            urls.append(url)

    failed = False
    for url in urls:
        if url in widevine_urls:
            ok, msg = check_stream(url)
            if ok:
                print(f'::warning file={M3U_FILE}::Widevine stream {url}: URL reachable, but full validation not possible (DRM protected).')
            else:
                print(f'::error file={M3U_FILE}::Widevine stream {url}: URL unreachable or invalid ({msg})')
                failed = True
        else:
            ok, msg = check_stream(url)
            if ok:
                if msg:
                    print(f'::warning file={M3U_FILE}::Stream {url}: {msg}')
                else:
                    print(f'OK: {url}')
            else:
                print(f'::error file={M3U_FILE}::Broken or invalid stream: {url} ({msg})')
                failed = True
    if failed:
        exit(1)

if __name__ == '__main__':
    main()
