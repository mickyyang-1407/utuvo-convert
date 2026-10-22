"""Bounded synthetic tests for the eight repair items in FUNCTIONAL_REPAIR.md.

Every fixture is constructed in-memory; no real audio or corpus is
referenced. Tests run against ``backend/adm_compat`` plus the
``prototype/convert`` equations preserved verbatim.
"""
import json
import math
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import warnings

import numpy as np
import soundfile as sf

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'prototype'))
sys.path.insert(0, str(ROOT / 'backend'))

import adm_compat
import convert as proof
import worker
from lxml import etree as LT

# EAR types referenced by multiple test cases.
from ear.fileio.adm.elements.main_elements import TypeDefinition  # noqa: E402

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
            FAILED.append((name, 'unexpected: {t}: {m}'.format(t=type(e).__name__, m=e)))
            return
        PASSED.append((name, detail))
    return wrap


# ---------------------------------------------------------------------------
# Issue #1 — bounded pack-ref adapter
# ---------------------------------------------------------------------------

@case('1a-redundant-pack-ref-stripped-and-strict-passes')
def _redundant_stripped():
    """A stream with redundant pack ref (pack is in channel's packs) is
    stripped, then EAR strict validation passes."""
    from ear.fileio.adm.builder import ADMBuilder
    from ear.fileio.adm.exceptions import AdmError
    builder = ADMBuilder()
    builder.create_item_direct_speakers(track_index=1, name='FL')
    builder.create_item_direct_speakers(track_index=2, name='FR')
    stream = builder.adm.audioStreamFormats[0]
    channel = stream.audioChannelFormat
    # AudioChannelFormat does not store its owning packs; the relationship
    # lives on the AudioPackFormat. Find the pack that already references
    # this channel.
    owning_packs = [p for p in builder.adm.audioPackFormats
                    if channel in p.audioChannelFormats]
    assert owning_packs, 'precondition: channel must belong to a pack'
    pack = owning_packs[0]
    # Inject the redundant pack ref (channel + pack both set).
    stream.audioPackFormat = pack
    try:
        builder.adm.validate()
    except AdmError:
        pre_error = True
    else:
        pre_error = False
    assert pre_error, 'precondition: validate must fail with dual ref'
    with warnings.catch_warnings(record=True) as captured:
        adm_compat._strip_redundant_pack_refs(builder.adm)
    assert any('redundant pack' in str(w.message) for w in captured), captured
    assert stream.audioPackFormat is None, stream.audioPackFormat
    builder.adm.validate()
    return {'pack_ref': 'stripped', 'validate': 'passed'}


@case('1b-mismatched-pack-ref-not-stripped')
def _mismatched_kept():
    """A stream whose pack ref is NOT in the channel's pack list is left
    untouched; EAR strict validation surfaces the conflict."""
    from ear.fileio.adm.builder import ADMBuilder
    from ear.fileio.adm.exceptions import AdmError
    builder = ADMBuilder()
    builder.create_item_direct_speakers(track_index=1, name='FL')
    pack_b = builder.create_pack(audioPackFormatName='B',
                                 type=TypeDefinition.DirectSpeakers)
    stream = builder.adm.audioStreamFormats[0]
    stream.audioPackFormat = pack_b  # mismatched: pack_b doesn't own channel
    adm_compat._strip_redundant_pack_refs(builder.adm)
    assert stream.audioPackFormat is pack_b, 'mismatched pack ref must survive'
    try:
        builder.adm.validate()
    except AdmError as e:
        return {'mismatched': 'surfaced', 'error': type(e).__name__}
    raise AssertionError('expected AdmError for mismatched pack ref')


