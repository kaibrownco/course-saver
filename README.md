# Course Saver

Save Kajabi courses you own — videos, lesson text and attachments — so you can watch them offline.
One app for **Windows** and **Android** (built with [Flet](https://flet.dev)), plus a command-line tool.

## For the person using the app

1. **Install** (see "Getting the apps" below).
2. Tap **+** (Add a site), type the website where you bought your courses, and sign in with the email and
   password you use there. The app lists **every course you own on that site** — tap **Add** next to the ones you
   want. Your password is not saved. (The refresh button on a site looks for newly purchased courses; the refresh
   button on a course checks for new lessons. Nothing you already downloaded is ever deleted by a refresh.)
3. Pick a **video quality**:
   - *Phone size* – smallest files, best for a phone
   - *HD 720p* – good for a computer
   - *Original* – the full upload; can be many gigabytes
4. Tick the lessons you want (or **Select everything new**) and press **Download**. You can **Pause** at any
   time and continue later; nothing is re-downloaded.
5. Tap a lesson to watch it, read its text, or open its files — no internet needed.
6. To free up space, tap a saved lesson's green check → **Delete video and files**, use the trash button on the lesson
   screen, or the ⋮ menu on a course → **Delete all downloads**. Each asks first; the course and its lessons stay and can be
   downloaded again.

On Windows your downloads are in `Documents\Course Saver` (the folder button at the top opens it).
On Android they live inside the app's storage and play in the app; removing the course (⋮ menu) frees the space.

Sign-in only works for sites that use an email and password. Courses that use Google/Facebook sign-in or
emailed magic links aren't supported yet.

## Getting the apps

Builds come from GitHub Actions (`.github/workflows/build.yml`) so no Windows machine or Android tooling is needed.
Tests run on every push; the slow app builds (10–20 min) only run when you ask for one:

| To build… | Do this |
|---|---|
| Along with a normal commit | Put **`[build]`** in the commit message (or PR title): `git commit -am "Fix login [build]" && git push` |
| Without any code change | `git commit --allow-empty -m "Build [build]" && git push` |
| From the terminal, any branch | `gh workflow run build.yml` |
| A published release | `git tag v0.1.0 && git push --tags` (builds, then attaches both files to a GitHub Release) |
| By clicking | **Actions → Build apps → Run workflow** |

Then download `CourseSaver-windows.zip` and `CourseSaver-android` (the `.apk`) from the finished run
(`gh run download` also works). AI agents are told (in `AGENTS.md`) never to add the build flag unless you explicitly ask; just say "start a build".

- **Windows:** unzip, double-click `course_saver.exe`. Windows SmartScreen will warn about an unknown publisher the
  first time: click **More info → Run anyway**. (Removing the warning requires a paid code-signing certificate.)
- **Android:** copy the `.apk` to the phone and open it. Builds are signed with a permanent key (stored as the GitHub secrets
  `ANDROID_KEYSTORE_BASE64` / `ANDROID_KEYSTORE_PASSWORD`), so a newer APK installs **over** the old one and keeps its
  downloads. (Only builds made before that was set up need an uninstall first.) Android asks to allow installs from this source: allow it
  for the file manager/browser you used. (Google Play Protect may add a "scan app" prompt.)

## Developing

Python 3.10+ is needed for the app (3.9+ works for the command-line tool).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest

flet run src/main.py            # run the app in a desktop window
flet run --web src/main.py      # ...or in the browser
flet build apk                  # Android build (downloads Flutter + Android SDK on first run)
flet build windows              # only on Windows (needs Visual Studio "Desktop development with C++")
```

Set `COURSE_SAVER_LIBRARY=/some/folder` to point the app at a different library folder.

### Command-line tool

Handy for scripting. Uses the same library folder format as the app.

```bash
kajabi-scrape login https://SITE.mykajabi.com            # email + password prompt; session saved
kajabi-scrape discover https://SITE.mykajabi.com         # list every course your account owns there
kajabi-scrape scrape-tree https://SITE.mykajabi.com/products/COURSE --title "Course"
kajabi-scrape scrape-posts library/course.json
kajabi-scrape download library/course.json --quality phone   # phone | hd | original; --limit 2 to test
kajabi-scrape generate-site                                  # optional static website in library/site
kajabi-scrape sync URL --cookies cookies/course.json         # all of the above in one go
```

Instead of `login` you can pass `--cookies` with a browser cookie export. Cookie files and `library/sessions/`
are credentials: they are gitignored — never commit or share them.

## Layout

- `src/main.py` — the Flet app (all UI)
- `src/core` — login, fetcher, parser, Wistia client, downloader (resumable), models, `service.py` (library + jobs)
- `src/cli` — the `kajabi-scrape` command
- `src/sitegen` — optional static-site export (not used by the app)
- `tests/` — `pytest` (local stub servers stand in for Kajabi and the video CDN)

## Known limitations

- Android: attachments are saved but there is no "open file" button yet; videos and lesson text work in-app.
  Large downloads can be paused by Android if the screen is off for a long time — keep the app open and the
  phone plugged in, and use *Phone size*. Paused downloads resume.
- Single account per course site; sessions expire after a couple of weeks and the app asks for the password again.
- Not tested on a real Windows PC or Android phone yet — the first CI build may need small fixes.
