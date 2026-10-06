"""Everything the CLI and the app share: the on-disk library, scraping, and downloading.

Layout under the library root:
    <name>.json                         one file per course
    sessions/<host>.json                saved login cookies (credentials! never commit/share)
    media/<name>/<cat>/<post>/video.mp4 and attachments/
"""
from __future__ import annotations

import json
import os
import re
import threading
import warnings
from urllib.parse import urlparse

import jsonpickle

from . import auth
from .downloader import Downloader, Cancelled, safe_name, video_size
from .fetcher import Fetcher, SessionExpired
from .models import Category, Product
from .parser import Parser
from .wistia_client import WistiaClient


warnings.filterwarnings('ignore', message='keys will default to True', category=DeprecationWarning)


class NeedsLogin(Exception):
    """No saved session for this site."""


def slug_from_url(url: str) -> str:
    m = re.search(r'/products/([^/?#]+)', urlparse(url).path)
    return m.group(1) if m else safe_name(urlparse(url).netloc)


def all_posts(product: Product):
    for cat in product.categories:
        for post in cat.posts:
            yield cat, post


class Library:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    # -- products ---------------------------------------------
    def product_path(self, name: str) -> str:
        return os.path.join(self.root, f'{name}.json')

    def products(self) -> list[Product]:
        out = []
        for f in sorted(os.listdir(self.root)):
            if f.endswith('.json'):
                try:
                    obj = jsonpickle.decode(open(os.path.join(self.root, f), encoding='utf-8').read())
                except Exception:
                    continue
                if isinstance(obj, Product):
                    out.append(obj)
        return out

    def load(self, name: str) -> Product:
        return jsonpickle.decode(open(self.product_path(name), encoding='utf-8').read())

    def save(self, product: Product):
        path = self.product_path(product.name)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(jsonpickle.encode(product, make_refs=False))
        os.replace(tmp, path)  # atomic: an interrupted save can't corrupt the course

    def delete(self, product: Product, media: bool = True):
        import shutil
        if os.path.exists(self.product_path(product.name)):
            os.remove(self.product_path(product.name))
        if media:
            shutil.rmtree(os.path.join(self.root, 'media', safe_name(product.name)), ignore_errors=True)

    # -- deleting downloads (always an explicit user action; the course outline is kept) -------------
    def _inside_media(self, product: Product, path: str) -> bool:
        """True only for paths under this course's media folder, so a bad/legacy path can never delete elsewhere."""
        root = os.path.realpath(os.path.join(self.root, 'media', safe_name(product.name)))
        real = os.path.realpath(path)
        return real != root and os.path.commonpath([real, root]) == root

    def lesson_download_bytes(self, product: Product, post) -> int:
        """Bytes of this lesson's downloaded video/files (what deleting them would free)."""
        total = 0
        for rel in [post.video_path] + [a.local_file_path for a in post.attachments or []]:
            path = self.abs_path(rel) if rel else None
            if path and os.path.isfile(path) and self._inside_media(product, path):
                total += os.path.getsize(path)
        return total

    def delete_lesson_downloads(self, product: Product, post) -> int:
        """Delete one lesson's downloaded video and files from this device. Forgets their paths so the lesson
        shows as not downloaded again; the lesson itself stays in the course. Returns bytes freed."""
        import shutil
        freed = self.lesson_download_bytes(product, post)
        for rel in [post.video_path] + [a.local_file_path for a in post.attachments or []]:
            path = self.abs_path(rel) if rel else None
            if path and os.path.isfile(path) and self._inside_media(product, path):
                os.remove(path)
        lesson_dir = self.media_dir(product, post)  # also clears leftovers such as a half-finished .part file
        if os.path.isdir(lesson_dir) and self._inside_media(product, lesson_dir):
            shutil.rmtree(lesson_dir, ignore_errors=True)
        post.video_path = None
        for att in post.attachments or []:
            att.local_file_path = None
        self.save(product)
        return freed

    def delete_course_downloads(self, product: Product) -> int:
        """Delete every downloaded video/file of a course but keep the course and its lessons. Returns bytes freed."""
        import shutil
        freed = sum(self.lesson_download_bytes(product, post) for _, post in all_posts(product))
        shutil.rmtree(os.path.join(self.root, 'media', safe_name(product.name)), ignore_errors=True)
        for _, post in all_posts(product):
            post.video_path = None
            for att in post.attachments or []:
                att.local_file_path = None
        self.save(product)
        return freed

    def delete_site(self, site: 'Site'):
        """Explicit user action only: remove a site's courses, downloads, saved list and sign-in."""
        for prod in site.products:
            self.delete(prod)
        for path in (self._catalog_file(site.host), self._session_file(site.root)):
            if os.path.exists(path):
                os.remove(path)

    # -- media paths ------------------------------------------
    def media_dir(self, product, post) -> str:
        """Files live under the lesson id only (not its section), so a lesson that moves keeps its files."""
        return os.path.join(self.root, 'media', safe_name(product.name), safe_name(str(post.pk)))

    def abs_path(self, rel_path: str) -> str:
        return os.path.join(self.root, rel_path)

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.root).replace(os.sep, '/')

    def has_video_file(self, post) -> bool:
        return bool(post.video_path) and os.path.exists(self.abs_path(post.video_path))

    # -- sites ------------------------------------------------
    def _catalog_file(self, host: str) -> str:
        return os.path.join(self.root, 'sites', safe_name(host) + '.json')

    def save_catalog(self, host: str, title, email, courses: list[dict]):
        path = self._catalog_file(host)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'host': host, 'title': title, 'email': email, 'courses': courses}, f)

    def sites(self) -> list[Site]:
        """Every site we know about (discovered or with saved courses), each with its added courses."""
        by_host: dict[str, Site] = {}
        sites_dir = os.path.join(self.root, 'sites')
        for f in sorted(os.listdir(sites_dir)) if os.path.isdir(sites_dir) else []:
            try:
                d = json.load(open(os.path.join(sites_dir, f), encoding='utf-8'))
                by_host[d['host']] = Site(d['host'], d.get('title'), d.get('email'), d.get('courses'))
            except Exception:
                continue
        for prod in self.products():
            host = host_of(prod.url)
            site = by_host.setdefault(host, Site(host, email=prod.email))
            site.products.append(prod)
            site.email = site.email or prod.email
        return sorted(by_host.values(), key=lambda s: s.title.lower())

    # -- sessions ---------------------------------------------
    def _session_file(self, url: str) -> str:
        return os.path.join(self.root, 'sessions', safe_name(urlparse(auth.site_root(url)).netloc) + '.json')

    def save_session(self, url: str, cookies: list[dict]):
        path = self._session_file(url)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(cookies, f)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def fetcher_for(self, url: str) -> Fetcher:
        path = self._session_file(url)
        if not os.path.exists(path):
            raise NeedsLogin(url)
        return Fetcher(path)

    def sign_in(self, url: str, email: str, password: str):
        """Log in (raises auth.LoginError) and remember the session. Returns a ready Fetcher."""
        cookies = auth.login(url, email, password)
        self.save_session(url, cookies)
        return Fetcher(cookies)