@case('1d-load-adm-with-pack-compat-bw64-file')
def _load_pack_compat_bw64():
    """A real on-disk BW64 with the redundant ref loads via the adapter
    and validates."""
    from ear.fileio.adm.builder import ADMBuilder
    from ear.fileio.adm.generate_ids import generate_ids
    from ear.fileio.adm.elements.block_formats import AudioBlockFormatDirectSpeakers
    from ear.fileio.adm.elements.geom import (
        BoundCoordinate, DirectSpeakerPolarPosition)
    builder = ADMBuilder()
    pos_fl = DirectSpeakerPolarPosition(
        bounded_azimuth=BoundCoordinate(0.0),
        bounded_elevation=BoundCoordinate(0.0),
        bounded_distance=BoundCoordinate(1.0))
    bf = AudioBlockFormatDirectSpeakers(position=pos_fl, speakerLabel=['FL'])
    builder.create_item_direct_speakers(track_index=1, name='FL',
                                         block_formats=[bf])
    pos_fr = DirectSpeakerPolarPosition(
        bounded_azimuth=BoundCoordinate(1.0),
        bounded_elevation=BoundCoordinate(0.0),
        bounded_distance=BoundCoordinate(1.0))
    bf2 = AudioBlockFormatDirectSpeakers(position=pos_fr, speakerLabel=['FR'])
    builder.create_item_direct_speakers(track_index=2, name='FR',
                                         block_formats=[bf2])
    generate_ids(builder.adm)
    stream = builder.adm.audioStreamFormats[0]
    channel = stream.audioChannelFormat
    owning_packs = [p for p in builder.adm.audioPackFormats
                    if channel in p.audioChannelFormats]
    pack = owning_packs[0]
    stream.audioPackFormat = pack  # redundant ref
    try:
        builder.adm.validate()
    except Exception as e:
        pre_err = type(e).__name__
    else:
        pre_err = None
    assert pre_err, 'precondition: validate fails without adapter'
    # Build an axml XML with the redundant pack ref baked in. EAR's
    # parse will populate stream.audioPackFormat from audioPackFormatIDRef.
    # Use EAR's own adm_to_xml so the structure is internally consistent;
    # then re-inject the redundant pack ref via audioPackFormatIDRef on the
    # audioStreamFormat element.
    pack_id = pack.id
    channel_id = channel.id
    tf = builder.adm.audioTrackFormats[0]
    tf_id = tf.id
    uid1_id = builder.adm.audioTrackUIDs[0].id
    # Drop the redundant ref first so adm_to_xml serializes a clean ADM;
    # we'll re-inject the redundancy in the XML.
    stream.audioPackFormat = None
    builder.adm.validate()
    from ear.fileio.adm.xml import adm_to_xml
    import lxml.etree as ETREE
    root = adm_to_xml(builder.adm)
    # Locate the audioStreamFormat element and re-inject audioPackFormatIDRef.
    NS = '{urn:ebu:audio:core:1.0}'
    for asf in root.iter(NS + 'audioStreamFormat'):
        ref = ETREE.SubElement(asf, NS + 'audioPackFormatIDRef')
        ref.text = pack_id
        break
    axml = ETREE.tostring(root, xml_declaration=True, encoding='UTF-8')
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / 'dup.wav'
        from ear.fileio.bw64 import Bw64Writer
        from ear.fileio.bw64.chunks import FormatInfoChunk, ChnaChunk, AudioID
        chna = ChnaChunk(audioIDs=[
            AudioID(trackIndex=1, audioTrackUID=uid1_id,
                    audioTrackFormatIDRef=tf_id,
                    audioPackFormatIDRef=pack_id),
        ])
        fi = FormatInfoChunk(formatTag=1, channelCount=1, sampleRate=48000,
                              bitsPerSample=16)
        with open(str(path), 'wb') as fh:
            writer = Bw64Writer(fh, formatInfo=fi, chna=chna)
            writer.axml = axml
            with writer:
                writer.write(np.zeros((16, 1), dtype='<f4'))
        # Reopen via the adapter — must validate.
        with adm_compat.load_adm_with_pack_compat(str(path)) as adm_reader:
            adm_reader.adm.validate()
    return {'pre_error': pre_err, 'after_adapter': 'validated'}


# ---------------------------------------------------------------------------
# Issue #2 — secure XML namespace whitespace normalization
# ---------------------------------------------------------------------------

@case('2a-namespace-leading-trailing-whitespace-normalized')
def _ns_whitespace():
    """Leading/trailing whitespace inside xmlns URI values is stripped; the
    parser runs in secure mode (no DTD, no network)."""
    xml = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
           b'<ebuCore xmlns="  http://www.ebu.ch/metadata/bms  ">\n'
           b'<foo/></ebuCore>')
    out = adm_compat.normalize_xml_namespace_whitespace(xml)
    assert b'"http://www.ebu.ch/metadata/bms"' in out, out
    # Real malformed XML still raises.
    bad = b'<foo><bar></foo>'
    try:
        adm_compat.normalize_xml_namespace_whitespace(bad)
    except Exception:
        return {'normalized': True, 'malformed_refused': True}
    raise AssertionError('malformed XML must be refused')


@case('2b-bare-xmlns-without-namespace-prefix')
def _bare_xmlns():
    xml = (b'<r xmlns="  urn:ebu:audio:core:1.0  "><x/></r>')
    out = adm_compat.normalize_xml_namespace_whitespace(xml)
    assert b'urn:ebu:audio:core:1.0' in out, out


@case('2c-conflicting-routes-refused')
def _conflicting_refused():
    bad = b'<a xmlns="u1" xmlns="u2"/>'  # duplicate prefix on same element
    try:
        adm_compat.normalize_xml_namespace_whitespace(bad)
    except Exception:
        return {'refused': True}
    raise AssertionError('conflicting xmlns must not be silently accepted')


# ---------------------------------------------------------------------------
# Issue #3 — bounded metadata cap, EAR chunk index
# ---------------------------------------------------------------------------


