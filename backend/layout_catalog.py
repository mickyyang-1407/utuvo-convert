"""Layout catalogue loader for the upmix engine.

One product JSON registry read by Python and Swift. The catalogue lists
explicitly implemented upmix output layouts plus reference-only entries
that are exposed for documentation but never selectable. The loader
validates schema, normalises labels, and exposes a small read-only API
used by ``backend/upmix.py``, ``backend/worker.py``,
``prototype/convert.py``, and the App side via the bundled JSON copy.
"""
from __future__ import annotations

import json
import pathlib
from typing import Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / 'backend' / 'upmix_layouts.json'

LAYOUT_BUNDLED_PATH = ROOT / 'build' / 'Audio Converter.app' / 'Contents' / 'Resources' / 'OfflineEngine' / 'upmix_layouts.json'

# Bit positions for canonical WAV ascending-bit ordering. Must match
# ``prototype/convert.BITS``.
BITS = {
    'FL': 0, 'FR': 1, 'FC': 2, 'LFE': 3, 'BL': 4, 'BR': 5,
    'SL': 9, 'SR': 10, 'TFL': 12, 'TFR': 14, 'TBL': 15, 'TBR': 17,
}

REQUIRED_KEYS = (
    'id', 'name', 'labels', 'channels', 'has_height', 'has_lfe',
    'has_centre', 'loro_pairs', 'height_pairs',
)


class CatalogError(ValueError):
    """Raised when the catalogue JSON is malformed."""


def _coerce_bool(value, field: str, layout_id: str) -> bool:
    if isinstance(value, bool):
        return value
    raise CatalogError(
        '{f} for layout {l} must be a boolean'.format(f=field, l=layout_id))