# -- sites & courses ----------------------------------------------------

def host_of(url: str) -> str:
    return urlparse(auth.site_root(url)).netloc


def pretty_host(host: str) -> str:
    """some-academy.mykajabi.com -> Some Academy; courses.example-school.com -> Example School."""
    parts = host.split('.')
    label = parts[0] if host.endswith('mykajabi.com') or len(parts) < 3 else parts[-2]
    return re.sub(r'[-_]+', ' ', label).title()


def discover_courses(fetcher: Fetcher, site_url: str) -> tuple[str | None, list[dict]]:
    """Read the member library page. Returns (site title, [{slug, url, title}])."""
    url = auth.site_root(site_url) + '/library'
    html = fetcher.get_page(url).text
    return Parser.get_site_title(html), Parser.get_library_products(html, url)


class Site:
    """A Kajabi site the user has signed in to: its title, account email, and the courses they own there."""

    def __init__(self, host, title=None, email=None, courses=None, products=None):
        self.host, self.email = host, email
        self.title = title or pretty_host(host)
        self.courses = courses or []      # every course the site lists for this account: {slug, url, title}
        self.products = products or []    # courses already added to the app (Product objects)

    @property
    def root(self) -> str:
        return 'https://' + self.host

    def product_for(self, slug: str):
        return next((p for p in self.products if slug_from_url(p.url) == slug), None)


