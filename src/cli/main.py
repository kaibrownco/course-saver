from __future__ import annotations

import os
import random
import time

import click

from core.auth import LoginError
from core.downloader import QUALITIES
from core.fetcher import Fetcher
from core.models import Product
from core.service import (Library, DownloadManager, all_posts, scrape_posts as svc_scrape_posts,
                          scrape_tree as svc_scrape_tree, slug_from_url, lesson_bytes)

DEFAULT_LIBRARY = 'library'


def nap(enabled: bool):
    if enabled:
        time.sleep(random.uniform(2, 6))


def print_tree(product: Product):
    click.echo(product.display_title)
    cats = product.categories
    for i, cat in enumerate(cats):
        last = i == len(cats) - 1
        click.echo(f"{'└─' if last else '├─'} {cat.name}")
        prefix = '   ' if last else '│  '
        for j, post in enumerate(cat.posts):
            click.echo(f"{prefix}{'└─' if j == len(cat.posts) - 1 else '├─'} {post.name}")


def open_product(product_json: str):
    """-> (Library, Product) for a <library>/<name>.json path."""
    lib = Library(os.path.dirname(os.path.abspath(product_json)))
    return lib, lib.load(os.path.splitext(os.path.basename(product_json))[0])


def fetcher_from(cookies, lib, url):
    """A --cookies file wins; otherwise use the session saved by `login`."""
    if cookies:
        return Fetcher(cookies)
    try:
        return lib.fetcher_for(url)
    except Exception:
        raise click.ClickException('No saved login for this site. Run `kajabi-scrape login URL` first, or pass --cookies.')


cookies_option = click.option('--cookies', type=click.Path(exists=True, dir_okay=False),
                              help='Cookies JSON (browser export or {name: value}). Optional if you ran `login`.')
library_option = click.option('--library', default=DEFAULT_LIBRARY, show_default=True, type=click.Path(file_okay=False))
quality_option = click.option('--quality', type=click.Choice(list(QUALITIES) + ['best', 'compact']), default='original',
                              show_default=True, help='phone = small (~360p), hd = ~720p, original = full upload (large).')


@click.group()
def cli():
    """kajabi-scraper: save Kajabi courses you own for offline viewing."""


@cli.command('login')
@click.argument('site_url')
@library_option
@click.option('--email', prompt=True)
@click.option('--password', prompt=True, hide_input=True)
def login_cmd(site_url, library, email, password):
    """Sign in with email + password and save the session for later commands."""
    try:
        Library(library).sign_in(site_url, email, password)
    except LoginError as e:
        raise click.ClickException(str(e))
    click.echo('Signed in. Session saved.')


@cli.command('discover')
@click.argument('site_url')
@cookies_option
@library_option
@click.option('--email', help='Account email, remembered with the site.')
def discover(site_url, cookies, library, email):
    """List every course your account has on a site (its member library) and remember the list."""
    from core.service import discover_courses, host_of
    lib = Library(library)
    fetcher = fetcher_from(cookies, lib, site_url)
    title, courses = discover_courses(fetcher, site_url)
    lib.save_catalog(host_of(site_url), title, email, courses)
    click.echo(f'{title or host_of(site_url)}: {len(courses)} course(s)')
    for c in courses:
        click.echo(f"  {c['title']}  ({c['url']})")


@cli.command('show-tree')
@click.argument('product_json', type=click.Path(exists=True))
def show_tree(product_json):
    """Print a product's category/post tree."""
    print_tree(open_product(product_json)[1])


@cli.command('scrape-tree')
@click.argument('product_url')
@cookies_option
@library_option
@click.option('--name', help='Short name used for filenames (default: slug from the URL).')
@click.option('--title', help='Human-readable course title (default: the name).')
@click.option('--pause', is_flag=True, help='Sleep 2-6s between requests.')
def scrape_tree(product_url, cookies, library, name, title, pause):
    """Scrape a product's categories and post list into <library>/<name>.json."""
    lib = Library(library)
    fetcher = fetcher_from(cookies, lib, product_url)
    name = name or slug_from_url(product_url)
    existing = lib.load(name) if os.path.exists(lib.product_path(name)) else None
    try:
        product = svc_scrape_tree(fetcher, product_url, name, title, existing, pause=lambda: nap(pause))
    except ValueError as e:
        raise click.ClickException(str(e))
    lib.save(product)
    print_tree(product)
    click.echo(f'Saved {lib.product_path(name)}')


