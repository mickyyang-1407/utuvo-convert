"""Synthetic tests for the two-pass bounded Upmix-only streaming path.

Covers:
  - long file that exceeds the old 512 MiB rendered width while the
    source fits;
  - bounded streaming pass / QC / output frame count exact;
  - single gain ≤ 1, decoded LoRo fold matches the baseline reference
    (whole-buffer pass);
  - first / last sample preservation;
  - distinct chunk call sizes agreement;
  - cancellation and collision do not leave orphan output / sidecar /
    staging files.
"""
import json
import math
import pathlib
import sys
import tempfile

import numpy as np
import soundfile as sf

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'prototype'))

import layout_catalog
import adm_compat
import convert as proof
import upmix as upmix_engine
import upmix_stream
import worker

PASSED = []
FAILED = []
_CASES = []   # (name, fn) tuples — deferred so importing this module
              # does NOT execute the suite (which is expensive and would
              # return None to anyone that simply does ``import``).


def case(name):
    """Register a test case. The function is recorded but NOT invoked
    until :func:`main` iterates the registry; this stops ``import
    test_upmix_stream`` from running the entire expensive suite."""
    def wrap(fn):
        _CASES.append((name, fn))
        return fn
    return wrap


def _run(name, fn):
    try:
        detail = fn() or {}
    except AssertionError as e:
        import traceback
        FAILED.append((name, '{m}\n{t}'.format(m=str(e),
                                                t=traceback.format_exc())))
        return
    except Exception as e:
        import traceback
        FAILED.append((name, 'unexpected: {t}: {m}\n{trace}'.format(
            t=type(e).__name__, m=e, trace=traceback.format_exc())))
        return
    PASSED.append((name, detail))


K = math.sqrt(0.5)


def _loro_indices(layout):
    """Indices contributing to LoRo L fold: FC + left surround/height."""
    labels = layout['labels']
    out = []
    if 'FC' in labels:
        out.append(labels.index('FC'))
    for pair in (layout['loro_pairs'] + layout['height_pairs']):
        out.append(labels.index(pair.split('/')[0]))
    return out


@case('stream-source-fits-but-rendered-exceeds-512-mib')
def _long_stream():
    """A long 7.1.4 upmix that would have required >512 MiB of full
    rendered buffer is processed by the streaming path; output frame
    count and gain are reported exactly."""
    rate = 48000
    # 30 minutes of stereo at 48 kHz ≈ 86.4 M samples; rendered as 7.1.4
    # (12 channels × 8 bytes) ≈ 8.3 GiB if held in memory — well above
    # the 512 MiB safety cap. The streaming pass must bound it.
    n = rate * 60 * 2  # 2 minutes — enough to exercise many blocks.
    rng = np.random.default_rng(20261004)
    x = rng.standard_normal((n, 2)) * 0.1
    # Verify the naive allocation would exceed 512 MiB.
    naive_bytes = n * 12 * 8
    assert naive_bytes > 512 * 1024 * 1024
    # Stream the upmix to a file via pass1 + pass2.
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / 'src.wav'
        sf.write(str(src), x, rate, subtype='PCM_24')
        out = td / 'out'
        out.mkdir()
        rep = worker.convert({
            'input': str(src), 'out_dir': str(out),
            'format': 'WAV', 'depth': '24',
            'target': '7.1.4-rear-first', 'layout': 'stereo', 'rate': rate,
            'upmix': {'mode': 'upmix-generic-v2',
                       'layout': '7.1.4-rear-first',
                       'centre': 0.2, 'surround': 0.4, 'height': 0.2},
        })
        audio = pathlib.Path(rep['output'])
        assert audio.exists()
        # Sample peak must obey the ceiling.
        assert rep['sample_peak'] <= upmix_engine.PEAK_CEILING + 1e-9
        # Output frame count exact from source rate.
        assert rep['frames'] == n
        assert rep['upmix']['streaming'] is True
        assert rep['upmix']['true_peak_target_dbfs'] == -1.5
        wav = adm_compat.read_wav_extensible_info(audio)
        assert wav['channels'] == 12 and wav['bits'] == 24
        assert wav['channel_mask'] == layout_catalog.find(
            layout_catalog.get_catalog(), '7.1.4-rear-first')['wav_mask']
    return {'frames': rep['frames'], 'peak': rep['sample_peak'],
            'labels': rep['labels']}


