from __future__ import annotations

import json
import time

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


class Fetcher:
    def __init__(self, cookies, retries: int = 3):
        self.session = requests.Session()
        cookies, ua = load_cookies(cookies)
        self.session.headers['User-Agent'] = ua or DEFAULT_UA
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

    def get_page(self, url, **kwargs):
        """GET an HTML page and fail loudly if we were bounced to a login page."""
        resp = self.get(url, **kwargs)
        resp.raise_for_status()
        if any(s in resp.url for s in ('/login', '/sign_in', '/sign-in')):
            raise SessionExpired(f'Redirected to login ({resp.url}); the session has expired.')
        return resp
