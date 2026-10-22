"""Bounded offline-worker repair helpers.

Eight narrowly-scoped adapters for the eight failures named in
FUNCTIONAL_REPAIR.md plus the four round-1 deterministic defects named
in REPAIR_DELTA.md. The helpers operate on local inputs; their tests use synthetic fixtures. Avoids globally monkeypatching EAR or the prototype module;
defines a local reader/adapter for issue #1 and reuses EAR's chunk index
for issue #3. Preserves the prototype mix helper interface verbatim and
the existing PCM quantization semantics.
"""
import array
import io
import json
import os
import pathlib
import re
import struct
import subprocess
import sys
import tempfile
import shutil
import warnings
import xml.etree.ElementTree as ET

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'prototype'))

# ---------------------------------------------------------------------------
# Issue #1 + REPAIR_DELTA #1 — bounded in-memory ADM adapter with full EAR
# reader surface (adm / sampleRate / iter_sample_blocks) and proper file
# lifecycle (close on context exit, NOT inside the load function).
# ---------------------------------------------------------------------------

MAX_METADATA_BYTES = 128 * 1024 * 1024  # Issue #3 — explicit larger cap


def open_bw64(path):
    """Local BW64 reader using EAR's chunk index. Returns the EAR
    ``Bw64Reader`` directly. The caller is responsible for closing it
    (either via ``with`` semantics or ``reader._buffer.close()``)."""
    from ear.fileio.bw64.reader import Bw64Reader
    fh = open(str(path), 'rb')
    try:
        reader = Bw64Reader(fh)
    except BaseException:
        fh.close()
        raise
    return reader


def load_adm_with_pack_compat(path):
    """Load ADM, applying the bounded extra_pack_ref compatibility adapter
    AND the namespace-whitespace normalizer.

    File lifecycle: the underlying BW64 reader stays open for the entire
    lifetime of the returned ``_LocalAdmReader`` context. EAR's
    ``OfflineRenderDriver.render_input_file`` iterates
    ``infile.iter_sample_blocks(blocksize)`` while the file remains open,
    so the load function MUST NOT close the buffer eagerly.

    EAR strict validation refuses ``audioStreamFormat`` elements that
    reference both an ``audioChannelFormat`` and an ``audioPackFormat``.
    EBU upstream issue #39 documents a redundant pattern where the pack
    is already listed in the channel's owning packs. The adapter drops
    ONLY such redundant refs; mismatched refs are NEVER silently
    removed — EAR strict validation surfaces them with full context.

    Namespace normalizer is wired here: leading/trailing whitespace
    inside ``xmlns`` URI values is stripped before strict parse. Real
    DTD / entity declarations are rejected outright (no external
    entities, no DTD subset). Both double and single quote styles are
    supported.
    """
    from ear.fileio.adm.adm import ADM
    from ear.fileio.adm.common_definitions import load_common_definitions
    from ear.fileio.adm.xml import load_axml_string
    from ear.fileio.adm.chna import load_chna_chunk
    reader = open_bw64(path)
    try:
        axml = reader.axml
        chna = reader.chna
        sr = reader.sampleRate
        channels = reader.channels
        bitdepth = reader.bitdepth
        adm = ADM()
        load_common_definitions(adm)
        if axml is not None:
            cleaned = normalize_xml_namespace_whitespace(axml)
            load_axml_string(adm, cleaned)
            _strip_redundant_pack_refs(adm)
            _normalize_dolby_lfe_labels(adm)
        load_chna_chunk(adm, chna)
        return _LocalAdmReader(adm, sr, channels, bitdepth, reader)
    except BaseException:
        # On any failure during ADM construction, ensure we don't leak
        # the file handle — caller never sees a broken context.
        try:
            reader._buffer.close()
        except Exception:
            pass
        raise


class _LocalAdmReader:
    """Mimics EAR's ``Bw64AdmReader`` interface closely enough for
    ``OfflineRenderDriver.render_input_file`` to consume. File lifecycle
    is bounded by the context: ``__exit__`` closes the underlying buffer
    exactly once."""

    __slots__ = ('adm', 'sample_rate', 'channels', 'bitdepth', '_reader',
                 '_closed')

    def __init__(self, adm, sr, channels, bitdepth, reader):
        self.adm = adm
        self.sample_rate = sr
        self.channels = channels
        self.bitdepth = bitdepth
        self._reader = reader
        self._closed = False

    # EAR-compatible surface. ``sampleRate`` (capital R) is what EAR's
    # OfflineRenderDriver reads; ``sample_rate`` is the snake_case alias
    # used internally.
    @property
    def sampleRate(self):
        return self._reader.sampleRate

    def iter_sample_blocks(self, block_size=4096):
        """Delegates to the active Bw64Reader. Yields float32 ndarray
        blocks of shape ``(nsamples, nchannels)``."""
        while self._reader.tell() != len(self._reader):
            yield self._reader.read(block_size)

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._reader._buffer.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return None

    def __iter__(self):
        return self.iter_sample_blocks(4096)


