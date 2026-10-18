# AnyPS5 Launcher guide

Everything the launcher does, screen by screen. Back to the [README](../README.md).

## Console view (the home screen)
The launcher opens on a console-style home screen modelled on the PS5's layout:
- The focused game's artwork (`sce_sys/pic0.png`) fills the window behind a darkening gradient. Games without artwork get a deep-blue gradient.
- A row of rounded game tiles runs along the top, most recently played first; the focused tile is larger, outlined, and named.
- The title, its status, a **Play** button (it reads "Convert and play" when the game needs converting first) and a **•••** options button sit below.
- Four frosted cards show your progress, compatibility, frame rate and the latest screenshot.
- **Controls**: Left/Right (or the mouse wheel) to choose, Enter to play, the Menu key or right-click for options, F11 for full screen. With an Xbox-style controller: the D-pad or left stick to choose, A to play, Y for options, View for the classic view.
- **Library**, or the ☰ icon, opens the classic view, which has every detail and setting; **Console view** in its header comes back. The launcher remembers which one you used last.
- The look echoes the PS5's layout, not Sony's branding: no PlayStation logos, fonts or sounds are used.

Tk cannot draw translucent shapes, so the gradient and the frosted cards are small RGBA PNGs generated at run time and blended onto the artwork with Tk's own image compositing.

