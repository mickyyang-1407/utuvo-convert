"""Synthetic tests for the generic v2 upmix layouts.

Covers every implemented layout in the catalogue: distinct registry
labels / counts, raw LoRo fold residual < 1e-9, gain-adjusted LoRo fold
residual < 1e-9 after applying the global static attenuation, six
supported rates, chunked vs whole-buffer agreement < 1e-9, range
refusals, LFE silent, quad centre ineffective.
"""
import json
import math
import sys
import pathlib
import tempfile

import numpy as np
import soundfile as sf

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'prototype'))

import layout_catalog
import upmix as upmix_engine
import adm_compat
import convert as proof
import worker

PASSED = []
FAILED = []


def case(name):
    def wrap(fn):
        try:
            detail = fn() or {}
        except AssertionError as e:
            FAILED.append((name, str(e)))
            return
        except Exception as e:
            FAILED.append((name, 'unexpected: {t}: {m}'.format(
                t=type(e).__name__, m=e)))
            return
        PASSED.append((name, detail))
    return wrap


K = math.sqrt(0.5)


def loro_indices(layout):
    """Indices into the output buffer that contribute to the LoRo L
    fold for the given layout: FC plus the left label of every
    surround / height pair, plus FL (always index 0)."""
    labels = layout['labels']
    out = [labels.index('FL')]
    if 'FC' in labels:
        out.append(labels.index('FC'))
    for pair in (layout['loro_pairs'] + layout['height_pairs']):
        out.append(labels.index(pair.split('/')[0]))
    return out


def loro_left(six, layout):
    """Build L = FL + K*FC + sum(K*left surround/height) for the
    given layout, returning a 1-D array of length ``six.shape[0]``."""
    L = six[:, 0].copy()
    if 'FC' in layout['labels']:
        L = L + K * six[:, layout['labels'].index('FC')]
    for pair in (layout['loro_pairs'] + layout['height_pairs']):
        L = L + K * six[:, layout['labels'].index(pair.split('/')[0])]
    return L


def loro_right(six, layout):
    R = six[:, 1].copy()
    if 'FC' in layout['labels']:
        R = R + K * six[:, layout['labels'].index('FC')]
    for pair in (layout['loro_pairs'] + layout['height_pairs']):
        R = R + K * six[:, layout['labels'].index(pair.split('/')[1])]
    return R


@case('layout-catalogue-has-ten-distinct-implemented-entries')
def _catalogue_distinct():
    cat = layout_catalog.load()
    ids = layout_catalog.implemented_ids(cat)
    assert len(ids) == 10, ids
    assert len(set(ids)) == len(ids), 'duplicate ids in catalogue'
    for lid in ids:
        layout = layout_catalog.find(cat, lid)
        assert layout['channels'] == len(layout['labels']), (
            lid, layout['channels'], len(layout['labels']))
    return {'count': len(ids), 'ids': ids}


@case('layout-each-channel-count-matches-labels')
def _channels_match_labels():
    cat = layout_catalog.load()
    for layout in cat['implemented']:
        labels = layout['labels']
        assert layout['channels'] == len(labels), layout
    return {'layouts': [l['id'] for l in cat['implemented']]}


@case('layout-LFE-rises-by-band-true-only-with-lfe-label')
def _lfe_label():
    cat = layout_catalog.load()
    for layout in cat['implemented']:
        if 'LFE' in layout['labels']:
            assert layout['has_lfe']
        if layout['has_lfe']:
            assert 'LFE' in layout['labels']
    return {'checked': len(cat['implemented'])}


@case('layout-FC-present-only-when-has-centre')
def _centre_flag():
    cat = layout_catalog.load()
    for layout in cat['implemented']:
        if 'FC' in layout['labels']:
            assert layout['has_centre']
        if layout['has_centre']:
            assert 'FC' in layout['labels']
    return {'checked': len(cat['implemented'])}


