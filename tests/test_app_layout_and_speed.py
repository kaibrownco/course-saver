"""Playback speed (all platforms), and the page layout: centered max-width on wide windows, untouched on phones."""
import asyncio
import json
import os
from types import SimpleNamespace

import flet as ft
import flet_video as fv

import main as app_module
from core.service import Library
from test_app_android import lesson, make_app, walk


def chips(view):
    return [c for c in walk(view) if isinstance(c, ft.Chip)]


def videos(view):
    return [c for c in walk(view) if isinstance(c, fv.Video)]


def labels(view):
    return [c.label.value for c in chips(view)]


# ---- speed ----------------------------------------------------------------

def test_every_video_gets_speed_buttons_whether_saved_or_streamed(tmp_path):
    app, page, lib = make_app(tmp_path)
    lesson(lib, downloaded_attachment=True, with_video=True)       # streaming (video not downloaded)
    view = app.lesson_view('p', '1', '10')
    assert labels(view) == ['0.75×', '1×', '1.25×', '1.5×', '1.75×', '2×']
    assert [c.selected for c in chips(view)] == [False, True, False, False, False, False]   # 1x by default
    assert videos(view)[0].playback_rate == 1.0

    prod = lib.load('p')                                           # now pretend it is downloaded
    post = prod.categories[0].posts[0]
    path = lib.abs_path('media/p/10/video.mp4')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'wb').write(b'x')
    post.video_path = 'media/p/10/video.mp4'
    lib.save(prod)
    assert labels(app.lesson_view('p', '1', '10')) == ['0.75×', '1×', '1.25×', '1.5×', '1.75×', '2×']


def test_picking_a_speed_changes_the_player_and_is_remembered(tmp_path):
    app, page, lib = make_app(tmp_path)
    lesson(lib, with_video=True)
    view = app.lesson_view('p', '1', '10')
    two = next(c for c in chips(view) if c.data == 2.0)
    two.on_select(None)
    assert videos(view)[0].playback_rate == 2.0
    assert [c.data for c in chips(view) if c.selected] == [2.0]       # exactly one chip highlighted
    assert Library(str(tmp_path)).get_setting('playback_rate') == 2.0   # saved to disk
    # the next lesson (and the next launch of the app) starts at the speed she chose
    again = app.lesson_view('p', '1', '10')
    assert videos(again)[0].playback_rate == 2.0 and [c.data for c in chips(again) if c.selected] == [2.0]
    fresh_app = app_module.App(make_app(tmp_path)[1], Library(str(tmp_path)))
    assert fresh_app.playback_rate == 2.0


def test_a_corrupt_saved_speed_falls_back_to_normal(tmp_path):
    (tmp_path / 'settings.json').write_text(json.dumps({'playback_rate': 99}))
    app, page, lib = make_app(tmp_path)
    assert app.playback_rate == 1.0
    (tmp_path / 'settings.json').write_text('not json at all')
    assert make_app(tmp_path)[0].playback_rate == 1.0


def test_settings_file_is_not_mistaken_for_a_course(tmp_path):
    lib = Library(str(tmp_path))
    lib.set_setting('playback_rate', 1.5)
    assert lib.products() == [] and lib.sites() == [] and lib.get_setting('missing', 'dflt') == 'dflt'


def test_text_only_lessons_have_no_speed_buttons(tmp_path):
    app, page, lib = make_app(tmp_path)
    lesson(lib, with_video=False)
    assert chips(app.lesson_view('p', '1', '10')) == []


# ---- layout ---------------------------------------------------------------

def test_phones_and_small_windows_keep_the_normal_16px_margin(tmp_path):
    app, page, lib = make_app(tmp_path)
    for width in (None, 0, 360, 412, 700, 900):                  # includes the Android phone sizes
        page.width = width
        assert app.gutter() == 16, width


def test_wide_windows_center_the_content_at_max_width(tmp_path):
    app, page, lib = make_app(tmp_path)
    page.width = 1400
    assert app.gutter() == (1400 - app_module.MAX_CONTENT_WIDTH) / 2
    page.width = 2000
    assert 2000 - 2 * app.gutter() == app_module.MAX_CONTENT_WIDTH   # content column is exactly max width


def test_resizing_the_window_updates_every_open_screen(tmp_path):
    app, page, lib = make_app(tmp_path)
    page.width = 400
    page.views.extend([app.home(), app.add_site_view()])
    page.width = 1500
    app.on_resize()
    expected = (1500 - app_module.MAX_CONTENT_WIDTH) / 2
    for v in page.views:
        assert v.controls[0].content.padding.left == expected and v.controls[0].content.padding.right == expected
    page.width = 390                                              # window shrunk back to phone width
    app.on_resize()
    assert all(v.controls[0].content.padding.left == 16 for v in page.views)


def test_video_fills_the_width_at_16_by_9_instead_of_a_fixed_size(tmp_path):
    app, page, lib = make_app(tmp_path)
    lesson(lib, with_video=True)
    view = app.lesson_view('p', '1', '10')
    frame = next(c for c in walk(view) if isinstance(c, ft.Container) and any(isinstance(x, fv.Video) for x in [c.content]))
    assert frame.aspect_ratio == 16 / 9 and frame.expand is True and frame.height is None and frame.width is None