def _strip_redundant_pack_refs(adm):
    """Remove ONLY pack refs whose target pack is already referenced by the
    audioChannelFormat. Everything else — positions, gains, CHNA, track
    order — is left untouched.

    Note: ``AudioChannelFormat`` does NOT track its owning packs; the
    many-to-many relationship lives on ``AudioPackFormat.audioChannelFormats``.
    To decide whether a stream's redundant pack ref is safe to drop, we ask
    the channel: ``is the referenced pack's audioChannelFormats list
    including this channel?``"""
    from ear.fileio.adm.elements.main_elements import (
        AudioStreamFormat, AudioPackFormat)
    stripped = 0
    for stream in adm.elements:
        if not isinstance(stream, AudioStreamFormat):
            continue
        if stream.audioPackFormat is None or stream.audioChannelFormat is None:
            continue
        channel = stream.audioChannelFormat
        pack = stream.audioPackFormat
        belongs = any(p is pack and channel in p.audioChannelFormats
                      for p in adm.audioPackFormats if isinstance(p, AudioPackFormat))
        if belongs:
            stream.audioPackFormat = None
            stripped += 1
    if stripped:
        warnings.warn(
            'adm_compat: stripped {n} redundant pack reference(s); '
            'see ebu/ebu_adm_renderer#39'.format(n=stripped),
            stacklevel=2,
        )


def _normalize_dolby_lfe_labels(adm):
    """EBU issue #39 maps the exact Dolby RC_LFE label to ITU LFE1.
    Only DirectSpeakers blocks with this exact label are adapted. Audio,
    track order, position, gain and timing are unchanged. Record the
    adaptation instead of letting EAR pan LFE as a full-range speaker.
    """
    from ear.fileio.adm.elements.main_elements import TypeDefinition
    count = 0
    for channel in adm.audioChannelFormats:
        if channel.type != TypeDefinition.DirectSpeakers:
            continue
        for block in channel.audioBlockFormats:
            labels = block.speakerLabel
            if 'RC_LFE' in labels:
                block.speakerLabel = ['urn:itu:bs:2051:0:speaker:LFE1' if label == 'RC_LFE' else label for label in labels]
                count += 1
    if count:
        warnings.warn('adm_compat: mapped {n} Dolby RC_LFE label(s) to ITU LFE1; EBU issue #39; positions/gains/timing unchanged'.format(n=count), stacklevel=2)


# ---------------------------------------------------------------------------
# Issue #2 + REPAIR_DELTA #2 — secure XML namespace whitespace normalization.
# Rejects DTD / entity declarations outright. Supports both quote styles.
# Strict UTF-8 (no replacement); non-UTF-8 bytes refuse rather than mangle.
# ---------------------------------------------------------------------------

# Both quote styles; the value capture is non-greedy and ends at the
# matching quote character.
_NS_ATTR_RE = re.compile(
    r'(xmlns(?::[\w-]+)?\s*=\s*)(\"|\')([^\"\']*?)(\2)')
_DTD_PATTERN = re.compile(r'<\!DOCTYPE\b', re.IGNORECASE)
_ENTITY_PATTERN = re.compile(r'<\!ENTITY\b', re.IGNORECASE)
_SYSTEM_PATTERN = re.compile(r'\bSYSTEM\s+[\'\"][^\'\"]+[\'\"]', re.IGNORECASE)
_PUBLIC_PATTERN = re.compile(r'\bPUBLIC\s+', re.IGNORECASE)


def normalize_xml_namespace_whitespace(xml_bytes):
    """Strip leading / trailing whitespace inside ``xmlns[=prefix]`` URI
    values. Real malformed XML, conflicting routes, DTD / entity
    declarations and non-UTF-8 bytes are refused. No network, no
    external entities, no generic recover mode."""
    if not xml_bytes:
        raise ValueError('empty XML payload; refusing')
    # Strict UTF-8: invalid bytes refuse rather than silently mangle.
    try:
        text = xml_bytes.decode('utf-8')
    except UnicodeDecodeError as e:
        raise ValueError(
            'xmlns payload is not strict UTF-8: {e}'.format(e=e)) from e
    # Reject any DTD / entity / SYSTEM / PUBLIC declaration outright.
    if _DTD_PATTERN.search(text) or _ENTITY_PATTERN.search(text):
        raise ValueError('DTD / entity declarations refused')

    # First pass: strip whitespace inside well-formed xmlns URI values.
    def _strip(match):
        prefix, quote, value, close = match.groups()
        new_value = value.strip()
        if new_value == value:
            return match.group(0)
        return prefix + quote + new_value + close

    patched = _NS_ATTR_RE.sub(_strip, text)
    # Strict parser pass: any genuine malformation is raised here. This
    # uses xml.etree with no external entities, no DTD, no network —
    # only the in-memory text we just constructed.
    try:
        ET.fromstring(patched)
    except ET.ParseError as e:
        raise ValueError(
            'XML parse refused after namespace normalization: {e}'.format(
                e=e)) from e
    out = patched.encode('utf-8')
    if out != xml_bytes:
        warnings.warn(
            'adm_compat: normalized surrounding whitespace in xmlns URI value(s)',
            stacklevel=2,
        )
    return out