@cli.command('scrape-posts')
@click.argument('product_json', type=click.Path(exists=True))
@cookies_option
@click.option('--force', is_flag=True, help='Re-scrape posts that were already scraped.')
@click.option('--pause', is_flag=True, help='Sleep 2-6s between requests.')
def scrape_posts(product_json, cookies, force, pause):
    """Scrape every post for its text, attachments and Wistia video info. Resumable."""
    lib, product = open_product(product_json)
    fetcher = fetcher_from(cookies, lib, product.url)
    svc_scrape_posts(fetcher, product, lib.save, force=force, pause=lambda: nap(pause),
                     on_progress=lambda n, total, name: click.echo(f'[{n}/{total}] {name}'),
                     on_warning=lambda m: click.secho(f'  warning: {m}', fg='yellow', err=True))


@cli.command('download')
@click.argument('product_json', type=click.Path(exists=True))
@cookies_option
@quality_option
@click.option('--limit', type=int, help='Only download the first N lessons (for testing).')
def download(product_json, cookies, quality, limit):
    """Download lesson videos and attachments into <library>/media/. Skips finished files."""
    lib, product = open_product(product_json)
    fetcher = fetcher_from(cookies, lib, product.url)
    items = [(c, p) for c, p in all_posts(product) if p.wistia_assets or p.attachments][:limit]
    mgr = DownloadManager(lib, product, fetcher, quality)
    mgr.start(items)
    last = None
    while mgr.running:
        label = f'[{mgr.index + 1}/{len(items)}] {mgr.current_name[:44]}'
        pct = f' {mgr.done_bytes * 100 // mgr.total_bytes}%' if mgr.total_bytes else ''
        line = f'{label}{pct}'
        if line != last:
            click.echo(line)
            last = line
        time.sleep(0.5)
    for e in mgr.errors:
        click.secho(f'FAILED {e}', fg='red', err=True)
    if mgr.session_expired:
        raise click.ClickException('Session expired. Run `kajabi-scrape login URL` again.')
    click.echo(f'Done: {len(items) - len(mgr.errors)}/{len(items)} lessons')
    if mgr.errors:
        raise SystemExit(1)


@cli.command('generate-site')
@library_option
@click.option('--output', '-o', 'output_path', type=click.Path(file_okay=False),
              help='Where to write the site (default: <library>/site).')
@click.option('--verbose', '-v', is_flag=True)
def generate_site_cli(library, output_path, verbose):
    """Build a static offline website from the library (optional; the app has its own viewer)."""
    from sitegen.sitegen import generate_site
    output_path = output_path or os.path.join(library, 'site')
    n = generate_site(library_path=library, output_path=output_path, verbose=verbose)
    click.echo(f'Generated {n} product(s) in {output_path}\nOpen {os.path.join(output_path, "index.html")}')


@cli.command('sync')
@click.argument('product_url')
@cookies_option
@library_option
@click.option('--name')
@click.option('--title')
@quality_option
@click.option('--pause', is_flag=True)
@click.pass_context
def sync(ctx, product_url, cookies, library, name, title, quality, pause):
    """Run the whole pipeline: scrape, download videos + attachments, build the site."""
    name = name or slug_from_url(product_url)
    pj = os.path.join(library, f'{name}.json')
    ctx.invoke(scrape_tree, product_url=product_url, cookies=cookies, library=library, name=name, title=title, pause=pause)
    ctx.invoke(scrape_posts, product_json=pj, cookies=cookies, force=False, pause=pause)
    ctx.invoke(download, product_json=pj, cookies=cookies, quality=quality, limit=None)
    ctx.invoke(generate_site_cli, library=library, output_path=None, verbose=False)


if __name__ == '__main__':
    cli()
