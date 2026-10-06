"""Data model for a scraped Kajabi product.

Objects are persisted with jsonpickle, so class names/module paths and
attribute names are part of the on-disk format. New fields get class-level
defaults so JSON written by older versions (which jsonpickle restores without
calling __init__) still loads.
"""
from __future__ import annotations

import os
import jsonpickle


class Page:
    def __init__(self, name, url, prefetched_html=None, path=None):
        self.name = name
        self.url = url
        self.prefetched_html = prefetched_html
        self.path = path

    def __str__(self):
        return f'{self.name}'

    def save_json(self, path=None):
        path = path or self.path or f'{self.name}.json'
        self.path = path
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, 'w') as f:
            f.write(jsonpickle.encode(self, make_refs=False))


class Product(Page):
    title = None  # human-readable course title; falls back to name
    email = None  # account used to sign in (never the password)

    def __init__(self, name, url, prefetched_html=None, path=None, categories=None, title=None):
        super().__init__(name, url, prefetched_html, path)
        self.categories = categories if categories is not None else []
        self.title = title

    @property
    def display_title(self):
        return self.title or self.name


class Category(Page):
    def __init__(self, pk, name, url, prefetched_html=None, path=None, posts=None):
        super().__init__(name, url, prefetched_html, path)
        self.pk = pk
        self.posts = posts if posts is not None else []


class Post(Page):
    # paths below are relative to the library root, e.g. media/<product>/<cat>/<post>/video.mp4
    video_path = None
    scraped = False

    def __init__(self, pk, name, url, prefetched_html=None, path=None, wistia_assets=None, content=None, attachments=None):
        super().__init__(name, url, prefetched_html, path)
        self.pk = pk
        self.wistia_assets = wistia_assets
        self.content = content
        self.attachments = attachments if attachments is not None else []

    @property
    def has_video(self):
        return bool(self.wistia_assets)


class Attachment:
    def __init__(self, url, name, local_file_path=None):
        self.url = url
        self.name = name
        self.local_file_path = local_file_path
