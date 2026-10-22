#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
# All audio inputs are generated in temporary directories. No playback.
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
sh prototype/build.sh
mkdir -p .build
swiftc -O -parse-as-library -module-cache-path .build/spatial-cache prototype/vendor/SpatialRenderer.swift backend/spatial_main.swift -o .build/spatial-helper
.runtime/bin/python -m unittest discover -s tests -p 'test_mp3*.py' -v
.runtime/bin/python -m unittest discover -s tests -p 'test_local_formats.py' -v
.runtime/bin/python tests/test_upmix_layouts.py
.runtime/bin/python tests/test_upmix_stream.py
