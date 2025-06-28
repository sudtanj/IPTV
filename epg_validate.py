import re
import requests
import sys
from xml.etree import ElementTree

M3U_FILE = 'index.m3u'
TIMEOUT = 15

with open(M3U_FILE, encoding='utf-8') as f:
    content = f.read()

# Find all EPG URLs in the playlist
urls = re.findall(r'(https?://[^\s,]+\.xml)', content)
if not urls:
    print('No EPG XML URLs found.')
    sys.exit(0)

failures = []
for url in set(urls):
    print(f'Checking EPG: {url}')
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            failures.append(f'{url} - HTTP {r.status_code}')
            continue
        # Try parsing XML
        try:
            ElementTree.fromstring(r.content)
        except Exception as e:
            failures.append(f'{url} - XML parse error: {e}')
    except Exception as e:
        failures.append(f'{url} - ERROR: {e}')

if failures:
    print('Some EPG files failed:')
    for fail in failures:
        print(fail)
    sys.exit(1)
else:
    print('All EPG XML files are valid.')