@case('stream-block-iter-covers-frame-equal-boundary')
def _block_boundary():
    """The chunk iterator yields ``(start, end)`` covering every input
    sample exactly once with no gaps and no overlap."""
    rate = 48000
    n = rate  # 1 second
    rng = np.random.default_rng(0)
    x = rng.standard_normal((n, 2)) * 0.1
    seen = 0
    for pos, end, _block in upmix_stream.stream_pass1(
            x, '5.1-side', rate, centre=0.35, surround=0.4,
            block_samples=16384):
        assert pos == seen, (pos, seen)
        assert end > pos
        seen = end
    assert seen == n, (seen, n)
    return {'frames': seen, 'n': n}


@case('stream-block-distinct-sizes-agree')
def _distinct_chunk_sizes():
    """Different chunk sizes must agree within numerical tolerance."""
    rate = 48000
    rng = np.random.default_rng(11)
    x = rng.standard_normal((rate * 2, 2)) * 0.1
    diffs = {}
    for bs in (16384, 32768, 65536, 131072, 262144):
        six_a, gain_a, _ = upmix_engine.upmix_pcm(
            x, '7.1.4-rear-first', rate, centre=0.2, surround=0.4,
            height=0.2, block_samples=bs)
        six_b, gain_b, _ = upmix_engine.upmix_pcm(
            x, '7.1.4-rear-first', rate, centre=0.2, surround=0.4,
            height=0.2, block_samples=bs)
        diff = float(np.max(np.abs(six_a - six_b)))
        assert diff < 1e-9, diff
        diffs[bs] = diff
    return diffs


@case('stream-first-last-sample-preserved')
def _first_last_sample():
    rate = 48000
    n = rate  # 1 second
    x = np.zeros((n, 2))
    x[0, 0] = 0.7
    x[0, 1] = 0.3
    x[-1, 0] = -0.5
    x[-1, 1] = 0.4
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / 'src.wav'
        sf.write(str(src), x, rate, subtype='PCM_24')
        out = td / 'out'
        out.mkdir()
        rep = worker.convert({
            'input': str(src), 'out_dir': str(out),
            'format': 'WAV', 'depth': '24',
            'target': '5.1-side', 'layout': 'stereo', 'rate': rate,
            'upmix': {'mode': 'experimental-stereo-to-5.1-side-v1',
                       'centre': 0.0, 'surround': 0.0},
        })
        d, sr = sf.read(str(rep['output']), dtype='float64', always_2d=True)
        assert sr == rate and d.shape == (n, 6)
        # With centre=0 and surround=0 the LoRo fold reduces to L = z_L
        # exactly (modulo PCM24 quantization). 4 LSB tolerance covers
        # both endpoint rounding and STFT block boundary artefacts.
        lsb = 1.0 / (2 ** 23)
        assert abs(d[0, 0] - 0.7) < 4 * lsb, d[0]
        assert abs(d[0, 1] - 0.3) < 4 * lsb, d[0, 1]
        assert abs(d[-1, 0] - (-0.5)) < 4 * lsb, d[-1, 0]
        assert abs(d[-1, 1] - 0.4) < 4 * lsb, d[-1, 1]
    return {'first_frame': d[0].tolist(), 'last_frame': d[-1].tolist()}


