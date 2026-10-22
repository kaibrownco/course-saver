from __future__ import annotations

import json
import time
from urllib.parse import urlparse

import requests

DEFAULT_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')


def load_cookies(source):
    """Load cookies from a file path or an already-parsed object. Accepts a browser-extension
    export / list of cookie dicts, or a simple {name: value} mapping. Returns (cookies, user_agent)."""
    if isinstance(source, (str, bytes)) or hasattr(source, '__fspath__'):
        with open(source) as f:
            raw = json.load(f)
    else:
        raw = source
    ua = None
    cookies = []
    if isinstance(raw, list):
        for c in raw:
            cookies.append((c['name'], c['value'], c.get('domain'), c.get('path', '/')))
    else:
        ua = raw.get('userAgent')
        cookies = [(k, v, None, '/') for k, v in raw.items() if k != 'userAgent']
    return cookies, ua


class SessionExpired(PermissionError):
    """The saved login no longer works; the user needs to sign in again."""


class SiteBlocked(Exception):
    """The site's bot protection (Cloudflare) is showing a security check instead of the page."""


BLOCKED_MESSAGE = ('The website is showing a security check and blocked the app. Wait a few minutes and try again. '
                   'If it keeps happening, switch networks (Wi-Fi or mobile data) and try once more.')

LOGIN_PATHS = ('/login', '/sign_in', '/sign-in')


def is_login_url(url: str) -> bool:
    return urlparse(url).path.rstrip('/') in LOGIN_PATHS


def is_challenge(resp) -> bool:
    """Cloudflare's 'prove you are human' response (it marks these with a cf-mitigated header)."""
    return resp.headers.get('cf-mitigated', '').lower() == 'challenge'


class Fetcher:
    def __init__(self, cookies, retries: int = 3):
        self.session = requests.Session()
        cookies, ua = load_cookies(cookies)
        self.session.headers.update({
            'User-Agent': ua or DEFAULT_UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        for name, value, domain, path in cookies:  # noqa
            kwargs = {'path': path}
            if domain:
                kwargs['domain'] = domain
            self.session.cookies.set(name, value, **kwargs)
        self.retries = retries

    def get(self, url, **kwargs):
        """GET with simple retry/backoff on network errors and 5xx/429."""
        kwargs.setdefault('timeout', 30)
        last = None
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, **kwargs)
                if resp.status_code not in (429, 500, 502, 503, 504):
                    return resp
                last = requests.HTTPError(f'{resp.status_code} for {url}')
            except requests.RequestException as e:
                last = e
            time.sleep(2 ** attempt)
        raise last

    def _check(self, resp):
        """Turn the ways a request can quietly go wrong into clear errors.

        Order matters: a logged-out request is redirected to /login, and the login page itself may then be blocked by
        Cloudflare (403). That is still an expired session (sign in again), not a generic HTTP error."""
        if is_login_url(resp.url):
            raise SessionExpired(f'Redirected to login ({resp.url}); the session has expired.')
        if is_challenge(resp):
            raise SiteBlocked(BLOCKED_MESSAGE)
        if resp.status_code == 401:
            raise SessionExpired('The site rejected the saved sign-in.')
        resp.raise_for_status()

    def get_page(self, url, **kwargs):
        """GET an HTML page. Raises SessionExpired if we were bounced to the login page, SiteBlocked if the
        site's bot protection stepped in."""
        resp = self.get(url, **kwargs)
        self._check(resp)
        return resp

    def get_file(self, url, **kwargs):
        """Start downloading a file (streamed). Raises SessionExpired if the site answered with its login page or
        any web page instead of the file, so an expired session can never be saved as a corrupt "file"."""
        resp = self.get(url, stream=True, **kwargs)
        self._check(resp)
        if resp.headers.get('content-type', '').lower().startswith('text/html'):
            resp.close()
            raise SessionExpired('The site returned a web page instead of the file; the sign-in may have expired.')
        return resp
