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
- **Local database and cache.** Every scraped post is stored in a local SQLite
  file (`scenesorter.db`). The stored fields are:
  - title fields: studio, performers, title, scene date and resolution
  - release info: release group, file name, duration and size
  - post info: tags, the date it was posted, thumbnail URL and links
  - download history

  On start the app shows everything from the cache. A refresh fetches pages
  only until it reaches one with no new posts. Thumbnails are cached on disk,
  so each one is downloaded only once.
- **Release groups.** The group is detected from the release name, in forms
  like `…XXX.1080p.HEVC.x265.PRT`, `…MP4-GRP` and `[GRP]`, or from a
  `Group:` / `Filename:` line in the post. You can filter by group. Preferred
  groups (PRT by default) are highlighted and offered first.
- **Self-updating exe.** GitHub Actions builds `SceneSorter.exe`. The app checks
  GitHub Releases on start and can update itself in place.

## Portable install

SceneSorter doesn't install anything and never writes to AppData. Everything
is kept beside the exe:

```
SceneSorter.exe
data/
  settings.json      your settings
  scenesorter.db     cached posts, metadata, download history
  quota.json         daily action counters
  cache/thumbs/      cached thumbnails
downloads/           default download folder   (configurable)
library/             default sorted library    (configurable)
```

Relative paths in Settings are resolved against the exe's folder, so you can
move the whole folder or run it from a USB drive. Self-updates replace only the
exe and leave `data/` alone. Put it somewhere you can write to, not
`Program Files`.

## Project layout

```
scenesorter/
  models.py        Release (one post) / Scene (merged) data model
  parsing.py       Release-title parser + match key
  merge.py         Exact + fuzzy merging across sites/resolutions
  sources/         Source plugin interface + adapters (wordpress.py = generic WP REST)
  downloader.py    Download queue (HTTP with progress; torrent hand-off)
  library.py       Rename templates, sort into library, scan owned files
  db.py            SQLite cache: posts, per-resolution files, download history
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

Versions are flat build numbers: `v1`, `v2`, `v3`, and so on.

- To release, go to **Actions ▸ Release ▸ Run workflow**. Leave the version
  field empty and the workflow uses the next number. You can also push a tag
  like `v7`.
- The workflow runs the tests, builds `SceneSorter.exe`, and publishes a GitHub
  Release. Copies already installed find the new build on their next start.
- Every push to `main` also builds a test exe. Download it from that workflow
  run's artifacts.

## Planned integrations

- **ThePornDB / StashDB (stash-box GraphQL).** Match scenes to stash-box IDs
  for clean metadata, performers, tags and covers. Use those IDs to name
  files.
- **Stash.** Check which scenes are already in Stash, trigger a scan after
  sorting a file, and use the stash-box ID to link the two.
- **Whisparr.** Sync what Whisparr wants and what it already has, and import
  finished downloads.
