# SceneSorter

A desktop download manager and library sorter for scene releases. It
pulls post listings from configured sources and merges them into one entry per
scene. It can start downloads, then rename and move finished files into a
destination folder for Whisparr to pick up. It also reads your final,
studio-sorted library to show what you already have.

## Features

- **Merged listings.** Each site posts every resolution of a scene as a
  separate entry. SceneSorter parses the release titles and shows one card per
  scene, with chips for 2160p / 1080p / 720p and a dot for each source.
- **Thumbnails from any source.** If one site's image fails to load, the app
  falls back to another site's. Images are cached on disk.
- **"Picture removed" placeholders are skipped.** Some hosts, such as pixhost,
  answer a taken-down image with a normal "Picture removed" picture instead of
  an error. When the same picture comes back for four or more different
  scenes, it's treated as missing. Its cached copies are deleted and those
  cards fall back to another site's thumbnail or the stash-box cover.
  Placeholders already in the cache are cleared once on start. Dead image
  links are retried after two weeks.
- **Download queue.** Direct links download inside the app with progress and
  speed. Magnet and .torrent links are passed to your torrent client.
- **Daily quota tracking.** Sources can declare a daily action limit. Each
  started download counts as one action in a rolling 24-hour window. The UI
  shows how many are left and blocks downloads once the limit is reached.
- **Auto-sort.** Finished downloads are moved into the **Destination** folder.
  Renaming is a separate toggle: turn it off to keep the original file name. The default
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
- **Library.** Set **Settings ▸ General ▸ Library** to your final collection,
  sorted one folder per studio (`Library/<Studio>/<file>`), for example where
  Whisparr imports to. SceneSorter only reads it. Matching scenes get an
  **IN LIBRARY** badge, even when your file is a different resolution or came
  from another site. When a file name doesn't name the studio, the studio
  folder is used.
- **Upgrades tab.** Lists scenes in your library whose file is below your
  preference: under your wanted resolution, or not from a preferred release
  group. A PRT file at 720p counts, and so does a 1080p file from another
  group. It only lists scenes where a release that meets your preference has
  been posted and isn't grabbed yet. Tick **Also show ones with no upgrade
  posted yet** to see the rest. **Download all shown upgrades** queues them
  all, or only the selected ones, with the usual login and daily-limit checks.
  Single scenes get a **⬆ Download upgrade** button and right-click entry.
- **What's recorded per library file.** Resolution, release group and codec
  are read from the name, using your naming scheme when it fits. The scan also
  records how the file was matched: naming scheme, file name, folder name or
  studio folder. The detail panel shows all of this under "In library".
- **Fast library checks.** The scan runs in the background. It remembers each
  folder's modified date and contents. A folder's date changes whenever a file
  in it is added, removed or renamed, so unchanged folders aren't read again
  and their files aren't looked at. Only new or changed files are matched.
  The library is checked on start, when Settings are saved, and on the
  import-folder timer. **Scan library** in the toolbar checks now, and its
  arrow menu has a full rescan that reads every folder and file again.
- **Library naming.** Paste Whisparr's scene naming format, for example
  `{Studio CleanTitleSlug}{.Release Date}.{Scene.CleanTitle}{.Scene.Performers}{.Quality.Title}{.MediaInfo VideoCodec}{-Release Group}`.
  File names that fit it are read field by field, so studio, date, quality,
  codec and group come from fixed places instead of being guessed. Names that
  don't fit are matched by guessing as before. The settings page shows how an
  example name is read.
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
- **Flag for review.** Right-click a studio, performer or scene and choose
  "⚑ Flag for review…", with an optional note. **Export flagged…** on the
  Studios or Performers tab saves a text report listing the posts behind each
  flagged item, ready to hand over for parser fixes.
- **Hide performers.** Works like hiding a studio: the performer and their
  scenes are hidden unless **Show hidden** is ticked. An optional setting shows
  scenes from hidden studios when a favourite performer is in them.
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
  folder are scanned on a timer (every 5 min by default), and so is the
  destination folder if you allow it. Files still being written are skipped until they've
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
- **Smooth with large libraries.** Merging, filtering, sorting, status and
  studio counts all run on worker threads, and the window only swaps in
  finished lists. New posts no longer move what you're looking at: the card at
  the top of the view stays put. Click empty space, click the selected card
  again, or press Esc to deselect.
- **Sorting and codec filter.** Sort by newest posted (the default), scene
  date, studio A–Z (newest first within each studio), title or length. Filter
  by video codec (HEVC, AVC, AV1, VP9, …), read from the release name, file
  name or tags.
- **Safe to close, update or pause at any time.**
  - **Closing, updating and pausing:** nothing new starts, and anything already
    running is allowed to finish: a page being fetched, a merge, a file scan,
    a stash-box lookup. Only then does the app close, install the update or
    report that it's paused. If HTTP downloads are running, you're asked
    first, and stopped downloads show as failed so you can retry them.
  - **Crashes and power cuts:** each page's posts are saved in one database
    transaction, and a page is marked done only after its posts are stored.
    A cut-off can cause a page to be fetched again, but never skipped.
  - **Interrupted checks:** a check for new posts that's cut off partway
    remembers where it stopped and finishes before anything else, so no hole
    is left between the newest posts and the cache.
  - **Offline or VPN switching:** shows "Can't reach …" and retries within
    2 minutes instead of backing off for an hour. Images and stash-box lookups
    retry once the connection is back.
  - **Interrupted HTTP downloads:** shown as failed after a restart, so you
    can retry them. Image cache files are written atomically.
- **Bounded memory.** Images are decoded straight to display size, so a
  4000 px portrait never sits in memory at full size. Only the most recently
  used few hundred of each kind are kept in memory. The disk cache stores
  downscaled copies and is capped at 1 GB by default (Settings ▸ General ▸
  Image cache size). The least recently used images are removed first, and a
  **Clear image cache** button deletes them all.
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
destination/         default destination for finished downloads (configurable)
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
  library.py       Rename templates, move finished downloads to the destination
  importer.py      Match files on disk to scenes (import folders, library scan)
  naming.py        Read file names that follow a Whisparr-style naming format
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
