import os
import pytest
from core.parser import Parser
from core.models import Attachment

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')

def load_fixture(name):
    path = os.path.join(FIXTURES, name)
    with open(path, 'r') as f:
        return f.read()

def test_get_categories():
    html = load_fixture('category_list.html')
    cats = Parser.get_categories(html, 'https://courses.example.com/products/demo/categories')
    assert len(cats) == 2
    assert cats[0].pk == '123'
    assert cats[0].name == 'Cat 1'
    assert cats[1].pk == '456'
    assert cats[1].name == 'Cat 2'

def test_get_posts():
    html = load_fixture('post_list.html')
    posts = Parser.get_posts(html, 'https://courses.example.com/products/demo')
    assert len(posts) == 2
    assert posts[0].pk == '123'
    assert posts[0].name == 'Post 1'
    assert posts[1].pk == '456'
    assert posts[1].name == 'Post 2'

def test_parse_post():
    html = load_fixture('post_theme_a.html')
    data = Parser.parse_post(html)
    assert data['wistia_id'] == 'abcde12345'
    assert 'This is the post content.' in data['content']
    attachments = data['attachments']
    assert len(attachments) == 1
    assert isinstance(attachments[0], Attachment)
    assert attachments[0].url == 'https://courses.example.com/courses/downloads/111111/primer-pdf'
    assert attachments[0].name == 'GEI Primer Slides'

    html = load_fixture('post_theme_b.html')
    data = Parser.parse_post(html)
    assert data['wistia_id'] == 'fghij67890'
    assert 'This is the post content.' in data['content']
    attachments = data['attachments']
    assert len(attachments) == 1
    assert isinstance(attachments[0], Attachment)
    assert attachments[0].url == 'https://courses.example.com/courses/downloads/222222/daily_ritual-pdf'
    assert attachments[0].name == 'Daily_Ritual_-_The_Hour_of_Power.pdf'