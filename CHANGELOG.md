# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [1.1.0] — 2026-10-25

### Fixed

- Words with a digit inside — script and tool names such as `f2b`, `x11`,
  `h264` — are no longer altered. The digit used to be skipped while the
  letters around it were glued into one word, so `f2b` could come out as
  `аи2`. Any input with a digit in it is now left alone as a whole and never
  reaches the log, as the user manual has always promised.
- Words added to "My dictionary" by hand no longer vanish. When the service
  learned from an undo, it rewrote the dictionary from memory and could drop
  a word added from the window or the console moments earlier. It now
  re-reads the file before every write.
- The dictionary window no longer reports "Saved" when the save failed. It
  now says so and stays open, so nothing you typed is lost.

### Added

- Search in "My dictionary", in the window and in the console. It finds a
  word in either layout: `ghbdtn` finds `привет`, `руддщ` finds `hello`.
  - In the window, the matches appear in a panel above the full dictionary.
    Clicking one jumps the editor to that word. The panel stays put and
    updates as you edit, so you can fix several words in a row. "Save" works
    at any time and always writes the whole dictionary.
  - In the console, the new "Find a word" item lists the matches. For each
    match you can switch its language or delete it, then pick the next word
    from the same list.

### Changed

- "My dictionary" keeps the order in which words were added, newest first,
  instead of sorting them alphabetically. A word you have just added or the
  program has just learned is at the top, where you can see it. This
  applies everywhere: the window, the console, words learned from undo,
  imported settings and the installer.
- "My dictionary" opens ready for a new word, both in the window and with
  "Edit file" in the console. The cursor is on an empty line right under
  the hint, so there is no scrolling to the end of a long list.

## [1.0.0] — 2026-09-17

First public release.

### Added

- Automatic keyboard layout switching for Russian and English, driven by
  dictionaries and letter-combination analysis.
- Early switching: the layout changes mid-word as soon as the language is
  clear, without waiting for the space bar.
- Layout switching on a single Shift press, independent of the system
  shortcut.
- Undo with a double Shift or Pause; the word is restored and remembered in
  "My dictionary" so it is never corrected again.
- "My dictionary": add, view, edit and clear your own words from the window
  or the console.
- Application exclusions: pick a window from the list, add the one in focus,
  or take the ready-made set for Steam games.
- Two leading capitals fixed: `HEllo` becomes `Hello`, with dictionary
  words such as `IDs` and `PCs` left intact.
- Passwords are never converted and never logged, not even with debug
  logging on.
- Panel indicator with configurable size and per-state colours.
- Program window (`asc-gui`) with draggable tiles and a remembered layout.
- Console menu (`asc`) with the same capabilities, plus short commands for
  scripts.
- Settings export and import in a single plain-text file.
- The installer applies a settings file found next to it, which makes
  installing across many computers a single command.
- Built-in user manual in both languages, thirteen sections.
- Installer for four package managers: apt, dnf/yum, pacman, zypper.
- Support for X11 and Wayland, with a GNOME extension, a KDE plasmoid and a
  KWin script for active-window detection.
