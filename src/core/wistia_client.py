from __future__ import annotations

import json
import re

import requests


class WistiaClient:
    @staticmethod
    def get_assets(vid_id: str):
        """Fetch the Wistia embed iframe, extract the 'assets' list, add a
        downloadable_url to each, and return it. Returns None on any error."""
        try:
            url = f'https://fast.wistia.net/embed/iframe/{vid_id}?videoFoam=true'
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()

            match = re.search(r'iframeInit\(\s*(\{.*?\})\s*,\s*\{\s*\}\s*\)', resp.text, re.S)
            if not match:
                return None
            assets = json.loads(match.group(1)).get('assets')
            if not isinstance(assets, list):
                return None

            for asset in assets:
                url = asset.get('url')
                if not isinstance(url, str):
                    continue
                ext = asset.get('ext') or ('mp4' if asset.get('type') == 'original' else '')
                if ext:
                    if not ext.startswith('.'):
                        ext = f'.{ext}'
                    asset['downloadable_url'] = url.replace('.bin', ext)
            return assets
        except (requests.RequestException, ValueError, TypeError, KeyError):
            return None
