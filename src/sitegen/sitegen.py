from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from urllib.parse import quote

import jsonpickle
from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.models import Product

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = HERE / 'templates'
STATIC = HERE / 'static'


def generate_site(library_path: str = 'library', output_path: str | None = None, verbose: bool = False) -> int:
    """Render an offline static site from the product JSONs in library_path.

    Layout:  <out>/index.html                      all courses
             <out>/<product>/index.html            a course's categories
             <out>/<product>/<cat>/index.html      a category's posts
             <out>/<product>/<cat>/<post>/index.html
    Videos/attachments are referenced by relative path into <library>/media,
    so the library folder can be moved or opened from disk as a whole.
    Returns the number of products rendered.
    """
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format='%(levelname)s: %(message)s')
    lib = Path(library_path).resolve()
    out = Path(output_path).resolve() if output_path else lib / 'site'
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(STATIC, out / 'assets', dirs_exist_ok=True)

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(['html', 'jinja']))
    products = load_products(lib)

    def render(template, target: Path, **ctx):
        target.parent.mkdir(parents=True, exist_ok=True)
        depth = len(target.parent.relative_to(out).parts)

        def media(path):
            """URL (relative to this page) of a file stored under the library root."""
            return quote(os.path.relpath(lib / path, target.parent).replace(os.sep, '/'))

        html = env.get_template(template).render(root='../' * depth, media=media, **ctx)
        target.write_text(html, encoding='utf-8')
        logger.info('wrote %s', target)

    render('products.jinja', out / 'index.html', products=products)
    for prod in products:
        render('product.jinja', out / prod.name / 'index.html', product=prod)
        flat = [(c, p) for c in prod.categories for p in c.posts]
        for cat in prod.categories:
            render('category.jinja', out / prod.name / str(cat.pk) / 'index.html', product=prod, category=cat)
        for i, (cat, post) in enumerate(flat):
            render('post.jinja', out / prod.name / str(cat.pk) / str(post.pk) / 'index.html',
                   product=prod, category=cat, post=post,
                   prev=flat[i - 1] if i > 0 else None,
                   next=flat[i + 1] if i + 1 < len(flat) else None)
    return len(products)


def load_products(lib: Path) -> list[Product]:
    """Decode every top-level *.json in the library that is a Product (sorted by name)."""
    products = []
    for f in sorted(lib.glob('*.json')):
        try:
            obj = jsonpickle.decode(f.read_text(encoding='utf-8'))
        except Exception as e:
            logger.warning("Skipping '%s': failed to decode (%s)", f, e)
            continue
        if isinstance(obj, Product):
            products.append(obj)
        else:
            logger.warning("Skipping '%s': not a product file", f)
    return products