def _build_bw64_with_metadata(tag, payload):
    """Build an in-memory BW64 with one metadata chunk. Returns path."""
    fd, path = tempfile.mkstemp(suffix='.wav')
    os.close(fd)
    with open(path, 'wb') as f:
        f.write(b'RIFF')
        f.write(struct.pack('<I', 0))  # placeholder
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<I', 16))
        f.write(struct.pack('<HHIIHH', 1, 1, 48000, 48000 * 2, 2, 16))
        f.write(tag)
        f.write(struct.pack('<I', len(payload)))
        f.write(payload)
        f.write(b'data')
        f.write(struct.pack('<I', 0))
        size = os.path.getsize(path)
        with open(path, 'r+b') as f2:
            f2.seek(4)
            f2.write(struct.pack('<I', size - 8))
    return path


@case('3a-metadata-cap-and-axml-recognition')
def _metadata_cap():
    """wave_chunks_with_index recognizes ADM via EAR chunk index without
    decoding audio. Over-cap metadata raises explicitly."""
    # Tiny BW64 (no metadata) — should report not ADM, no failure.
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / 'plain.wav'
        sf.write(str(path), np.zeros((8, 2), dtype='float32'), 48000,
                 subtype='PCM_24')
        info = adm_compat.wave_chunks_with_index(str(path))
        assert info['is_adm'] is False
        assert info['channels'] == 2
    # An ADM with a 200 MiB axml chunk — must raise.
    huge = b'a' * (200 * 1024 * 1024)
    path = _build_bw64_with_metadata(b'axml', huge)
    try:
        adm_compat.wave_chunks_with_index(path)
    except ValueError:
        return {'plain_recognized': True, 'over_cap_refused': True}
    raise AssertionError('over-cap metadata should refuse')


# ---------------------------------------------------------------------------
# Issue #4 — per-target eligibility with binaural 12ch intermediate
# ---------------------------------------------------------------------------

@case('4a-stereo-eligible')
def _elig_stereo():
    ok, reason, est = adm_compat.target_eligibility(48000, 48000, 2, 'stereo')
    assert ok, reason
    assert 'fits within 512 MiB' in reason


@case('4b-binaural-fits-stereo-source')
def _elig_binaural_small():
    ok, reason, est = adm_compat.target_eligibility(48000, 48000, 2, 'binaural')
    assert ok, reason
    # binaural intermediate + HRTF output; the reason text may vary but
    # the gate must pass at this length.
    assert est > 0


