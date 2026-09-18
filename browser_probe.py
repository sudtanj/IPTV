"""
This script is for research and personal study purposes only.
Do not use for rebroadcasting or commercial purposes.

Capture the stream URL a broadcaster's own player actually requests.

Regex-scraping a modern live page finds nothing useful: the player gets its
source from an API call made after the page loads, and any URL sitting in the
HTML is often a decoy or a stale default. Driving a real browser and watching
the network is the only way to see what the page ends up playing - and to see
the headers it sends, which these CDNs generally require.

Playwright is optional: if it isn't installed, probing simply yields nothing
and the caller falls back to its other sources.
"""
import os
import re

STREAM_URL_RE = re.compile(r'\.(?:m3u8|mpd)(?:\?|$)', re.IGNORECASE)

# A player fetches its master playlist first, then variants and segments. The
# master is the one worth keeping, so anything that looks like a segment or a
# variant chunklist sorts last.
SEGMENT_HINTS = ('chunklist', 'chunk_', 'segment', 'seg-', '/seg/', 'media_')

# Players differ in whether they autoplay; these cover the common ones.
PLAY_SELECTORS = (
    '.vjs-big-play-button',
    '.jw-icon-display',
    'button[aria-label*="play" i]',
    'button[title*="play" i]',
    '[class*="play-button"]',
    '[class*="btn-play"]',
    'video',
)

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def probe_stream_urls(page_url, nav_timeout_ms=30000, settle_ms=12000,
                      grace_ms=2500, user_agent=DEFAULT_UA):
    """Load `page_url` in a headless browser and return the stream URLs its
    player requests, best first, as (url, headers) pairs. `headers` are the
    ones the browser actually sent, so the caller can reproduce the request.

    Returns [] if Playwright isn't available or the page yields nothing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('  browser probe skipped: playwright not installed')
        return []

    captured = []
    seen = set()

    def on_request(request):
        url = request.url
        if url in seen or not STREAM_URL_RE.search(url):
            return
        seen.add(url)
        headers = request.headers
        kept = {}
        if headers.get('user-agent'):
            kept['User-Agent'] = headers['user-agent']
        if headers.get('referer'):
            kept['Referer'] = headers['referer']
        captured.append((url, kept))

    # Set CHROMIUM_EXECUTABLE_PATH when the available Chromium build doesn't
    # match what this Playwright expects (a preinstalled browser, say);
    # otherwise Playwright uses the one it installed itself.
    executable_path = os.environ.get('CHROMIUM_EXECUTABLE_PATH') or None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True,
                                        executable_path=executable_path,
                                        args=[
                # Without this the player never starts, so it never requests
                # the stream and we capture nothing.
                '--autoplay-policy=no-user-gesture-required',
                '--mute-audio',
                '--disable-dev-shm-usage',
            ])
            try:
                context = browser.new_context(user_agent=user_agent,
                                              ignore_https_errors=True)
                page = context.new_page()
                page.on('request', on_request)
                page.goto(page_url, wait_until='domcontentloaded',
                          timeout=nav_timeout_ms)

                waited = 0
                step = 500
                while waited < settle_ms:
                    if captured:
                        # Let the master's variants arrive before stopping, so
                        # the ranking below has everything to choose from.
                        page.wait_for_timeout(grace_ms)
                        break
                    if waited and waited % 3000 == 0:
                        _try_play(page)
                    page.wait_for_timeout(step)
                    waited += step
            finally:
                browser.close()
    except Exception as e:
        print(f'  browser probe failed for {page_url}: {e}')
        return []

    captured.sort(key=lambda pair: _looks_like_segment(pair[0]))
    return captured


def _looks_like_segment(url):
    lowered = url.lower()
    return any(hint in lowered for hint in SEGMENT_HINTS)


def _try_play(page):
    """Click whatever looks like a play button; players that don't autoplay
    never issue the stream request until something starts them."""
    for selector in PLAY_SELECTORS:
        try:
            element = page.query_selector(selector)
            if element:
                element.click(timeout=1500)
                return
        except Exception:
            continue


if __name__ == '__main__':
    import sys
    for url, headers in probe_stream_urls(sys.argv[1]):
        print(url)
        print(f'    {headers}')