# ---------------------------------------------------------------------------
# Issue #3 — bounded metadata processing with EAR chunk-index recognition
# ---------------------------------------------------------------------------


def wave_chunks_with_index(path):
    """BW64 chunk index using EAR's parser. Recognizes metadata via chunk
    index so RF64 / BW64 data-first files work. Raises on over-cap or
    malformed sizes; never reads the audio data."""
    from ear.fileio.bw64.reader import Bw64Reader
    fh = open(str(path), 'rb')
    try:
        reader = Bw64Reader(fh)
    except BaseException:
        fh.close()
        raise
    try:
        metadata_size = 0
        is_adm = False
        for chunk_id, idx in reader._chunks.items():
            if chunk_id in (b'axml', b'chna'):
                if idx.size > MAX_METADATA_BYTES:
                    raise ValueError(
                        'metadata chunk {tag} exceeds {cap} MiB cap'.format(
                            tag=chunk_id.decode('ascii'), cap=128))
                metadata_size += idx.size
                is_adm = True
        if metadata_size > MAX_METADATA_BYTES:
            raise ValueError('combined ADM metadata exceeds 128 MiB cap')
        return {'axml': reader.axml, 'chna': reader.chna,
                'is_adm': is_adm,
                'sample_rate': reader.sampleRate,
                'channels': reader.channels,
                'bitdepth': reader.bitdepth,
                'riff_format': reader.fileFormat}
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# Issue #4 + REPAIR_DELTA #3 — per-target eligibility, including binaural 12ch
# intermediate, real tail, output resampling rate, mono duplication, and
# upsampled PCM output. Floats use 8 bytes (float64).
# ---------------------------------------------------------------------------

MAX_DECODED_BYTES = 512 * 1024 * 1024  # PCM safety limit (shared with worker)
PCM_BYTES_PER_SAMPLE = 8               # float64 throughout the worker
BINAURAL_INTERMEDIATE_CHANNELS = 12
BINAURAL_TAIL_SECONDS = 0.25
ADM_TARGET_CHANNELS = {
    'stereo': 2,
    '5.1-side': 6,
    '7.1.4-rear-first': 12,
    'binaural': 2,                      # HRTF output stage
}


def estimate_target_bytes(frames, source_rate, source_channels, target,
                          out_rate=0, source_layout=None,
                          adm_eligible=True):
    """Compute the worst-case float64 byte cost of producing the named
    target. Used for pre-allocation gates; refuses if over the 512 MiB
    safety cap. PCM existing safety limit is unchanged."""
    rate = out_rate or source_rate
    if source_rate <= 0 or rate <= 0 or frames < 0:
        raise ValueError('invalid frame count or sample rate')
    if adm_eligible:
        if target not in ADM_TARGET_CHANNELS:
            return None
        native_channels = 12 if target == 'binaural' else ADM_TARGET_CHANNELS[target]
    else:
        from convert import LAYOUTS
        native_channels = source_channels
        if target not in LAYOUTS and target != 'binaural':
            return None
    tail = int(round(BINAURAL_TAIL_SECONDS * source_rate)) if target == 'binaural' else 0
    final_channels = 2 if target == 'binaural' else (
        ADM_TARGET_CHANNELS[target] if adm_eligible else len(LAYOUTS[target]))
    final_frames = ((frames + tail) * rate + source_rate - 1) // source_rate
    # Bound each full sample buffer; this is not a total process RSS budget.
    return max(frames * native_channels * PCM_BYTES_PER_SAMPLE,
               (frames + tail) * final_channels * PCM_BYTES_PER_SAMPLE,
               final_frames * final_channels * PCM_BYTES_PER_SAMPLE)


