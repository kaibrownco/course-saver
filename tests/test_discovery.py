import os

from core.models import Category, Post, Product
from core.parser import Parser
from core.service import Library, add_course, discover_courses, pretty_host

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')
BASE = 'https://example.mykajabi.com/library'


def load(name):
    return open(os.path.join(FIXTURES, name)).read()


def test_get_library_products_titles_dedupe_and_urls():
    found = Parser.get_library_products(load('library.html'), BASE)
    assert [(p['slug'], p['title']) for p in found] == [
        ('course-one', 'Course One'), ('course-two', 'Course Two - Advanced'), ('no-title-course', 'No Title Course')]
    assert found[1]['url'] == 'https://example.mykajabi.com/products/course-two'
    assert Parser.get_site_title(load('library.html')) == 'Example Academy'


def test_pretty_host():
    assert pretty_host('some-academy.mykajabi.com') == 'Some Academy'
    assert pretty_host('courses.example-school.com') == 'Example School'


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages

    def get_page(self, url):
        from types import SimpleNamespace
        return SimpleNamespace(text=self.pages[url])


def test_discover_courses_reads_library_page():
    f = FakeFetcher({'https://example.mykajabi.com/library': load('library.html')})
    title, courses = discover_courses(f, 'https://example.mykajabi.com/products/course-one')
    assert title == 'Example Academy' and len(courses) == 3


def test_sites_group_discovered_and_added_courses_by_host(tmp_path):
    lib = Library(str(tmp_path))
    lib.save_catalog('example.mykajabi.com', 'Example Academy', 'me@x.com',
                     [{'slug': 'course-one', 'url': 'https://example.mykajabi.com/products/course-one', 'title': 'Course One'}])
    prod = Product('course-one', 'https://example.mykajabi.com/products/course-one')
    prod.email = 'me@x.com'
    lib.save(prod)
    lib.save(Product('other', 'https://other.example.org/products/other'))
    sites = {s.host: s for s in lib.sites()}
    assert set(sites) == {'example.mykajabi.com', 'other.example.org'}
    ex = sites['example.mykajabi.com']
    assert ex.title == 'Example Academy' and ex.email == 'me@x.com'
    assert ex.product_for('course-one').name == 'course-one' and ex.product_for('nope') is None
    assert sites['other.example.org'].title == 'Example'  # no catalog: falls back to the host


def test_add_course_reads_outline_and_lessons(tmp_path):
    base = 'https://example.mykajabi.com/products/c'
    pages = {
        base: '<a href="/products/c/categories/1"><h3 class="title">Sec</h3></a>',
        base + '/categories/1': '<a href="/products/c/categories/1/posts/9"><h3 class="title">Lesson</h3></a>',
        base + '/categories/1/posts/9': '<div class="post-body"><p>Hello</p></div>',
    }
    # the parser resolves relative links against the fetched url, so serve with the same host
    lib = Library(str(tmp_path))
    statuses = []
    prod = add_course(lib, FakeFetcher(pages), base, title='C', email='me@x.com', on_status=statuses.append)
    assert prod.title == 'C' and prod.email == 'me@x.com'
    post = prod.categories[0].posts[0]
    assert post.content == 'Hello' and post.scraped
    assert statuses[0].startswith('Reading the course') and 'Reading lesson 1 of 1…' in statuses
    assert lib.load('c').categories[0].posts[0].content == 'Hello'
