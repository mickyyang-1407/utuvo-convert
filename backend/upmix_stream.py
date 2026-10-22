"""Two-pass bounded Upmix-only long output streaming.

For opt-in generic v2 Upmix, exposes hop-aligned raw block iteration with
halos, pass1 analyse max peak, pass2 emit globally attenuated blocks
directly to a staged codec / WAV writer. Memory of the rendered target
scales with chunk size, not total track length.

Design contract:
  - ``stream_pass1`` consumes the source array hop-aligned; the source
    may live in any float64 ndarray, no temporary copies are made of
    the full output buffer.
  - ``stream_pass2`` re-iterates the same hop-aligned blocks, applies
    the global common gain computed from pass1 peak, and yields
    ``(start, end, block)`` tuples sized exactly to ``[start, end)``
    on the original input frame axis. The caller writes them to the
    staged output.
  - The same smoothing halo used by the whole-buffer path applies:
    symmetric ``HALO_FRAMES * hop`` samples on each side of every
    chunk, with no manual zero pad before smoothing (scipy
    ``boundary='zeros'`` handles the global boundary).
  - The LoRo-fold identity is preserved exactly by the in-DSP
    allocation; the global common attenuation is applied uniformly
    to every emitted sample.
"""
from __future__ import annotations

import math
from typing import Iterator, Tuple

import numpy as np
from scipy.signal import resample_poly

import layout_catalog
import upmix as upmix_engine
from upmix import (
    _build_pair_plan, _process_chunk_layout, _frame_size,
    _round_to_hop, HALO_FRAMES, BLOCK_SAMPLES_DEFAULT,
PEAK_CEILING, SUPPORTED_RATES,
)

# scipy.signal.resample_poly's default up=4 FIR is a 41-tap Kaiser filter,
# with ten source-frame support on either side. Surveying sixteen frames
# around each emitted block leaves a small guard while keeping the analysis
# bounded and aligned with the final output's 4x reconstruction.
TRUE_PEAK_UPSAMPLE = 4
TRUE_PEAK_SUPPORT_FRAMES = 16
TRUE_PEAK_TARGET = 10.0 ** (-1.5 / 20.0)


def _block_iter(n_frames: int, block_samples: int, hop: int
                ) -> Iterator[Tuple[int, int, int, int]]:
    """Yield ``(pos, end, halo_start, halo_end)`` for each hop-aligned
    block covering ``[0, n_frames)``.
    """
    halo_samples = upmix_engine.HALO_FRAMES * hop
    pos = 0
    while pos < n_frames:
        end = min(n_frames, pos + block_samples)
        halo_start = max(0, pos - halo_samples)
        halo_end = min(n_frames, end + halo_samples)
        yield pos, end, halo_start, halo_end
        pos = end


def _process_block(x: np.ndarray, plan, centre_amount: float,
                   freqs_hz: np.ndarray, size: int, hop: int,
                   labels: tuple, height_amount: float = 0.0) -> np.ndarray:
    return _process_chunk_layout(
        x, plan, centre_amount, freqs_hz, size, hop, labels,
        height_amount=height_amount)


def stream_pass1(stereo: np.ndarray, layout_id: str, sample_rate: int,
                 *, centre: float = 0.0, surround: float = 0.4,
                 height: float = 0.2,
                 block_samples: int = BLOCK_SAMPLES_DEFAULT,
                 ) -> Iterator[Tuple[int, int, np.ndarray]]:
    """Iterate the input once, yielding ``(start, end, six_block)`` for
    each hop-aligned chunk with smoothing halos. ``six_block`` has the
    same row count as ``[halo_start, halo_end)``; the caller is
    responsible for cropping to ``[start, end)``.

    The gain at this stage is fixed at 1.0; pass1 only surveys the
    peak so the caller can compute the global common attenuation.
    """
    catalogue = layout_catalog.get_catalog()
    layout = layout_catalog.find(catalogue, layout_id)
    array = np.ascontiguousarray(stereo, dtype=np.float64)
    n = array.shape[0]
    size = _frame_size(sample_rate)
    hop = size // 4
    freqs_hz = np.fft.rfftfreq(size, d=1.0 / sample_rate)
    block_samples = _round_to_hop(int(block_samples), hop)
    plan = _build_pair_plan(layout)
    surround_pairs = [p for p in plan if p['kind'] == 'surround']
    height_pairs = [p for p in plan if p['kind'] == 'height']
    for p in surround_pairs:
        p['amount'] = surround
    for p in height_pairs:
        p['amount'] = surround
    labels = tuple(layout['labels'])
    for pos, end, halo_start, halo_end in _block_iter(n, block_samples, hop):
        block = array[halo_start:halo_end]
        chunk_out = _process_block(block, plan, centre, freqs_hz, size, hop,
                                    labels, height_amount=height)
        yield pos, end, chunk_out


