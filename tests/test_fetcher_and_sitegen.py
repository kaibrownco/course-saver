import json

from core.fetcher import load_cookies
from core.models import Product, Category, Post, Attachment
from core.parser import Parser
from core.service import Library
from sitegen.sitegen import generate_site


def test_load_cookies_browser_export_and_dict(tmp_path):
    export = tmp_path / 'a.json'
    export.write_text(json.dumps([{'name': 'k', 'value': 'v', 'domain': 'x.com', 'path': '/'}]))
    cookies, ua = load_cookies(str(export))
    assert cookies == [('k', 'v', 'x.com', '/')] and ua is None

    simple = tmp_path / 'b.json'
    simple.write_text(json.dumps({'userAgent': 'UA', 'k': 'v'}))
    cookies, ua = load_cookies(str(simple))
    assert cookies == [('k', 'v', None, '/')] and ua == 'UA'


def test_get_categories_dedupes_resume_link_by_pk():
    html = ('<a href="/products/p/categories/1/posts/9"><h3 class="title">Get Started</h3></a>'
            '<a href="/products/p/categories/1"><h3 class="title">Real Name</h3></a>')
    cats = Parser.get_categories(html, 'https://x.com/products/p')
    assert [(c.pk, c.name, c.url) for c in cats] == [('1', 'Real Name', 'https://x.com/products/p/categories/1')]


def test_generate_site_links_local_media(tmp_path):
    lib = tmp_path / 'lib'
    (lib / 'media' / 'demo' / '2').mkdir(parents=True)
    (lib / 'media' / 'demo' / '2' / 'video.mp4').write_bytes(b'x')
    post = Post('2', 'Lesson', 'u', content='Hello\n\nWorld', attachments=[Attachment('u', 'Notes.pdf')])
    post.wistia_assets = [{'type': 'original'}]
    post.video_path = 'media/demo/2/video.mp4'
    prod = Product('demo', 'u', categories=[Category('1', 'Section', 'u', posts=[post])])
    Library(str(lib)).save(prod)
    (lib / 'cookies.json').write_text('{"not": "a product"}')  # must be ignored

    assert generate_site(str(lib)) == 1
    html = (lib / 'site' / 'demo' / '1' / '2' / 'index.html').read_text()
    assert 'src="../../../../media/demo/2/video.mp4"' in html
    assert '<p>World</p>' in html and 'Notes.pdf (not downloaded)' in html
    assert (lib / 'site' / 'assets' / 'style.css').exists()
