# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

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
