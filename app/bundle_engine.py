"""Bundle a local development runtime and authored visual resources.

The resulting app depends on the Python installation used by scripts/setup.sh.
It is not a standalone distribution or a notarized release.
"""
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

root = Path(__file__).resolve().parents[1]
bundle = root / "build/UTUVO convert.app"
resources = bundle / "Contents/Resources"
resources.mkdir(parents=True, exist_ok=True)
required = [root / name for name in (".runtime/bin/python", "backend/worker.py",
            "prototype/convert.py", ".build/mix-helper", ".build/spatial-helper")]
for path in required:
    if not path.exists():
        raise SystemExit(f"Missing local build dependency: {path.relative_to(root)}")
digest = hashlib.sha256()
# The catalogue JSON is consumed by both the Python backend and the App;
# it must therefore participate in the engine version hash so a catalogue
# change always triggers a rebundle. The staging step below copies it
# along with the rest of ``backend/``.
for path in sorted([root / "requirements-lock.txt",
                    root / "backend/upmix_layouts.json",
                    *root.glob("backend/*.py"),
                    *root.glob("prototype/*.py"),
                    *root.glob("prototype/vendor/*.swift"),
                    root / ".build/mix-helper",
                    root / ".build/spatial-helper"]):
    digest.update(path.read_bytes())
version = digest.hexdigest()[:16]
engine = resources / ("OfflineEngine-" + version)
if not engine.exists():
    staging = Path(tempfile.mkdtemp(prefix=".engine-staging-", dir=resources))
    try:
        for name in (".runtime", "backend", "prototype", "third-party"):
            shutil.copytree(root / name, staging / name, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (staging / ".build").mkdir()
        for name in ("mix-helper", "spatial-helper"):
            shutil.copy2(root / ".build" / name, staging / ".build" / name)
        (staging / "evidence").mkdir()
        shutil.copy2(root / "LICENSE", staging / "LICENSE")
        shutil.copy2(root / "THIRD_PARTY_NOTICES.md", staging / "THIRD_PARTY_NOTICES.md")
        shutil.copy2(root / "requirements-lock.txt", staging / "requirements-lock.txt")
        (staging / "bundle-manifest.json").write_text(json.dumps({
            "version": version, "architecture": "arm64", "resources": "bundled",
            "runtime_scope": "Machine-local Homebrew interpreter/library links remain; no standalone distribution claim",
            "reuse": "UTUVO source: MIT; dependencies retain their own licenses"
        }, indent=2))
        staging.rename(engine)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
alias = resources / "OfflineEngine"
if alias.exists() and not alias.is_symlink():
    alias.rename(resources / ("OfflineEngine-retained-" + uuid.uuid4().hex))
temporary_alias = resources / (".engine-link-" + uuid.uuid4().hex)
temporary_alias.symlink_to(engine.name, target_is_directory=True)
os.replace(temporary_alias, alias)
print(f"Bundled offline engine {version} in {bundle.relative_to(root)}")

# Prerendered Blender visuals are separate from hashed DSP/runtime resources.
button_assets = resources / "ButtonAssets"
button_assets.mkdir(exist_ok=True)
for name in ("normal-body.png", "pressed-body.png"):
    source = root / "design/blender-button" / name
    if not source.exists():
        raise SystemExit(f"Missing authored button render: {source.relative_to(root)}")
    temporary = button_assets / ("." + name + ".new")
    shutil.copy2(source, temporary)
    os.replace(temporary, button_assets / name)

# A complete immutable visual set, swapped only after all authored frames exist.
iris_source = root / "design/iris-crystal-v5/app-assets"
iris_files = [Path("manifest.json"), Path("layout-5/manifest.json"), Path("backdrop.png"), Path("center-normal.png"), Path("center-pressed.png"),
              *(Path(f"frames/iris-{i:03}.png") for i in range(73)),
              *(Path(f"layout-5/frames/iris-{i:03}.png") for i in range(73))]
iris_files += [Path("matte") / relative for relative in list(iris_files)]
visual_digest = hashlib.sha256()
for relative in iris_files:
    source = iris_source / relative
    if not source.is_file():
        raise SystemExit(f"Missing authored iris asset: {source.relative_to(root)}")
    visual_digest.update(source.read_bytes())
iris_version = visual_digest.hexdigest()[:16]
iris_bundle = resources / ("IrisAssets-" + iris_version)
if not iris_bundle.exists():
    staging = Path(tempfile.mkdtemp(prefix=".iris-staging-", dir=resources))
    try:
        for relative in iris_files:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(iris_source / relative, target)
        staging.rename(iris_bundle)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
iris_alias = resources / "IrisAssets"
if iris_alias.exists() and not iris_alias.is_symlink():
    iris_alias.rename(resources / ("IrisAssets-retained-" + uuid.uuid4().hex))
iris_link = resources / (".iris-link-" + uuid.uuid4().hex)
iris_link.symlink_to(iris_bundle.name, target_is_directory=True)
os.replace(iris_link, iris_alias)
print(f"Bundled two 73-pose glass wheels with soft/gloss endpoints, visual version {iris_version}")