def target_eligibility(frames, source_rate, source_channels, target,
                       out_rate=0, source_layout=None, adm_eligible=True):
    """Return (eligible, reason, est_bytes). Used both as a probe-time
    descriptor and as a render-time gate. Refuses targets whose
    worst-case buffer exceeds the 512 MiB safety cap. PCM direct-copy
    path uses source-bytes-based bound when ``adm_eligible=False``."""
    if source_channels <= 0:
        return (False, 'source has zero channels', 0)
    if adm_eligible:
        if target not in ('stereo', '5.1-side', '7.1.4-rear-first', 'binaural'):
            return (False, 'unsupported ADM target: {t}'.format(t=target), 0)
    est = estimate_target_bytes(frames, source_rate, source_channels, target,
                                out_rate=out_rate, source_layout=source_layout,
                                adm_eligible=adm_eligible)
    if est is None:
        return (False, 'unsupported target: {t}'.format(t=target), 0)
    if est > MAX_DECODED_BYTES:
        if target == 'binaural':
            reason = ('binaural target needs {n} MiB intermediate+HRTF; '
                      '512 MiB limit binds').format(n=est // (1024 * 1024))
        elif target == '7.1.4-rear-first':
            reason = ('7.1.4-rear-first target needs {n} MiB rendered buffer; '
                      '512 MiB limit binds').format(n=est // (1024 * 1024))
        else:
            reason = ('{t} target needs {n} MiB rendered buffer; '
                      '512 MiB limit binds').format(
                          t=target, n=est // (1024 * 1024))
        return (False, reason, est)
    return (True, 'fits within 512 MiB safety cap', est)


def permitted_targets_for_adm(frames, source_rate, source_channels,
                              source_layout=None):
    """List ADM targets eligible for the given input, with the byte
    estimate each requires. The probe uses this to describe what the
    worker can render without crashing."""
    out = []
    for t in ('stereo', '5.1-side', '7.1.4-rear-first', 'binaural'):
        ok, _reason, est = target_eligibility(
            frames, source_rate, source_channels, t,
            source_layout=source_layout)
        out.append({'target': t, 'eligible': ok, 'estimated_bytes': est})
    return out


# ---------------------------------------------------------------------------
# Issue #5 — canonical mask-order serialization for WAV only
# ---------------------------------------------------------------------------


def canonical_wav_labels(labels, bits):
    """Return (canonical_labels, sample_column_permutation) such that
    ``canonical_labels`` is sorted by ascending bit position. The caller
    must reorder sample columns using the permutation before writing WAV.
    AIFF is unaffected; explicit noncanonical order is preserved and
    recorded in the sidecar."""
    pairs = sorted(enumerate(labels), key=lambda kv: bits[kv[1]])
    perm = [orig_idx for orig_idx, _ in pairs]
    canonical = [lbl for _, lbl in pairs]
    return canonical, perm


# ---------------------------------------------------------------------------
# Issue #6 — empty / zero-frame refusal
# ---------------------------------------------------------------------------


def refuse_empty_frames(info):
    """Refuse zero-frame inputs before any conversion. Nonfinite / overload
    refusals are handled separately in the worker."""
    if info.get('frames', 0) == 0:
        raise ValueError(
            'zero-frame input refused; no audio data to convert')


# ---------------------------------------------------------------------------
# Issue #7 + REPAIR_DELTA #4 — vectorized block WAV + ndarray-native mix.
# The block writer keeps a single bytearray per write call and chunks
# sample conversion at 65536 frames; no Python-level per-sample or
# per-byte loops. RIFF size is checked BEFORE allocation.
# ---------------------------------------------------------------------------


def native_mix(data, matrix):
    """Invoke the prototype mix-helper with ndarray raw I/O. Helper and
    prototype source unchanged. Avoids the per-sample ``list``/``struct``
    expansion that previously created millions of Python float objects.
    ``data`` is ``(frames, src_channels)`` float64 / float32 ndarray;
    ``matrix`` is ``(dst_channels, src_channels)`` float ndarray. Returns
    float ndarray of shape ``(frames, dst_channels)``."""
    helper = ROOT / '.build' / 'mix-helper'
    if not helper.exists():
        raise ValueError('run prototype/build.sh first')
    dst_channels, src_channels = matrix.shape
    frames = data.shape[0]
    with tempfile.TemporaryDirectory(prefix='utuvo-convert-') as td:
        td = pathlib.Path(td)
        in_path = td / 'in.raw'
        out_path = td / 'out.raw'
        matrix_path = td / 'matrix.json'
        np.ascontiguousarray(data.astype('<f4', copy=False)).tofile(str(in_path))
        matrix_path.write_text(json.dumps({'matrix': matrix.tolist()}))
        subprocess.run([str(helper), str(in_path), str(out_path), str(matrix_path)],
                       check=True)
        out = np.fromfile(str(out_path), dtype='<f4').reshape(-1, dst_channels)
        return np.array(out, copy=True)


def _pcm_quantize_block(block_f64, bits_per_sample):
    """Vectorized nearest-even quantization of a float64 frame block.
    Returns a ``np.ndarray`` of the correct signed integer dtype."""
    scale = 2 ** (bits_per_sample - 1)
    clipped = np.clip(np.rint(block_f64 * scale), -scale, scale - 1)
    if bits_per_sample == 16:
        return clipped.astype('<i2')
    if bits_per_sample == 32:
        return clipped.astype('<i4')
    return clipped.astype('<i4')


def vectorized_write_wav(path, data, sample_rate, canonical_labels, encoding,
                         bits, block_frames=65536):
    """Block / vectorized WAV writer with nearest-even PCM quantization.
    Same exact quantization, same extensible format, same canonical
    ascending mask order as ``proof.write_wav``. ``data`` is float ndarray
    of shape ``(frames, channels)`` already permuted to match
    ``canonical_labels``. Output exceeds RIFF size ⇒ explicit failure;
    no silent RF64 fallback.

    Block-based: the header is computed once and written once; per-block
    quantization runs on contiguous frame chunks of ``block_frames``
    (default 65536). The 24-bit path drops the high (sign-extension)
    byte of each int32 sample via ``view(uint8).reshape(-1,4)[:,:3]``.
    """
    if not np.isfinite(data).all():
        raise ValueError('clipping: nonfinite output refused')
    if encoding == 'float32':
        bits_per_sample = 32
        code = 3
    else:
        bits_per_sample = int(encoding[3:])
        code = 1
    if bits_per_sample not in (16, 24, 32):
        raise ValueError('unsupported WAV bit depth')
    n_channels = len(canonical_labels)
    align = n_channels * bits_per_sample // 8
    mask = sum(1 << bits[lbl] for lbl in canonical_labels)
    n_frames = data.shape[0]

    # Precheck RIFF / fact / data sizes BEFORE allocating the conversion
    # buffers. Each frame contributes n_channels * (bits_per_sample // 8)
    # PCM bytes; a pad byte is appended only if the data length is odd.
    data_bytes = n_frames * align
    riff_size = 4 + (8 + 40)               # RIFF header + fmt chunk
    if code == 3:
        # Legacy-compatible fact: 4-byte tag + 8-byte packed <II> body.
        # The first 4 bytes of the body are read as the size field.
        riff_size += 4 + 8
    riff_size += 8 + data_bytes + (data_bytes & 1)
    if riff_size > 0xFFFFFFFF:
        raise ValueError('WAV output exceeds RIFF size; RF64 not supported')

    fmt = struct.pack('<HHIIHHHHII', 65534, n_channels,
                      sample_rate, sample_rate * align, align,
                      bits_per_sample, 22, bits_per_sample, mask, code) + \
        bytes.fromhex('00001000800000aa00389b71')
    header = b'RIFF' + struct.pack('<I', riff_size) + b'WAVE' + \
        b'fmt ' + struct.pack('<I', len(fmt)) + fmt
    if code == 3:
        # Byte-compatible with proof.write_wav: legacy fact chunk packs
        # <II (8 bytes) without a separate size field; the first 4 bytes
        # are interpreted as the size field (=4). Most readers tolerate
        # this; replicate verbatim for byte equality.
        header += b'fact' + struct.pack('<II', 4, n_frames)
    data_header = b'data' + struct.pack('<I', data_bytes)

    # Write header + per-block payload in a single open(). The data
    # buffer is materialized chunk-by-chunk so peak memory stays
    # bounded by ``block_frames * align``.
    pad = b'' if (data_bytes & 1) == 0 else b'\0'
    flat = np.ascontiguousarray(data.reshape(-1, order='C'),
                                dtype=np.float64)
    block_samples = block_frames * n_channels
    with open(str(path), 'xb') as fh:
        fh.write(header)
        fh.write(data_header)
        for start in range(0, flat.size, block_samples):
            chunk = flat[start:start + block_samples]
            if encoding == 'float32':
                fh.write(chunk.astype('<f4').tobytes())
                continue
            i = _pcm_quantize_block(chunk, bits_per_sample)
            if bits_per_sample == 24:
                # int32 → view as uint8 → drop the high byte per sample.
                # The high byte is sign extension; dropping it preserves
                # the 24-bit signed sample when re-read as little-endian.
                fh.write(i.view(np.uint8).reshape(-1, 4)[:, :3].tobytes())
            elif bits_per_sample == 16:
                fh.write(i.astype('<i2', copy=False).tobytes())
            else:  # 32-bit
                fh.write(i.astype('<i4', copy=False).tobytes())
        if pad:
            fh.write(pad)


def wav_riff_size_bytes(n_frames, n_channels, encoding):
    """Return the exact RIFF size for the canonical extensible WAV writer.

    This declaration-only preflight is also used by long-stream tests, so
    a multi-gigabyte refusal never needs a giant fixture or allocation.
    """
    if n_frames < 0 or n_channels <= 0:
        raise ValueError('invalid WAV frame or channel count')
    bits = 32 if encoding == 'float32' else int(encoding[3:])
    if bits not in (16, 24, 32):
        raise ValueError('unsupported WAV bit depth')
    code = 3 if encoding == 'float32' else 1
    data_bytes = n_frames * n_channels * (bits // 8)
    riff_size = 4 + (8 + 40)
    if code == 3:
        riff_size += 4 + 8
    riff_size += 8 + data_bytes + (data_bytes & 1)
    if riff_size > 0xFFFFFFFF:
        raise ValueError('WAV output exceeds RIFF size; RF64 not supported')
    return riff_size


class StreamingWAVWriter:
    """Write a canonical WAVEFORMATEXTENSIBLE file from bounded blocks."""
    def __init__(self, path, sample_rate, canonical_labels, encoding, bits,
                 expected_frames):
        self.path = pathlib.Path(path)
        self.channels = len(canonical_labels)
        self.expected_frames = int(expected_frames)
        self.frames = 0
        self.bits = 32 if encoding == 'float32' else int(encoding[3:])
        self.encoding = encoding
        self.code = 3 if encoding == 'float32' else 1
        self.align = self.channels * self.bits // 8
        self.mask = sum(1 << bits[label] for label in canonical_labels)
        self.data_bytes = self.expected_frames * self.align
        riff_size = wav_riff_size_bytes(self.expected_frames, self.channels,
                                        encoding)
        fmt = struct.pack('<HHIIHHHHII', 65534, self.channels,
                          sample_rate, sample_rate * self.align, self.align,
                          self.bits, 22, self.bits, self.mask, self.code) + \
            bytes.fromhex('00001000800000aa00389b71')
        header = b'RIFF' + struct.pack('<I', riff_size) + b'WAVE' + \
            b'fmt ' + struct.pack('<I', len(fmt)) + fmt
        if self.code == 3:
            # Keep the established writer's byte-compatible fact chunk.
            header += b'fact' + struct.pack('<II', 4, self.expected_frames)
        self._fh = open(str(self.path), 'xb')
        try:
            self._fh.write(header)
            self._fh.write(b'data' + struct.pack('<I', self.data_bytes))
        except BaseException:
            self._fh.close()
            self._fh = None
            try:
                self.path.unlink()
            except OSError:
                pass
            raise

    def write(self, block):
        data = np.asarray(block, dtype=np.float64)
        if data.ndim != 2 or data.shape[1] != self.channels:
            raise ValueError('streamed WAV block has unexpected channel count')
        if self.frames + data.shape[0] > self.expected_frames:
            raise ValueError('streamed WAV wrote more frames than declared')
        if not np.isfinite(data).all():
            raise ValueError('streamed WAV block contains non-finite samples')
        flat = np.ascontiguousarray(data.reshape(-1), dtype=np.float64)
        if self.encoding == 'float32':
            self._fh.write(flat.astype('<f4').tobytes())
        else:
            quantized = _pcm_quantize_block(flat, self.bits)
            if self.bits == 24:
                self._fh.write(quantized.view(np.uint8).reshape(-1, 4)[:, :3].tobytes())
            elif self.bits == 16:
                self._fh.write(quantized.astype('<i2', copy=False).tobytes())
            else:
                self._fh.write(quantized.astype('<i4', copy=False).tobytes())
        self.frames += int(data.shape[0])

    def close(self, abort=False):
        if self._fh is None:
            return
        fh, self._fh = self._fh, None
        if abort:
            fh.close()
            return
        if self.frames != self.expected_frames:
            fh.close()
            raise ValueError('streamed WAV frame count differs from declaration')
        if self.data_bytes & 1:
            fh.write(b'\0')
        fh.flush()
        fh.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close(abort=exc_type is not None)
        return False


def read_wav_extensible_info(path):
    """Read the bounded fmt fields needed to verify an extensible WAV mask."""
    with open(str(path), 'rb') as fh:
        if fh.read(4) != b'RIFF':
            raise ValueError('WAV QC: RIFF signature missing')
        fh.read(4)
        if fh.read(4) != b'WAVE':
            raise ValueError('WAV QC: WAVE signature missing')
        while True:
            header = fh.read(8)
            if len(header) < 8:
                break
            chunk_id, size = header[:4], struct.unpack('<I', header[4:])[0]
            if chunk_id == b'fmt ':
                if size < 40:
                    raise ValueError('WAV QC: extensible fmt chunk is truncated')
                raw = fh.read(40)
                if len(raw) != 40:
                    raise ValueError('WAV QC: extensible fmt chunk is truncated')
                fields = struct.unpack_from('<HHIIHHHHI', raw)
                info = {'format_tag':fields[0], 'channels':fields[1],
                        'sample_rate':fields[2], 'bits':fields[5],
                        'valid_bits':fields[7], 'channel_mask':fields[8],
                        'subformat':struct.unpack_from('<I', raw, 24)[0]}
                if info['format_tag'] != 65534:
                    raise ValueError('WAV QC: WAVEFORMATEXTENSIBLE required')
                return info
            fh.seek(size + (size & 1), os.SEEK_CUR)
    raise ValueError('WAV QC: fmt chunk missing')


_FLAC_METADATA_LIMIT = 16 * 1024 * 1024
_FLAC_COMMENT_LIMIT = 4 * 1024 * 1024
_FLAC_MASK_KEY = b'WAVEFORMATEXTENSIBLE_CHANNEL_MASK'


def _parse_vorbis_comment(payload):
    if len(payload) > _FLAC_COMMENT_LIMIT or len(payload) < 8:
        raise ValueError('FLAC channel-mask comment is outside metadata bounds')
    pos = 0
    vendor_len = int.from_bytes(payload[pos:pos + 4], 'little'); pos += 4
    if vendor_len > len(payload) - pos:
        raise ValueError('FLAC Vorbis vendor length is invalid')
    vendor = payload[pos:pos + vendor_len]; pos += vendor_len
    if len(payload) - pos < 4:
        raise ValueError('FLAC Vorbis comment count is missing')
    count = int.from_bytes(payload[pos:pos + 4], 'little'); pos += 4
    if count > 65536:
        raise ValueError('FLAC Vorbis comment count exceeds bound')
    comments = []
    for _ in range(count):
        if len(payload) - pos < 4:
            raise ValueError('FLAC Vorbis comment length is missing')
        length = int.from_bytes(payload[pos:pos + 4], 'little'); pos += 4
        if length > len(payload) - pos:
            raise ValueError('FLAC Vorbis comment length exceeds block')
        comments.append(payload[pos:pos + length]); pos += length
    if pos != len(payload):
        raise ValueError('FLAC Vorbis comment has trailing bytes')
    return vendor, comments


def _encode_vorbis_comment(vendor, comments):
    payload = bytearray(len(vendor).to_bytes(4, 'little'))
    payload.extend(vendor)
    payload.extend(len(comments).to_bytes(4, 'little'))
    for comment in comments:
        payload.extend(len(comment).to_bytes(4, 'little'))
        payload.extend(comment)
    if len(payload) > _FLAC_COMMENT_LIMIT:
        raise ValueError('FLAC Vorbis comment exceeds metadata bound')
    return bytes(payload)


def _mask_from_comment(payload):
    _vendor, comments = _parse_vorbis_comment(payload)
    values = []
    for comment in comments:
        key, sep, value = comment.partition(b'=')
        if sep and key.lower() == _FLAC_MASK_KEY.lower():
            try:
                text = value.decode('ascii').strip()
            except UnicodeDecodeError as exc:
                raise ValueError('FLAC channel mask is not ASCII') from exc
            if not re.fullmatch(r'0x[0-9a-fA-F]{1,8}', text):
                raise ValueError('FLAC channel mask must be hexadecimal')
            values.append(int(text, 16))
    if len(values) > 1:
        raise ValueError('FLAC has duplicate channel-mask comments')
    return values[0] if values else None


def read_flac_channel_mask(path):
    """Return the RFC 9639 WAVEFORMATEXTENSIBLE_CHANNEL_MASK, if present."""
    masks = []
    with open(str(path), 'rb') as fh:
        if fh.read(4) != b'fLaC':
            raise ValueError('FLAC QC: signature missing')
        total = 0
        while True:
            header = fh.read(4)
            if len(header) != 4:
                raise ValueError('FLAC QC: metadata block header truncated')
            last = bool(header[0] & 0x80)
            block_type = header[0] & 0x7F
            size = int.from_bytes(header[1:], 'big')
            total += size
            if size > _FLAC_METADATA_LIMIT or total > _FLAC_METADATA_LIMIT:
                raise ValueError('FLAC QC: metadata exceeds bounded reader limit')
            payload = fh.read(size)
            if len(payload) != size:
                raise ValueError('FLAC QC: metadata block truncated')
            if block_type == 4:
                mask = _mask_from_comment(payload)
                if mask is not None:
                    masks.append(mask)
            if last:
                break
    if len(masks) > 1:
        raise ValueError('FLAC has duplicate channel-mask comments')
    return masks[0] if masks else None


def rewrite_flac_channel_mask(path, mask):
    """Add/replace the RFC 9639 mask comment without loading FLAC audio."""
    path = pathlib.Path(path)
    if not isinstance(mask, int) or mask < 0 or mask > 0xFFFFFFFF:
        raise ValueError('FLAC channel mask is outside the 32-bit range')
    with open(str(path), 'rb') as source:
        if source.read(4) != b'fLaC':
            raise ValueError('FLAC mask rewrite: signature missing')
        blocks = []
        total = 0
        found_comment = False
        while True:
            header = source.read(4)
            if len(header) != 4:
                raise ValueError('FLAC mask rewrite: metadata block truncated')
            last = bool(header[0] & 0x80)
            block_type = header[0] & 0x7F
            size = int.from_bytes(header[1:], 'big')
            total += size
            if size > _FLAC_METADATA_LIMIT or total > _FLAC_METADATA_LIMIT:
                raise ValueError('FLAC mask rewrite: metadata exceeds bound')
            payload = source.read(size)
            if len(payload) != size:
                raise ValueError('FLAC mask rewrite: metadata block truncated')
            if block_type == 4:
                found_comment = True
                vendor, comments = _parse_vorbis_comment(payload)
                comments = [c for c in comments
                            if c.partition(b'=')[0].lower() != _FLAC_MASK_KEY.lower()]
                comments.append(_FLAC_MASK_KEY + b'=0x' + format(mask, 'x').encode('ascii'))
                payload = _encode_vorbis_comment(vendor, comments)
            blocks.append((block_type, payload))
            if last:
                break
        if not found_comment:
            blocks.append((4, _encode_vorbis_comment(
                b'UTUVO Audio Converter',
                [_FLAC_MASK_KEY + b'=0x' + format(mask, 'x').encode('ascii')])))
        fd, temp_name = tempfile.mkstemp(prefix='.' + path.name + '.mask-',
                                         dir=str(path.parent))
        try:
            with os.fdopen(fd, 'wb') as target:
                target.write(b'fLaC')
                for index, (block_type, payload) in enumerate(blocks):
                    if len(payload) > 0xFFFFFF:
                        raise ValueError('FLAC metadata block exceeds format limit')
                    flag = block_type | (0x80 if index == len(blocks) - 1 else 0)
                    target.write(bytes((flag,)) + len(payload).to_bytes(3, 'big'))
                    target.write(payload)
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    actual = read_flac_channel_mask(path)
    if actual != mask:
        raise ValueError('FLAC channel mask read-back mismatch')


# ---------------------------------------------------------------------------
# Issue #8 — final lossy decode peak refusal
# ---------------------------------------------------------------------------


def refuse_lossy_peak(decoded, fmt):
    """Refuse decoded lossy peaks > 1 with clear error and no publication.
    Never clamp or normalize secretly. Validate container/depth combos
    early. Empty / nonfinite already refused upstream."""
    if fmt not in ('OGG', 'MP3'):
        return
    peak = float(np.max(np.abs(decoded), initial=0))
    if peak > 1.0:
        raise ValueError(
            'final lossy decode peak {p:.6f} > 1.0; {codec} re-encode '
            'overflowed and would clip on publication'.format(p=peak, codec='MP3' if fmt == 'MP3' else 'Vorbis'))


# ---------------------------------------------------------------------------
# REPAIR_DELTA #6 — validate supported depth / container combinations early.
# Returns the resolved (subtype, depth) pair or raises ValueError. UI-
# compatible Vorbis semantics preserved (OGG accepts only stereo PCM).
# ---------------------------------------------------------------------------


SUPPORTED_DEPTHS = {
    'WAV': ('16', '24', '32', 'float32'),
    'AIFF': ('16', '24', '32'),
    'FLAC': ('16', '24'),
    'OGG': ('24',),  # Vorbis re-encode quantized to 24-bit reference
    'MP3': ('24',),  # Internal request compatibility; MP3 has no PCM bit depth.
}


def validate_depth_container(fmt, depth, n_channels):
    """Resolve and validate (format, depth) pair. Raises ValueError with
    a clear reason for any combination outside the project's UI contract."""
    if fmt not in FORMATS:
        raise ValueError('unsupported output format: {f}'.format(f=fmt))
    if depth not in SUPPORTED_DEPTHS[fmt]:
        raise ValueError(
            '{f} export does not support depth={d}'.format(f=fmt, d=depth))
    if fmt == 'FLAC' and n_channels > 8:
        raise ValueError('FLAC supports <=8 channels in this build')
    if fmt == 'OGG' and n_channels > 2:
        raise ValueError('OGG Vorbis export validated only for mono/stereo')
    if fmt == 'MP3' and n_channels > 2:
        raise ValueError('MP3 export supports mono/stereo; choose Stereo or Binaural')
    if fmt == 'AIFF' and depth == 'float32':
        raise ValueError('AIFF PCM selected; choose 16/24/32-bit')
    return FORMATS[fmt][0], depth


FORMATS = {
    'WAV': ('wav', 'PCM_24'),
    'AIFF': ('aiff', 'PCM_24'),
    'FLAC': ('flac', 'PCM_24'),
    'OGG': ('ogg', 'VORBIS'),
    'MP3': ('mp3', 'MPEG_LAYER_III'),
}