def stream_pass2(stereo: np.ndarray, layout_id: str, sample_rate: int,
                 global_gain: float,
                 *, centre: float = 0.0, surround: float = 0.4,
                 height: float = 0.2,
                 block_samples: int = BLOCK_SAMPLES_DEFAULT,
                 ) -> Iterator[Tuple[int, int, np.ndarray]]:
    """Iterate the input a second time, applying ``global_gain`` to
    every chunk before yielding. The crop is performed so the caller
    receives ``six_block`` of length ``end - pos`` and may write it
    directly to the staged file.
    """
    catalogue = layout_catalog.get_catalog()
    layout = layout_catalog.find(catalogue, layout_id)
    array = np.ascontiguousarray(stereo, dtype=np.float64)
    n = array.shape[0]
    size = _frame_size(sample_rate)
    hop = size // 4
    freqs_hz = np.fft.rfftfreq(size, d=1.0 / sample_rate)
    block_samples = _round_to_hop(int(block_samples), hop)
    plan = _build_pair_plan(layout)
    surround_pairs = [p for p in plan if p['kind'] == 'surround']
    height_pairs = [p for p in plan if p['kind'] == 'height']
    for p in surround_pairs:
        p['amount'] = surround
    for p in height_pairs:
        p['amount'] = surround
    labels = tuple(layout['labels'])
    for pos, end, halo_start, halo_end in _block_iter(n, block_samples, hop):
        block = array[halo_start:halo_end]
        chunk_out = _process_block(block, plan, centre, freqs_hz, size, hop,
                                    labels, height_amount=height)
        # Crop to [pos, end) within the chunk_out buffer; the chunk_out
        # length should be halo_end - halo_start (or longer due to
        # scipy's zero padding).
        crop_start = pos - halo_start
        crop_end = crop_start + (end - pos)
        if crop_end > chunk_out.shape[0]:
            crop_end = chunk_out.shape[0]
        cropped = chunk_out[crop_start:crop_end] * global_gain
        # Force LFE silent (when present) so the streamed output is
        # truthful even at the boundary where padding effects leak.
        if 'LFE' in labels:
            cropped[:, labels.index('LFE')] = 0.0
        yield pos, end, cropped


def compute_global_gain(stereo: np.ndarray, layout_id: str, sample_rate: int,
                         *, centre: float = 0.0, surround: float = 0.4,
                         height: float = 0.2,
                         block_samples: int = BLOCK_SAMPLES_DEFAULT,
                         progress=None,
                         ) -> Tuple[float, dict]:
    """Survey emitted crops and their FIR support at 4x, then choose one
    common attenuation that targets -1.5 dBFS estimated true peak.

    This is an oversampled sample-peak bound for this renderer, not an
    EBU R128 / BS.1770 certified true-peak meter.
    """
    sample_peak = 0.0
    reconstructed_peak = 0.0
    blocks = 0
    array = np.asarray(stereo, dtype=np.float64)
    n_frames = len(array)
    support = TRUE_PEAK_SUPPORT_FRAMES
    for pos, end, six_block in stream_pass1(
            stereo, layout_id, sample_rate,
            centre=centre, surround=surround, height=height,
            block_samples=block_samples):
        # stream_pass1 returns the halo buffer, while pass2 emits the crop.
        layout = layout_catalog.find(layout_catalog.get_catalog(), layout_id)
        hop = _frame_size(sample_rate) // 4
        halo_start = max(0, pos - upmix_engine.HALO_FRAMES * hop)
        crop_start = pos - halo_start
        crop_end = crop_start + (end - pos)
        crop = six_block[crop_start:crop_end]
        if crop.size:
            sample_peak = max(sample_peak,
                               float(np.max(np.abs(crop))))
            start = max(0, crop_start - support)
            stop = min(six_block.shape[0], crop_end + support)
            supported = six_block[start:stop]
            upsampled = resample_poly(supported, TRUE_PEAK_UPSAMPLE, 1,
                                      axis=0)
            # The local segment begins on an integer source frame, so its
            # phase aligns with the global 4x grid. Trim only context whose
            # reconstruction is not part of this emitted crop.
            keep_start = (crop_start - start) * TRUE_PEAK_UPSAMPLE
            keep_end = keep_start + len(crop) * TRUE_PEAK_UPSAMPLE
            reconstructed_peak = max(
                reconstructed_peak,
                float(np.max(np.abs(upsampled[keep_start:keep_end]),
                             initial=0.0)))
        blocks += 1
        if progress is not None:
            progress(blocks / max(1, math.ceil(
                n_frames / upmix_engine._round_to_hop(
                    int(block_samples), hop))))
    peak_bound = max(sample_peak, reconstructed_peak)
    if peak_bound > TRUE_PEAK_TARGET:
        gain = TRUE_PEAK_TARGET / peak_bound
    else:
        gain = 1.0
    return gain, {
        'peak': sample_peak,
        'sample_peak': sample_peak,
        'estimated_true_peak_4x': reconstructed_peak,
        'peak_bound_4x': peak_bound,
        'target_peak': TRUE_PEAK_TARGET,
        'peak_target_dbfs': -1.5,
        'blocks': blocks,
        'true_peak_method': 'scipy resample_poly 4x; estimated, not certified',
    }