## Library
- **Grid of covers or a list**, switched with the buttons in the toolbar. Covers come from each game's `sce_sys/icon0.png`, and the details panel shows `sce_sys/pic0.png` as a banner.
- **Light, dark, or matching Windows**: Settings > Appearance. The Windows title bar follows the theme.
- **Search** (Ctrl+F) by title or title ID, a **filter** (Ready, Needs re-convert, Not converted, Won't start yet) and a **sort** (name, recently played, most played, recently added).
- Right-click a game for its actions; double-click or Enter plays it.
- **Favorites, tags and notes**: the ★ in the Overview (or the right-click menu) marks a favorite; the Tags and notes section takes comma-separated tags and free notes. The filter offers Favorites and one entry per tag, the sort offers Favorites first, and the search box also searches tags and notes.
- **+ Add game > Scan a folder or drive for games** finds every game folder below a folder: one with `eboot.bin` (or `.elf`/`.self`) and `sce_sys/param.json` or a module folder. It skips the launcher's own converted folders and lists what it found; tick the games to add, choose where the converted copies go, and they are added and converted in one go.
- **Storage** shows the space each converted game uses: game files (only those not hard-linked to the original), libraries, shader cache, saves and backups. Shader caches can be cleared (they are rebuilt while playing), and older automatic save backups trimmed to the newest 3.

## Converting and playing
- **Add game** reads the title, ID and icon, then converts the game in one step. Game files are hard-linked into `app0/` (no extra space), and the original folder is never changed. Fake-signed homebrew SELF files are unwrapped; encrypted retail SELFs are refused.
- Intel CPUs get `--to-intel` automatically. A compatibility check runs after every conversion, and **Play** warns before launching a game that is missing system functions. Functions a game imports as optional (weak imports) don't count against it: AnyPS5 leaves them empty and the game decides whether to use them, so the check lists them separately.
- Game output streams into the log, with plain-English hints for known failures. The log has three views: **Game output** (the default), **Everything**, and **Problems only** (warnings, errors and hints). Runtime trace lines such as `[src/Open.cpp:139 sceKernelOpen] …` (one per file opened) show only under Everything; otherwise they are counted beside the toolbar. It also has a search box, and **Copy report** puts a summary on the clipboard for asking for help: game, AnyPS5 build, status, compatibility, changed settings, problems and the last log lines, with traces left out. The full output of each run stays in `anyps5-last-run.log`.
- **Per-game settings**: GPU, console language, thread priority, start in fullscreen, store-at-flip, environment variables, and the conversion options (Intel fix, unused-import filter, extra module folders, startup diagnostics).
- **Start in fullscreen** presses the runtime's F11 toggle once the game window is in front and showing frames. AnyPS5 has no start-up option for fullscreen. If you rebound ToggleFullscreen away from F11, the launcher skips this and says so.

## Screenshots
- Press **F9** while a game runs, or **Take one now** in the Overview, to save the game's picture as a PNG in `Pictures\AnyPS5\<game>`. The picture is read from the screen, which also covers Vulkan and fullscreen games. The key can be changed in Settings > Playing (F8, F9, F10, Ctrl+F12, Print Screen); F12 is not offered, because Windows reserves it for its debugger and refuses it as a hot key. The key is registered only while a game runs.
- The Overview shows the newest six as a gallery: click to open, right-click to show in the folder or delete.

## Discord status
- Settings > Playing > Discord status shows the game you play, with elapsed time, on your Discord profile, using Discord's local connection. Discord shows activity under an application, so paste the ID of one you create at discord.com/developers/applications (New Application, name it AnyPS5). Empty means off; without Discord running, nothing happens.

## Frame rate
- AnyPS5 writes the current frame rate into the game window's title. The launcher reads it once a second, shows it in the status bar, and records each session's average, lowest and highest 2-second reading. The first 10 seconds are skipped, because loading and the first shader compiles would skew the numbers. The Overview shows the last session and the average across sessions.
- **FPS counter** (Settings > Playing): a small always-on-top counter in the corner of the game picture. It is created before the game window, never takes focus, lets clicks through, and has no taskbar button, so the game's input is unaffected.

## Fonts
- **Settings > Fonts > Download free fonts…** fetches exactly the substitute files AnyPS5 loads when a game opens the console's system fonts: Noto Sans (eight weights and styles), Noto Sans Mono and Noto Sans Thai (about 7.5 MB), plus optionally Noto Sans CJK for Japanese, Chinese and Korean (about 76 MB). The fonts are SIL Open Font License, downloaded file by file from Google's notofonts repositories into `launcher/data/fonts`, and checked to be font files. Without them, such games show no text.

## Controllers
- **Per-game keyboard and mouse**: Game settings > Keyboard & mouse > Customize for this game gives a game its own `anyps5-input.ini` beside its `eboot.exe`. The runtime reads that file first; otherwise the game uses the layout shared by all games (header > Controls). "Use the shared layout again" removes the game's own file.
- **Controls > Test controller…** shows an Xbox-style (XInput) controller live: buttons, D-pad, triggers and sticks, labelled with the PS5 buttons AnyPS5 maps them to (View is the touchpad). It also has a vibration test. PlayStation controllers are not XInput devices, so they are detected and named but not shown live; games read them directly.

## Saves and playtime
- **Playtime**: total time, sessions, last session length and last played, shown in the Overview. Runs shorter than 10 seconds count as failed starts, not sessions.
- **Automatic save backups** before and after every session, skipped when nothing changed. The last 10 are kept; turn this off in Settings.
- **Saves…** backs up now, restores (the current saves are backed up first, so a restore can be undone), exports or imports a zip, and opens the saves or backups folder.
- Removing a game together with its folder backs up its saves first.

## Shortcuts and quick launch
- **More > Create desktop shortcut** puts a shortcut with the game's icon on the desktop. Double-clicking it starts the game with the launcher minimized. The launcher closes again when the game exits cleanly, and comes back with the log when something went wrong.
- **Shortcut folders** (Settings > Shortcut folders): an "AnyPS5 Games" folder in the Start menu and/or on the desktop, holding one shortcut per game plus the launcher. It is updated when games are added, converted or removed (so the shortcuts pick up the game icons), and removed when you switch it off. Games then show up in Windows search.
- **Continue \<game\>** in the header resumes the most recently played game.
- From the command line: `pythonw "launcher\AnyPS5 Launcher.pyw" --play PPSA99666` (title ID or launcher ID). Only one launcher runs at a time; a second start hands its request to the open one.

## Updates
- The launcher checks for new AnyPS5 changes at start and every 24 hours. When there are some, it shows a banner with **Update now** and sends one Windows notification per new upstream version; notifications can be turned off in Settings.
- **Update AnyPS5 > Build latest from source** pulls your clone of the official [boykopovar/AnyPS5](https://github.com/boykopovar/AnyPS5) (`C:\dev\AnyPS5` by default, changeable in Settings), rebuilds it with WinLibs GCC and swaps the result into `AnyPS5-latest`, then offers to re-convert every game the update made outdated. If the build fails, it tries once more with half the parallel jobs, since a build that runs out of memory fails the same way. If Windows won't let it rename the new build into place (antivirus or OneDrive still reading the new files), it waits up to 20 seconds and otherwise keeps the previous install. The full output of the last update stays in `launcher/data/anyps5-last-update.log`. **Download latest official release** installs the newest official release from [boykopovar/AnyPS5](https://github.com/boykopovar/AnyPS5/releases) next to it instead.
- **Re-convert outdated games** and **Re-check compatibility of all games** are in the same menu.
- **What's new in AnyPS5** (Update menu, or the banner) lists the upstream changes your build does not have yet, grouped into New, Fixed, Faster and Other, with their pull requests. Changes that name a function one of your games is missing or has unfinished (from its last compatibility check), or its title ID, are listed first. After updating, each re-check names the functions that became available since the previous check.
- **Move to another PC** (Settings > General): **Export…** writes one zip with the library (playtime, tags, notes, history, per-game settings), settings that make sense elsewhere, controls, the newest saves and all save backups, and optionally screenshots. **Import…** on the other PC shows which game folders it found, can look for missing ones under a folder by title ID, and imports. Games are converted again there, and their saves come back automatically after the first conversion. **More > Change game folder…** points a game at a folder that moved.
- **Compatibility history**: every check is recorded per game. The Overview charts the number of missing functions over time, and **Full history** lists each check with its AnyPS5 build.

## Where things live

| Path | Contents |
|---|---|
| `launcher/data/library.json`, `launcher/data/settings.json` | Your library (including playtime and history) and the launcher settings |
| `launcher/data/backups/<game id>/` | Save backups (zip) |
| `launcher/data/anyps5-input.ini` | Custom controls; when absent, the built-in bindings apply |
| `launcher/data/fonts/` | Optional system fonts for games that need them |
| `launcher/data/anyps5-last-update.log` | The full output of the last **Build latest from source** |
| `<game folder>-anyps5/` | The converted game, its saves (`_sd/`), downloads (`download0/`) and shader cache. Re-converting rebuilds `eboot.exe`, `libs/`, `app0/` and `_input/` only. |

Set `ANYPS5_LAUNCHER_DATA` to keep the data somewhere else (the tests use this).

## Code

| File | Role |
|---|---|
| `launcher/anyps5core.py` | Engine without UI: library, SELF unwrapping, conversion, launching, saves, playtime, history, shortcuts, notifications, updates, single instance |
| `launcher/AnyPS5 Launcher.pyw` | Main window (classic view, and switching to the console view) |
| `launcher/ui_console.py` | Console-style home screen |
| `launcher/ui_library.py` | Cover grid, list, artwork loading, scroll container, history chart |
| `launcher/ui_dialogs.py` | Add, remove, settings, controls, saves and history dialogs |
| `launcher/ui_theme.py` | Light and dark palettes for ttk and classic Tk widgets |

```powershell
python -m unittest discover -s launcher\tests -v
```
