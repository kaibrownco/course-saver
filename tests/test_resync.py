"""Re-syncing a course whose outline changed must never lose or delete the user's downloads."""
from types import SimpleNamespace

from core.models import Attachment, Category, Post, Product
from core.service import REMOVED_PK, Library, merge_attachments, scrape_posts, scrape_tree

BASE = 'https://x.com/products/p'


class FakeFetcher:
    """Serves tiny HTML pages: {url: html}."""
    def __init__(self, pages):
        self.pages = pages

    def get_page(self, url):
        return SimpleNamespace(text=self.pages[url])


def outline(sections):
    """sections: {cat_pk: [(post_pk, name), ...]} -> fake site pages."""
    pages = {BASE: ''.join(f'<a href="{BASE}/categories/{c}"><h3 class="title">Section {c}</h3></a>' for c in sections)}
    for c, posts in sections.items():
        pages[f'{BASE}/categories/{c}'] = ''.join(
            f'<a href="{BASE}/categories/{c}/posts/{pk}"><h3 class="title">{name}</h3></a>' for pk, name in posts)
    return pages


def downloaded_post(pk, name):
    post = Post(pk, name, f'{BASE}/categories/1/posts/{pk}')
    post.scraped = True
    post.video_path = f'media/p/{pk}/video.mp4'
    post.attachments = [Attachment('u', 'Notes.pdf', local_file_path=f'media/p/{pk}/attachments/Notes.pdf')]
    return post


def existing_product():
    return Product('p', BASE, categories=[Category('1', 'Section 1', 'u', posts=[
        downloaded_post('10', 'Old name'), downloaded_post('11', 'Will be removed'), Post('12', 'Never downloaded', 'u')])])


def test_renamed_and_moved_lessons_keep_their_downloads():
    pages = outline({'1': [('12', 'Never downloaded')], '2': [('10', 'New name')]})  # 10 moved to section 2 and renamed
    prod = scrape_tree(FakeFetcher(pages), BASE, 'p', existing=existing_product())
    moved = next(p for c in prod.categories if c.pk == '2' for p in c.posts)
    assert moved.pk == '10' and moved.name == 'New name'
    assert moved.video_path == 'media/p/10/video.mp4' and moved.attachments[0].local_file_path
    assert moved.scraped is False  # will be re-read, but downloads are untouched


def test_vanished_lesson_with_downloads_is_kept_and_without_is_dropped():
    pages = outline({'1': [('10', 'Old name')]})  # 11 (downloaded) and 12 (not downloaded) are gone
    prod = scrape_tree(FakeFetcher(pages), BASE, 'p', existing=existing_product())
    removed = next(c for c in prod.categories if c.pk == REMOVED_PK)
    assert [p.pk for p in removed.posts] == ['11'] and removed.posts[0].video_path
    assert '12' not in [p.pk for c in prod.categories for p in c.posts]

    # syncing again keeps it parked there (it is not lost on the second pass) ...
    again = scrape_tree(FakeFetcher(pages), BASE, 'p', existing=prod)
    assert [p.pk for c in again.categories if c.pk == REMOVED_PK for p in c.posts] == ['11']
    # ... and if it reappears on the site it returns to a normal section with its files
    back = scrape_tree(FakeFetcher(outline({'1': [('10', 'Old name'), ('11', 'Back again')]})), BASE, 'p', existing=again)
    assert not [c for c in back.categories if c.pk == REMOVED_PK]
    assert next(p for p in back.categories[0].posts if p.pk == '11').video_path


def test_merge_attachments_keeps_local_paths_and_vanished_downloads():
    old = [Attachment('u1', 'A.pdf', 'media/A.pdf'), Attachment('u2', 'B.pdf', 'media/B.pdf'), Attachment('u3', 'C.pdf')]
    new = [Attachment('u1', 'A.pdf'), Attachment('u9', 'B.pdf'), Attachment('u4', 'D.pdf')]  # B's url changed, C/D new
    merged = merge_attachments(old, new)
    paths = {a.name: a.local_file_path for a in merged}
    assert paths == {'A.pdf': 'media/A.pdf', 'B.pdf': 'media/B.pdf', 'D.pdf': None}  # C never downloaded: dropped


def test_rescrape_posts_does_not_unlink_downloaded_attachments(tmp_path):
    post = downloaded_post('10', 'L')
    post.scraped = False
    prod = Product('p', BASE, categories=[Category('1', 'S', 'u', posts=[post])])
    html = '<div class="post-body"><p>Hi</p></div><a href="/courses/downloads/5/notes-pdf">Notes.pdf</a>'
    scrape_posts(FakeFetcher({post.url: html}), prod, Library(str(tmp_path)).save)
    assert post.content == 'Hi' and post.scraped
    assert post.attachments[0].local_file_path == 'media/p/10/attachments/Notes.pdf'
    assert post.video_path == 'media/p/10/video.mp4'
