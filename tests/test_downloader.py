import os
import pytest
from core.downloader import Downloader

class DummyResponse:
    def __init__(self, url, content, headers):
        self.url = url
        self._content = content
        self.headers = headers
    def iter_content(self, chunk_size):
        yield self._content
    def raise_for_status(self):
        pass

def test_download_file(tmp_path):
    content = b'hello world'
    headers = {'content-type': 'application/octet-stream'}
    resp = DummyResponse('http://example.com/file.bin', content, headers)
    dest = tmp_path / 'out'
    path = Downloader.download_file(resp, str(dest))
    assert os.path.exists(path)
    with open(path, 'rb') as f:
        assert f.read() == content

def test_download_file_uses_content_disposition_and_no_double_extension(tmp_path):
    headers = {'content-type': 'application/pdf', 'content-disposition': 'attachment; filename="Workbook.pdf"'}
    resp = DummyResponse('http://example.com/courses/downloads/1/workbook-pdf', b'%PDF', headers)
    path = Downloader.download_file(resp, str(tmp_path))
    assert os.path.basename(path) == 'Workbook.pdf'
    assert not os.path.exists(path + '.part')


def test_download_file_falls_back_to_suggested_name(tmp_path):
    resp = DummyResponse('http://example.com/dl/abc-pdf', b'x', {'content-type': 'application/pdf'})
    path = Downloader.download_file(resp, str(tmp_path), filename='My Notes')
    assert os.path.basename(path) == 'My Notes.pdf'


def test_pick_video_prefers_original_then_largest():
    from core.downloader import pick_video
    assets = [
        {'type': 'original', 'ext': '', 'downloadable_url': 'o.mp4', 'width': 1920, 'height': 1080},
        {'type': 'md_mp4_video', 'ext': '.mp4', 'downloadable_url': 'md.mp4', 'width': 1280, 'height': 720},
        {'type': 'iphone_video', 'ext': '.mp4', 'downloadable_url': 'ip.mp4', 'width': 640, 'height': 360},
        {'type': 'still_image', 'ext': '.jpg', 'downloadable_url': 's.jpg'},
    ]
    assert pick_video(assets)['downloadable_url'] == 'o.mp4'
    assert pick_video(assets, 'hd')['downloadable_url'] == 'md.mp4'
    assert pick_video(assets, 'phone')['downloadable_url'] == 'ip.mp4'
    with pytest.raises(ValueError):
        pick_video([assets[3]])
