"""Course Saver: download Kajabi courses you own for offline viewing (Windows + Android).

One Flet codebase. Screens: My Courses -> Add course -> Course (pick lessons, download) -> Lesson (watch/read).
All the real work lives in core.service; this file is only UI.
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time

import flet as ft

try:
    import flet_video as fv
except Exception:  # video control missing: lessons fall back to the system player
    fv = None

from core.auth import LoginError
from core.downloader import pick_video
from core.fetcher import SessionExpired, SiteBlocked
from core.service import (DownloadManager, Library, NeedsLogin, Site, add_course, all_posts, discover_courses,
                          host_of, lesson_bytes, slug_from_url)
from core.auth import site_root as auth_root

APP_NAME = 'Course Saver'
SPEEDS = (0.75, 1.0, 1.25, 1.5, 1.75, 2.0)   # playback speeds offered on every platform
SEEK_SECONDS = 5                              # left/right arrow keys skip this far in the lesson video
MAX_CONTENT_WIDTH = 900                       # wide windows keep content this wide and centered; phones use the full width
MAX_COPY_BYTES = 150 * 1024 * 1024  # biggest file the save-a-copy button will read into memory
QUALITY_LABELS = {
    'phone': 'Phone size (smallest)',
    'hd': 'HD 720p',
    'original': 'Original (largest)',
}


def speed_label(rate: float) -> str:
    return f'{rate:g}×'


def human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit in ('B', 'KB') else f'{n:.1f} {unit}'
        n /= 1024


def lesson_label(post, quality: str, saved: bool) -> str:
    parts = []
    if post.wistia_assets:
        parts.append('Video' if saved else f'Video · {human_size(lesson_bytes(post, quality))}')
    if post.attachments:
        parts.append(f'{len(post.attachments)} file{"" if len(post.attachments) == 1 else "s"}')
    return ' · '.join(parts) or 'Text only'


def pretty_name(slug: str) -> str:
    return slug.replace('-', ' ').replace('_', ' ').title()


def open_with_system(path: str):
    """Open a file in the OS default app (desktop only)."""
    if sys.platform.startswith('win'):
        os.startfile(path)  # noqa: S606
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


class App:
    def __init__(self, page: ft.Page, lib: Library):
        self.page = page
        self.lib = lib
        self.is_mobile = page.platform in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS)
        self.default_quality = 'phone' if self.is_mobile else 'hd'
        saved_rate = lib.get_setting('playback_rate', 1.0)
        self.playback_rate = saved_rate if saved_rate in SPEEDS else 1.0
        self.manager: DownloadManager | None = None  # one active download job at a time
        self.slots = {}  # id(view) -> (status slot, scrolling body): where inline messages for that screen appear
        self.active_video = None  # the video on the lesson screen that is open (the arrow keys seek this one)
        self.video_pos_ms = None  # its position as of the last poll (before any key press is applied)
        self.seek_to_ms, self.seek_at = None, 0.0   # where our last arrow-key skip went, and when (chained presses)
        # Services for handing files to other apps on Android (share sheet) / saving them to a chosen folder
        self.share = ft.Share()
        self.picker = ft.FilePicker()
        page.services.extend([self.share, self.picker])

    # ----- navigation (own view stack so the Android back button works) -----
    def push(self, view: ft.View):
        self.page.views.append(view)
        self.page.update()

    def pop(self, *_):
        if len(self.page.views) > 1:
            gone = self.page.views.pop()
            self.slots.pop(id(gone), None)
            self.active_video = None   # leaving the lesson screen (the only screen with a video)
            self.video_pos_ms = self.seek_to_ms = None
            top = self.page.views[-1]
            if callable(top.data):
                top.data()  # let the screen refresh itself
            self.page.update()

    def gutter(self) -> float:
        """Side margin of the page content: 16 on phones; on wide windows the content stays MAX_CONTENT_WIDTH
        wide and centered instead of stretching (or hugging the left edge)."""
        width = getattr(self.page, 'width', None) or 0
        return max(16, (width - MAX_CONTENT_WIDTH) / 2) if width else 16

    def narrow(self, cap: float) -> float:
        """A width of at most `cap` that still fits inside the page margins."""
        width = getattr(self.page, 'width', None) or 0
        return min(cap, max(240, width - 2 * self.gutter())) if width else cap

    def on_resize(self, e=None):
        pad = ft.Padding.symmetric(horizontal=self.gutter(), vertical=8)
        for v in self.page.views:
            try:
                v.controls[0].content.padding = pad   # SafeArea -> the padded body container
            except Exception:
                pass
        self.page.update()

    def view(self, title, controls, actions=None, fab=None, back=True, refresh=None, footer=None):
        slot = ft.Column(spacing=0)   # inline messages (progress, confirmations, errors) appear here, above the list
        body = ft.Column(controls, spacing=12, scroll=ft.ScrollMode.AUTO, expand=True)
        v = ft.View(
            route=f'/{title}',
            appbar=ft.AppBar(
                title=ft.Text(title, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                leading=ft.IconButton(ft.Icons.ARROW_BACK, on_click=self.pop) if back else None,
                actions=actions or [], center_title=False,
            ),
            controls=[ft.SafeArea(content=ft.Container(ft.Column(
                [slot, body] + ([footer] if footer else []),
                spacing=8), padding=ft.Padding.symmetric(horizontal=self.gutter(), vertical=8), expand=True), expand=True)],
            floating_action_button=fab, data=refresh, padding=0,
        )
        self.slots[id(v)] = (slot, body)
        return v

    def toast(self, message: str):
        self.page.show_dialog(ft.SnackBar(ft.Text(message)))

    def inline(self, controls, modal: bool = False):
        """Show a card at the top of the current screen (pinned above the scrolling list) and return a function that
        removes it. The app's own messages use this instead of pop-up dialogs: Flet keeps a closed dialog around until
        the window reports it dismissed, and if the screen changed while it was closing, the dialog came back from
        the dead and blocked everything once the user went back. Plain controls have no such lifecycle."""
        entry = self.slots.get(id(self.page.views[-1])) if self.page.views else None
        if entry is None:
            return lambda: None
        slot, body = entry
        card = ft.Card(ft.Container(ft.Column(controls, spacing=8, tight=True), padding=14))
        slot.controls = [card]
        body.disabled = modal      # while a job runs or a question is open, ignore taps on the list
        self.page.update()

        def close():
            if slot.controls and slot.controls[0] is card:   # don't remove a newer message
                slot.controls = []
                body.disabled = False
                self.page.update()
        return close

    def alert(self, title: str, message: str):
        close = None
        close = self.inline([ft.Text(title, size=16, weight=ft.FontWeight.W_600), ft.Text(message, selectable=True),
                             ft.Row([ft.TextButton('OK', on_click=lambda e: close())], alignment=ft.MainAxisAlignment.END)])

    def confirm(self, title: str, message: str, yes_label: str, on_yes):
        """Ask before doing something destructive; on_yes runs only if the user confirms."""
        close = None

        def yes(e):
            close()
            on_yes()
            self.page.update()

        close = self.inline([ft.Text(title, size=16, weight=ft.FontWeight.W_600), ft.Text(message),
                             ft.Row([ft.TextButton('Cancel', on_click=lambda e: close()), ft.TextButton(yes_label, on_click=yes)],
                                    alignment=ft.MainAxisAlignment.END)], modal=True)

    def ui(self, fn):
        """Run fn on the UI side from a worker thread."""
        try:
            fn()
            self.page.update()
        except Exception:
            pass

    # ----- Home: one card per site -----
    def home(self) -> ft.View:
        col = ft.Column(spacing=10)
        footer = ft.Text(f'Saved in: {self.lib.root}', size=12, color=ft.Colors.OUTLINE, selectable=True)

        def refresh():
            col.controls.clear()
            sites = self.lib.sites()
            if not sites:
                col.controls.append(ft.Container(ft.Row([ft.Container(ft.Column([
                    ft.Icon(ft.Icons.SCHOOL_OUTLINED, size=64, color=ft.Colors.OUTLINE),
                    ft.Text('No courses yet', size=20, weight=ft.FontWeight.W_600),
                    ft.Text('Sign in to the website where you bought your courses and we will find them all.',
                            text_align=ft.TextAlign.CENTER, color=ft.Colors.OUTLINE),
                    ft.Button('Add a site', icon=ft.Icons.ADD, on_click=lambda e: self.push(self.add_site_view())),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=12), width=self.narrow(380))],
                    alignment=ft.MainAxisAlignment.CENTER), padding=ft.Padding.only(top=60)))
            for site in sites:
                col.controls.append(self.site_card(site, refresh))

        actions = []
        if not self.is_mobile:
            actions.append(ft.IconButton(ft.Icons.FOLDER_OPEN, tooltip='Open the folder with my downloads',
                                         on_click=lambda e: open_with_system(self.lib.root)))
        v = self.view(APP_NAME, [col, footer], actions=actions, back=False, refresh=refresh,
                      fab=ft.FloatingActionButton(icon=ft.Icons.ADD, tooltip='Add a site',
                                                  on_click=lambda e: self.push(self.add_site_view())))
        refresh()
        return v

    def site_card(self, site, refresh):
        available = len({slug_from_url(c['url']) for c in site.courses} | {slug_from_url(p.url) for p in site.products})
        saved = sum(1 for p in site.products for _, post in all_posts(p) if self.lib.has_video_file(post))

        def remove(e):
            self.confirm('Remove this site?',
                         f'This deletes everything saved from {site.title} on this device: its '
                         f'{len(site.products)} added course(s), their downloaded videos and files, and your sign-in.',
                         'Remove', lambda: (self.lib.delete_site(site), refresh(), self.page.update()))

        return ft.Card(ft.ListTile(
            leading=ft.Icon(ft.Icons.LANGUAGE, size=34),
            title=ft.Text(site.title, weight=ft.FontWeight.W_600),
            subtitle=ft.Text(f'{len(site.products)} of {available} courses added · {saved} videos saved'),
            trailing=ft.PopupMenuButton(items=[ft.PopupMenuItem('Remove site', icon=ft.Icons.DELETE_OUTLINE, on_click=remove)]),
            on_click=lambda e: self.push(self.site_view(site.host)),
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=8)))

    # ----- Add a site -----
    def add_site_view(self) -> ft.View:
        last = next((s for s in reversed(self.lib.sites()) if s.email), None)
        field_width = self.narrow(480)   # comfortable on a big window, never wider than a phone screen
        url = ft.TextField(label='Website address', hint_text='https://your-course-site.com', width=field_width,
                           keyboard_type=ft.KeyboardType.URL, autofocus=True)
        email = ft.TextField(label='Email', value=last.email if last else '', width=field_width,
                             keyboard_type=ft.KeyboardType.EMAIL)
        password = ft.TextField(label='Password', password=True, can_reveal_password=True, width=field_width)
        status = ft.Text('', color=ft.Colors.OUTLINE)
        error = ft.Text('', color=ft.Colors.ERROR, visible=False)
        ring = ft.ProgressRing(visible=False, width=22, height=22)
        btn = ft.Button('Sign in and find my courses', icon=ft.Icons.LOGIN)

        def fail(msg):
            error.value, error.visible, ring.visible, btn.disabled = msg, True, False, False
            status.value = ''
            self.page.update()

        def work():
            try:
                site_url = url.value.strip()
                if '//' not in site_url:
                    site_url = 'https://' + site_url
                self.ui(lambda: setattr(status, 'value', 'Signing in…'))
                fetcher = self.lib.sign_in(site_url, email.value.strip(), password.value)
                self.ui(lambda: setattr(status, 'value', 'Finding your courses…'))
                title, courses = discover_courses(fetcher, site_url)
                slug = re.search(r'/products/([^/?#]+)', site_url)
                if slug and slug.group(1) not in {c['slug'] for c in courses}:  # they pasted a specific course: keep it
                    courses.append({'slug': slug.group(1), 'url': f'{auth_root(site_url)}/products/{slug.group(1)}',
                                    'title': pretty_name(slug.group(1))})
                if not courses:
                    fail('You are signed in, but no courses were found on this site. If you know the address of a '
                         'course, paste that address instead (it ends in /products/the-course-name).')
                    return
                self.lib.save_catalog(host_of(site_url), title, email.value.strip(), courses)
                self.page.views.pop()  # leave the add screen
                top = self.page.views[-1]
                callable(top.data) and top.data()
                self.push(self.site_view(host_of(site_url)))
            except (LoginError, ValueError) as e:
                fail(str(e))
            except Exception as e:
                fail(f'Something went wrong: {e}')

        def go(e):
            if not url.value.strip() or not email.value.strip() or not password.value:
                fail('Please fill in the website address, email and password.')
                return
            error.visible, ring.visible, btn.disabled = False, True, True
            self.page.update()
            threading.Thread(target=work, daemon=True).start()

        btn.on_click = go
        return self.view('Add a site', [
            ft.Text('Enter the website where you bought your courses and sign in with the email and password you use '
                    'there. We will list every course you own on that site.', color=ft.Colors.OUTLINE),
            url, email, password,
            ft.Row([btn, ring], spacing=12), status, error,
            ft.Text('Your password is only used to sign in and is not saved.', size=12, color=ft.Colors.OUTLINE),
        ])

    # ----- Site: the courses you own there -----
    def site_view(self, host: str) -> ft.View:
        col = ft.Column(spacing=10)
        title_ref = {'site': next((s for s in self.lib.sites() if s.host == host), Site(host))}

        def entries(site):
            """Discovered courses plus any added by address, one row per course."""
            rows = {slug_from_url(c['url']): (c['title'], c['url']) for c in site.courses}
            for p in site.products:
                rows.setdefault(slug_from_url(p.url), (p.display_title, p.url))
            return [(slug, t, u) for slug, (t, u) in rows.items()]

        def refresh():
            site = next((s for s in self.lib.sites() if s.host == host), Site(host))
            title_ref['site'] = site
            col.controls.clear()
            for slug, title, url in entries(site):
                prod = site.product_for(slug)
                col.controls.append(self.course_row(site, prod, title, url, refresh))
            if not col.controls:
                col.controls.append(ft.Text('No courses found. Tap the refresh button to look again.', color=ft.Colors.OUTLINE))
            self.page.update() if self.page.views and self.page.views[-1].data is refresh else None

        def rediscover(e):
            site = title_ref['site']

            def work(fetcher, status):
                status('Looking for your courses…')
                title, courses = discover_courses(fetcher, site.root)
                known = {c['slug'] for c in courses}
                courses += [c for c in site.courses if c['slug'] not in known]  # never forget a course we had
                self.lib.save_catalog(host, title or site.title, site.email, courses)
                return len(courses)

            self.busy('Refreshing', work, lambda n: (refresh(), self.toast(f'{n} course(s) on this site.'), self.page.update()),
                      site.root, site.title, site.email)

        v = self.view(title_ref['site'].title, [col], refresh=refresh,
                      actions=[ft.IconButton(ft.Icons.REFRESH, tooltip='Look for new courses', on_click=rediscover)])
        refresh()
        return v

    def course_row(self, site, prod, title, url, refresh):
        if prod is None:
            return ft.Card(ft.ListTile(
                leading=ft.Icon(ft.Icons.CLOUD_DOWNLOAD_OUTLINED, size=30, color=ft.Colors.OUTLINE),
                title=ft.Text(title, weight=ft.FontWeight.W_600), subtitle=ft.Text('Not added yet'),
                trailing=ft.Button('Add', icon=ft.Icons.ADD, on_click=lambda e: self.add_course_ui(site, title, url, refresh)),
                on_click=lambda e: self.add_course_ui(site, title, url, refresh),
                content_padding=ft.Padding.symmetric(horizontal=12, vertical=8)))
        posts = [p for _, p in all_posts(prod)]
        with_video = [p for p in posts if p.wistia_assets]
        saved = [p for p in with_video if self.lib.has_video_file(p)]

        def remove(e):
            self.confirm('Remove this course?',
                         f'This deletes "{prod.display_title}" and all of its downloaded videos and files from this '
                         'device. It stays on the site and you can add it again.',
                         'Remove', lambda: (self.lib.delete(prod), refresh(), self.page.update()))

        return ft.Card(ft.ListTile(
            leading=ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINE, size=34),
            title=ft.Text(prod.display_title, weight=ft.FontWeight.W_600),
            subtitle=ft.Text(f'{len(posts)} lessons · {len(saved)} of {len(with_video)} videos saved'),
            trailing=ft.PopupMenuButton(items=[ft.PopupMenuItem('Remove course', icon=ft.Icons.DELETE_OUTLINE, on_click=remove)]),
            on_click=lambda e: self.push(self.course_view(prod.name)),
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=8)))

    def add_course_ui(self, site, title, url, refresh):
        def work(fetcher, status):
            return add_course(self.lib, fetcher, url, title=title, email=site.email, on_status=status)

        def done(prod):
            refresh()
            self.push(self.course_view(prod.name))

        self.busy(f'Adding {title}', work, done, url, site.title, site.email)

    # ----- helpers: a progress card that handles expired sign-ins -----
    def busy(self, title, work, on_done, site_url, label, email):
        """Run work(fetcher, status_fn) on a thread behind a progress card at the top of the screen.
        Asks to sign in again if needed; reports failures in a card."""
        status = ft.Text('Starting…', size=13, color=ft.Colors.OUTLINE)
        close = self.inline([ft.Row([ft.ProgressRing(width=22, height=22),
                                     ft.Column([ft.Text(title, weight=ft.FontWeight.W_600), status], spacing=2, expand=True)],
                                    spacing=14)], modal=True)

        def run():
            try:
                result = work(self.lib.fetcher_for(site_url), lambda m: self.ui(lambda: setattr(status, 'value', m)))
            except (NeedsLogin, SessionExpired):
                close()
                self.reauth(site_url, label, email, lambda: self.busy(title, work, on_done, site_url, label, email))
                return
            except SiteBlocked as e:
                close()
                self.alert('The website blocked the request', str(e))
                return
            except Exception as e:
                close()
                self.alert('Something went wrong', 'This course could not be read.' if isinstance(e, ValueError) else str(e))
                return
            close()
            on_done(result)
            self.page.update()

        threading.Thread(target=run, daemon=True).start()

    # ----- Course -----
    def course_view(self, name: str) -> ft.View:
        prod = self.lib.load(name)
        quality = {'value': self.default_quality}
        selected: set = set()
        rows: dict = {}  # post pk -> (checkbox, status icon)
        summary = ft.Text('', color=ft.Colors.OUTLINE)
        sections = ft.Column(spacing=0)
        dl_btn = ft.Button('Download', icon=ft.Icons.DOWNLOAD)
        bar = ft.ProgressBar(value=0, visible=False)
        job_text = ft.Text('', size=13)
        pause_btn = ft.TextButton('Pause', visible=False)
        job_box = ft.Card(ft.Container(ft.Column([job_text, bar, ft.Row([pause_btn], alignment=ft.MainAxisAlignment.END)],
                                                 spacing=6), padding=12), visible=False)

        def downloadable(post):
            return bool(post.wistia_assets or post.attachments)

        def fully_saved(post):
            return (not post.wistia_assets or self.lib.has_video_file(post)) and all(
                a.local_file_path and os.path.exists(self.lib.abs_path(a.local_file_path)) for a in post.attachments)

        def update_summary():
            n = len(selected)
            size = sum(lesson_bytes(p, quality['value']) for c, p in all_posts(prod) if (c.pk, p.pk) in selected)
            summary.value = f'{n} lesson{"" if n == 1 else "s"} selected' + (f' · about {human_size(size)}' if n else '')
            dl_btn.disabled = n == 0 or bool(self.manager and self.manager.running)

        def toggle(cat, post, value):
            (selected.add if value else selected.discard)((cat.pk, post.pk))
            update_summary()
            self.page.update()

        def build_sections():
            sections.controls.clear()
            rows.clear()
            for cat in prod.categories:
                items = []
                for post in cat.posts:
                    ok = fully_saved(post)
                    if ok and self.lib.lesson_download_bytes(prod, post) > 0:
                        # saved on this device: tapping the green check offers to delete it
                        icon = ft.PopupMenuButton(
                            icon=ft.Icons.CHECK_CIRCLE, icon_color=ft.Colors.GREEN, tooltip='Saved. Tap for options',
                            items=[ft.PopupMenuItem('Delete video and files from this device', icon=ft.Icons.DELETE_OUTLINE,
                                                    on_click=lambda e, p=post: ask_delete_lesson(p))])
                    else:
                        icon = ft.Icon(ft.Icons.CHECK_CIRCLE if ok else (ft.Icons.CLOUD_OUTLINED if downloadable(post) else ft.Icons.ARTICLE_OUTLINED),
                                       color=ft.Colors.GREEN if ok else ft.Colors.OUTLINE, size=20)
                    cb = ft.Checkbox(value=(cat.pk, post.pk) in selected, visible=downloadable(post) and not ok,
                                     on_change=lambda e, c=cat, p=post: toggle(c, p, e.control.value))
                    rows[post.pk] = (cb, icon)
                    items.append(ft.ListTile(
                        leading=cb if (downloadable(post) and not ok) else ft.Container(width=40),
                        title=ft.Text(post.name.strip(), max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                        subtitle=ft.Text(lesson_label(post, quality['value'], ok), size=12),
                        trailing=icon, dense=True,
                        on_click=lambda e, c=cat, p=post: self.push(self.lesson_view(prod.name, c.pk, p.pk))))
                done = sum(1 for p in cat.posts if fully_saved(p))
                sections.controls.append(ft.ExpansionTile(
                    title=ft.Text(cat.name.strip(), weight=ft.FontWeight.W_600),
                    subtitle=ft.Text(f'{done} of {len(cat.posts)} saved'), controls=items,
                    expanded=len(prod.categories) == 1, controls_padding=0))
            update_summary()

        def select(mode):
            selected.clear()
            if mode == 'unsaved':
                selected.update((c.pk, p.pk) for c, p in all_posts(prod) if downloadable(p) and not fully_saved(p))
            build_sections()
            self.page.update()

        def on_quality(e):
            quality['value'] = e.control.value
            build_sections()
            self.page.update()

        def refresh():
            nonlocal prod
            prod = self.lib.load(name)
            build_sections()

        # --- download job ---
        last_ui = [0.0]

        def on_update(m: DownloadManager):
            now = time.time()
            if not m.finished and now - last_ui[0] < 0.25:
                return
            last_ui[0] = now
            if m.finished:
                job_box.visible = False
                pause_btn.visible = bar.visible = False
                if m.session_expired:
                    self.ui(lambda: self.reauth(prod.url, prod.display_title, prod.email, lambda: start(resume=True)))
                    return
                paused = m.cancel.is_set()
                failed = len(m.errors)
                if not paused:
                    selected.clear()
                refresh()
                self.page.update()
                if paused:
                    self.toast('Paused. Tap Download to continue where you left off.')
                elif failed:
                    self.alert('Some lessons failed', '\n'.join(m.errors[:5]))
                else:
                    self.toast('All done! Your lessons are saved on this device.')
                return
            frac = (m.done_bytes / m.total_bytes) if m.total_bytes else None
            job_text.value = (f'Downloading {m.index + 1} of {len(m.items)}: {m.current_name.strip()}'
                              + (f'  ({human_size(m.done_bytes)} of {human_size(m.total_bytes)})' if m.total_bytes else ''))
            bar.value = frac
            self.ui(lambda: None)

        def start(resume=False):
            try:
                fetcher = self.lib.fetcher_for(prod.url)
            except NeedsLogin:
                self.reauth(prod.url, prod.display_title, prod.email, lambda: start())
                return
            items = [(c, p) for c, p in all_posts(prod) if (c.pk, p.pk) in selected]
            if not items:
                return
            self.manager = DownloadManager(self.lib, prod, fetcher, quality['value'], on_update)
            job_box.visible = pause_btn.visible = bar.visible = True
            job_text.value, bar.value = 'Starting…', None
            dl_btn.disabled = True
            self.page.update()
            self.manager.start(items)

        dl_btn.on_click = lambda e: start()
        pause_btn.on_click = lambda e: self.manager and self.manager.pause()

        qdrop = ft.Dropdown(value=quality['value'], label='Video quality', on_select=on_quality, expand=True,
                            options=[ft.DropdownOption(k, v) for k, v in QUALITY_LABELS.items()])
        def ask_delete_lesson(post):
            size = human_size(self.lib.lesson_download_bytes(prod, post))
            self.confirm('Delete this download?',
                         f'This removes the saved video and files for "{post.name.strip()}" from this device ({size}). '
                         'The lesson stays in the course and you can download it again.', 'Delete',
                         lambda: (self.lib.delete_lesson_downloads(prod, post), refresh(),
                                  self.toast(f'Deleted. Freed {size}.')))

        def ask_delete_all(e=None):
            size = human_size(sum(self.lib.lesson_download_bytes(prod, p) for _, p in all_posts(prod)))
            self.confirm('Delete all downloads for this course?',
                         f'This removes every saved video and file of "{prod.display_title}" from this device ({size}). '
                         'The course and its lessons stay, and you can download them again.', 'Delete all',
                         lambda: (self.lib.delete_course_downloads(prod), refresh(), self.toast(f'Deleted. Freed {size}.')))

        def resync(e):
            """Re-read the outline and lessons. New lessons appear; nothing already downloaded is ever removed."""
            def work(fetcher, status):
                return add_course(self.lib, fetcher, prod.url, title=prod.title, email=prod.email, on_status=status)

            self.busy('Checking for new lessons', work,
                      lambda p: (refresh(), self.toast('Course is up to date.'), self.page.update()),
                      prod.url, prod.display_title, prod.email)

        build_sections()
        return self.view(prod.display_title, [
            ft.Row([qdrop]),
            ft.Row([ft.TextButton('Select everything new', on_click=lambda e: select('unsaved')),
                    ft.TextButton('Clear', on_click=lambda e: select('none'))], wrap=True),
            sections,
        ], refresh=refresh, actions=[ft.IconButton(ft.Icons.REFRESH, tooltip='Check for new lessons', on_click=resync),
                     ft.PopupMenuButton(items=[ft.PopupMenuItem('Delete all downloads', icon=ft.Icons.DELETE_SWEEP_OUTLINED,
                                                                on_click=ask_delete_all)])],
            footer=ft.Column([job_box, ft.Row([dl_btn, summary], spacing=12,
                                                              vertical_alignment=ft.CrossAxisAlignment.CENTER)], spacing=6))

    def reauth(self, site_url, label, email, then):
        """Session expired: ask for the password again (the email is remembered), in a card on the current screen."""
        pw = ft.TextField(label='Password', password=True, can_reveal_password=True, autofocus=True,
                          width=self.narrow(360))
        err = ft.Text('', color=ft.Colors.ERROR, size=13)
        close = None

        def ok(e=None):
            try:
                self.lib.sign_in(site_url, email or '', pw.value)
            except LoginError as ex:
                err.value = str(ex)
                self.page.update()
                return
            close()
            then()

        pw.on_submit = ok
        close = self.inline([
            ft.Text('Please sign in again', size=16, weight=ft.FontWeight.W_600),
            ft.Text(f'Your sign-in for {label} has expired.' + (f'\nAccount: {email}' if email else '')),
            pw, err,
            ft.Row([ft.TextButton('Cancel', on_click=lambda e: close()), ft.TextButton('Sign in', on_click=ok)],
                   alignment=ft.MainAxisAlignment.END)], modal=False)

    # ----- Lesson -----
    async def _track_position(self, video):
        """Keep a recent playback position for the arrow-key skip while this lesson's video is open."""
        while self.active_video is video:
            try:
                self.video_pos_ms = (await video.get_current_position()).in_milliseconds
            except Exception:
                pass   # not loaded yet, or the window is closing
            await asyncio.sleep(0.15)

    async def on_key(self, e):
        """Left/right arrow keys skip SEEK_SECONDS back/forward in the lesson video.

        Flet's player already handles the arrow keys itself with a fixed 2-second step and cannot be switched off,
        so the target is computed from the position just BEFORE the key press and sought to absolutely; our seek lands
        after the player's own, so the result is exactly +/- SEEK_SECONDS instead of the two stacking."""
        video = self.active_video
        if video is None or e.key not in ('Arrow Left', 'Arrow Right') or e.ctrl or e.alt or e.meta or e.shift:
            return
        step_ms = SEEK_SECONDS * 1000 * (1 if e.key == 'Arrow Right' else -1)
        try:
            now = time.monotonic()
            if self.seek_to_ms is not None and now - self.seek_at < 0.8:
                base = self.seek_to_ms            # pressed again straight away: build on where we just sent it
            elif self.video_pos_ms is not None:
                base = self.video_pos_ms          # position before this key press
            else:
                base = (await video.get_current_position()).in_milliseconds
            duration = (await video.get_duration()).in_milliseconds
            target = max(0, base + step_ms)
            if duration:
                target = min(target, duration)
            self.seek_to_ms, self.seek_at = int(target), now
            await video.seek(ft.Duration(milliseconds=int(target)))
        except Exception:
            pass   # e.g. the video is not ready yet; skipping is a convenience, never worth an error

    def player(self, source: str, on_error=None):
        """A video that fills the page width at 16:9, plus a row of speed buttons. Returns [video row, speed row].
        The chosen speed is remembered and applies to the next lesson too."""
        kwargs = {'on_error': on_error} if on_error else {}
        # Desktop (Mac/Windows): clicking the picture itself plays/pauses, like most players. Phones keep their own
        # touch controls (material=None leaves Android/iOS at the defaults).
        controls = fv.AdaptiveVideoControls(material_desktop=fv.MaterialDesktopVideoControls(play_and_pause_on_tap=True))
        video = fv.Video(playlist=[fv.VideoMedia(source)], autoplay=False, aspect_ratio=16 / 9, expand=True,
                         playback_rate=self.playback_rate, controls=controls, **kwargs)
        self.active_video = video
        self.video_pos_ms = self.seek_to_ms = None
        self.page.run_task(self._track_position, video)
        frame = ft.Container(video, aspect_ratio=16 / 9, expand=True, bgcolor=ft.Colors.BLACK,
                             border_radius=12, clip_behavior=ft.ClipBehavior.HARD_EDGE)
        chips = []

        def pick(rate):
            self.playback_rate = rate
            self.lib.set_setting('playback_rate', rate)
            video.playback_rate = rate
            for chip in chips:
                chip.selected = chip.data == rate
            self.page.update()

        for rate in SPEEDS:
            chips.append(ft.Chip(label=ft.Text(speed_label(rate)), data=rate, selected=rate == self.playback_rate,
                                 show_checkmark=False, on_select=lambda e, r=rate: pick(r)))
        speed_row = ft.Row([ft.Text('Speed', size=13, color=ft.Colors.OUTLINE), *chips],
                           wrap=True, spacing=6, run_spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        return [ft.Row([frame]), speed_row]

    def lesson_view(self, name: str, cat_pk: str, post_pk: str) -> ft.View:
        prod = self.lib.load(name)
        cat = next(c for c in prod.categories if c.pk == cat_pk)
        post = next(p for p in cat.posts if p.pk == post_pk)
        controls = []

        if self.lib.has_video_file(post):
            path = self.lib.abs_path(post.video_path)
            if fv is not None:
                controls.extend(self.player(path))
            if not self.is_mobile:
                controls.append(ft.Row([ft.TextButton('Open in my video player', icon=ft.Icons.OPEN_IN_NEW,
                                                      on_click=lambda e: open_with_system(path))]))
        elif post.wistia_assets:
            # Not saved on this device: stream it from the course's video host instead (needs internet).
            try:
                url = pick_video(post.wistia_assets, self.default_quality)['downloadable_url']
            except ValueError:
                url = None
            if url and fv is not None:
                note = ft.Text('Streaming from the internet. Save this lesson to watch it offline.',
                               size=12, color=ft.Colors.OUTLINE)

                def stream_error(e):
                    note.value = 'Could not play the video. Check your internet connection, or save the lesson to watch it offline.'
                    note.color = ft.Colors.ERROR
                    self.page.update()

                controls.extend(self.player(url, on_error=stream_error))
                controls.append(ft.Row([note, ft.TextButton('Open in browser instead', icon=ft.Icons.OPEN_IN_NEW, url=url)],
                                       wrap=True, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER))
            elif url:
                controls.append(ft.Button('Watch online', icon=ft.Icons.PLAY_ARROW, url=url))
            else:
                controls.append(ft.Card(ft.Container(ft.Text('No video is available for this lesson.'), padding=16)))

        controls.append(ft.Text(post.name.strip(), size=22, weight=ft.FontWeight.W_600, selectable=True))
        if post.content:
            controls.append(ft.Text(post.content, selectable=True))
        if post.attachments:
            controls.append(ft.Text('Files', size=16, weight=ft.FontWeight.W_600))
            for att in post.attachments:
                local = bool(att.local_file_path) and os.path.exists(self.lib.abs_path(att.local_file_path))
                trailing = None
                if local and self.is_mobile:
                    trailing = ft.Row([
                        ft.IconButton(ft.Icons.SHARE, tooltip='Open in another app / share',
                                      on_click=lambda e, a=att: self.page.run_task(self.share_attachment, a)),
                        ft.IconButton(ft.Icons.SAVE_ALT, tooltip='Save a copy to my phone',
                                      on_click=lambda e, a=att: self.page.run_task(self.save_attachment_copy, a)),
                    ], tight=True, spacing=0)
                controls.append(ft.ListTile(
                    leading=ft.Icon(ft.Icons.ATTACH_FILE), title=ft.Text(att.name or 'File'),
                    subtitle=ft.Text(('Tap to open' if local else 'Not downloaded yet')),
                    trailing=trailing, dense=True,
                    on_click=self.attachment_click(att) if local else None))
            if self.is_mobile:
                controls.append(ft.Text('Tap a file to open it in another app. Use the save button to put a copy in a '
                                        'folder you choose (for example Downloads).', size=12, color=ft.Colors.OUTLINE))
        actions = []
        if self.lib.lesson_download_bytes(prod, post) > 0:
            def ask_delete(e):
                size = human_size(self.lib.lesson_download_bytes(prod, post))
                self.confirm('Delete this download?',
                             f'This removes the saved video and files for this lesson from this device ({size}). '
                             'The lesson stays in the course and you can download it again.', 'Delete',
                             lambda: (self.lib.delete_lesson_downloads(prod, post), self.pop(), self.toast(f'Deleted. Freed {size}.')))
            actions.append(ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip='Delete this download', on_click=ask_delete))
        return self.view(post.name.strip(), controls, actions=actions)

    def attachment_click(self, att):
        """Tapping a saved file: share sheet on phones, the default app on desktop."""
        if self.is_mobile:
            return lambda e: self.page.run_task(self.share_attachment, att)
        return lambda e: self.open_attachment(att)

    def open_attachment(self, att):
        try:
            open_with_system(self.lib.abs_path(att.local_file_path))
        except Exception as e:
            self.alert('Could not open the file', str(e))

    def saved_file(self, att):
        """Absolute path of a downloaded attachment, or None (with a friendly message) if it is gone."""
        path = self.lib.abs_path(att.local_file_path) if att.local_file_path else None
        if not path or not os.path.exists(path):
            self.alert('File not found', 'This file is no longer on this device. Go back to the course and download it again.')
            return None
        return path

    async def share_attachment(self, att):
        """Android: the system share sheet (lists PDF viewers, Drive, Gmail, ...) for this file."""
        path = self.saved_file(att)
        if path is None:
            return
        try:
            # ShareFile(...) rather than ShareFile.from_path(): only the constructor accepts a mime type,
            # which helps Android offer the right apps (PDF viewer, music player, ...).
            shared = ft.ShareFile(path=path, name=os.path.basename(path), mime_type=mimetypes.guess_type(path)[0])
            await self.share.share_files([shared], title=att.name or os.path.basename(path))
        except Exception as e:
            self.alert('Could not open the file', str(e))

    async def save_attachment_copy(self, att):
        """Android: let the user pick where to save a copy (Downloads, Drive, ...)."""
        path = self.saved_file(att)
        if path is None:
            return
        if os.path.getsize(path) > MAX_COPY_BYTES:  # the save dialog needs the whole file in memory
            self.alert('File is too large to copy here',
                       f'Use the share button instead and choose "Save to Files" or Drive. ({human_size(os.path.getsize(path))})')
            return
        try:
            with open(path, 'rb') as f:
                data = f.read()
            dest = await self.picker.save_file(dialog_title='Save a copy', file_name=os.path.basename(path), src_bytes=data)
            if dest:
                self.toast('Saved.')
        except Exception as e:
            self.alert('Could not save the file', str(e))


# ----- startup -----
async def resolve_root(page: ft.Page) -> str:
    override = os.environ.get('COURSE_SAVER_LIBRARY')
    if override:
        return override
    try:
        sp = ft.StoragePaths()
        page.services.append(sp)
        docs = await sp.get_application_documents_directory()
        mobile = page.platform in (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS)
        return os.path.join(docs, 'courses' if mobile else 'Course Saver')
    except Exception:
        return os.path.join(os.path.expanduser('~'), 'Documents', 'Course Saver')


async def main(page: ft.Page):
    page.title = APP_NAME
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO, use_material3=True)
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.padding = 0
    try:
        page.window.min_width, page.window.min_height = 380, 560
    except Exception:
        pass

    lib = Library(await resolve_root(page))
    app = App(page, lib)
    page.on_view_pop = app.pop
    page.on_resize = app.on_resize
    page.on_keyboard_event = app.on_key
    page.views.clear()
    page.views.append(app.home())
    page.update()
    app.on_resize()  # apply the wide-window margins once the real window size is known


if __name__ == '__main__':
    ft.run(main)
