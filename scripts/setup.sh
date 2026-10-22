#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3.12}
"$PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 is required"'
"$PYTHON" -m venv .runtime
.runtime/bin/python -m pip install -r requirements-lock.txt
.runtime/bin/python -c 'import soundfile as sf; assert "MP3" in sf.available_formats(), "libsndfile has no MP3 support"'
printf 'Runtime ready. Run: sh app/build.sh\n'
