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
- **Auto-sort.** Finished downloads are moved into your library. Renaming is a
  separate toggle: turn it off to keep the original file name. The default
  template is
  `{studio}/{studio} - {date} - {performers} - {title} [{resolution}] [{group}]`.
  Available fields are `{studio} {date} {year} {performers} {title}
  {resolution} {group} {source}`. Empty fields and their brackets are dropped.
- **Download preferred.** One button in the scene panel and the right-click
  menu, which also works on several selected scenes. It picks the release in
  this order:
  1. your wanted resolution
  2. your preferred release group
  3. your preferred source, then the rest in Sources order

  With several scenes selected, ones already downloaded are skipped.
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
- **Background caching.** A slow crawler walks every listing page of each
  source until the whole site is in the local database. By default it sends
  one request every 30 seconds, and you can change that in Settings. It
  resumes where it stopped after a restart. On HTTP 429/503 it waits as long
  as `Retry-After` says, and on other errors it backs off exponentially. Once a
  source is fully cached, the app only checks for new posts, every 30 minutes by
  default. Search and filters run on the local database, so they work offline.
- **Fill gaps (rescan all pages).** In **Settings ▸ Sources**, each source has
  a **Rescan all pages** button. It walks every listing page from newest to
  oldest, one request every 5 s by default, and adds any post missing from the
  cache. Nothing is removed. It resumes after a restart, waits when a site
  says to slow down, and shows progress in the status bar. The same button
  stops it.
- **Release groups.** The group is detected from the release name, in forms
  like `…XXX.1080p.HEVC.x265.PRT`, `…MP4-GRP` and `[GRP]`, or from a
  `Group:` / `Filename:` line in the post. You can filter by group. Preferred
  groups (PRT by default) are highlighted and offered first.
- **Download tracking, including partial downloads.** Each release is
  recorded as *grabbed*, *done* or *failed*:
  - **grabbed:** an HTTP download started, or the release was sent to the
    torrent client or black hole
  - **done:** the HTTP download finished, or you marked it by hand
  - **failed:** the download failed or you cancelled it

  A scene counts as **Downloaded** only once a release at your *wanted
  resolution* (Settings), or higher, is done. If that resolution isn't posted,
  the best posted one counts. Otherwise the scene is **Partial** (for example,
  720p finished but 1080p didn't) or **Grabbed**, and stays wanted. The
  **Downloaded** tab shows all three, each with its own badge. The toolbar
  filter has options for all scenes, still wanted, grabbed/partial, and
  downloaded.
- **Right-click menu and blacklist.** Right-click a scene, or a Ctrl/Shift
  selection of several, to:
  - mark it as downloaded, or as not downloaded
  - hide the scene
  - hide every scene from its studio
  - show only that studio
  - open the post

  Hidden items come back with **Show hidden**, where you can unhide them. You
  can also hide studios from the Studios tab.
- **Studios.** Every studio gets a card with its StashDB logo and official name.
  Scene cards use the official name too. Clicking a studio filters Browse, and
  the filter chip shows the logo. Logos are fetched slowly in the background,
  one every 1.5 s, with on-screen studios first. Add API keys in
  **Settings ▸ Stash-box**.
- **Import by file name.** Drag video files or folders onto the window, or
  use **Import files…**, and SceneSorter marks the matching releases as
  downloaded (verified). Matching is fuzzy. It understands:
  - original release names
  - Whisparr/Sonarr renames, for example
    `Studio - 2024-01-15 - Performer - Title [WEBDL-1080p][PRT].mp4`
  - SceneSorter's own rename template
  - release names that are only in the parent folder's name

  It requires the studio and date to agree (±1 day) or a strong title match,
  and it skips unfinished downloads (`.part`, `.crdownload` and so on).
- **Import folders.** Folders such as your browser or JDownloader download
  folder are scanned on a timer (every 5 min by default), and so is the library
  folder if you allow it. Files still being written are skipped until they've
  been unchanged for a minute.
- **stash-box scene matching.** Each scene is looked up on ThePornDB and
  StashDB in the background, one lookup every 3 s, with on-screen scenes first.
  A match is accepted only when studio and date agree. When both sides know
  the duration, it's checked too: a clearly different length rejects the
  candidate, and a near-identical length breaks ties, such as Part 1 vs Part 2
  on the same day. Matches add performers, duration and links to the scene
  pages. Posts without a thumbnail, or whose thumbnail won't load, show the
  matched scene's cover instead: ThePornDB's first, then StashDB's.
- **Favourites.** Star studios and performers in any of these places:
  - the right-click menu on a Studios or Performers card
  - the ☆ next to a name in the detail panel
  - the right-click menu on a scene

  Favourites sort first and have a "★ Favorites only" filter on both tabs.
  Browse also gets a **★ Favorites** filter for scenes from a favourite studio
  or with a favourite performer.
- **Performers tab.** Builds the performer list from three places:
  - stash-box matches
  - names in post titles
  - known names spotted inside dotted release names

  Portraits come from StashDB or ThePornDB. Click a performer to filter
  Browse.
- **Torrent black hole.** Set a folder and `.torrent` files are saved there for
  your client's watch folder. Magnets can optionally be saved as `.magnet` files.
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
