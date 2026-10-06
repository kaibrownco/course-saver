from __future__ import annotations

import os
import re
import mimetypes
from urllib.parse import urlparse, unquote

import requests

VIDEO_TYPES = {'original', 'hd_mp4_video', 'md_mp4_video', 'mp4_video', 'iphone_video'}
UA = {'User-Agent': 'Mozilla/5.0'}
QUALITIES = ('phone', 'hd', 'original')
CHUNK = 256 * 1024


class Cancelled(Exception):
    """Raised inside a download when the user pauses/cancels it."""


def safe_name(name: str, default: str = 'file') -> str:
    """Make a string safe to use as a single path component."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name).strip(' .')
    return name[:150] or default


def _filename_from_response(response, suggested=None):
    cd = response.headers.get('content-disposition', '')
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
    name = unquote(m.group(1).strip('"')) if m else None
    name = name or suggested or os.path.basename(unquote(urlparse(response.url).path)) or 'file'
    name = safe_name(name)
    if not os.path.splitext(name)[1]:
        ctype = response.headers.get('content-type', '').split(';')[0].strip()
        name += mimetypes.guess_extension(ctype, False) or ''
    return name


def _pixels(a):
    return (a.get('width') or 0) * (a.get('height') or 0)


def video_options(assets):
    """Downloadable mp4 assets, excluding images/storyboards."""
    return [a for a in assets or []
            if a.get('downloadable_url') and (a.get('ext') == '.mp4' or a.get('type') in VIDEO_TYPES)]


def pick_video(assets, quality='original'):
    """Choose the asset for a quality preset.
    phone: smallest re-encoded mp4 (usually 360p); hd: largest re-encoded mp4 (usually 720p);
    original: the original upload (often huge), else the largest mp4. Legacy names: best, compact."""
    quality = {'best': 'original', 'compact': 'hd'}.get(quality, quality)
    vids = video_options(assets)
    if not vids:
        raise ValueError('No downloadable video found')
    encoded = [a for a in vids if a.get('type') != 'original']
    if quality == 'phone' and encoded:
        return min(encoded, key=lambda a: (_pixels(a) or 10**9, a.get('size') or 10**12))
    if quality == 'hd' and encoded:
        return max(encoded, key=lambda a: (_pixels(a), a.get('bitrate') or 0))
    original = next((a for a in vids if a.get('type') == 'original'), None)
    return original or max(vids, key=lambda a: (_pixels(a), a.get('bitrate') or 0))


def video_size(assets, quality='original'):
    """Expected download size in bytes for a quality preset (None if unknown)."""
    try:
        return pick_video(assets, quality).get('size')
    except ValueError:
        return None


class Downloader:
    @staticmethod
    def download_file(response, dest, filename=None, progress=None, cancel=None):
        """Stream a response into dest/<filename>, via a .part file. Returns the path."""
        os.makedirs(dest, exist_ok=True)
        path = os.path.join(dest, _filename_from_response(response, filename))
        tmp = path + '.part'
        with open(tmp, 'wb') as f:
            for chunk in response.iter_content(CHUNK):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                f.write(chunk)
                if progress:
                    progress(len(chunk))
        os.replace(tmp, path)
        return path

    @staticmethod
    def download_wistia(assets, folder, session=None, retries=3, progress=None, quality='original', cancel=None):
        """Download a video into folder/video.mp4 and return the path.

        Resumable: a partial video.mp4.part is continued with an HTTP Range request.
        progress(done_bytes, total_bytes) is called as data arrives. Setting the `cancel`
        event raises Cancelled and keeps the partial file for later. Skips finished files."""
        selected = pick_video(assets, quality)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, 'video.mp4')
        tmp = path + '.part'
        expected = selected.get('size')
        if os.path.exists(path) and (not expected or os.path.getsize(path) == expected):
            if progress:
                progress(os.path.getsize(path), os.path.getsize(path))
            return path

        getter = (session or requests).get
        last = None
        for attempt in range(retries):
            try:
                have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                headers = dict(UA)
                if have:
                    headers['Range'] = f'bytes={have}-'
                resp = getter(selected['downloadable_url'], stream=True, headers=headers, timeout=30)
                if resp.status_code == 416:  # nothing left to fetch: .part is already complete
                    os.replace(tmp, path)
                    return path
                resp.raise_for_status()
                if resp.status_code == 206:
                    mode, done = 'ab', have
                    total = have + int(resp.headers.get('content-length') or 0)
                else:  # server ignored Range (or fresh start)
                    mode, done = 'wb', 0
                    total = int(resp.headers.get('content-length') or expected or 0)
                with open(tmp, mode) as f:
                    for chunk in resp.iter_content(CHUNK):
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                os.replace(tmp, path)
                return path
            except requests.RequestException as e:
                last = e
        raise last