@case('layout-height-labels-only-when-has-height')
def _height_flag():
    cat = layout_catalog.load()
    for layout in cat['implemented']:
        for label in layout['labels']:
            if label.startswith('T'):
                assert layout['has_height'], (layout['id'], label)
        if layout['has_height']:
            assert any(l.startswith('T') for l in layout['labels'])
    return {'checked': len(cat['implemented'])}


@case('layout-raw-loro-fold-residual-le-one-minus-nine-all-layouts')
def _raw_loro_fold():
    cat = layout_catalog.load()
    rng = np.random.default_rng(20261004)
    rate = 48000
    x = rng.standard_normal((rate // 2, 2)) * 0.1
    max_err = 0.0
    for layout in cat['implemented']:
        height = 0.2 if layout['has_height'] else 0.0
        centre = 0.35 if layout['has_centre'] else 0.0
        six, _, _ = upmix_engine.upmix_pcm(
            x, layout['id'], rate, centre=centre, surround=0.4, height=height)
        L = loro_left(six, layout)
        R = loro_right(six, layout)
        err_l = float(np.max(np.abs(L - x[:, 0])))
        err_r = float(np.max(np.abs(R - x[:, 1])))
        assert err_l < 1e-9, (layout['id'], err_l)
        assert err_r < 1e-9, (layout['id'], err_r)
        max_err = max(max_err, err_l, err_r)
    return {'max_residual': max_err}


@case('layout-gain-adjusted-loro-fold-residual-le-one-minus-nine')
def _gain_adjusted_loro():
    cat = layout_catalog.load()
    rng = np.random.default_rng(42)
    rate = 48000
    x = rng.standard_normal((rate // 2, 2)) * 0.3
    max_err = 0.0
    for layout in cat['implemented']:
        height = 0.2 if layout['has_height'] else 0.0
        centre = 0.35 if layout['has_centre'] else 0.0
        six, gain, _ = upmix_engine.upmix_pcm(
            x, layout['id'], rate, centre=centre, surround=0.5, height=height)
        L = loro_left(six, layout)
        R = loro_right(six, layout)
        assert (L / gain).shape == x[:, 0].shape
        err_l = float(np.max(np.abs(L / gain - x[:, 0])))
        err_r = float(np.max(np.abs(R / gain - x[:, 1])))
        assert err_l < 1e-9, (layout['id'], err_l)
        assert err_r < 1e-9, (layout['id'], err_r)
        max_err = max(max_err, err_l, err_r)
    return {'max_residual': max_err}


@case('layout-six-supported-rates-all-pass')
def _six_rates():
    cat = layout_catalog.load()
    rng = np.random.default_rng(1)
    max_err = 0.0
    for rate in (44100, 48000, 88200, 96000, 176400, 192000):
        x = rng.standard_normal((rate // 4, 2)) * 0.1
        for layout in cat['implemented']:
            # Layouts are rate-agnostic; only sample rate matters.
            if layout['id'] == '7.1.4-rear-first' and rate == 192000:
                # 12 channels × 48000 frames × 8 bytes = 4.5 MiB. Still
                # fine. The safety cap is 512 MiB.
                pass
            height = 0.2 if layout['has_height'] else 0.0
            centre = 0.35 if layout['has_centre'] else 0.0
            six, _, _ = upmix_engine.upmix_pcm(
                x, layout['id'], rate, centre=centre, surround=0.4,
                height=height)
            assert six.shape == (x.shape[0], layout['channels'])
            assert np.isfinite(six).all()
            L = loro_left(six, layout)
            err = float(np.max(np.abs(L - x[:, 0])))
            assert err < 1e-9, (rate, layout['id'], err)
            max_err = max(max_err, err)
    return {'rates': (44100, 48000, 88200, 96000, 176400, 192000),
            'max_residual': max_err}


@case('layout-chunked-equals-whole-buffer-small-inputs')
def _chunked_equals_whole():
    cat = layout_catalog.load()
    rng = np.random.default_rng(13)
    rate = 48000
    x = rng.standard_normal((rate, 2)) * 0.1
    max_diff = 0.0
    for layout in cat['implemented']:
        height = 0.2 if layout['has_height'] else 0.0
        centre = 0.35 if layout['has_centre'] else 0.0
        whole, gw, _ = upmix_engine.upmix_pcm(
            x, layout['id'], rate, centre=centre, surround=0.4,
            height=height, block_samples=rate + 1)
        chunk, gc, _ = upmix_engine.upmix_pcm(
            x, layout['id'], rate, centre=centre, surround=0.4,
            height=height, block_samples=4096)
        diff = float(np.max(np.abs(whole - chunk)))
        assert diff < 1e-9, (layout['id'], diff)
        max_diff = max(max_diff, diff)
    return {'max_diff': max_diff}


@case('layout-LFE-exactly-silent-every-layout-with-lfe')
def _lfe_silent():
    cat = layout_catalog.load()
    rng = np.random.default_rng(7)
    rate = 48000
    x = rng.standard_normal((rate // 2, 2)) * 0.1
    for layout in cat['implemented']:
        if not layout['has_lfe']:
            continue
        height = 0.2 if layout['has_height'] else 0.0
        centre = 0.35 if layout['has_centre'] else 0.0
        six, _, _ = upmix_engine.upmix_pcm(
            x, layout['id'], rate, centre=centre, surround=0.4, height=height)
        lfe_idx = layout['labels'].index('LFE')
        assert float(np.max(np.abs(six[:, lfe_idx]))) == 0.0, layout['id']
    return {'layouts_with_lfe': [l['id'] for l in cat['implemented']
                                   if l['has_lfe']]}


@case('layout-quad-centre-amount-doesnt-affect-fc-when-no-fc')
def _quad_centre():
    rng = np.random.default_rng(11)
    rate = 48000
    x = rng.standard_normal((rate, 2)) * 0.1
    # Quad has no FC. Requesting centre > 0 must refuse; centre = 0 ok.
    six, _, meta = upmix_engine.upmix_pcm(
        x, 'quad', rate, centre=0.0, surround=0.4, height=0.0)
    assert six.shape == (rate, 4)
    # Try refuse centre > 0 for quad.
    raised = False
    try:
        upmix_engine.upmix_pcm(x, 'quad', rate, centre=0.5, surround=0.4,
                                height=0.0)
    except ValueError:
        raised = True
    assert raised, 'quad must refuse centre > 0'
    return {'shape': list(six.shape)}


@case('layout-height-zero-when-no-height-label')
def _height_zero():
    rng = np.random.default_rng(99)
    rate = 48000
    x = rng.standard_normal((rate, 2)) * 0.1
    raised = False
    try:
        upmix_engine.upmix_pcm(x, '5.1-side', rate, centre=0.0,
                                surround=0.4, height=0.1)
    except ValueError:
        raised = True
    assert raised, '5.1-side must refuse non-zero height'
    return {'refused': True}


@case('layout-refuses-bool-and-nan-and-out-of-range-amounts')
def _range_refusals():
    rng = np.random.default_rng(2026)
    x = rng.standard_normal((256, 2)) * 0.1
    for layout_id in ('quad', '5.1-side', '7.1.4-rear-first'):
        for kwargs in (
            {'centre': True},
            {'surround': True},
            {'height': True},
            {'surround': -0.1},
            {'surround': 1.5},
            {'centre': -0.1},
            {'centre': 0.9},
            {'height': -0.1},
            {'height': 0.6},
            {'centre': float('nan')},
            {'surround': float('inf')},
        ):
            raised = False
            try:
                upmix_engine.upmix_pcm(x, layout_id, 48000, **kwargs)
            except ValueError:
                raised = True
            assert raised, ('layout {l} kwargs {k} must refuse'.format(
                l=layout_id, k=kwargs))
    return {'layouts': ['quad', '5.1-side', '7.1.4-rear-first']}


@case('layout-refuses-unknown-id-and-reference-id')
def _refuse_unknown():
    cat = layout_catalog.load()
    x = np.zeros((64, 2))
    for lid in ('22.2', '5.1.6', '7.1.4-side-first', 'unknown-layout',
                '6.1.4'):
        raised = False
        try:
            upmix_engine.upmix_pcm(x, lid, 48000)
        except ValueError as e:
            raised = str(e)
        assert raised, 'must refuse {l}'.format(l=lid)
    return {'refused': ['22.2', '5.1.6', '7.1.4-side-first', 'unknown-layout',
                         '6.1.4']}


@case('layout-stationary-coherent-mono-stays-front-all-layouts')
def _stationary_front():
    cat = layout_catalog.load()
    rate = 48000
    n = rate // 2
    t = np.arange(n) / rate
    s = 0.3 * (np.sin(2 * math.pi * 437 * t)
              + 0.2 * np.sin(2 * math.pi * 1761 * t))
    x = np.column_stack((s, s))
    for layout in cat['implemented']:
        height = 0.2 if layout['has_height'] else 0.0
        centre = 0.35 if layout['has_centre'] else 0.0
        six, _, _ = upmix_engine.upmix_pcm(
            x, layout['id'], rate, centre=centre, surround=0.4, height=height)
        # All non-front labels must stay below the input peak.
        front_labels = {'FL', 'FR'}
        if layout['has_centre']:
            front_labels.add('FC')
        front_peak = max(float(np.max(np.abs(six[:, layout['labels'].index(l)])))
                          for l in front_labels)
        for l in layout['labels']:
            if l in front_labels or l == 'LFE':
                continue
            non_front_peak = float(np.max(np.abs(six[:, layout['labels'].index(l)])))
            assert non_front_peak <= front_peak + 1e-9, (layout['id'], l,
                                                          non_front_peak,
                                                          front_peak)
    return {'layouts': [l['id'] for l in cat['implemented']]}


@case('layout-moving-source-recorded-honestly')
def _moving_source():
    cat = layout_catalog.load()
    rate = 48000
    n = rate
    t = np.arange(n) / rate
    s = 0.3 * np.sin(2 * math.pi * 437 * t)
    ang = np.linspace(0, math.pi / 2, n)
    x = np.column_stack((s * np.cos(ang), s * np.sin(ang)))
    metrics = {}
    for layout_id in ('5.1-side', '7.1.4-rear-first'):
        layout = layout_catalog.find(layout_catalog.load(), layout_id)
        height = 0.2 if layout['has_height'] else 0.0
        centre = 0.35 if layout['has_centre'] else 0.0
        six, _, _ = upmix_engine.upmix_pcm(
            x, layout_id, rate, centre=centre, surround=0.4, height=height)
        trim = slice(rate // 3, -rate // 3)
        rear_idx = [i for i, l in enumerate(layout['labels'])
                    if l not in ('FL', 'FR', 'FC', 'LFE')]
        rear = np.zeros(n)
        for i in rear_idx:
            rear = rear + six[:, i] ** 2
        front = sum(six[:, layout['labels'].index(l)] ** 2
                     for l in ('FL', 'FR'))
        leakage = 10 * math.log10(max(float(np.sum(rear[trim])), 1e-300)
                                  / float(np.sum(front[trim])))
        metrics[layout_id] = leakage
    return metrics


@case('layout-quad-without-fc-keeps-fc-column-silent')
def _quad_no_fc():
    rng = np.random.default_rng(123)
    x = rng.standard_normal((48000, 2)) * 0.1
    six, _, _ = upmix_engine.upmix_pcm(x, 'quad', 48000,
                                         centre=0.0, surround=0.4, height=0.0)
    # Labels: FL FR BL BR. No FC index.
    L = six[:, 0] + K * six[:, 2]
    err = float(np.max(np.abs(L - x[:, 0])))
    assert err < 1e-9, err
    return {'loro_err': err}


@case('layout-worker-v2-emits-correct-sidecar-record')
def _worker_v2_sidecar():
    cat = layout_catalog.load()
    sys.path.insert(0, str(ROOT / 'prototype'))
    import worker
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        rng = np.random.default_rng(2)
        src = td / 'src.wav'
        x = rng.standard_normal((48000, 2)) * 0.1
        sf.write(str(src), x, 48000, subtype='PCM_24')
        out = td / 'out'
        out.mkdir()
        rep = worker.convert({
            'input': str(src), 'out_dir': str(out),
            'format': 'WAV', 'depth': '24',
            'target': '5.1.2',
            'layout': 'stereo', 'rate': 48000,
            'upmix': {'mode': 'upmix-generic-v2',
                       'layout': '5.1.2',
                       'centre': 0.2, 'surround': 0.4, 'height': 0.2},
        })
        audio = pathlib.Path(rep['output'])
        sidecar = audio.with_suffix(audio.suffix + '.conversion.json')
        assert sidecar.exists()
        side = json.loads(sidecar.read_text())
        assert side['upmix']['mode'] == 'upmix-generic-v2'
        assert side['upmix']['layout_id'] == '5.1.2'
        assert side['upmix']['height'] == 0.2
        assert side['labels'] == ['FL', 'FR', 'FC', 'LFE', 'SL', 'SR',
                                   'TFL', 'TFR']
        # LFE silent in the actual file.
        d, sr = sf.read(str(audio), dtype='float64', always_2d=True)
        assert sr == 48000 and d.shape[1] == 8
        lfe_idx = side['labels'].index('LFE')
        assert float(np.max(np.abs(d[:, lfe_idx]))) == 0.0
    return {'labels': side['labels']}


@case('layout-known-pcm-side-first-714-downmixes-to-stereo')
def _side_first_downmix():
    labels = proof.LAYOUTS['7.1.4-side-first']
    rate = 48000
    n = 4096
    t = np.arange(n) / rate
    source = np.column_stack([
        0.015 * (index + 1) * np.sin(2 * math.pi * (311 + index * 37) * t)
        for index in range(len(labels))
    ])
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / 'side-first.wav'
        writer = adm_compat.StreamingWAVWriter(
            src, rate, labels, 'pcm24', proof.BITS, n)
        writer.write(source)
        writer.close()
        out = td / 'out'
        out.mkdir()
        rep = worker.convert({
            'input': str(src), 'out_dir': str(out),
            'format': 'WAV', 'depth': '24', 'target': 'stereo',
            'layout': '7.1.4-side-first', 'rate': rate,
        })
        decoded, decoded_rate = sf.read(
            rep['output'], dtype='float64', always_2d=True)
        assert decoded_rate == rate and decoded.shape == (n, 2)
        K = math.sqrt(0.5)
        left = source[:, labels.index('FL')] + K * source[:, labels.index('FC')]
        right = source[:, labels.index('FR')] + K * source[:, labels.index('FC')]
        for name in ('SL', 'BL', 'TFL', 'TBL'):
            left += K * source[:, labels.index(name)]
        for name in ('SR', 'BR', 'TFR', 'TBR'):
            right += K * source[:, labels.index(name)]
        expected = np.column_stack((left, right))
        err = float(np.max(np.abs(decoded - expected)))
        assert err < 2e-6, err
        assert rep['source_layout'] == '7.1.4-side-first'
    return {'max_error': err, 'source_layout': rep['source_layout']}


@case('layout-all-known-multichannel-pcm-matrices-target-stereo')
def _known_layout_stereo_matrices():
    checked = []
    stereo = proof.LAYOUTS['stereo']
    for layout_id, labels in proof.LAYOUTS.items():
        if layout_id in ('mono', 'stereo'):
            continue
        matrix, _gain = proof.matrix_for(labels, stereo)
        assert len(matrix) == 2
        assert all(len(row) == len(labels) for row in matrix)
        checked.append(layout_id)
    return {'known_layouts': checked}


def main():
    print('cases run', len(PASSED) + len(FAILED))
    for name, detail in PASSED:
        print('PASS', name, detail)
    for name, err in FAILED:
        print('FAIL', name, err)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
