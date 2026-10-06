"""Runs the phone-only UI code paths with a stub page and the real Flet classes.

The share sheet / save dialog themselves need a device, but everything we hand to them is built here for real, so a
wrong argument (like the ShareFile mime_type bug) fails in CI instead of on a phone.
"""
import asyncio
from types import SimpleNamespace

import flet as ft

import main as app_module
from core.models import Attachment, Category, Post, Product
from core.service import Library


class StubPage:
    def __init__(self, platform):
        self.platform = platform
        self.services, self.views, self.dialogs = [], [], []

    def show_dialog(self, dlg):
        self.dialogs.append(dlg)

    def pop_dialog(self):
        self.dialogs.pop()

    def update(self):
        pass

    def run_task(self, fn, *a):  # mimic Page.run_task for handlers wired to buttons
        return asyncio.run(fn(*a))


class FakeShare:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    async def share_files(self, files, **kw):
        if self.fail:
            raise self.fail
        self.calls.append((files, kw))


class FakePicker:
    def __init__(self):
        self.calls = []

    async def save_file(self, **kw):
        self.calls.append(kw)
        return '/storage/emulated/0/Download/x.pdf'


def make_app(tmp_path, platform=ft.PagePlatform.ANDROID):
    lib = Library(str(tmp_path))
    page = StubPage(platform)
    app = app_module.App(page, lib)
    app.share, app.picker = FakeShare(), FakePicker()
    return app, page, lib


def saved_attachment(lib, name='Notes.pdf', data=b'%PDF-1.4 test'):
    path = lib.root + f'/media/p/10/attachments/{name}'
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'wb').write(data)
    return Attachment('u', 'Notes', local_file_path=lib.rel(path)), path


def dialog_text(page):
    dlg = page.dialogs[-1]
    return f'{dlg.title.value} {dlg.content.value}'


def test_share_builds_a_real_sharefile_with_mime_type_and_name(tmp_path):
    app, page, lib = make_app(tmp_path)
    att, path = saved_attachment(lib)
    asyncio.run(app.share_attachment(att))
    assert not page.dialogs, f'unexpected error dialog: {page.dialogs and dialog_text(page)}'
    (files, kw), = app.share.calls
    assert isinstance(files[0], ft.ShareFile)
    assert (files[0].path, files[0].name, files[0].mime_type) == (path, 'Notes.pdf', 'application/pdf')
    assert kw['title'] == 'Notes'


def test_share_handles_unknown_extension_and_odd_names(tmp_path):
    app, page, lib = make_app(tmp_path)
    att, path = saved_attachment(lib, name='Day1 (Audio) Mac users.wma', data=b'x')
    asyncio.run(app.share_attachment(att))
    assert not page.dialogs
    assert app.share.calls[0][0][0].name == 'Day1 (Audio) Mac users.wma'  # mime may be None; share_plus guesses


def test_share_missing_file_shows_friendly_message_not_a_crash(tmp_path):
    app, page, lib = make_app(tmp_path)
    asyncio.run(app.share_attachment(Attachment('u', 'Gone', local_file_path='media/p/1/attachments/gone.pdf')))
    assert 'no longer on this device' in dialog_text(page) and not app.share.calls
    asyncio.run(app.share_attachment(Attachment('u', 'Never downloaded')))  # no path at all
    assert len(page.dialogs) == 2


def test_share_failure_is_reported_in_a_dialog(tmp_path):
    app, page, lib = make_app(tmp_path)
    att, _ = saved_attachment(lib)
    app.share = FakeShare(fail=RuntimeError('boom'))
    asyncio.run(app.share_attachment(att))
    assert 'Could not open the file' in dialog_text(page) and 'boom' in dialog_text(page)


def test_save_copy_passes_bytes_and_filename(tmp_path):
    app, page, lib = make_app(tmp_path)
    att, _ = saved_attachment(lib, data=b'hello pdf')
    asyncio.run(app.save_attachment_copy(att))
    call, = app.picker.calls
    assert call['src_bytes'] == b'hello pdf' and call['file_name'] == 'Notes.pdf'
    assert page.dialogs and isinstance(page.dialogs[-1], ft.SnackBar)  # the "Saved." toast


def test_save_copy_refuses_files_too_big_to_hold_in_memory(tmp_path, monkeypatch):
    app, page, lib = make_app(tmp_path)
    att, _ = saved_attachment(lib, data=b'0123456789')
    monkeypatch.setattr(app_module, 'MAX_COPY_BYTES', 5)
    asyncio.run(app.save_attachment_copy(att))
    assert 'too large' in dialog_text(page) and not app.picker.calls


