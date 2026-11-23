import time

from scenesorter.downloader import DownloadManager, State
from scenesorter.models import DownloadLink, LinkKind, Release


class FakeResp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, content):
        self.content = content

    def get(self, url, timeout=None):
        return FakeResp(self.content)


def wait(job):
    for _ in range(100):
        if job.state not in (State.QUEUED, State.RUNNING):
            return
        time.sleep(0.02)


def rel():
    return Release("blog", "1", "", "Studio.24.01.15.Jane.XXX.1080p.MP4-PRT", filename="Studio.Jane.PRT.mkv")


def test_torrent_to_blackhole(tmp_path):
    dm = DownloadManager(session=FakeSession(b"d8:announce...e"))
    dm.blackhole_dir = tmp_path
    job = dm.enqueue(rel(), DownloadLink("http://x/a.torrent", LinkKind.TORRENT), tmp_path / "dl")
    wait(job)
    assert job.state is State.BLACKHOLE
    assert (tmp_path / "Studio.Jane.PRT.torrent").read_bytes().startswith(b"d8:")


def test_magnet_to_blackhole_and_bad_torrent(tmp_path):
    dm = DownloadManager(session=FakeSession(b"<html>nope</html>"))
    dm.blackhole_dir = tmp_path
    job = dm.enqueue(rel(), DownloadLink("magnet:?xt=urn:btih:abc", LinkKind.MAGNET), tmp_path)
    wait(job)
    assert job.state is State.BLACKHOLE and (tmp_path / "Studio.Jane.PRT.magnet").read_text().startswith("magnet:")
    bad = dm.enqueue(rel(), DownloadLink("http://x/b.torrent", LinkKind.TORRENT), tmp_path)
    wait(bad)
    assert bad.state is State.FAILED
