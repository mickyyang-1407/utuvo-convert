"""Package the product icon at macOS iconset sizes; no image synthesis."""
from pathlib import Path
import hashlib
import json
import plistlib
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / 'design/app-icon'
MASTER = ART / 'ConvertIcon-v2.png'
ICONSET = ART / 'ConvertIcon.iconset'
APP = ROOT / 'build/UTUVO convert.app'
assert MASTER.is_file(), 'Missing product icon master'
ICONSET.mkdir(exist_ok=True)
for logical in (16, 32, 128, 256, 512):
    for scale in (1, 2):
        size = logical * scale
        name = f'icon_{logical}x{logical}' + ('@2x' if scale == 2 else '') + '.png'
        subprocess.run(['/usr/bin/sips', '-z', str(size), str(size), str(MASTER),
                        '--out', str(ICONSET / name)], check=True, capture_output=True)
subprocess.run(['/usr/bin/iconutil', '-c', 'icns', str(ICONSET),
                '-o', str(ART / 'ConvertIcon.icns')], check=True)
resources = APP / 'Contents/Resources'
resources.mkdir(parents=True, exist_ok=True)
shutil.copy2(ART / 'ConvertIcon.icns', resources / 'ConvertIcon.icns')
plist = APP / 'Contents/Info.plist'
info = plistlib.loads(plist.read_bytes())
info['CFBundleIconFile'] = 'ConvertIcon'
plist.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML, sort_keys=False))
manifest = {'master': str(MASTER.relative_to(ROOT)),
            'master_sha256': hashlib.sha256(MASTER.read_bytes()).hexdigest(),
            'icns_sha256': hashlib.sha256((ART / 'ConvertIcon.icns').read_bytes()).hexdigest(),
            'iconset': sorted(p.name for p in ICONSET.glob('*.png')),
            'CFBundleIconFile': info['CFBundleIconFile'],
            'method': 'Built-in imagegen; sips size export and iconutil packaging'}
(ART / 'manifest.json').write_text(json.dumps(manifest, indent=2))
print(json.dumps({'pass': True, 'iconset_sizes': len(manifest['iconset']),
                  'bundle_icon': str(resources / 'ConvertIcon.icns')}))
