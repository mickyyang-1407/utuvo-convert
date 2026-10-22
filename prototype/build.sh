#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p ../.build
swiftc -O -module-cache-path ../.build/module-cache vendor/MixingPrecision.swift main.swift -o ../.build/mix-helper
