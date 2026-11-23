# SceneSorter

A desktop download manager and library sorter for scene releases. It
pulls post listings from configured sources and merges them into one entry per
scene. It can start downloads, then rename and move finished files into
your library.

## Features

- **Merged listings.** Each site posts every resolution of a scene as a
  separate entry. SceneSorter parses the release titles and shows one card per
  scene, with chips for 2160p / 1080p / 720p and a dot for each source.
- **Thumbnails from any source.** If one site's image fails to load, the app
  falls back to another site's. Images are cached on disk.
- **Download queue.** Direct links download inside the app with progress and
  speed. Magnet and .torrent links are passed to your torrent client.
- **Daily quota tracking.** Sources can declare a daily action limit. Each
  started download counts as one action in a rolling 24-hour window. The UI
  shows how many are left and blocks downloads once the limit is reached.
- **Auto-sort.** Finished downloads are renamed from a template and moved into
  your library. The default template is
  `{studio}/{studio} - {date} - {performers} - {title} [{resolution}]`.
- **Library awareness.** The app scans your library and marks scenes you
  already have, even when the file you have is a different resolution or came
  from another site.
- **Self-updating exe.** GitHub Actions builds `SceneSorter.exe`. The app checks
  GitHub Releases on start and can update itself in place.

## Project layout

```
scenesorter/
  models.py        Release (one post) / Scene (merged) data model
  parsing.py       Release-title parser + match key
  merge.py         Exact + fuzzy merging across sites/resolutions
  sources/         Source plugin interface + adapters (wordpress.py = generic WP REST)
  downloader.py    Download queue (HTTP with progress; torrent hand-off)
  library.py       Rename templates, sort into library, scan owned files
  quota.py         Rolling 24h per-source action counter
  updater.py       GitHub Releases self-update
  gui/             PySide6 UI
```

## Adding a source

Subclass `scenesorter.sources.base.Source`, implement `list_page()` (and
`load_details()` if links are only on the post page), then register the
class in `scenesorter/sources/__init__.py`. Sources that need a login set
`requires_login = True` and implement `is_logged_in()`. Sources with a daily
cap set `daily_quota`. Sources are configured in **Settings ▸ Sources**. A
source that appears higher in that list is preferred when a scene is
available from more than one source.

## Development

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
python -m scenesorter
```

## Releasing

To publish a release, do either of the following:

- Push a tag: `git tag v0.2.0 && git push origin v0.2.0`
- Go to **Actions ▸ Release ▸ Run workflow** and enter `0.2.0`

The workflow runs the tests, builds `SceneSorter.exe` with PyInstaller and
publishes a GitHub Release. Installed copies find the new release on their next
start. Every push to `main` also builds a test exe, which you can download from
that workflow run's artifacts.
