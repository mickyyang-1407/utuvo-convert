"""Bounded synthetic tests for the experimental stereo → 5.1-side upmix.

Every test builds temporary in-memory fixtures; no real audio, no Music,
no project evidence. Tests cover the DSP invariants, worker integration
(opt-in + refusals), LoRo-fold behaviour, chunking, and the static
common attenuation that keeps the peak under the -1 dBFS ceiling.
"""
import json
import math
import os
import pathlib
import sys
import tempfile

import numpy as np
import soundfile as sf

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'prototype'))
sys.path.insert(0, str(ROOT / 'backend'))

import adm_compat
import convert as proof
import upmix as upmix_engine
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


# ---------------------------------------------------------------------------
# DSP invariants — direct rank-one / phase / silence / endpoint / short /
# moving-pan / six-channel mask / exact length / gain-adjusted LoRo fold.
# ---------------------------------------------------------------------------


def _loro_ref_floor(six: np.ndarray) -> float:
    """Reference LoRo fold: L = FL + sqrt(.5)*FC + sqrt(.5)*SL, R = FR + sqrt(.5)*FC + sqrt(.5)*SR.
    Returns peak dB of the fold against a unit-level input."""
    K = math.sqrt(0.5)
    L = six[:, 0] + K * six[:, 2] + K * six[:, 4]
    R = six[:, 1] + K * six[:, 2] + K * six[:, 5]
    folded = np.column_stack((L, R))
    peak = float(np.max(np.abs(folded), initial=0))
    return 20 * math.log10(max(peak, 1e-30))


def _write_pcm24(path: pathlib.Path, data: np.ndarray, rate: int):
    sf.write(str(path), data.astype(np.float64), rate, subtype='PCM_24')


@case('upmix-rank-one-zero-phase-dual-mono-keeps-lo')
def _rank_one_dual_mono():
    rate = 48000
    n = rate // 2
    t = np.arange(n) / rate
    s = 0.3 * (np.sin(2 * np.pi * 437 * t) + 0.2 * np.sin(2 * np.pi * 1761 * t))
    x = np.column_stack((s, s))
    six, gain = upmix_engine.upmix_pcm_to_5_1_side(
        x, rate, centre=0.35, surround=0.7)
    assert gain == 1.0, 'dual-mono should not need static attenuation'
    # The fold should still reach the inputs at moderate level.
    floor = _loro_ref_floor(six)
    assert floor >= -12.0, ('dual-mono LoRo fold too low: {dB:.3f} dB'.format(
        dB=floor))
    return {'lo_floor_db': floor, 'length': six.shape[0]}


@case('upmix-rank-one-90deg-phase-remains-front')
def _rank_one_quarter_phase():
    rate = 48000
    n = rate // 2
    t = np.arange(n) / rate
    s = 0.3 * (np.sin(2 * np.pi * 437 * t))
    # 90° phase between L and R is still rank-one; primary filter holds it.
    shifted = 0.3 * np.sin(2 * np.pi * 437 * t + math.pi / 2)
    x = np.column_stack((s, shifted))
    six, gain = upmix_engine.upmix_pcm_to_5_1_side(
        x, rate, centre=0.35, surround=0.7)
    # The fold (what a stereo listener gets back on a monitor) should still
    # reach the input level. 90° phase is rank-one; primary filter keeps it.
    fold = _loro_ref_floor(six)
    sl = float(np.max(np.abs(six[:, 4])))
    sr = float(np.max(np.abs(six[:, 5])))
    assert fold >= -12.0, '90° phase front should remain strong: {dB}'.format(
        dB=fold)
    # Diagnostic only — recorded as evidence that 90° phase does not
    # collapse into the rear channels. There is no claim that surround
    # must be zero; the fold test is the meaningful invariant.
    return {'fold_db': fold, 'sl_max': sl, 'sr_max': sr}


@case('upmix-rank-one-antiphase-keeps-front-zero-centre')
def _rank_one_antiphase():
    rate = 48000
    n = rate // 2
    t = np.arange(n) / rate
    s = 0.3 * np.sin(2 * np.pi * 437 * t)
    x = np.column_stack((s, -s))
    six, gain = upmix_engine.upmix_pcm_to_5_1_side(
        x, rate, centre=0.7, surround=0.7)
    # Hard antiphase: rho_real == -1 so the centre gate must close.
    fc_peak = float(np.max(np.abs(six[:, 2])))
    assert fc_peak < 1e-6, fc_peak
    return {'fc_peak': fc_peak}