def add_course(lib: 'Library', fetcher: Fetcher, url: str, title: str | None = None, email: str | None = None,
               on_status=None, cancel: threading.Event | None = None) -> Product:
    """Add (or re-sync) one course: read its outline, then every lesson. Never loses existing downloads."""
    name = slug_from_url(url)
    existing = lib.load(name) if os.path.exists(lib.product_path(name)) else None
    if on_status:
        on_status('Reading the course outline…')
    prod = scrape_tree(fetcher, url, name, title or (existing.title if existing else None), existing)
    prod.email = email or prod.email
    lib.save(prod)
    scrape_posts(fetcher, prod, lib.save, cancel=cancel,
                 on_progress=(lambda n, total, nm: on_status(f'Reading lesson {n} of {total}…')) if on_status else None)
    return prod


# -- scraping -----------------------------------------------------------

def scrape_tree(fetcher: Fetcher, url: str, name: str | None = None, title: str | None = None,
                existing: Product | None = None, pause=None) -> Product:
    """Fetch a course's sections and lesson lists. Keeps scraped data from `existing` (re-runs)."""
    product = Product(name=name or slug_from_url(url), url=url, title=title)
    product.categories = Parser.get_categories(fetcher.get_page(url).text, url)
    if not product.categories:
        raise ValueError('No sections found. Is this the course page URL (…/products/<course>)?')
    for cat in product.categories:
        cat.posts = Parser.get_posts(fetcher.get_page(cat.url).text, cat.url)
        if pause:
            pause()
    if existing is not None:
        merge_existing(product, existing)
    return product


REMOVED_PK = 'removed'


def _has_downloads(post) -> bool:
    return bool(post.video_path) or any(a.local_file_path for a in post.attachments or [])


def merge_existing(product: Product, existing: Product):
    """Fold a freshly scraped outline into what we already have, without ever losing downloads.

    - lessons still on the site keep their saved-file info, but take the site's current name/url,
      and are marked for a text/video refresh;
    - lessons that moved to another section are matched by id, so they keep their files;
    - lessons that vanished from the site and have downloads move to a "No longer on the site" section
      (their files stay reachable and are never deleted); vanished lessons with nothing saved are dropped.
    """
    old = {p.pk: p for _, p in all_posts(existing)}
    seen = set()
    for cat in product.categories:
        merged = []
        for fresh in cat.posts:
            seen.add(fresh.pk)
            kept = old.get(fresh.pk)
            if kept is None:
                merged.append(fresh)
                continue
            kept.name, kept.url = fresh.name, fresh.url
            kept.scraped = False  # re-read its text/files/video info (downloads are kept)
            merged.append(kept)
        cat.posts = merged
    gone = [p for pk, p in old.items() if pk not in seen and _has_downloads(p)]
    if gone:
        product.categories.append(Category(REMOVED_PK, 'No longer on the site', product.url, posts=gone))
    product.title = product.title or existing.title
    product.email = existing.email