def walk(control, seen=None):
    """Every control under `control`, following the common child slots."""
    seen = seen if seen is not None else []
    if control is None or isinstance(control, (str, int, float, bool)):
        return seen
    seen.append(control)
    for slot in ('controls', 'content', 'trailing', 'leading', 'actions', 'title', 'subtitle'):
        child = getattr(control, slot, None)
        for c in (child if isinstance(child, list) else [child]):
            if isinstance(c, ft.Control):
                walk(c, seen)
    return seen


def lesson(lib, downloaded_attachment=True, with_video=True):
    att, _ = saved_attachment(lib) if downloaded_attachment else (Attachment('u', 'Notes'), None)
    post = Post('10', 'Lesson', 'u', content='Body text')
    post.attachments = [att]
    if with_video:
        post.wistia_assets = [{'type': 'mp4_video', 'ext': '.mp4', 'downloadable_url': 'https://v.example/x.mp4',
                               'size': 100, 'width': 640, 'height': 360}]
    prod = Product('p', 'https://courses.example.com/products/p', categories=[Category('1', 'Sec', 'u', posts=[post])])
    lib.save(prod)


def tooltips(view):
    """Tooltips of every icon button on a screen, including the ones in its top bar."""
    controls = walk(view) + list(view.appbar.actions if view.appbar else [])
    return {c.tooltip for c in controls if isinstance(c, ft.IconButton) and c.tooltip}


def test_mobile_lesson_shows_share_and_save_buttons_and_streams_unsaved_video(tmp_path):
    app, page, lib = make_app(tmp_path)
    lesson(lib)
    view = app.lesson_view('p', '1', '10')
    tips = tooltips(view)
    assert 'Open in another app / share' in tips and 'Save a copy to my phone' in tips
    videos = [c for c in walk(view) if c.__class__.__name__ == 'Video']
    assert videos and videos[0].playlist[0].resource == 'https://v.example/x.mp4'  # streams while not downloaded


def test_desktop_lesson_has_no_phone_only_buttons(tmp_path):
    app, page, lib = make_app(tmp_path, platform=ft.PagePlatform.WINDOWS)
    lesson(lib)
    view = app.lesson_view('p', '1', '10')
    assert not tooltips(view) & {'Open in another app / share', 'Save a copy to my phone'}


def test_lesson_with_nothing_downloaded_still_builds(tmp_path):
    app, page, lib = make_app(tmp_path)
    lesson(lib, downloaded_attachment=False, with_video=False)
    assert app.lesson_view('p', '1', '10') is not None


def find(view, pred):
    return [c for c in walk(view) if pred(c)]


def test_lesson_screen_has_a_delete_button_only_when_something_is_downloaded(tmp_path):
    app, page, lib = make_app(tmp_path)
    lesson(lib, downloaded_attachment=True, with_video=False)
    assert 'Delete this download' in tooltips(app.lesson_view('p', '1', '10'))
    lesson(lib, downloaded_attachment=False, with_video=False)
    assert 'Delete this download' not in tooltips(app.lesson_view('p', '1', '10'))


def test_deleting_from_the_lesson_screen_asks_first_then_deletes(tmp_path):
    import os
    app, page, lib = make_app(tmp_path)
    lesson(lib, downloaded_attachment=True, with_video=False)
    page.views.append(SimpleNamespace(data=None))            # a screen to go back to
    view = app.lesson_view('p', '1', '10')
    trash = next(c for c in view.appbar.actions if getattr(c, 'tooltip', '') == 'Delete this download')
    trash.on_click(None)
    dlg = page.dialogs[-1]
    assert 'Delete this download?' in dlg.title.value and 'stays in the course' in dlg.content.value
    path = lib.abs_path(lib.load('p').categories[0].posts[0].attachments[0].local_file_path)
    assert os.path.exists(path)                               # nothing deleted yet
    dlg.actions[0].on_click(None)                             # Cancel
    assert os.path.exists(path)
    trash.on_click(None)
    page.dialogs[-1].actions[1].on_click(None)                # confirm
    assert not os.path.exists(path)
    assert lib.load('p').categories[0].posts[0].attachments[0].local_file_path is None


def test_course_screen_offers_delete_for_saved_lessons_and_for_everything(tmp_path):
    app, page, lib = make_app(tmp_path)
    lesson(lib, downloaded_attachment=True, with_video=False)
    view = app.course_view('p')
    menus = find(view, lambda c: isinstance(c, ft.PopupMenuButton) and c.tooltip == 'Saved. Tap for options')
    assert len(menus) == 1                                    # the saved lesson shows a tappable check
    top_menu = next(a for a in view.appbar.actions if isinstance(a, ft.PopupMenuButton))
    assert top_menu.items[0].content == 'Delete all downloads'