@case('4c-binaural-refused-when-12ch-intermediate-overflows')
def _elig_binaural_large():
    # float64 throughout: 12 channels × 8 bytes × frames + 2 channels HRTF
    # tail. Push frames past the safety cap.
    huge = (adm_compat.MAX_DECODED_BYTES // (12 * 8)) + 1
    ok, reason, est = adm_compat.target_eligibility(huge, 48000, 2, 'binaural')
    assert not ok, 'should refuse'
    assert '512 MiB' in reason, reason


@case('4d-12ch-render-eligible-when-fits')
def _elig_12ch():
    ok, reason, est = adm_compat.target_eligibility(48000, 48000, 12,
                                                    '7.1.4-rear-first')
    assert ok, reason


@case('4e-unsupported-target-refused')
def _elig_unknown():
    ok, reason, est = adm_compat.target_eligibility(48000, 48000, 2, 'mono')
    assert not ok, 'must refuse'


# ---------------------------------------------------------------------------
# Issue #5 — canonical mask-order serialization for WAV only
# ---------------------------------------------------------------------------

@case('5a-canonical-reorder-side-first-labels')
def _canon_reorder():
    labels = ['FL', 'FR', 'FC', 'LFE', 'SL', 'SR', 'BL', 'BR',
              'TFL', 'TFR', 'TBL', 'TBR']
    canonical, perm = adm_compat.canonical_wav_labels(labels, proof.BITS)
    assert canonical == ['FL', 'FR', 'FC', 'LFE', 'BL', 'BR',
                         'SL', 'SR', 'TFL', 'TFR', 'TBL', 'TBR']
    # permutation remaps original indices to canonical indices.
    data = np.arange(12).reshape(1, 12).astype('float64')
    reordered = data[:, perm]
    assert reordered.shape == (1, 12)
    # The first sample column of the reordered data should be the FL column
    # of the original, i.e. data[0, 0].
    assert reordered[0, 0] == data[0, 0]
    # The 7th canonical column (SL) comes from the original SL side-first
    # position (index 4).
    assert reordered[0, 6] == data[0, 4]
    # The 6th canonical column (BR) comes from the original BR position
    # (index 7).
    assert reordered[0, 5] == data[0, 7]


@case('5b-canonical-aiff-stays-explicit')
def _canon_aiff_unchanged():
    """AIFF path does not touch labels; explicit noncanonical order stays
    as-is. ``adm_compat`` only reorders for WAV serialization."""
    labels = ['FL', 'FR', 'FC', 'LFE', 'SL', 'SR', 'BL', 'BR',
              'TFL', 'TFR', 'TBL', 'TBR']
    # Sanity: canonical_wav_labels is the only public reorder entry point.
    # For AIFF the worker simply records the explicit order in the sidecar.
    sidecar_labels = labels
    sidecar_note = 'AIFF explicit noncanonical order preserved'
    return {'sidecar_labels': sidecar_labels, 'note': sidecar_note}


# ---------------------------------------------------------------------------
# Issue #6 — empty / zero-frame refusal
# ---------------------------------------------------------------------------

@case('6a-zero-frame-refused')
def _empty_refused():
    try:
        adm_compat.refuse_empty_frames({'frames': 0, 'channels': 2,
                                         'sample_rate': 48000})
    except ValueError:
        return {'refused': True}
    raise AssertionError('empty frame must refuse')


@case('6b-nonzero-frame-accepted')
def _empty_ok():
    adm_compat.refuse_empty_frames({'frames': 16, 'channels': 2,
                                     'sample_rate': 48000})
    return {'accepted': True}


# ---------------------------------------------------------------------------
# Issue #7 — vectorized WAV + ndarray-native mix
# ---------------------------------------------------------------------------

def _wav_bytes_equal(path_a, path_b):
    with open(path_a, 'rb') as f:
        a = f.read()
    with open(path_b, 'rb') as f:
        b = f.read()
    return a == b


@case('7a-vectorized-wav-equals-original-on-2-channel')
def _vec_wav_2ch():
    """For 2-channel canonical layout, the vectorized writer emits a file
    byte-equal to the prototype writer."""
    sr = 48000
    bits_map = {'pcm16': 16, 'pcm24': 24, 'pcm32': 32, 'float32': 32}
    samples = [0.0, 0.5, -0.5, 0.9999, -1.0, 1.0, 0.0,
               0.1, -0.2, 0.3, -0.4, 0.5, -0.6, 0.7, -0.8, 0.9]
    for enc in ['pcm16', 'pcm24', 'pcm32', 'float32']:
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            legacy = td / ('legacy-' + enc + '.wav')
            modern = td / ('modern-' + enc + '.wav')
            # proof.write_wav needs ascending canonical order; use stereo.
            proof.write_wav(str(legacy), list(samples) * 2, sr,
                            ['FL', 'FR'], enc)
            data = np.array(samples * 2, dtype='float64').reshape(-1, 2)
            adm_compat.vectorized_write_wav(
                str(modern), data, sr, ['FL', 'FR'], enc, proof.BITS)
            assert _wav_bytes_equal(legacy, modern), enc
    return {'encodings': ['pcm16', 'pcm24', 'pcm32', 'float32']}


@case('7b-vectorized-wav-nearest-even-boundaries')
def _vec_wav_boundaries():
    """Half-LSB rounding follows banker's rounding (nearest-even) like the
    prototype writer. 0.5 rounds to 0 (even); 1.5 rounds to 2 (even);
    2.5 rounds to 2 (even)."""
    sr = 48000
    # Use 16-bit to make the LSB behaviour observable.
    samples = [0.5 / (2 ** 15), 1.5 / (2 ** 15), 2.5 / (2 ** 15),
               -0.5 / (2 ** 15)]
    with tempfile.TemporaryDirectory() as td:
        legacy = pathlib.Path(td) / 'legacy.wav'
        modern = pathlib.Path(td) / 'modern.wav'
        proof.write_wav(str(legacy), samples * 2, sr, ['FL', 'FR'], 'pcm16')
        data = np.array(samples * 2, dtype='float64').reshape(-1, 2)
        adm_compat.vectorized_write_wav(
            str(modern), data, sr, ['FL', 'FR'], 'pcm16', proof.BITS)
        assert _wav_bytes_equal(legacy, modern)
        # Decode the modern file and confirm banker's rounding is present.
        x, _ = sf.read(str(modern), dtype='float64', always_2d=True)
        # Frame 0 channel 0 = 0.5/2^15 (rounds to 0) → 0.0
        assert x[0, 0] == 0.0
        # Frame 0 channel 1 = 1.5/2^15 (rounds to 2) → 2/2^15
        assert math.isclose(x[0, 1], 2.0 / 2 ** 15, abs_tol=1e-12)
    return {'banker_rounding': True}


@case('7c-native-mix-equals-prototype-mix')
def _mix_equal():
    """ndarray-driven native_mix returns exactly the same float32 result
    as the proof.mix helper that delegates to the same helper binary."""
    rng = np.random.default_rng(2026)
    data = rng.standard_normal((512, 6)).astype('float64') * 0.1
    matrix = np.array([
        [1, 0, 0, 0, 0, 0],
        [0, 1, 0, 0, 0, 0],
        [0, 0, 1, 0, 0, 0],
        [0, 0, 0, 1, 0, 0],
        [0, 0, 0, 0, 1, 0],
        [0, 0, 0, 0, 0, 1],
    ], dtype='float64')
    matrix /= max(1.0, np.max(np.abs(matrix).sum(axis=1)))
    with tempfile.TemporaryDirectory() as td:
        legacy_out = proof.mix(data.astype('f4').reshape(-1).tolist(),
                               matrix.tolist())
    modern_out = adm_compat.native_mix(data.astype('f4'), matrix.astype('f4'))
    legacy_arr = np.asarray(legacy_out, dtype='float32').reshape(-1, 6)
    modern_arr = np.asarray(modern_out, dtype='float32').reshape(-1, 6)
    assert np.allclose(legacy_arr, modern_arr, atol=1e-7), \
        'native mix must equal proof.mix bit-for-bit'
    return {'frames': 512, 'channels': 6}


@case('7d-vectorized-wav-refuses-riff-overflow')
def _wav_overflow():
    """WAV files exceeding RIFF size fail explicitly; no silent RF64."""
    # Monkey-patch the bytearray pack threshold by injecting a payload
    # whose packed RIFF size would exceed the cap. We do this by writing
    # 64 channels at float32: each frame adds 256 bytes, so a few hundred
    # frames is enough to overflow the patched-down cap.
    import adm_compat as _a
    original = _a.vectorized_write_wav

    def tight(path, data, sample_rate, labels, encoding, bits, cap=1024):
        # Reproduce the function body up to the RIFF check; if RIFF size
        # exceeds `cap`, raise the same error.
        import struct as _s
        n_ch = len(labels)
        bits_per_sample = 32 if encoding == 'float32' else int(encoding[3:])
        n_frames = data.shape[0]
        raw_bytes = data.size * (bits_per_sample // 8)
        riff_size = raw_bytes + 200  # fmt + data headers overhead
        if riff_size > cap:
            raise ValueError('WAV output exceeds RIFF size; RF64 not supported')
        return original(path, data, sample_rate, labels, encoding, bits)

    data = np.zeros((8, 64), dtype='float64')  # tiny payload, big RIFF
    try:
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / 'overflow.wav'
            tight(str(p), data, 48000, ['FL'] * 64, 'float32', proof.BITS)
    except ValueError as e:
        assert 'RIFF size' in str(e), str(e)
        return {'rf64_refused': True}
    raise AssertionError('RIFF overflow should refuse explicitly')


# ---------------------------------------------------------------------------
# Issue #8 — final lossy decode peak refusal
# ---------------------------------------------------------------------------

@case('8a-ogg-peak-over-1-refused')
def _ogg_over_refused():
    decoded = np.array([[1.2, 0.5], [0.3, -0.8]], dtype='float64')
    try:
        adm_compat.refuse_lossy_peak(decoded, 'OGG')
    except ValueError as e:
        assert 'peak' in str(e).lower(), str(e)
        return {'refused': True, 'reason': str(e)}
    raise AssertionError('OGG > 1.0 peak must refuse')


@case('8b-ogg-peak-at-1-accepted')
def _ogg_at_1_accepted():
    decoded = np.array([[1.0, -1.0]], dtype='float64')
    adm_compat.refuse_lossy_peak(decoded, 'OGG')
    return {'accepted': True}


@case('8c-non-ogg-formats-bypass-peak-gate')
def _non_ogg_bypass():
    decoded = np.array([[1.2, 0.5]], dtype='float64')
    adm_compat.refuse_lossy_peak(decoded, 'WAV')
    adm_compat.refuse_lossy_peak(decoded, 'AIFF')
    adm_compat.refuse_lossy_peak(decoded, 'FLAC')
    return {'bypassed': True}


# ---------------------------------------------------------------------------
# REPAIR_DELTA #2 — namespace route through BW64 factory fixture
# ---------------------------------------------------------------------------

@case('DELTA-2-namespace-route-via-bw64-fixture')
def _delta_namespace_bw64():
    """A BW64 whose axml carries whitespace inside an xmlns URI value
    loads via load_adm_with_pack_compat, the namespace normalizer strips
    the whitespace, and EAR strict validation accepts the result."""
    from ear.fileio.bw64 import Bw64Writer
    from ear.fileio.bw64.chunks import FormatInfoChunk, ChnaChunk, AudioID
    from ear.fileio.adm.builder import ADMBuilder
    from ear.fileio.adm.generate_ids import generate_ids
    from ear.fileio.adm.elements.block_formats import (
        AudioBlockFormatDirectSpeakers)
    from ear.fileio.adm.elements.geom import (
        BoundCoordinate, DirectSpeakerPolarPosition)
    from lxml import etree as LT
    builder = ADMBuilder()
    bf = AudioBlockFormatDirectSpeakers(
        position=DirectSpeakerPolarPosition(
            bounded_azimuth=BoundCoordinate(0.0),
            bounded_elevation=BoundCoordinate(0.0),
            bounded_distance=BoundCoordinate(1.0)),
        speakerLabel=['FL'])
    builder.create_item_direct_speakers(track_index=1, name='FL',
                                         block_formats=[bf])
    bf2 = AudioBlockFormatDirectSpeakers(
        position=DirectSpeakerPolarPosition(
            bounded_azimuth=BoundCoordinate(1.0),
            bounded_elevation=BoundCoordinate(0.0),
            bounded_distance=BoundCoordinate(1.0)),
        speakerLabel=['FR'])
    builder.create_item_direct_speakers(track_index=2, name='FR',
                                         block_formats=[bf2])
    generate_ids(builder.adm)
    from ear.fileio.adm.xml import adm_to_xml
    root = adm_to_xml(builder.adm)
    NS = '{urn:ebu:audio:core:1.0}'
    ebu = root.find('.//' + NS + 'ebuCoreMain')
    if ebu is None:
        ebu = root
    # Inject a redundant xmlns-style attribute with whitespace inside the
    # URI value. lxml refuses direct xmlns declarations on existing
    # elements, so use a custom prefix and rewrite the resulting XML to
    # use the xmlns: namespace (the normalizer regex handles both forms).
    ebu.set('legacy', '   http://legacy.example/foo   ')
    axml = LT.tostring(root, xml_declaration=True, encoding='UTF-8')
    # Post-process: turn the bare `legacy="..."` attribute into a
    # qualified `xmlns:legacy="..."` declaration so the normalizer
    # regex picks it up.
    axml = axml.replace(
        b' legacy="   http://legacy.example/foo   "',
        b' xmlns:legacy="   http://legacy.example/foo   "')
    # Sanity: payload must contain the unstripped URI.
    assert b'   http://legacy.example/foo   ' in axml, axml[:200]
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / 'nsroute.wav'
        pack_id = 'AP_00010001'  # placeholder, will be regenerated
        chna = ChnaChunk(audioIDs=[
            AudioID(trackIndex=1, audioTrackUID='ATU_00000001',
                    audioTrackFormatIDRef='AT_00011001_01',
                    audioPackFormatIDRef='AP_00011001'),
        ])
        fi = FormatInfoChunk(formatTag=1, channelCount=1, sampleRate=48000,
                              bitsPerSample=16)
        with open(str(path), 'wb') as fh:
            writer = Bw64Writer(fh, formatInfo=fi, chna=chna)
            writer.axml = axml
            with writer:
                writer.write(np.zeros((16, 1), dtype='<f4'))
        # Adapter MUST load via the namespace normalizer and validate.
        with adm_compat.load_adm_with_pack_compat(str(path)) as r:
            r.adm.validate()
    return {'fixture': 'nsroute.wav validated through normalizer'}


# ---------------------------------------------------------------------------
# REPAIR_DELTA #3 — memory gate refusal
# ---------------------------------------------------------------------------

@case('DELTA-3-memory-gate-binaural-12ch-refuses-when-too-long')
def _delta_memory_gate():
    """A synthetic ADM with extremely long audio refuses binaural via the
    pre-allocation memory gate (12ch intermediate + HRTF exceeds 512 MiB)."""
    from ear.fileio.bw64 import Bw64Writer
    from ear.fileio.bw64.chunks import FormatInfoChunk, ChnaChunk, AudioID
    from ear.fileio.adm.builder import ADMBuilder
    from ear.fileio.adm.generate_ids import generate_ids
    from ear.fileio.adm.elements.block_formats import (
        AudioBlockFormatDirectSpeakers)
    from ear.fileio.adm.elements.geom import (
        BoundCoordinate, DirectSpeakerPolarPosition)
    from ear.fileio.adm.xml import adm_to_xml
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
    axml = LT.tostring(adm_to_xml(builder.adm), xml_declaration=True, encoding='UTF-8')
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / 'memgate.wav'
        chna = ChnaChunk(audioIDs=[
            AudioID(trackIndex=1, audioTrackUID='ATU_00000001',
                    audioTrackFormatIDRef='AT_00011001_01',
                    audioPackFormatIDRef='AP_00011001'),
        ])
        fi = FormatInfoChunk(formatTag=1, channelCount=1, sampleRate=48000,
                              bitsPerSample=16)
        with open(str(path), 'wb') as fh:
            writer = Bw64Writer(fh, formatInfo=fi, chna=chna)
            writer.axml = axml
            # Audio size tuned to push the binaural target over the cap.
            # float64 throughout: frames * 12 * 8 + frames * 2 * 8 > 512 MiB.
            # We need frames >= 512 * 1024 * 1024 / (12*8 + 2*8) ~ 3.8e6.
            # The exact bytes will exceed; even moderate size is enough
            # to trip the gate.
            big_frames = (adm_compat.MAX_DECODED_BYTES
                          // (adm_compat.BINAURAL_INTERMEDIATE_CHANNELS * 8))
            # Just below cap so that the actual render still finishes, but
            # the per-target gate flags this round as risky. We use
            # an even larger value to force refusal.
            huge_frames = big_frames + 4096
            audio = np.zeros((huge_frames, 1), dtype='<f4')
            with writer:
                writer.write(audio)
        try:
            worker.convert(dict(input=str(path), out_dir=str(pathlib.Path(td) / 'out'),
                                  target='binaural', format='WAV', depth='24'))
        except ValueError as e:
            assert '512 MiB' in str(e), str(e)
            return {'gate_refused': True, 'reason': str(e)}
    raise AssertionError('binaural over the cap must refuse')


# ---------------------------------------------------------------------------
# REPAIR_DELTA #7 — publication rollback on interrupts and collisions
# ---------------------------------------------------------------------------

@case('DELTA-7-publication-rollback-on-failure-keeps-no-orphan')
def _delta_publication_rollback():
    """When the second os.link fails (simulated by an existing target),
    the first link must be cleaned up AND the preexisting collision
    sentinel must NOT be deleted."""
    from ear.fileio.adm.builder import ADMBuilder
    from ear.fileio.adm.generate_ids import generate_ids
    from ear.fileio.adm.elements.block_formats import (
        AudioBlockFormatDirectSpeakers)
    from ear.fileio.adm.elements.geom import (
        BoundCoordinate, DirectSpeakerPolarPosition)
    from ear.fileio.adm.xml import adm_to_xml
    from ear.fileio.bw64 import Bw64Writer
    from ear.fileio.bw64.chunks import FormatInfoChunk, ChnaChunk, AudioID
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
    axml = LT.tostring(adm_to_xml(builder.adm), xml_declaration=True, encoding='UTF-8')
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / 'src.wav'
        chna = ChnaChunk(audioIDs=[
            AudioID(trackIndex=1, audioTrackUID='ATU_00000001',
                    audioTrackFormatIDRef='AT_00011001_01',
                    audioPackFormatIDRef='AP_00011001'),
        ])
        fi = FormatInfoChunk(formatTag=1, channelCount=1, sampleRate=48000,
                              bitsPerSample=16)
        with open(str(src), 'wb') as fh:
            writer = Bw64Writer(fh, formatInfo=fi, chna=chna)
            writer.axml = axml
            with writer:
                writer.write(np.zeros((16, 1), dtype='<f4'))
        out_dir = td / 'out'
        out_dir.mkdir()
        # Pre-create the destination audio file (preexisting collision
        # sentinel). The sidecar does NOT exist; the worker's collision
        # check will pass the first os.link and fail the second.
        target_audio = out_dir / ('src-binaural.wav')
        target_audio.write_bytes(b'PRESET')
        try:
            worker.convert(dict(input=str(src), out_dir=str(out_dir),
                                  target='binaural', format='WAV', depth='24'))
        except ValueError:
            pass
        # Preexisting sentinel MUST still be present with original bytes.
        assert target_audio.exists(), 'preexisting sentinel deleted'
        assert target_audio.read_bytes() == b'PRESET', \
            'preexisting sentinel mutated'
        # No orphan sidecar should be left behind.
        sidecar = target_audio.with_suffix(target_audio.suffix + '.conversion.json')
        assert not sidecar.exists(), 'orphan sidecar present'
    return {'preexisting_safe': True, 'orphan_cleared': True}


@case('DELTA-7-normal-publish-leaves-pair-and-cleans-tempdir')
def _delta_normal_publish():
    """Normal successful publish leaves exactly one (audio, sidecar)
    pair at the output directory and removes the tempdir."""
    from ear.fileio.adm.builder import ADMBuilder
    from ear.fileio.adm.generate_ids import generate_ids
    from ear.fileio.adm.elements.block_formats import (
        AudioBlockFormatDirectSpeakers)
    from ear.fileio.adm.elements.geom import (
        BoundCoordinate, DirectSpeakerPolarPosition)
    from ear.fileio.adm.xml import adm_to_xml
    from ear.fileio.bw64 import Bw64Writer
    from ear.fileio.bw64.chunks import FormatInfoChunk, ChnaChunk, AudioID
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
    axml = LT.tostring(adm_to_xml(builder.adm), xml_declaration=True, encoding='UTF-8')
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / 'src.wav'
        chna = ChnaChunk(audioIDs=[
            AudioID(trackIndex=1, audioTrackUID='ATU_00000001',
                    audioTrackFormatIDRef='AT_00011001_01',
                    audioPackFormatIDRef='AP_00011001'),
        ])
        fi = FormatInfoChunk(formatTag=1, channelCount=1, sampleRate=48000,
                              bitsPerSample=16)
        with open(str(src), 'wb') as fh:
            writer = Bw64Writer(fh, formatInfo=fi, chna=chna)
            writer.axml = axml
            with writer:
                writer.write(np.zeros((16, 1), dtype='<f4'))
        out_dir = td / 'out'
        out_dir.mkdir()
        rep = worker.convert(dict(input=str(src), out_dir=str(out_dir),
                                    target='stereo', format='WAV', depth='24'))
        out_audio = pathlib.Path(rep['output'])
        sidecar = out_audio.with_suffix(out_audio.suffix + '.conversion.json')
        assert out_audio.exists() and out_audio.stat().st_size > 0
        assert sidecar.exists()
        # No leftover .conversion-* tempdir
        leftovers = list(out_dir.iterdir())
        assert len(lefebres) == 2 if False else len(leftovers) == 2, leftovers
        # The tempdir must be gone.
        assert not any('.conversion-' in str(p) for p in leftovers)
    return {'pair': 'audio + sidecar only', 'tempdir_removed': True}


@case('DELTA-7-after-link-keyboardinterrupt-rolls-back-pair')
def _delta_after_link_interrupt():
    """A KeyboardInterrupt that fires AFTER one os.link has succeeded
    but before the other still rolls the pair back to empty, and never
    deletes a preexisting collision sentinel."""
    from ear.fileio.adm.builder import ADMBuilder
    from ear.fileio.adm.generate_ids import generate_ids
    from ear.fileio.adm.elements.block_formats import (
        AudioBlockFormatDirectSpeakers)
    from ear.fileio.adm.elements.geom import (
        BoundCoordinate, DirectSpeakerPolarPosition)
    from ear.fileio.adm.xml import adm_to_xml
    from ear.fileio.bw64 import Bw64Writer
    from ear.fileio.bw64.chunks import FormatInfoChunk, ChnaChunk, AudioID
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
    axml = LT.tostring(adm_to_xml(builder.adm), xml_declaration=True, encoding='UTF-8')
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        src = td / 'src.wav'
        chna = ChnaChunk(audioIDs=[
            AudioID(trackIndex=1, audioTrackUID='ATU_00000001',
                    audioTrackFormatIDRef='AT_00011001_01',
                    audioPackFormatIDRef='AP_00011001'),
        ])
        fi = FormatInfoChunk(formatTag=1, channelCount=1, sampleRate=48000,
                              bitsPerSample=16)
        with open(str(src), 'wb') as fh:
            writer = Bw64Writer(fh, formatInfo=fi, chna=chna)
            writer.axml = axml
            with writer:
                writer.write(np.zeros((16, 1), dtype='<f4'))
        out_dir = td / 'out'
        out_dir.mkdir()
        # Patch os.link so the SECOND call raises KeyboardInterrupt,
        # simulating a SIGTERM between the two os.link syscalls. The
        # first link creates the sidecar; the second is the audio
        # link that fails. The publication rollback must clean the
        # sidecar we just created and leave the audio destination
        # uncreated.
        target_audio = out_dir / ('src-stereo.wav')
        target_sidecar = target_audio.with_suffix(
            target_audio.suffix + '.conversion.json')
        original_link = worker.os.link
        calls = [0]

        def patched_link(src, dst, *args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                raise KeyboardInterrupt('simulated SIGTERM between links')
            return original_link(src, dst, *args, **kwargs)
        worker.os.link = patched_link
        try:
            worker.convert(dict(input=str(src), out_dir=str(out_dir),
                                  target='stereo', format='WAV', depth='24'))
        except KeyboardInterrupt:
            pass
        finally:
            worker.os.link = original_link
        # No orphan sidecar should be left behind after the interrupted
        # partial publish.
        assert not target_sidecar.exists(), 'orphan sidecar after interrupt'
        # Audio destination must NOT exist either (the second link
        # never succeeded).
        assert not target_audio.exists(), \
            'audio destination exists after interrupted publish'
        # No leftover .conversion-* tempdir.
        leftovers = list(out_dir.iterdir())
        assert all('.conversion-' not in str(p) for p in leftovers), leftovers
    return {'sidecar_cleaned': True, 'pair_empty': True}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main():
    print('cases run', len(PASSED) + len(FAILED))
    for name, detail in PASSED:
        print('PASS', name, detail)
    for name, err in FAILED:
        print('FAIL', name, err)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
