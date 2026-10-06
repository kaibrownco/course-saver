"""Local stub servers stand in for Kajabi (login form) and the Wistia CDN (Range requests)."""
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from core.auth import login, LoginError
from core.downloader import Downloader, Cancelled
from core.service import Library

FORM = ('<form action="/login" method="post"><input type="hidden" name="authenticity_token" value="tok">'
        '<input type="text" name="member[email]"><input type="password" name="member[password]">'
        '<input type="checkbox" name="member[remember_me]" value="1"><input type="submit" name="commit"></form>')


def serve(handler):
    srv = HTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f'http://127.0.0.1:{srv.server_port}'


class KajabiStub(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body=b'', headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/login':
            self._send(200, FORM.encode(), [('Content-Type', 'text/html')])
        else:
            self._send(200, b'<h1>library</h1>', [('Content-Type', 'text/html')])

    def do_POST(self):
        data = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        ok = (data.get('member[email]') == ['a@b.c'] and data.get('member[password]') == ['pw']
              and data.get('authenticity_token') == ['tok'])
        if ok:
            self._send(302, headers=[('Location', '/library'), ('Set-Cookie', 'remember_member_token=abc; Path=/')])
        else:
            self._send(200, FORM.encode(), [('Content-Type', 'text/html')])


def test_login_success_and_failure():
    srv, url = serve(KajabiStub)
    try:
        cookies = login(url, 'a@b.c', 'pw')
        assert any(c['name'] == 'remember_member_token' for c in cookies)
        with pytest.raises(LoginError, match='email and password'):
            login(url, 'a@b.c', 'wrong')
    finally:
        srv.shutdown()


def test_login_saves_session_for_fetcher(tmp_path):
    srv, url = serve(KajabiStub)
    try:
        lib = Library(str(tmp_path))
        lib.sign_in(url, 'a@b.c', 'pw')
        assert lib.fetcher_for(url).session.cookies.get('remember_member_token') == 'abc'
    finally:
        srv.shutdown()


DATA = bytes(range(256)) * 4000  # ~1 MB


class CdnStub(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        rng = self.headers.get('Range')
        start = int(rng.split('=')[1].split('-')[0]) if rng else 0
        body = DATA[start:]
        self.send_response(206 if rng else 200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_video_download_resumes_after_cancel(tmp_path):
    srv, url = serve(CdnStub)
    try:
        assets = [{'type': 'original', 'ext': '.mp4', 'downloadable_url': url + '/v.mp4', 'size': len(DATA)}]
        cancel = threading.Event()

        def stop_early(done, total):
            if done > 300_000:
                cancel.set()

        with pytest.raises(Cancelled):
            Downloader.download_wistia(assets, str(tmp_path), progress=stop_early, cancel=cancel)
        part = tmp_path / 'video.mp4.part'
        assert 0 < part.stat().st_size < len(DATA) and not (tmp_path / 'video.mp4').exists()

        path = Downloader.download_wistia(assets, str(tmp_path))
        assert open(path, 'rb').read() == DATA and not part.exists()
    finally:
        srv.shutdown()


def test_download_manager_saves_video_and_updates_model(tmp_path):
    from core.models import Product, Category, Post
    from core.service import DownloadManager

    srv, url = serve(CdnStub)
    try:
        lib = Library(str(tmp_path))
        post = Post('2', 'Lesson', 'u')
        post.wistia_assets = [{'type': 'iphone_video', 'ext': '.mp4', 'downloadable_url': url + '/v.mp4',
                               'size': len(DATA), 'width': 640, 'height': 360}]
        prod = Product('demo', 'u', categories=[Category('1', 'Sec', 'u', posts=[post])])
        lib.save(prod)

        mgr = DownloadManager(lib, prod, fetcher=None, quality='phone')
        mgr.start([(prod.categories[0], post)])
        mgr.thread.join(10)

        assert mgr.finished and not mgr.errors and not mgr.session_expired
        assert lib.has_video_file(post)
        assert lib.load('demo').categories[0].posts[0].video_path == 'media/demo/2/video.mp4'
    finally:
        srv.shutdown()