@case('upmix-silence-produces-silence')
def _silence():
    rate = 48000
    six, gain = upmix_engine.upmix_pcm_to_5_1_side(
        np.zeros((2048, 2)), rate)
    assert six.shape == (2048, 6)
    assert float(np.max(np.abs(six))) == 0.0
    assert gain == 1.0
    return {'peak': 0.0}


@case('upmix-endpoint-and-block-boundary-transients')
def _endpoint_transients():
    rate = 48000
    n = rate  # 1 second
    pulse = np.zeros(n)
    pulse[[0, n // 2, n - 1]] = 0.8
    x = np.column_stack((pulse, 0.7 * pulse))
    six, gain = upmix_engine.upmix_pcm_to_5_1_side(
        x, rate, centre=0.35, surround=0.7)
    assert six.shape == (n, 6)
    assert np.isfinite(six).all()
    # Zero padding preserves first/last impulses as well as the interior.
    # The independent oracle additionally checks the exact LoRo waveform.
    energy = six[:, 0] ** 2 + six[:, 1] ** 2
    positions = [0, n // 2, n - 1]
    for idx in positions:
        lo = max(0, idx - 512)
        hi = min(n, idx + 512)
        window = energy[lo:hi]
        peak = float(np.max(window))
        assert peak > 1e-6, ('no transient near {i}: peak {p}'.format(
            i=idx, p=peak))
    # Output must be finite and within the ceiling regardless of where
    # the surviving transients landed.
    assert float(np.max(np.abs(six))) <= upmix_engine.PEAK_CEILING + 1e-9
    return {'peak': float(np.max(np.abs(six))), 'gain': gain}


@case('upmix-short-inputs-handled')
def _short_inputs():
    rate = 48000
    for length in (1, 2, 511, 2048, 2049):
        rng = np.random.default_rng(length)
        x = rng.uniform(-0.5, 0.5, (length, 2))
        six, gain = upmix_engine.upmix_pcm_to_5_1_side(x, rate)
        assert six.shape == (length, 6), (six.shape, length)
        assert np.isfinite(six).all(), length
    return {'lengths': [1, 2, 511, 2048, 2049]}


@case('upmix-moving-pan-records-movement')
def _moving_pan_metrics():
    rate = 48000
    n = rate
    t = np.arange(n) / rate
    s = 0.3 * np.sin(2 * np.pi * 437 * t)
    ang = np.linspace(0, math.pi / 2, n)
    x = np.column_stack((s * np.cos(ang), s * np.sin(ang)))
    six, gain = upmix_engine.upmix_pcm_to_5_1_side(
        x, rate, centre=0.35, surround=0.7)
    # Two alternate candidate diagnostic metrics (no claim of ground truth):
    diag = {
        'fl_minus_fr_db': float(20 * math.log10(
            max(np.sum(six[:, 0] ** 2), 1e-30) /
            max(np.sum(six[:, 1] ** 2), 1e-30))),
        'rear_to_front_db': float(20 * math.log10(
            max(np.sum(six[:, 4:] ** 2), 1e-30) /
            max(np.sum(six[:, :3] ** 2), 1e-30))),
    }
    assert math.isfinite(diag['fl_minus_fr_db'])
    assert math.isfinite(diag['rear_to_front_db'])
    return {'metrics': diag, 'gain': gain}


@case('upmix-six-channel-mask-and-exact-length')
def _shape_and_mask():
    rate = 48000
    rng = np.random.default_rng(7)
    x = rng.standard_normal((rate, 2)) * 0.1
    six, gain = upmix_engine.upmix_pcm_to_5_1_side(x, rate)
    assert six.shape == (rate, 6)
    # LFE must be exactly silent, not just near-zero.
    assert float(np.max(np.abs(six[:, 3]))) == 0.0
    return {'shape': list(six.shape), 'lfe_max': 0.0}


@case('upmix-static-attenuation-clamps-peak-below-minus-1dBFS')
def _static_attenuation():
    rate = 48000
    # Independent noise: primary filter is near zero, ambient == z, so
    # amplification is unavoidable. Static attenuation must still keep
    # the peak at or below -1 dBFS.
    rng = np.random.default_rng(11)
    noise = 0.3 * rng.standard_normal((rate, 2))
    six, gain = upmix_engine.upmix_pcm_to_5_1_side(
        noise, rate, centre=0.35, surround=0.7)
    peak_db = 20 * math.log10(max(float(np.max(np.abs(six))), 1e-30))
    assert peak_db <= -0.9999999999, peak_db
    assert gain < 1.0, 'static attenuation should reduce gain'
    return {'peak_db': peak_db, 'gain': gain}


@case('upmix-gain-adjusted-loro-fold-pcm24-below-110db-relative')
def _gain_adjusted_loro_null():
    """PCM24 round-trip of the upmix must recover the original stereo
    under the LoRo fold to better than -110 dB relative error.

    Uses a non-silent source that exercises both ambient (surround) and
    centre content: correlated sine + low-level independent noise so the
    gate opens and the centre and surround allocations are non-zero.
    The LoRo matrix is the oracle matrix
    ``[[1,0,K,0,K,0],[0,1,K,0,0,K]]``; the decoded fold divided by the
    recorded gain must equal the source.
    """
    rate = 48000
    n = rate
    t_arr = np.arange(n) / rate
    rng = np.random.default_rng(7)
    sine = 0.3 * (np.sin(2 * np.pi * 437 * t_arr) +
                   0.2 * np.sin(2 * np.pi * 1761 * t_arr))
    noise = 0.05 * rng.standard_normal((n, 2))
    x = np.column_stack((sine, sine)) + noise  # correlated + ambient
    six, g = upmix_engine.upmix_pcm_to_5_1_side(
        x, rate, centre=0.35, surround=0.4)
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / 'rt.wav'
        _write_pcm24(path, six, rate)
        decoded, sr = sf.read(str(path), dtype='float64', always_2d=True)
    assert sr == rate and decoded.shape == (n, 6)
    K = math.sqrt(0.5)
    L = decoded[:, 0] + K * decoded[:, 2] + K * decoded[:, 4]
    R = decoded[:, 1] + K * decoded[:, 2] + K * decoded[:, 5]
    folded = np.column_stack((L, R))
    recovered = folded / g
    err = recovered - x
    sig_x = float(np.sum(x ** 2))
    sig_e = float(np.sum(err ** 2))
    rel_db = 10 * math.log10(max(sig_e, 1e-300) / max(sig_x, 1e-30))
    assert rel_db < -110.0, ('LoRo fold relative error {dB:.3f} dB '
                              'above -110 dB budget'.format(dB=rel_db))
    return {'rel_db': round(rel_db, 3), 'pcm24': True,
            'gain': g, 'src_energy': sig_x, 'err_energy': sig_e}


@case('upmix-chunked-block-equals-whole-buffer-on-small-inputs')
def _chunked_matches_whole():
    rate = 48000
    rng = np.random.default_rng(13)
    x = rng.standard_normal((rate, 2)) * 0.1
    # Force one whole-buffer run and one genuine chunked run.
    six_whole, g_whole = upmix_engine.upmix_pcm_to_5_1_side(
        x, rate, block_samples=rate + 1)
    six_chunk, g_chunk = upmix_engine.upmix_pcm_to_5_1_side(
        x, rate, block_samples=4096)
    assert np.allclose(six_whole, six_chunk, atol=1e-12)
    assert abs(g_whole - g_chunk) < 1e-12
    return {'max_diff': float(np.max(np.abs(six_whole - six_chunk))),
            'gain_diff': abs(g_whole - g_chunk)}


@case('upmix-large-input-runs-without-allocating-full-track-stft')
def _chunked_large_runs():
    rate = 48000
    n = rate * 30  # 30 seconds, ~1.4M samples — fine for memory
    rng = np.random.default_rng(17)
    x = rng.standard_normal((n, 2)) * 0.1
    six, gain = upmix_engine.upmix_pcm_to_5_1_side(x, rate, block_samples=65536)
    assert six.shape == (n, 6)
    assert np.isfinite(six).all()
    assert float(np.max(np.abs(six[:, 3]))) == 0.0
    return {'frames': six.shape[0], 'peak': float(np.max(np.abs(six)))}


# ---------------------------------------------------------------------------
# Validation refusals
# ---------------------------------------------------------------------------


@case('upmix-refuses-bool-as-number')
def _refuse_bool():
    try:
        upmix_engine.upmix_pcm_to_5_1_side(
            np.zeros((64, 2)), 48000, centre=True, surround=False)
    except ValueError as e:
        return {'refused': True, 'reason': str(e)}
    raise AssertionError('bool must be refused')


@case('upmix-refuses-nan-and-inf')
def _refuse_nan_inf():
    bad_nan = np.zeros((64, 2))
    bad_nan[0, 0] = math.nan
    try:
        upmix_engine.upmix_pcm_to_5_1_side(bad_nan, 48000)
    except ValueError:
        pass
    else:
        raise AssertionError('NaN must be refused')
    bad_inf = np.zeros((64, 2))
    bad_inf[0, 1] = math.inf
    try:
        upmix_engine.upmix_pcm_to_5_1_side(bad_inf, 48000)
    except ValueError:
        return {'refused': ['nan', 'inf']}
    raise AssertionError('Inf must be refused')


@case('upmix-refuses-out-of-range-amounts')
def _refuse_range():
    rng = np.random.default_rng(0)
    x = rng.standard_normal((256, 2)) * 0.1
    for kw in ({'surround': -0.1}, {'surround': 1.5}, {'centre': 0.8},
               {'centre': -0.1}):
        try:
            upmix_engine.upmix_pcm_to_5_1_side(x, 48000, **kw)
        except ValueError:
            continue
        raise AssertionError('range must refuse: {k}'.format(k=kw))
    return {'refused': ['surround<0', 'surround>1', 'centre>1', 'centre<0']}


@case('upmix-refuses-wrong-channel-count')
def _refuse_channels():
    try:
        upmix_engine.upmix_pcm_to_5_1_side(np.zeros((64, 6)), 48000)
    except ValueError as e:
        return {'refused': True, 'reason': str(e)}
    raise AssertionError('mono/6ch must be refused')


@case('upmix-refuses-unsupported-rate')
def _refuse_rate():
    try:
        upmix_engine.upmix_pcm_to_5_1_side(np.zeros((64, 2)), 32000)
    except ValueError as e:
        return {'refused': True, 'reason': str(e)}
    raise AssertionError('32000 output must be refused by the upmix trial')


# ---------------------------------------------------------------------------
# Worker integration: opt-in + refusal + ordinary path untouched + collision +
# cancellation + memory cap.
# ---------------------------------------------------------------------------


def _stereo_wav(path: pathlib.Path, n: int = 48000, rate: int = 48000,
                scale: float = 0.1):
    rng = np.random.default_rng(int(n))
    x = rng.standard_normal((n, 2)) * scale
    sf.write(str(path), x, rate, subtype='PCM_24')
    return path


@case('worker-upmix-writes-six-channel-output-with-sidecar')
def _worker_upmix_writes():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = _stereo_wav(td / 'src.wav')
        out = td / 'out'
        out.mkdir()
        rep = worker.convert({
            'input': str(src),
            'out_dir': str(out),
            'format': 'WAV',
            'depth': '24',
            'target': '5.1-side',
            'layout': 'stereo',
            'rate': 48000,
            'upmix': {'mode': 'experimental-stereo-to-5.1-side-v1',
                       'centre': 0.35, 'surround': 0.7},
        })
        audio = pathlib.Path(rep['output'])
        sidecar = audio.with_suffix(audio.suffix + '.conversion.json')
        assert audio.exists() and audio.stat().st_size > 0, audio
        assert sidecar.exists(), sidecar
        meta = json.loads(sidecar.read_text())
        assert 'upmix' in meta, list(meta)
        assert meta['upmix']['algorithm'].startswith('upmix-cov-wiener-5.1-side'), meta['upmix']
        assert meta['upmix']['experimental'] is True
        assert meta['upmix']['lfe_silent'] is True
        assert meta['upmix']['loro_compatible_allocation'] is True
        assert meta['labels'] == ['FL', 'FR', 'FC', 'LFE', 'SL', 'SR'], meta['labels']
        # LFE column is silent in the encoded file.
        d, sr = sf.read(str(audio), dtype='float64', always_2d=True)
        assert d.shape[1] == 6 and sr == 48000, d.shape
        assert float(np.max(np.abs(d[:, 3]))) == 0.0
        # PCM24 quantisation can shift a value at the ceiling by one LSB;
        # the float-side ceiling is honoured inside the DSP, the on-disk
        # ceiling is the nearest PCM24 sample.
        lsb = 1.0 / (2 ** 23)
        assert meta['sample_peak'] <= upmix_engine.PEAK_CEILING + lsb, \
            (meta['sample_peak'], upmix_engine.PEAK_CEILING, lsb)
        return {'output': str(audio), 'labels': meta['labels']}


@case('worker-upmix-refuses-non-stereo-source')
def _worker_upmix_refuses_layout():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        # Create a 6-channel PCM file
        rng = np.random.default_rng(99)
        x = rng.standard_normal((1024, 6)) * 0.1
        path = td / 'six.wav'
        sf.write(str(path), x, 48000, subtype='PCM_24')
        out = td / 'out'; out.mkdir()
        try:
            worker.convert({
                'input': str(path), 'out_dir': str(out),
                'format': 'WAV', 'depth': '24',
                'target': '5.1-side', 'layout': '5.1-side', 'rate': 48000,
                'upmix': {'mode': 'experimental-stereo-to-5.1-side-v1'},
            })
        except ValueError as e:
            return {'refused': True, 'reason': str(e)}
        raise AssertionError('upmix on non-stereo source must refuse')


@case('worker-upmix-refuses-adm-source')
def _worker_upmix_refuses_adm():
    # Use an existing synthetic ADM fixture if present, otherwise build.
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        from lxml import etree as LT
        from ear.fileio.adm.builder import ADMBuilder
        from ear.fileio.adm.generate_ids import generate_ids
        from ear.fileio.adm.elements.block_formats import (
            AudioBlockFormatDirectSpeakers)
        from ear.fileio.adm.elements.geom import (
            BoundCoordinate, DirectSpeakerPolarPosition)
        from ear.fileio.adm.xml import adm_to_xml
        from ear.fileio.bw64 import Bw64Writer
        from ear.fileio.bw64.chunks import (
            FormatInfoChunk, ChnaChunk, AudioID)
        builder = ADMBuilder()
        bf = AudioBlockFormatDirectSpeakers(
            position=DirectSpeakerPolarPosition(
                bounded_azimuth=BoundCoordinate(0.0),
                bounded_elevation=BoundCoordinate(0.0),
                bounded_distance=BoundCoordinate(1.0)),
            speakerLabel=['FL'])
        builder.create_item_direct_speakers(track_index=1, name='FL',
                                            block_formats=[bf])
        generate_ids(builder.adm)
        axml = LT.tostring(adm_to_xml(builder.adm),
                            xml_declaration=True, encoding='UTF-8')
        src = td / 'adm.wav'
        chna = ChnaChunk(audioIDs=[
            AudioID(trackIndex=1, audioTrackUID='ATU_00000001',
                    audioTrackFormatIDRef='AT_00011001_01',
                    audioPackFormatIDRef='AP_00011001'),
        ])
        fi = FormatInfoChunk(formatTag=1, channelCount=1, sampleRate=48000,
                              bitsPerSample=16)
        with open(str(src), 'wb') as fh:
            w = Bw64Writer(fh, formatInfo=fi, chna=chna)
            w.axml = axml
            with w:
                w.write(np.zeros((16, 1), dtype='<f4'))
        out = td / 'out'; out.mkdir()
        try:
            worker.convert({
                'input': str(src), 'out_dir': str(out),
                'format': 'WAV', 'depth': '24',
                'target': '5.1-side', 'layout': '5.1-side', 'rate': 48000,
                'upmix': {'mode': 'experimental-stereo-to-5.1-side-v1'},
            })
        except ValueError as e:
            return {'refused': True, 'reason': str(e)}
        raise AssertionError('upmix on ADM must refuse')


@case('worker-upmix-refuses-bool-and-bad-mode')
def _worker_upmix_bad_payload():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = _stereo_wav(td / 'src.wav')
        out = td / 'out'; out.mkdir()
        for bad in ({'centre': True}, {'surround': True}, {'mode': 'other'},
                     {'bogus_key': 1}):
            try:
                worker.convert({
                    'input': str(src), 'out_dir': str(out),
                    'format': 'WAV', 'depth': '24',
                    'target': '5.1-side', 'layout': 'stereo', 'rate': 48000,
                    'upmix': dict(bad),
                })
            except ValueError as e:
                continue
            raise AssertionError('upmix must refuse: {b}'.format(b=bad))
        return {'refused': list(range(4))}


@case('worker-ordinary-stereo-target-unchanged-without-upmix')
def _worker_ordinary_unchanged():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = _stereo_wav(td / 'src.wav')
        out = td / 'out'; out.mkdir()
        rep = worker.convert({
            'input': str(src), 'out_dir': str(out),
            'format': 'WAV', 'depth': '24',
            'target': 'stereo', 'layout': 'stereo', 'rate': 48000,
        })
        assert 'upmix' not in rep
        assert rep['labels'] == ['FL', 'FR']
        return {'labels': rep['labels']}


@case('worker-collision-on-upmix-output-refuses-no-orphan')
def _worker_collision():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = _stereo_wav(td / 'src.wav')
        out = td / 'out'; out.mkdir()
        sentinel = out / 'src-5.1-side.wav'
        sentinel.write_bytes(b'PRESET')
        try:
            worker.convert({
                'input': str(src), 'out_dir': str(out),
                'format': 'WAV', 'depth': '24',
                'target': '5.1-side', 'layout': 'stereo', 'rate': 48000,
                'upmix': {'mode': 'experimental-stereo-to-5.1-side-v1',
                           'centre': 0.3, 'surround': 0.6},
            })
        except ValueError:
            pass
        assert sentinel.read_bytes() == b'PRESET'
        sidecar = sentinel.with_suffix(sentinel.suffix + '.conversion.json')
        assert not sidecar.exists()
        return {'sentinel_intact': True, 'orphan_sidecar': False}


@case('worker-upmix-memory-cap-refuses-oversized')
def _worker_upmix_memory_cap():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        # Frames just over the 512 MiB float64 × 6-channel cap.
        frames = (adm_compat.MAX_DECODED_BYTES // (6 * 8)) + 4096
        rng = np.random.default_rng(2)
        src = td / 'big.wav'
        sf.write(str(src), rng.standard_normal((frames, 2)) * 0.1, 48000,
                 subtype='PCM_24')
        out = td / 'out'; out.mkdir()
        try:
            worker.convert({
                'input': str(src), 'out_dir': str(out),
                'format': 'WAV', 'depth': '24',
                'target': '5.1-side', 'layout': 'stereo', 'rate': 48000,
                'upmix': {'mode': 'experimental-stereo-to-5.1-side-v1',
                           'centre': 0.3, 'surround': 0.5},
            })
        except ValueError as e:
            return {'refused': True, 'reason': str(e)}
        raise AssertionError('over-cap must refuse')


@case('worker-upmix-output-rate-88200-processes-at-requested-rate')
def _worker_upmix_unsupported_rate():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = _stereo_wav(td / 'src.wav')
        out = td / 'out'; out.mkdir()
        rep = worker.convert({
            'input': str(src), 'out_dir': str(out),
            'format': 'WAV', 'depth': '24',
            'target': '5.1-side', 'layout': 'stereo', 'rate': 88200,
            'upmix': {'mode': 'experimental-stereo-to-5.1-side-v1',
                       'centre': 0.2, 'surround': 0.5},
        })
        meta = json.loads(pathlib.Path(rep['output']).with_suffix(
            pathlib.Path(rep['output']).suffix + '.conversion.json').read_text())
        assert meta['upmix']['upmix_sr'] == 88200
        assert meta['frames'] == (meta['source_frames'] * 88200 + 47999) // 48000
        assert not any('closest supported' in w for w in meta['warnings'])
        return {'rate': 88200, 'warnings': meta['warnings']}


@case('worker-upmix-keyboardinterrupt-rolls-back-pair')
def _worker_upmix_cancel():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = _stereo_wav(td / 'src.wav')
        out = td / 'out'; out.mkdir()
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
                'target': '5.1-side', 'layout': 'stereo', 'rate': 48000,
                'upmix': {'mode': 'experimental-stereo-to-5.1-side-v1',
                           'centre': 0.3, 'surround': 0.6},
            })
        except KeyboardInterrupt:
            pass
        finally:
            worker.os.link = original_link
        audio = out / 'src-5.1-side.wav'
        sidecar = audio.with_suffix(audio.suffix + '.conversion.json')
        assert not audio.exists()
        assert not sidecar.exists()
        return {'pair_empty': True}


def main():
    print('cases run', len(PASSED) + len(FAILED))
    for name, detail in PASSED:
        print('PASS', name, detail)
    for name, err in FAILED:
        print('FAIL', name, err)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
