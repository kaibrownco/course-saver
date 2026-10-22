"""What happens when the saved sign-in expires, or the site's bot protection (Cloudflare) steps in.

Real-world case this guards: a logged-out request to /library is redirected to /login, and the login page itself
answers 403 with "cf-mitigated: challenge". That must lead to "please sign in again", not a raw 403 error."""
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
import requests

import main as app_module
from core.auth import LoginError, login
from core.fetcher import SessionExpired, SiteBlocked, Fetcher
from core.models import Attachment, Category, Post, Product
from core.service import Library, NeedsLogin, download_post
from test_app_android import make_app, StubPage  # noqa: F401  (stub page + app factory)

CHALLENGE = ('cf-mitigated', 'challenge')


def serve(handler):
    srv = HTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f'http://127.0.0.1:{srv.server_port}'


class ExpiredSiteBlockedLogin(BaseHTTPRequestHandler):
    """Everything redirects to /login (signed out); /login is behind a Cloudflare challenge."""
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == '/login':
            body = b'<html>Just a moment...</html>'
            self.send_response(403)
            self.send_header(*CHALLENGE)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(302)
            self.send_header('Location', '/login')
            self.send_header('Content-Length', '0')
            self.end_headers()


class ChallengedPage(BaseHTTPRequestHandler):
    """A normal page that is itself blocked by the security check (no login redirect involved)."""
    def log_message(self, *a): pass

    def do_GET(self):
        self.send_response(403)
        self.send_header(*CHALLENGE)
        self.send_header('Content-Length', '0')
        self.end_headers()


class LoginPageOnFileUrl(BaseHTTPRequestHandler):
    """File links: /expired -> redirects to a login page that returns 200 HTML; /html returns HTML directly."""
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == '/expired':
            self.send_response(302)
            self.send_header('Location', '/login')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = b'<html><form>sign in</form></html>' if self.path in ('/login', '/html') else b'%PDF-1.4 real bytes'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html' if self.path in ('/login', '/html') else 'application/pdf')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def fetcher():
    return Fetcher([{'name': '_kjb_session', 'value': 'stale', 'domain': '127.0.0.1', 'path': '/'}])


def test_logged_out_redirect_to_a_blocked_login_page_means_sign_in_again():
    srv, url = serve(ExpiredSiteBlockedLogin)
    try:
        with pytest.raises(SessionExpired):      # before the fix this was a raw "403 Client Error"
            fetcher().get_page(url + '/library')
    finally:
        srv.shutdown()


def test_a_blocked_normal_page_says_the_site_blocked_us():
    srv, url = serve(ChallengedPage)
    try:
        with pytest.raises(SiteBlocked, match='security check'):
            fetcher().get_page(url + '/library')
    finally:
        srv.shutdown()


def test_sign_in_against_a_blocked_login_page_explains_the_security_check():
    srv, url = serve(ChallengedPage)
    try:
        with pytest.raises(LoginError, match='security check'):   # not the misleading "no sign-in form" message
            login(url, 'a@b.c', 'pw')
    finally:
        srv.shutdown()


def test_get_file_refuses_to_save_a_login_page_as_a_file():
    srv, url = serve(LoginPageOnFileUrl)
    try:
        f = fetcher()
        with pytest.raises(SessionExpired):
            f.get_file(url + '/expired')          # redirected to a 200 login page
        with pytest.raises(SessionExpired):
            f.get_file(url + '/html')             # a web page where a file was expected
        resp = f.get_file(url + '/real.pdf')
        assert resp.content == b'%PDF-1.4 real bytes'
    finally:
        srv.shutdown()


def test_download_with_an_expired_session_saves_nothing(tmp_path):
    srv, url = serve(LoginPageOnFileUrl)
    try:
        lib = Library(str(tmp_path))
        post = Post('10', 'L', 'u')
        post.attachments = [Attachment(url + '/expired', 'Notes.pdf')]
        prod = Product('p', url, categories=[Category('1', 'S', 'u', posts=[post])])
        lib.save(prod)
        with pytest.raises(SessionExpired):
            download_post(lib, prod, prod.categories[0], post, fetcher(), 'phone')
        assert not os.path.exists(os.path.join(lib.root, 'media'))          # no junk file written
        assert post.attachments[0].local_file_path is None
    finally:
        srv.shutdown()


class InlineThread:
    """threading.Thread that runs immediately, so UI tests can assert on the outcome."""
    def __init__(self, target=None, daemon=None, **kw):
        self.target = target

    def start(self):
        self.target()


def test_app_asks_for_the_password_when_the_session_expired(tmp_path, monkeypatch):
    app, page, lib = make_app(tmp_path)
    monkeypatch.setattr(app_module.threading, 'Thread', InlineThread)
    done = []

    def work(fetcher_, status):
        raise SessionExpired('login redirect')

    monkeypatch.setattr(lib, 'fetcher_for', lambda url: object())
    app.busy('Refreshing', work, done.append, 'https://x.example', 'Example', 'me@x.com')
    assert page.dialogs[-1].title.value == 'Please sign in again'   # the dialog she never saw
    assert 'me@x.com' in page.dialogs[-1].content.controls[0].value and not done


def test_app_asks_for_the_password_when_there_is_no_saved_session(tmp_path, monkeypatch):
    app, page, lib = make_app(tmp_path)
    monkeypatch.setattr(app_module.threading, 'Thread', InlineThread)
    app.busy('Refreshing', lambda f, s: 1, lambda r: None, 'https://x.example', 'Example', None)  # no session file
    assert page.dialogs[-1].title.value == 'Please sign in again'


def test_app_explains_a_security_check_instead_of_a_raw_error(tmp_path, monkeypatch):
    app, page, lib = make_app(tmp_path)
    monkeypatch.setattr(app_module.threading, 'Thread', InlineThread)
    monkeypatch.setattr(lib, 'fetcher_for', lambda url: object())

    def work(fetcher_, status):
        raise SiteBlocked('The website is showing a security check and blocked the app.')

    app.busy('Adding', work, lambda r: None, 'https://x.example', 'Example', None)
    dlg = page.dialogs[-1]
    assert dlg.title.value == 'The website blocked the request' and 'security check' in dlg.content.value


def test_reauth_shows_the_security_check_message_if_sign_in_is_blocked(tmp_path, monkeypatch):
    app, page, lib = make_app(tmp_path)
    monkeypatch.setattr(lib, 'sign_in', lambda *a: (_ for _ in ()).throw(LoginError('security check, wait a few minutes')))
    app.reauth('https://x.example', 'Example', 'me@x.com', lambda: None)
    dlg = page.dialogs[-1]
    ok = next(a for a in dlg.actions if a.content == 'Sign in')
    ok.on_click(None)
    err = dlg.content.controls[-1]
    assert 'security check' in err.value        # shown in the dialog so she knows why, and can try again
