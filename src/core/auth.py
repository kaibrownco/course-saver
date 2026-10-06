"""Email + password login for Kajabi member sites (a plain Rails form with a CSRF token)."""
from __future__ import annotations

from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .fetcher import DEFAULT_UA


class LoginError(Exception):
    """Raised when we could not sign in; the message is safe to show to the user."""


def site_root(url: str) -> str:
    p = urlparse(url if '//' in url else f'https://{url}')
    return f'{p.scheme}://{p.netloc}'


def _find_login_form(html: str):
    soup = BeautifulSoup(html, 'html.parser')
    for form in soup.find_all('form'):
        if form.find('input', attrs={'type': 'password'}):
            return form
    return None


def login(site_url: str, email: str, password: str, timeout: int = 30) -> list[dict]:
    """Sign in and return the session cookies as a list of {name, value, domain, path} dicts.
    Raises LoginError with a friendly message on any failure."""
    root = site_root(site_url)
    login_url = f'{root}/login'
    s = requests.Session()
    s.headers.update({'User-Agent': DEFAULT_UA, 'Accept-Language': 'en-US,en;q=0.9'})
    try:
        page = s.get(login_url, timeout=timeout)
        form = _find_login_form(page.text) if page.status_code == 200 else None
        if form is None:
            raise LoginError("Couldn't find the sign-in form on that site. It may use Google/Facebook "
                             "sign-in or a magic link, which this app can't use yet.")

        data = {}
        for inp in form.find_all('input'):
            name, typ = inp.get('name'), (inp.get('type') or 'text').lower()
            if not name or typ in ('submit', 'button', 'image') or (typ == 'checkbox' and name != 'member[remember_me]'):
                continue
            data[name] = inp.get('value', '')
        email_field = next((i['name'] for i in form.find_all('input', attrs={'name': True})
                            if i.get('type') == 'email' or 'email' in i['name']), 'member[email]')
        pass_field = form.find('input', attrs={'type': 'password'})['name']
        data[email_field], data[pass_field] = email, password
        if 'member[remember_me]' in data:
            data['member[remember_me]'] = '1'  # long-lived session, so she signs in rarely

        resp = s.post(urljoin(login_url, form.get('action') or '/login'), data=data, timeout=timeout,
                      headers={'Referer': login_url})
    except requests.RequestException as e:
        raise LoginError(f"Couldn't reach the site. Check your internet connection. ({type(e).__name__})")

    if resp.status_code >= 400 or '/login' in urlparse(resp.url).path or _find_login_form(resp.text):
        raise LoginError('Sign-in failed. Please check your email and password.')
    return [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path or '/'} for c in s.cookies]
