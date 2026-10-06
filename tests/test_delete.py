"""Deleting downloads: removes exactly what was asked for, keeps the course, never touches anything else."""
import os

from core.models import Attachment, Category, Post, Product
from core.service import Library


def put(lib, rel, data=b'x' * 100):
    path = lib.abs_path(rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'wb').write(data)
    return path


def course(lib, name='p'):
    posts = []
    for pk in ('10', '11'):
        post = Post(pk, f'Lesson {pk}', 'u')
        post.video_path = f'media/{name}/{pk}/video.mp4'
        put(lib, post.video_path, b'v' * 1000)
        post.attachments = [Attachment('u', 'Notes.pdf', local_file_path=f'media/{name}/{pk}/attachments/Notes.pdf')]
        put(lib, post.attachments[0].local_file_path, b'a' * 50)
        posts.append(post)
    prod = Product(name, 'https://x.example/products/' + name, categories=[Category('1', 'Sec', 'u', posts=posts)])
    lib.save(prod)
    return prod


def test_delete_one_lesson_keeps_the_other_and_the_course(tmp_path):
    lib = Library(str(tmp_path))
    prod = course(lib)
    a, b = prod.categories[0].posts
    assert lib.lesson_download_bytes(prod, a) == 1050
    freed = lib.delete_lesson_downloads(prod, a)
    assert freed == 1050
    assert not os.path.exists(lib.abs_path('media/p/10')) and os.path.exists(lib.abs_path('media/p/11/video.mp4'))
    assert a.video_path is None and a.attachments[0].local_file_path is None and b.video_path
    reloaded = lib.load('p').categories[0].posts       # persisted, and the lesson is still in the course
    assert [p.pk for p in reloaded] == ['10', '11'] and reloaded[0].video_path is None and reloaded[1].video_path


def test_delete_clears_half_finished_download_leftovers(tmp_path):
    lib = Library(str(tmp_path))
    prod = course(lib)
    put(lib, 'media/p/10/video.mp4.part', b'p' * 300)
    lib.delete_lesson_downloads(prod, prod.categories[0].posts[0])
    assert not os.path.exists(lib.abs_path('media/p/10'))


def test_delete_never_touches_files_outside_the_courses_media_folder(tmp_path):
    lib = Library(str(tmp_path))
    prod = course(lib)
    outside = tmp_path / 'precious.txt'
    outside.write_text('keep me')
    other = put(lib, 'media/other-course/99/video.mp4')
    other_json = lib.product_path('p')
    post = prod.categories[0].posts[0]
    post.video_path = '../precious.txt'                  # a corrupted / malicious path
    post.attachments[0].local_file_path = 'media/other-course/99/video.mp4'   # another course's file
    lib.delete_lesson_downloads(prod, post)
    assert outside.read_text() == 'keep me'
    assert os.path.exists(other)                          # other course untouched
    assert os.path.exists(other_json)                     # the course file itself untouched


def test_delete_works_for_files_from_the_old_section_based_layout(tmp_path):
    lib = Library(str(tmp_path))
    prod = course(lib)
    post = prod.categories[0].posts[0]
    old = put(lib, 'media/p/1/10/video.mp4', b'o' * 70)   # recorded under media/<course>/<section>/<lesson>/
    post.video_path = 'media/p/1/10/video.mp4'
    lib.delete_lesson_downloads(prod, post)
    assert not os.path.exists(old)


def test_delete_course_downloads_keeps_course_and_other_courses(tmp_path):
    lib = Library(str(tmp_path))
    prod = course(lib)
    other = course(lib, 'q')
    freed = lib.delete_course_downloads(prod)
    assert freed == 2100
    assert not os.path.exists(lib.abs_path('media/p')) and os.path.exists(lib.abs_path('media/q/10/video.mp4'))
    saved = lib.load('p')
    assert len(saved.categories[0].posts) == 2 and all(p.video_path is None for p in saved.categories[0].posts)
    assert lib.load('q').categories[0].posts[0].video_path   # untouched
    assert lib.delete_course_downloads(prod) == 0             # safe to repeat


def test_deleting_with_nothing_downloaded_is_a_noop(tmp_path):
    lib = Library(str(tmp_path))
    prod = Product('p', 'https://x.example/products/p', categories=[Category('1', 'S', 'u', posts=[Post('1', 'L', 'u')])])
    lib.save(prod)
    assert lib.delete_lesson_downloads(prod, prod.categories[0].posts[0]) == 0
