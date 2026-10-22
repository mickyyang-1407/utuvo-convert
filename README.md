# UTUVO convert

An offline audio converter for macOS with a floating, animated glass interface.
Built with SwiftUI, AppKit and a Python audio engine. English and Traditional
Chinese are included.

**Open source under the [MIT License](LICENSE).** Third-party dependencies retain
their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Features

- Export WAV, AIFF, FLAC, Ogg Vorbis and MP3.
- MP3 mono/stereo at 128, 192, 256 or 320 kbps CBR, 44.1 or 48 kHz.
- Batch conversion with per-file results and JSON quality reports.
- Explicit channel layouts, downmixing, and experimental PCM upmix layouts
  including 5.1 and 7.1.4.
- ADM/BWF rendering through EBU EAR and an Apple spatial-rendering route.
- No cloud service, account or API key required for conversion.

The initial open-source snapshot includes the October 21 MP3 batch fix: valid
MP3 reconstruction peaks above 0 dBFS produce a warning instead of discarding
the output. Source level is preserved; playback may clip.

## Build from source

Requirements: Apple Silicon Mac, macOS 26+, Xcode Command Line Tools with a
macOS 26+ SDK, Python **3.12**, and an internet connection for initial dependency
installation. The current validation host runs macOS 27; other OS versions and
Intel Macs have not been validated.

If you use Homebrew, install the prerequisites with:

```sh
brew install python@3.12 ffmpeg
```

FFmpeg is used only by independent test decoders, not by the app's conversion
engine. Python dependencies are installed into a project-local virtual environment.

```sh
git clone https://github.com/mickyyang-1407/utuvo-convert.git
cd utuvo-convert
sh scripts/setup.sh
sh app/build.sh
open "build/UTUVO convert.app"
```

Set `PYTHON=/path/to/python3.12` when running setup if it is not on your PATH.
The generated app is a **local development build**: its Python runtime depends
on the Python installation used at setup. Keep that installation available.
This build is not a standalone installer or an Apple-notarized release.
Pre-rendered visual assets are included; Blender is not required to build.

## Use

1. Click the centre or drop audio files onto it. A folder can be added for batch work.
2. Choose an output format, then an output channel layout.
3. Review settings and click Convert. Choose a separate output folder.
4. Inspect each result and its `.conversion.json` report.

Use **Command+,** for language and glass appearance settings. MP3 supports only
mono/stereo. The engine refuses unsupported layouts and existing output names.
Use your own copies for initial testing. See [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Tests

```sh
sh scripts/test.sh
```

These tests generate synthetic signals in temporary directories, exercise MP3,
other export formats, upmix layouts and streaming, and never play audio.
FFmpeg/FFprobe independently inspect and decode MP3 output.

## Source map

| Directory | Contents |
| --- | --- |
| `app/` | SwiftUI/AppKit interface and local build packaging |
| `backend/` | Conversion worker, ADM compatibility and upmix DSP |
| `prototype/` | PCM helpers and UTUVO-owned Swift renderer snapshots |
| `design/` | Authored icon and pre-rendered interface assets |
| `tests/` | Synthetic audio regression tests |
| `third-party/` | Dependency license texts and inventory |

The repository contains a clean source snapshot, without private development
history, signing credentials, user audio or local acceptance logs.

## 繁體中文

UTUVO convert 是離線 macOS 音訊轉檔工具，支援 WAV、AIFF、FLAC、OGG、MP3、
批次轉檔、明確聲道排列、Downmix、實驗性 Upmix 與 ADM 渲染。

使用 MIT 授權，可使用、修改與散布；第三方元件保留各自授權。依上方步驟安裝
Python 3.12、執行 `sh scripts/setup.sh` 和 `sh app/build.sh` 後即可開啟。
目前原始碼建置為本機開發版，需保留原 Python 安裝，未重新公證或製作獨立安裝包。

MP3 批次修補已納入：有效 MP3 的解碼峰值超過 0 dBFS 時會警告並保留輸出，
原音量不變；部分播放器仍可能削波。Upmix 是 PCM 估測，不是 Dolby Atmos
object／ADM 編碼。實體環繞喇叭與不同 Mac 的驗收仍需另行進行。

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). UTUVO names and logos identify the
original project; the MIT license does not grant trademark rights or imply
endorsement of forks.