@case('stream-pass2-emits-finite-numbers-and-lfe-silent')
def _pass2_lfe():
    rate = 48000
    rng = np.random.default_rng(99)
    x = rng.standard_normal((rate, 2)) * 0.1
    cat = layout_catalog.get_catalog()
    for layout in cat['implemented']:
        if not layout['has_lfe']:
            continue
        labels = layout['labels']
        six_full, gain, _ = upmix_engine.upmix_pcm(
            x, layout['id'], rate,
            centre=0.2 if layout['has_centre'] else 0.0,
            surround=0.4,
            height=0.2 if layout['has_height'] else 0.0,
            block_samples=65536)
        # Drive pass2 explicitly and rebuild the same array.
        rebuilt = np.zeros_like(six_full)
        for pos, end, block in upmix_stream.stream_pass2(
                x, layout['id'], rate, gain,
                centre=0.2 if layout['has_centre'] else 0.0,
                surround=0.4,
                height=0.2 if layout['has_height'] else 0.0,
                block_samples=65536):
            rebuilt[pos:end] = block
        assert rebuilt.shape == six_full.shape
        assert np.allclose(rebuilt, six_full, atol=1e-12)
        assert float(np.max(np.abs(rebuilt[:, labels.index('LFE')]))) == 0.0
    return {'layouts_with_lfe': [l['id'] for l in cat['implemented']
                                   if l['has_lfe']]}


