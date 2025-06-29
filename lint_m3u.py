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



# Check for valid license_key in .mpd stream URLs and try to access the stream with ffmpeg (clearkey only)
license_type = None
license_key = None
for i, line in enumerate(lines):
    lstr = line.strip()
    # Track license_type and license_key from KODIPROP lines
    if lstr.startswith('#KODIPROP:inputstream.adaptive.license_type='):
        license_type = lstr.split('=', 1)[1].strip()
    elif lstr.startswith('#KODIPROP:inputstream.adaptive.license_key='):
        license_key = lstr.split('=', 1)[1].strip()
    elif lstr.startswith('http') and '.mpd' in lstr:
        url = lstr
        # Widevine validation
        if license_type and 'widevine' in license_type.lower():
            if not license_key or not license_key.startswith('http'):
                print(f'::warning file={M3U_FILE},line={i+1}::.mpd stream Widevine license_key is not a URL, skipping ffmpeg decryption check: {url}')
            else:
                import time
                for attempt in range(1, 11):
                    try:
                        result = subprocess.run([
                            'ffmpeg', '-v', 'error', '-y', '-loglevel', 'error',
                            '-wvlicense', license_key,
                            '-i', url,
                            '-t', '1', '-f', 'null', '-'
                        ], capture_output=True, text=True)
                        if result.returncode == 0:
                            break
                        else:
                            if attempt < 10:
                                time.sleep(2)
                            else:
                                print(f'::error file={M3U_FILE},line={i+1}::.mpd Widevine stream could not be accessed or decrypted after 10 retries: {url}\nffmpeg error: {result.stderr.strip()}')
                                sys.exit(1)
                    except Exception as e:
                        if attempt < 10:
                            time.sleep(2)
                        else:
                            print(f'::error file={M3U_FILE},line={i+1}::.mpd Widevine stream ffmpeg check failed after 10 retries: {url}\nException: {e}')
                            sys.exit(1)
        # Clearkey validation
        elif license_type and 'clearkey' in license_type.lower():
            if not license_key or not license_key.startswith('http'):
                print(f'::warning file={M3U_FILE},line={i+1}::.mpd stream license_key is not a URL, skipping ffmpeg decryption check: {url}')
            else:
                print(f'::warning file={M3U_FILE},line={i+1}::.mpd stream license_key is a URL, skipping ffmpeg decryption check: {url}')
        # Reset license_type and license_key for next entry
        license_type = None
        license_key = None

print('Lint passed.')
