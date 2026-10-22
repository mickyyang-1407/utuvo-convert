# Initial source validation — 2026-10-23

On the validation host (Apple Silicon, macOS 27):

- Fresh Python 3.12 virtual environment: all pinned packages installed.
- `sh app/build.sh`: compiled both Swift helpers and the SwiftUI app; packaged
  the engine, 302 runtime visual resources and ten icon sizes successfully.
- `sh scripts/test.sh`: MP3 tests (8 methods), local format tests (2 methods),
  upmix layout tests and streaming tests all completed successfully.
- Bundled Python worker, with system-only PATH: a generated 1-second stereo
  WAV exported to MP3, 48 kHz, 48,000 decoded frames.
- Publication includes source/assets/notices only; no interpreter, package
  wheels, user audio, signing material or private Git history.
- PNG ancillary metadata was removed without changing compressed pixel data.

The current run did not repeat GUI automation, physical-speaker listening,
notarization or installation on another Mac. The MP3 batch fix had separate
native GUI validation before this source publication. Local developer builds
are not the previously notarized installer.
