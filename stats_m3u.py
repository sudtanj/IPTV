import re
from collections import Counter

M3U_FILE = 'index.m3u'

with open(M3U_FILE, encoding='utf-8') as f:
    content = f.read()

channels = re.findall(r'#EXTINF[^\n]*', content)
groups = re.findall(r'group-title="([^"]+)"', content)
tvgids = re.findall(r'tvg-id="([^"]*)"', content)

print(f'Total channels: {len(channels)}')
print(f'Unique groups: {len(set(groups))}')
print(f'Channels per group:')
for group, count in Counter(groups).most_common():
    print(f'  {group}: {count}')
print(f'Channels with tvg-id: {sum(1 for t in tvgids if t)}')
print(f'Channels missing tvg-id: {sum(1 for t in tvgids if not t)}')
