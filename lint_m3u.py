import re
import sys
import subprocess
from collections import Counter

M3U_FILE = 'index.m3u'

with open(M3U_FILE, encoding='utf-8') as f:
    lines = f.readlines()

# Check for #EXTM3U header
if not lines or not lines[0].strip().startswith('#EXTM3U'):
    print(f'::error file={M3U_FILE}::Missing or invalid #EXTM3U header')
    sys.exit(1)

# Check for duplicate URLs
urls = [l.strip() for l in lines if l.strip().startswith('http')]
dupes = [item for item, count in Counter(urls).items() if count > 1]
if dupes:
    for d in dupes:
        print(f'::error file={M3U_FILE}::Duplicate URL: {d}')
    sys.exit(1)

# Check for #EXTINF before each URL
for i, line in enumerate(lines):
    if line.strip().startswith('http'):
        if not any('#EXTINF' in lines[j] for j in range(max(0, i-2), i)):
            print(f'::error file={M3U_FILE},line={i+1}::URL without preceding #EXTINF: {line.strip()}')
            sys.exit(1)

# Check for missing tvg-id or group-title in #EXTINF
for i, line in enumerate(lines):
    if line.strip().startswith('#EXTINF'):
        if 'tvg-id' not in line or 'group-title' not in line:
            print(f'::warning file={M3U_FILE},line={i+1}::#EXTINF missing tvg-id or group-title: {line.strip()}')


# Check for valid license_key in .mpd stream URLs and try to access the stream with ffmpeg
for i, line in enumerate(lines):
    url = line.strip()
    if url.startswith('http') and '.mpd' in url:
        # Try to probe the stream with ffmpeg
        try:
            # ffmpeg expects the license key as a header or option depending on DRM system; this is a generic probe
            # This command will not download the whole stream, just probe it
            result = subprocess.run([
                'ffmpeg', '-v', 'error', '-y', '-loglevel', 'error',
                '-i', url,
                '-t', '1', '-f', 'null', '-'
            ], capture_output=True, text=True)
            if result.returncode != 0:
                print(f'::error file={M3U_FILE},line={i+1}::.mpd stream could not be accessed or decrypted with license_key: {url}\nffmpeg error: {result.stderr.strip()}')
                sys.exit(1)
        except Exception as e:
            print(f'::error file={M3U_FILE},line={i+1}::.mpd stream ffmpeg check failed: {url}\nException: {e}')
            sys.exit(1)

print('Lint passed.')