def _validate_layout(entry: dict, *, source: str) -> dict:
    lid = entry.get('id')
    if not isinstance(lid, str) or not lid:
        raise CatalogError('layout id missing or empty in {s}'.format(s=source))
    for key in REQUIRED_KEYS:
        if key not in entry:
            raise CatalogError(
                '{k} missing in layout {l}'.format(k=key, l=lid))
    labels = entry['labels']
    if not isinstance(labels, list) or not labels:
        raise CatalogError('labels missing or empty for layout {l}'.format(l=lid))
    if any(not isinstance(l, str) for l in labels):
        raise CatalogError('labels must be strings in layout {l}'.format(l=lid))
    if entry['channels'] != len(labels):
        raise CatalogError(
            'channels {c} != labels {n} in layout {l}'.format(
                c=entry['channels'], n=len(labels), l=lid))
    for lbl in labels:
        if lbl not in BITS:
            raise CatalogError(
                'unknown label {l} in layout {i}'.format(l=lbl, i=lid))
    has_height = _coerce_bool(entry['has_height'], 'has_height', lid)
    has_lfe = _coerce_bool(entry['has_lfe'], 'has_lfe', lid)
    has_centre = _coerce_bool(entry['has_centre'], 'has_centre', lid)
    if has_lfe and 'LFE' not in labels:
        raise CatalogError('has_lfe but LFE missing in {l}'.format(l=lid))
    if has_centre and 'FC' not in labels:
        raise CatalogError('has_centre but FC missing in {l}'.format(l=lid))
    if has_height:
        for lbl in labels:
            if not lbl.startswith('T'):
                continue
            # height labels are valid; presence of TFL/TFR/TBL/TBR is OK.
    loro_pairs = entry['loro_pairs']
    height_pairs = entry['height_pairs']
    if not isinstance(loro_pairs, list) or any(
            not isinstance(p, str) for p in loro_pairs):
        raise CatalogError('loro_pairs invalid in {l}'.format(l=lid))
    if not isinstance(height_pairs, list) or any(
            not isinstance(p, str) for p in height_pairs):
        raise CatalogError('height_pairs invalid in {l}'.format(l=lid))
    # WAV mask must equal the sorted-by-bit mask.
    sorted_labels = sorted(labels, key=lambda l: BITS[l])
    mask = sum(1 << BITS[l] for l in sorted_labels)
    expected = '0x{x:0{w}x}'.format(x=mask, w=(len(sorted_labels) + 1) // 4 + 1).lower()
    declared = str(entry.get('wav_mask', expected)).lower()
    if declared != expected:
        raise CatalogError(
            'wav_mask {d} != canonical {e} for layout {l}'.format(
                d=declared, e=expected, l=lid))
    return {
        'id': lid,
        'name': entry['name'],
        'labels': list(labels),
        'channels': int(entry['channels']),
        'has_height': has_height,
        'has_lfe': has_lfe,
        'has_centre': has_centre,
        'loro_pairs': list(loro_pairs),
        'height_pairs': list(height_pairs),
        'wav_mask': mask,
        'sorted_labels': sorted_labels,
        'input_only': bool(entry.get('input_only', False)),
    }


def _validate_reference(entry: dict) -> dict:
    if 'id' not in entry or 'channels' not in entry:
        raise CatalogError('reference entry missing id/channels')
    return {'id': str(entry['id']), 'channels': int(entry['channels']),
            'kind': str(entry.get('kind', ''))}


def load(path: pathlib.Path = None) -> dict:
    """Load and validate the catalogue. Returns a dict with:
       version, implemented (list of normalised layouts),
       input_orders (list of explicit input-only layouts),
       reference (list of reference-only entries), defaults, ranges.
    """
    source = path or CATALOG_PATH
    if not source.exists() and source == LAYOUT_BUNDLED_PATH:
        # fallback to source-of-truth copy
        source = CATALOG_PATH
    if not source.exists():
        raise CatalogError('catalogue not found at {p}'.format(p=str(source)))
    raw = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise CatalogError('catalogue root must be an object')
    impl = raw.get('implemented', [])
    if not isinstance(impl, list) or not impl:
        raise CatalogError('implemented list missing or empty')
    implemented = [_validate_layout(e, source=str(source)) for e in impl]
    input_orders = []
    for v in raw.get('explicit_input_orders', {}).values():
        input_orders.append(_validate_layout(v, source=str(source)))
    ref_entries = raw.get('reference', [])
    reference = [_validate_reference(e) for e in ref_entries]
    impl_ids = {l['id'] for l in implemented}
    impl_ids.update(l['id'] for l in input_orders)
    for r in reference:
        if r['id'] in impl_ids:
            raise CatalogError(
                'reference id {i} duplicates an implemented id'.format(i=r['id']))
    return {
        'version': raw.get('version', ''),
        'implemented': implemented,
        'input_orders': input_orders,
        'reference': reference,
        'defaults': {
            'surround': float(raw.get('surround_budget_default', 0.4)),
            'centre': float(raw.get('centre_default', 0.0)),
            'height': float(raw.get('height_default', 0.2)),
            'highpass_cutoff_hz': float(raw.get('highpass_cutoff_hz', 1000.0)),
        },
        'ranges': {
            'surround': tuple(raw.get('surround_budget_range', [0.0, 1.0])),
            'centre': tuple(raw.get('centre_range', [0.0, 0.7])),
            'height': tuple(raw.get('height_range', [0.0, 0.5])),
        },
        'rates': tuple(raw.get('rates_supported', [44100, 48000, 96000])),
    }


def find(catalog: dict, layout_id: str) -> dict:
    for layout in catalog['implemented']:
        if layout['id'] == layout_id:
            return layout
    for layout in catalog['input_orders']:
        if layout['id'] == layout_id:
            return layout
    raise KeyError('layout id {i} not in catalogue'.format(i=layout_id))


def is_implemented(catalog: dict, layout_id: str) -> bool:
    return any(l['id'] == layout_id for l in catalog['implemented'])


def implemented_ids(catalog: dict) -> list:
    return [l['id'] for l in catalog['implemented']]


def reference_ids(catalog: dict) -> list:
    return [r['id'] for r in catalog['reference']]


# Module-level cache so callers don't repeatedly parse the file.
_CACHE: dict = {}


def get_catalog() -> dict:
    if not _CACHE:
        _CACHE.update(load())
    return _CACHE


def reset_cache() -> None:
    _CACHE.clear()