import requests
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

# Check if license_key is a URL and test accessibility (after all other checks)
license_type = None
license_key = None
for i, line in enumerate(lines):
    lstr = line.strip()
    if lstr.startswith('#KODIPROP:inputstream.adaptive.license_type='):
        license_type = lstr.split('=', 1)[1].strip()
    elif lstr.startswith('#KODIPROP:inputstream.adaptive.license_key='):
        license_key = lstr.split('=', 1)[1].strip()
        if license_key.startswith('http'):
            try:
                resp = requests.head(license_key, timeout=10)
                if resp.status_code != 200:
                    print(f'::error file={M3U_FILE},line={i+1}::license_key URL not accessible (status {resp.status_code}): {license_key}')
                    sys.exit(1)
            except Exception as e:
                print(f'::error file={M3U_FILE},line={i+1}::license_key URL not accessible: {license_key} ({e})')
                sys.exit(1)
                
print('Lint passed.')
