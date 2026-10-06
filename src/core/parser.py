from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import Category, Post, Attachment

TITLE_SELECTORS = (
    lambda link: link.find(class_="syllabus__title"),
    lambda link: link.find("h3", class_="title"),
    lambda link: link.find(class_="title"),
)


def _link_title(link, extra=None):
    elem = next((e for e in (sel(link) for sel in TITLE_SELECTORS) if e), None)
    if elem is None and extra:
        elem = extra(link)
    return (elem or link).get_text(strip=True)


def _pretty_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


class Parser:

    @staticmethod
    def get_site_title(html: str) -> str | None:
        t = BeautifulSoup(html, "html.parser").find("title")
        return t.get_text(strip=True) if t and t.get_text(strip=True) else None

    @staticmethod
    def get_library_products(html: str, base_url: str) -> list[dict]:
        """Courses listed on a Kajabi member 'library' page: [{slug, url, title}] in page order.

        Finds each link to /products/<slug>, climbs to the card that contains only that course's link,
        and takes the card's title (class 'title', else a heading, else the image alt, else the slug)."""
        soup = BeautifulSoup(html, "html.parser")
        root = re.match(r"https?://[^/]+", base_url).group(0)
        out, seen = [], set()
        for link in soup.find_all("a", href=True):
            m = re.match(r"^/products/([^/?#]+)/?$", urljoin(base_url, link["href"]).replace(root, "", 1))
            if not m or m.group(1) in seen:
                continue
            slug = m.group(1)
            seen.add(slug)
            card = link
            for _ in range(6):
                parent = card.parent
                others = {x["href"] for x in parent.find_all("a", href=re.compile(r"/products/[^/?#]+/?$"))} if parent else set()
                if parent is None or len(others) > 1:
                    break
                card = parent
            title_el = card.find(class_=re.compile(r"(^|[-_ ])title($|[-_ ])")) or card.find(re.compile(r"^h[1-6]$"))
            img = card.find("img", alt=True)
            title = (title_el.get_text(" ", strip=True) if title_el else "") or (img["alt"].strip() if img else "")
            if not title or title.lower() in ("view product", "your library"):
                title = _pretty_slug(slug)
            out.append({"slug": slug, "url": f"{root}/products/{slug}", "title": title})
        return out

    @staticmethod
    def get_categories(html: str, base_url: str) -> list[Category]:
        """Extract all categories (links to /categories/<pk>) from a Kajabi product page."""
        soup = BeautifulSoup(html, "html.parser")
        by_pk, is_canonical = {}, {}  # pk -> Category (first-seen order), pk -> came from a plain category link
        for link in soup.find_all("a", href=True):
            full_url = urljoin(base_url, link["href"])
            m = re.search(r"/categories/(\d+)", full_url)
            if not m:
                continue
            pk = m.group(1)
            canonical = full_url[:m.end()] == full_url.rstrip("/")
            # Kajabi also links "resume" buttons to /categories/<pk>/posts/<pk>; keep the real category link
            if pk not in by_pk or (canonical and not is_canonical[pk]):
                by_pk[pk] = Category(pk, _link_title(link), full_url[:m.end()])
                is_canonical[pk] = canonical
        categories = list(by_pk.values())
        return categories

    @staticmethod
    def get_posts(html: str, base_url: str) -> list[Post]:
        """Extract all posts (links to /posts/<pk>) from a Kajabi category page."""
        soup = BeautifulSoup(html, "html.parser")
        posts, seen = [], set()
        for link in soup.find_all("a", href=True):
            full_url = urljoin(base_url, link["href"])
            m = re.search(r"/posts/(\d+)", full_url)
            if not m or full_url in seen:
                continue
            seen.add(full_url)
            name = _link_title(link, lambda l: l.find("p", class_="syllabus__text"))
            posts.append(Post(m.group(1), name, full_url))
        return posts

    @staticmethod
    def parse_post(html: str, base_url: str = "") -> dict:
        """Extract wistia id, text content and attachments from a post page."""
        soup = BeautifulSoup(html, "html.parser")
        data = {}

        wistia_id = None
        div = soup.find("div", id=re.compile(r"^wistia_"))
        if div:
            wistia_id = div["id"].split("_", 1)[1]
        else:
            m = re.search(r"wistia(?:_async_|\.(?:net|com)/embed/(?:iframe|medias)/)([a-z0-9]{10})", html)
            wistia_id = m.group(1) if m else None
        data["wistia_id"] = wistia_id

        block = soup.find("div", class_="post-body") or soup.find(
            "div", attrs={"kjb-settings-id": re.compile(r"show_desc")})
        if block:
            parts = [e.get_text(" ", strip=True) for e in block.find_all(["p", "li", "h1", "h2", "h3", "h4"])]
            data["content"] = "\n\n".join(p for p in parts if p)
        else:
            data["content"] = ""

        attachments, seen = [], set()
        for link in soup.find_all("a", href=True):
            if "/courses/downloads/" not in link["href"]:
                continue
            url = urljoin(base_url, link["href"])
            if url in seen:
                continue
            seen.add(url)
            attachments.append(Attachment(url, link.get_text(strip=True)))
        data["attachments"] = attachments
        return data