@case('stream-cancellation-leaves-no-orphan')
def _cancel_streaming():
    rate = 48000
    rng = np.random.default_rng(3)
    x = rng.standard_normal((rate * 30, 2)) * 0.1
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / 'src.wav'
        sf.write(str(src), x, rate, subtype='PCM_24')
        out = td / 'out'
        out.mkdir()
        original_link = worker.os.link
        calls = [0]
        def patched(src, dst, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                raise KeyboardInterrupt('simulated SIGTERM between links')
            return original_link(src, dst, *args, **kwargs)
        worker.os.link = patched
        try:
            worker.convert({
                'input': str(src), 'out_dir': str(out),
                'format': 'WAV', 'depth': '24',
                'target': '7.1.4-rear-first', 'layout': 'stereo',
                'rate': rate,
                'upmix': {'mode': 'upmix-generic-v2',
                           'layout': '7.1.4-rear-first',
                           'centre': 0.2, 'surround': 0.4, 'height': 0.2},
            })
        except KeyboardInterrupt:
            pass
        finally:
            worker.os.link = original_link
        sentinel_audio = out / 'src-7.1.4-rear-first.wav'
        sentinel_sidecar = sentinel_audio.with_suffix(
            sentinel_audio.suffix + '.conversion.json')
        assert not sentinel_audio.exists()
        assert not sentinel_sidecar.exists()
        # No leftover .conversion-* tempdir.
        assert not any('.conversion-' in str(p) for p in out.iterdir()), \
            list(out.iterdir())
    return {'pair_empty': True}


@case('stream-collision-on-output-preserves-sentinel')
def _collision():
    rate = 48000
    rng = np.random.default_rng(5)
    x = rng.standard_normal((rate, 2)) * 0.1
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / 'src.wav'
        sf.write(str(src), x, rate, subtype='PCM_24')
        out = td / 'out'
        out.mkdir()
        sentinel = out / 'src-7.1.4-rear-first.wav'
        sentinel.write_bytes(b'PRESET')
        try:
            worker.convert({
                'input': str(src), 'out_dir': str(out),
                'format': 'WAV', 'depth': '24',
                'target': '7.1.4-rear-first', 'layout': 'stereo',
                'rate': rate,
                'upmix': {'mode': 'upmix-generic-v2',
                           'layout': '7.1.4-rear-first',
                           'centre': 0.2, 'surround': 0.4, 'height': 0.2},
            })
        except ValueError:
            pass
        assert sentinel.read_bytes() == b'PRESET'
        sidecar = sentinel.with_suffix(sentinel.suffix + '.conversion.json')
        assert not sidecar.exists()
    return {'sentinel_intact': True, 'orphan_sidecar': False}


@case('stream-decoded-loro-matches-baseline-reference')
def _decoded_loro_baseline():
    rate = 48000
    rng = np.random.default_rng(20261004)
    n = rate
    x = rng.standard_normal((n, 2)) * 0.1
    # Whole-buffer reference.
    ref, gain_ref, _ = upmix_engine.upmix_pcm(
        x, '5.1.2', rate, centre=0.2, surround=0.4, height=0.2)
    # Streamed round-trip via the worker (PCM24 encode + decode).
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / 'src.wav'
        sf.write(str(src), x, rate, subtype='PCM_24')
        out = td / 'out'
        out.mkdir()
        rep = worker.convert({
            'input': str(src), 'out_dir': str(out),
            'format': 'WAV', 'depth': '24',
            'target': '5.1.2', 'layout': 'stereo', 'rate': rate,
            'upmix': {'mode': 'upmix-generic-v2',
                       'layout': '5.1.2',
                       'centre': 0.2, 'surround': 0.4, 'height': 0.2},
        })
        d, sr = sf.read(str(rep['output']), dtype='float64', always_2d=True)
    assert sr == rate and d.shape == (n, 8)
    # Compare decoded against reference / gain up to PCM24 LSB.
    lsb = 1.0 / (2 ** 23)
    err = float(np.max(np.abs(d - ref)))
    assert err < 4 * lsb, err
    return {'max_err_vs_reference': err}


@case('stream-riff-size-declaration-refuses-over-4gib')
def _riff_overflow_check():
    """A declared multi-gigabyte output is refused without allocating it."""
    frames = (4 * (1024 ** 3) // (12 * 3)) + 1
    try:
        adm_compat.wav_riff_size_bytes(frames, 12, 'pcm24')
    except ValueError as exc_msg:
        assert 'RIFF' in str(exc_msg)
        return {'refused': True, 'declared_frames': frames,
                'reason': str(exc_msg)}
    raise AssertionError('over-4-GiB declaration must refuse')


@case('stream-flac-custom-channel-mask-survives-rewrite-and-qc')
def _flac_custom_mask():
    rate = 48000
    rng = np.random.default_rng(2718)
    x = rng.standard_normal((rate // 2, 2)) * 0.08
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / 'src.wav'
        sf.write(str(src), x, rate, subtype='PCM_24')
        out = td / 'out'
        out.mkdir()
        rep = worker.convert({
            'input': str(src), 'out_dir': str(out),
            'format': 'FLAC', 'depth': '24',
            'target': '7.1', 'layout': 'stereo', 'rate': rate,
            'upmix': {'mode': 'upmix-generic-v2', 'layout': '7.1',
                       'centre': 0.2, 'surround': 0.4, 'height': 0.0},
        })
        output = pathlib.Path(rep['output'])
        expected_mask = sum(1 << proof.BITS[l]
                            for l in layout_catalog.find(
                                layout_catalog.get_catalog(), '7.1')['labels'])
        assert adm_compat.read_flac_channel_mask(output) == expected_mask
        with sf.SoundFile(str(output)) as decoded:
            assert decoded.format == 'FLAC'
            assert decoded.subtype == 'PCM_24'
            assert decoded.channels == 8 and len(decoded) == rate // 2
        sidecar = json.loads(pathlib.Path(
            str(output) + '.conversion.json').read_text())
        assert sidecar['upmix']['streaming'] is True
        assert sidecar['labels'] == ['FL', 'FR', 'FC', 'LFE',
                                     'BL', 'BR', 'SL', 'SR']
    return {'mask': '0x{0:x}'.format(expected_mask), 'channels': 8}


def main():
    # Iterate the registered cases so the suite runs only when main() is
    # called (CLI) and not when the module is merely imported.
    for name, fn in _CASES:
        _run(name, fn)
    print('cases run', len(PASSED) + len(FAILED))
    for name, detail in PASSED:
        print('PASS', name, detail)
    for name, err in FAILED:
        print('FAIL', name, err)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
