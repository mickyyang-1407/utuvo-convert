# Third-party notices

The MIT license covers UTUVO-owned source and visual assets in this repository,
including the two published Swift snapshots listed in `prototype/PROVENANCE.md`.
Third-party license texts in `third-party/` retain their original terms.

No Python interpreter, Python package wheel, codec binary, Apple SDK or FFmpeg
binary is distributed in this Git repository. Setup installs dependencies from
their package repositories into a local virtual environment.

| Dependency | Purpose | License |
| --- | --- | --- |
| EBU EAR | ADM rendering | BSD-3-Clause-Clear |
| SoundFile | Audio I/O | BSD-3-Clause |
| NumPy | Arrays and numerical operations | BSD-3-Clause and bundled notices |
| SciPy | Signal processing | BSD-3-Clause and bundled notices |
| attrs | EAR dependency | MIT |
| lxml | XML parsing | BSD-3-Clause and bundled notices |
| ruamel.yaml | YAML parsing | MIT |
| [multipledispatch](https://github.com/mrocklin/multipledispatch/blob/0.6.0/LICENSE.txt) | EAR dependency | BSD-3-Clause |
| six | Compatibility | MIT |
| cffi | Foreign-function interface | MIT |
| pycparser | C parser | BSD-3-Clause |
| setuptools | Package compatibility | MIT and bundled notices |

Pinned Python versions are in `requirements-lock.txt`; original dependency
metadata and license texts are in `third-party/`. Wheel payloads may also
contain OpenBLAS, LAPACK, compiler runtime libraries and their notices.

SoundFile uses [libsndfile](https://github.com/libsndfile/libsndfile/blob/1.2.2/COPYING) (LGPL-2.1-or-later). Codec libraries selected by a
particular wheel/system build, such as LAME, mpg123, FLAC, Ogg and Vorbis,
retain their own licenses. The installed wheel supplies its own binary notices.
A distributor bundling any runtime or codec must preserve and comply with
those exact binaries' licenses, including applicable LGPL source/relinking
obligations. This source publication does not relicense those libraries.

Apple Foundation, Accelerate, AudioToolbox, CoreAudio, AppKit and SwiftUI are
system dependencies under Apple's terms; they are not included here. FFmpeg
is an optional, separately installed testing tool with its own build-dependent
license. No Dolby SDK or proprietary third-party codec is included.