def merge_attachments(old: list, new: list) -> list:
    """New attachment list from the page, carrying over already-downloaded file paths.
    Downloaded files that disappeared from the page are kept rather than forgotten."""
    by_url = {a.url: a for a in old or []}
    by_name = {a.name: a for a in old or [] if a.name}
    used = set()
    for a in new:
        match = by_url.get(a.url) or by_name.get(a.name)
        if match is not None:
            a.local_file_path = match.local_file_path
            used.add(id(match))
    return new + [a for a in old or [] if id(a) not in used and a.local_file_path]


def scrape_posts(fetcher: Fetcher, product: Product, save, force=False, on_progress=None,
                 cancel: threading.Event | None = None, pause=None, on_warning=None):
    """Fetch each lesson's text, attachments and video info. Resumable (skips scraped lessons)."""
    todo = [(c, p) for c, p in all_posts(product) if force or not p.scraped]
    for n, (cat, post) in enumerate(todo, 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if on_progress:
            on_progress(n, len(todo), post.name)
        data = Parser.parse_post(fetcher.get_page(post.url).text, post.url)
        post.content = data['content']
        post.attachments = merge_attachments(post.attachments, data['attachments'])
        if data['wistia_id']:
            assets = WistiaClient.get_assets(data['wistia_id'])
            if assets:
                post.wistia_assets = assets
            elif on_warning:
                on_warning(f'No video found for "{post.name}".')
        post.scraped = True
        save(product)
        if pause:
            pause()


# -- downloading --------------------------------------------------------

def download_post(lib: Library, product: Product, cat, post, fetcher: Fetcher, quality: str,
                  on_video_progress=None, cancel: threading.Event | None = None):
    """Download a lesson's attachments and video. Safe to repeat; finished files are skipped."""
    dest = os.path.join(lib.media_dir(product, post), 'attachments')
    for att in post.attachments:
        if att.local_file_path and os.path.exists(lib.abs_path(att.local_file_path)):
            continue
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        resp = fetcher.get(att.url, stream=True)
        resp.raise_for_status()
        att.local_file_path = lib.rel(Downloader.download_file(resp, dest, filename=att.name, cancel=cancel))
        lib.save(product)
    if post.wistia_assets:
        path = Downloader.download_wistia(post.wistia_assets, lib.media_dir(product, post),
                                          quality=quality, progress=on_video_progress, cancel=cancel)
        post.video_path = lib.rel(path)
        lib.save(product)


def lesson_bytes(post, quality: str) -> int:
    return video_size(post.wistia_assets, quality) or 0


class DownloadManager:
    """Runs downloads for a list of lessons on a background thread.

    on_update(manager) is called (from the worker thread) whenever state changes."""

    def __init__(self, lib: Library, product: Product, fetcher: Fetcher, quality: str, on_update=None):
        self.lib, self.product, self.fetcher, self.quality = lib, product, fetcher, quality
        self.on_update = on_update or (lambda m: None)
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.items: list = []
        self.index = 0
        self.current_name = ''
        self.done_bytes = self.total_bytes = 0
        self.errors: list[str] = []
        self.session_expired = False
        self.finished = False

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, items):
        self.items, self.index, self.errors = list(items), 0, []
        self.session_expired = self.finished = False
        self.cancel.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def pause(self):
        """Stop after flushing the current chunk; partial files are kept and resume later."""
        self.cancel.set()

    def _progress(self, done, total):
        self.done_bytes, self.total_bytes = done, total
        self.on_update(self)

    def _run(self):
        try:
            for self.index, (cat, post) in enumerate(self.items):
                self.current_name, self.done_bytes, self.total_bytes = post.name, 0, 0
                self.on_update(self)
                try:
                    download_post(self.lib, self.product, cat, post, self.fetcher, self.quality,
                                  on_video_progress=self._progress, cancel=self.cancel)
                except Cancelled:
                    return
                except SessionExpired:
                    self.session_expired = True
                    return
                except Exception as e:
                    self.errors.append(f'{post.name}: {e}')
            self.index = len(self.items)
        finally:
            self.finished = True
            self.on_update(self)
