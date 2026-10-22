# Introduction page

Static GitHub Pages site, Traditional Chinese at `/` and English at `/en/`.
No JavaScript, external fonts, analytics, video or animation frames are loaded.
System appearance selects the page's light/dark theme. App imagery retains its
actual appearance; English copy identifies the Traditional Chinese app demo.

Only `site/` is uploaded by `.github/workflows/pages.yml`. Engine source,
runtimes, the 302 full-size app rendering assets and local evidence are excluded
from the deployed site.

## Image budget and provenance

| File | Dimensions | Bytes |
| --- | --- | ---: |
| hero-640.webp | 640 × 646 | 37,270 |
| hero-360.webp | 360 × 363 | 17,702 |
| mp3-settings.webp | 760 × 478 | 18,026 |
| icon.webp | 64 × 64 | 1,936 |

Total image storage: **74,934 bytes (73.2 KiB)**. The browser selects one hero
size using `srcset`; the settings image loads lazily. Each image is under 40 KB.
WebP quality 80; metadata removed. These assets are resized/cropped copies,
so the original app assets are unchanged.

The hero and MP3 settings are truthful crops of the October 5 native-interface
marketing demo (`still-format` and `still-advanced-mp3`), produced using synthetic
audio. No UI labels or controls have been repainted. The icon comes from the
published `design/app-icon/ConvertIcon-v2.png`. All are UTUVO-owned material
covered by this repository's MIT license.

Preview: `python3 -m http.server 51863 --bind 127.0.0.1 --directory site`.