def test_empty_home_screen_is_centered_and_fits_a_phone(tmp_path):
    app, page, lib = make_app(tmp_path)
    page.width = 1400
    home = app.home()
    rows = [c for c in walk(home) if isinstance(c, ft.Row) and c.alignment == ft.MainAxisAlignment.CENTER]
    assert rows, 'the empty-state block should sit in a centered row'
    inner = rows[0].controls[0]
    assert inner.width == 380                                       # comfortable fixed width on a wide window
    page.width = 360
    assert app.narrow(380) == 360 - 2 * 16                          # never wider than a phone screen allows
    assert any(isinstance(c, ft.Button) and c.content == 'Add a site' for c in walk(home))


def test_clicking_the_video_toggles_play_on_desktop_and_phones_keep_default_controls(tmp_path):
    for platform in (ft.PagePlatform.MACOS, ft.PagePlatform.WINDOWS, ft.PagePlatform.ANDROID):
        app, page, lib = make_app(tmp_path / platform.name, platform=platform)
        lesson(lib, with_video=True)
        video = videos(app.lesson_view('p', '1', '10'))[0]
        assert video.controls.material_desktop.play_and_pause_on_tap is True      # used on Mac/Windows/Linux
        assert video.controls.material is None                                    # Android/iOS controls untouched


class FakeVideo:
    def __init__(self, position_ms, duration_ms):
        self.pos, self.dur, self.seeks = position_ms, duration_ms, []

    async def get_current_position(self):
        return ft.Duration(milliseconds=self.pos)

    async def get_duration(self):
        return ft.Duration(milliseconds=self.dur)

    async def seek(self, position):
        self.seeks.append(position.in_milliseconds)


def key(name, **mods):
    return SimpleNamespace(key=name, ctrl=False, alt=False, meta=False, shift=False, **mods)


def test_arrow_keys_skip_five_seconds(tmp_path):
    app, page, lib = make_app(tmp_path)
    app.active_video = FakeVideo(20_000, 100_000)           # no tracked position yet: it reads the player directly
    asyncio.run(app.on_key(key('Arrow Right')))
    assert app.active_video.seeks == [25_000]


def test_the_players_own_two_second_step_is_overridden_not_added_to(tmp_path):
    """Flet's player already skips 2 s on arrow keys. By the time our handler runs the player may already be at +2 s;
    the target must come from the position BEFORE the key press, so the result is exactly +5 s (not +7 s)."""
    app, page, lib = make_app(tmp_path)
    app.active_video = FakeVideo(22_000, 100_000)           # the player has already jumped 20 s -> 22 s itself
    app.video_pos_ms = 20_000                               # what the tracker saw just before the key press
    asyncio.run(app.on_key(key('Arrow Right')))
    assert app.active_video.seeks == [25_000]
    # and the same for going back
    app2, _, _ = make_app(tmp_path / 'b')
    app2.active_video = FakeVideo(18_000, 100_000)
    app2.video_pos_ms = 20_000
    asyncio.run(app2.on_key(key('Arrow Left')))
    assert app2.active_video.seeks == [15_000]


def test_pressing_the_arrow_again_straight_away_chains_from_the_last_target(tmp_path):
    app, page, lib = make_app(tmp_path)
    app.active_video = FakeVideo(20_000, 100_000)
    app.video_pos_ms = 20_000
    for _ in range(3):                                      # the tracker has not caught up between rapid presses
        asyncio.run(app.on_key(key('Arrow Right')))
    assert app.active_video.seeks == [25_000, 30_000, 35_000]


def test_arrow_key_skips_stay_inside_the_video(tmp_path):
    app, page, lib = make_app(tmp_path)
    app.active_video = FakeVideo(2_000, 100_000)
    app.video_pos_ms = 2_000
    asyncio.run(app.on_key(key('Arrow Left')))              # near the start: clamp at 0, not negative
    assert app.active_video.seeks == [0]
    app2, _, _ = make_app(tmp_path / 'end')
    app2.active_video = FakeVideo(98_000, 100_000)
    app2.video_pos_ms = 98_000
    asyncio.run(app2.on_key(key('Arrow Right')))            # near the end: clamp at the duration
    assert app2.active_video.seeks == [100_000]


def test_other_keys_modifiers_and_no_video_do_nothing(tmp_path):
    app, page, lib = make_app(tmp_path)
    asyncio.run(app.on_key(key('Arrow Right')))            # no video open: no error
    app.active_video = FakeVideo(20_000, 100_000)
    asyncio.run(app.on_key(key('Arrow Up')))
    asyncio.run(app.on_key(key('a')))
    cmd = key('Arrow Right'); cmd.meta = True               # Cmd+Right is a different shortcut: leave it alone
    asyncio.run(app.on_key(cmd))
    assert app.active_video.seeks == []


def test_a_seek_problem_never_surfaces_as_an_error(tmp_path):
    app, page, lib = make_app(tmp_path)

    class NotReady:
        async def get_current_position(self):
            raise RuntimeError('player not ready')

    app.active_video = NotReady()
    asyncio.run(app.on_key(key('Arrow Right')))            # must not raise


def test_the_lesson_video_receives_the_keys_and_leaving_the_lesson_stops_it(tmp_path):
    app, page, lib = make_app(tmp_path)
    lesson(lib, with_video=True)
    page.views.append(app.lesson_view('p', '1', '10'))
    assert app.active_video is videos(page.views[-1])[0]
    app.pop()
    assert app.active_video is None
