"""Stereo → multi-channel experimental upmix.

Original implementation: 43 ms Hann STFT with 75 % overlap, complex
covariance smoothed across 9 STFT frames, Wiener-attenuated primary
filter smoothed across 3 STFT frames, LoRo-compatible allocation. The
generic v2 entry ``upmix_pcm`` distributes the ambient budget across
all emitted surround / height pairs using smooth complementary
frequency partitions so that adding speakers does not multiply the
ambience energy and pairs are not scalar duplicates of each other.

First audition candidate; the result is an estimate of ambience, centre
and (when present) height, not a claim of restored original surround
or semantic voice separation. No fake reverb / delays / crossfeed /
generated LFE; the LFE column is exactly silent.

Public API:
  - ``upmix_pcm_to_5_1_side`` — original v1 5.1-side wrapper.
  - ``upmix_pcm`` — generic v2 entry that accepts any implemented
    layout id from the catalogue.

The STFT/ISTFT boundary settings mirror the reference research code:
``boundary='zeros', padded=True`` on the forward pass and
``boundary=True`` on the inverse so the round-trip is exact even for
short inputs. Large inputs are processed in hop-aligned chunks with a
symmetric smoothing halo; no full-track STFT array is allocated.

Inputs and outputs are float64 PCM.
"""
from __future__ import annotations

import math

import numpy as np
from scipy import signal
from scipy.ndimage import uniform_filter1d

# ---------------------------------------------------------------------------
# Algorithm constants — keep in one obvious place so the sidecar can echo
# them without parsing the source.
# ---------------------------------------------------------------------------
PEAK_CEILING = 10 ** (-1 / 20)            # -1 dBFS sample ceiling
K = math.sqrt(0.5)                        # LoRo centre / surround gain
SUPPORTED_RATES = (44100, 48000, 88200, 96000, 176400, 192000)
COVARIANCE_FRAMES = 9                     # STFT frames for covariance smoothing
PRIMARY_FRAMES = 3                        # STFT frames for primary smoothing
COV_RADIUS = COVARIANCE_FRAMES // 2       # 4 STFT frames each side
PRI_RADIUS = PRIMARY_FRAMES // 2          # 1 STFT frame each side
ANALYSIS_SUPPORT = 4                     # synthesis / analysis support
HALO_FRAMES = COV_RADIUS + PRI_RADIUS + ANALYSIS_SUPPORT  # 9 frames each side
DEFAULT_SURROUND = 0.4
DEFAULT_CENTRE = 0.0
CENTRE_RANGE = (0.0, 0.7)
SURROUND_RANGE = (0.0, 1.0)
REAL_GATE_FLOOR = 0.7                     # rho_real lower bound for centre
ILD_GATE_CAP_DB = 6.0                     # ILD upper bound for centre
ALGORITHM_ID = "upmix-cov-wiener-5.1-side/v1"
ALGORITHM_VERSION = "experimental-v1"
BLOCK_SAMPLES_DEFAULT = 65536             # chunked scratch size

# Output channel order for 5.1-side.
OUTPUT_LABELS = ('FL', 'FR', 'FC', 'LFE', 'SL', 'SR')
LFE_INDEX = OUTPUT_LABELS.index('LFE')


def _smooth_complex(array: np.ndarray, frames: int, axis: int = 0) -> np.ndarray:
    """Uniform 1D smoothing applied independently to real / imag parts."""
    real = uniform_filter1d(array.real, frames, axis=axis, mode='nearest')
    imag = uniform_filter1d(array.imag, frames, axis=axis, mode='nearest')
    return real + 1j * imag


def _primary_filter(cov: np.ndarray) -> np.ndarray:
    """R = q u u^H + a I Wiener attenuation; not plain PCA."""
    values, vectors = np.linalg.eigh(cov)
    hi = np.maximum(values[..., 1], 0)
    lo = np.clip(values[..., 0], 0, hi)
    weight = np.divide(hi - lo, hi, out=np.zeros_like(hi), where=hi > 1e-30)
    v = vectors[..., :, 1]
    # Batched outer product: keep the freq/time axes as leading batch axes.
    return weight[..., None, None] * (v[..., :, None] * v[..., None, :].conj())


def _real_primary_filter(cov: np.ndarray) -> np.ndarray:
    """Real-coherence variant for research diagnostics only. App UI never
    reaches this; the projection is the real batched outer product so
    freq / time axes are preserved."""
    values, vectors = np.linalg.eigh(cov.real)
    v_true = vectors[..., :, 1]
    hi = np.maximum(values[..., 1], 0)
    lo = np.clip(values[..., 0], 0, hi)
    weight = np.divide(hi - lo, hi, out=np.zeros_like(hi), where=hi > 1e-30)
    return weight[..., None, None] * (v_true[..., :, None] * v_true[..., None, :])


def _frame_size(rate: int) -> int:
    return 2 ** round(math.log2(rate * 2048 / 48000))


def _stft_block(x: np.ndarray, size: int, hop: int):
    """Forward STFT with explicit zero-padded boundary to ensure the
    Hann 75 %-overlap window satisfies NOLA at every STFT frame.

    Returns ``(time, bin, channel)`` complex tensor.
    """
    if len(x) < size:
        padded = np.zeros((size, x.shape[1]), dtype=x.dtype)
        padded[:len(x)] = x
    else:
        padded = x
    _, _, z = signal.stft(
        padded.T, fs=1.0, window='hann', nperseg=size,
        noverlap=3 * size // 4, boundary='zeros', padded=True)
    return z.transpose(2, 1, 0)


def _istft_block(z: np.ndarray, size: int, hop: int) -> np.ndarray:
    """Inverse STFT; returns ``(samples, channels)`` real array."""
    _, y = signal.istft(
        z.transpose(2, 1, 0), fs=1.0, window='hann', nperseg=size,
        noverlap=3 * size // 4, boundary=True)
    return y.T


def _analyse_block(x: np.ndarray, size: int, hop: int):
    """Analyse a contiguous stereo block and return tensors + the
    smoothing halo radius so callers can size the chunk boundary
    correctly."""
    z = _stft_block(x, size, hop)
    cov = _smooth_complex(z[..., :, None] * z[..., None, :].conj(),
                          COVARIANCE_FRAMES, axis=0)
    gp = _smooth_complex(_primary_filter(cov), PRIMARY_FRAMES, axis=0)
    primary = np.einsum('tfij,tfj->tfi', gp, z)
    ambient = z - primary
    power = np.maximum(cov.diagonal(axis1=-2, axis2=-1).real, 0)
    denom = np.sqrt(power[..., 0] * power[..., 1])
    rho_real = np.divide(cov[..., 0, 1].real, denom,
                         out=np.zeros_like(denom), where=denom > 1e-30)
    ild = 10 * np.log10((power[..., 0] + 1e-30) / (power[..., 1] + 1e-30))
    gate = np.clip((rho_real - REAL_GATE_FLOOR) / 0.3, 0, 1) * \
        np.clip(1 - np.abs(ild) / ILD_GATE_CAP_DB, 0, 1)
    return z, primary, ambient, gate


def _allocate(z: np.ndarray, primary: np.ndarray, ambient: np.ndarray,
              gate: np.ndarray, centre: float, surround: float) -> np.ndarray:
    """Six-channel STFT: FL FR FC LFE SL SR (LoRo-compatible)."""
    c = centre * gate * (primary[..., 0] + primary[..., 1]) / (2 * K)
    sl = surround * ambient[..., 0]
    sr = surround * ambient[..., 1]
    return np.stack((
        z[..., 0] - K * c - K * sl,
        z[..., 1] - K * c - K * sr,
        c,
        np.zeros_like(c),
        sl,
        sr,
    ), axis=-1)


def _lp_butterworth(freqs_hz: np.ndarray, cutoff_hz: float) -> np.ndarray:
    """Magnitude of a first-order Butterworth lowpass at ``cutoff_hz``.
    Handles the DC bin (f=0) explicitly so the safe value 0 is returned."""
    with np.errstate(invalid='ignore', divide='ignore'):
        out = cutoff_hz ** 2 / (freqs_hz ** 2 + cutoff_hz ** 2)
    if freqs_hz.size and freqs_hz[0] == 0.0:
        out[0] = 0.0
    return out


def _hp_butterworth(freqs_hz: np.ndarray, cutoff_hz: float) -> np.ndarray:
    """Magnitude of a first-order Butterworth highpass at ``cutoff_hz``.
    The DC bin (f=0) is forced to 0; ``cutoff_hz=0`` short-circuits to 1."""
    if cutoff_hz == 0.0:
        return np.ones_like(freqs_hz)
    with np.errstate(invalid='ignore', divide='ignore'):
        out = freqs_hz ** 2 / (freqs_hz ** 2 + cutoff_hz ** 2)
    if freqs_hz.size and freqs_hz[0] == 0.0:
        out[0] = 0.0
    return out


def _band(freqs_hz: np.ndarray, low_hz: float, high_hz: float) -> np.ndarray:
    """Magnitude of a band-pass filter from ``low_hz`` to ``high_hz``
    built as ``lp(high) - lp(low)``."""
    return _lp_butterworth(freqs_hz, high_hz) - _lp_butterworth(freqs_hz, low_hz)


# Backwards-compatible simple-filter lookup. The v2 plan uses
# continuous band tiles below; this is for the v1 5.1-side wrapper.
def _spectral_weight(freqs_hz: np.ndarray, kind: str) -> np.ndarray:
    """Magnitude spectral weight for the v1 5.1-side wrapper."""
    if kind == 'full':
        return np.ones_like(freqs_hz)
    if kind == 'low':
        return _lp_butterworth(freqs_hz, 1000.0)
    if kind == 'high':
        return _hp_butterworth(freqs_hz, 1000.0)
    if kind == 'high_4k':
        return _hp_butterworth(freqs_hz, 4000.0)
    raise ValueError('unknown spectral kind {k}'.format(k=kind))


def _build_pair_plan(layout: dict) -> list:
    """Per-layout plan describing how to allocate ambient across pairs.

    Returns a list of dicts:
      ``{'left': str, 'right': str, 'kind': 'surround' | 'height',
         'amount': float, 'band': (low_hz, high_hz) | None,
         'out_index_left': int, 'out_index_right': int}``

    The plan tiles the spectrum across all emitted pairs using smooth
    complementary Butterworth partitions so the sum of every pair's
    band equals 1 at every frequency (the single-budget invariant that
    the 5.1-side reference satisfies with a single full-spectrum pair).
    Each pair gets the full surround budget — the band tile controls
    how the spectrum is shared, not the per-pair amount.
    """
    labels = layout['labels']
    label_to_idx = {l: i for i, l in enumerate(labels)}
    plan = []
    F1 = 1000.0
    F2 = 4000.0
    INF = float('inf')
    surround_count = len(layout['loro_pairs'])
    height_count = len(layout['height_pairs'])

    # Floor pairs cover the low-frequency region. With heights present
    # they must stay below F1; without heights they can split the full
    # spectrum into complementary low / high bands.
    if surround_count == 1 and height_count == 0:
        floor_bands = [(0.0, INF)]
    elif surround_count == 1 and height_count > 0:
        floor_bands = [(0.0, F1)]
    elif surround_count == 2 and height_count == 0:
        floor_bands = [(0.0, F1), (F1, INF)]
    elif surround_count == 2 and height_count >= 1:
        floor_bands = [(0.0, F1), (F1, F2)]
    elif surround_count == 1 and height_count == 2:
        floor_bands = [(0.0, F1)]
    else:
        floor_bands = [(0.0, F1)] * surround_count

    if height_count == 0:
        height_bands = []
    elif height_count == 1:
        height_bands = [(F1, INF)]
    elif height_count == 2:
        height_bands = [(F1, F2), (F2, INF)]
    else:
        height_bands = [(F1, INF)] * height_count

    amount = 0.0
    for i, pair in enumerate(layout['loro_pairs']):
        ll, rr = pair.split('/')
        band = floor_bands[i] if i < len(floor_bands) else (F1, F2)
        plan.append({'left': ll, 'right': rr, 'kind': 'surround',
                     'amount': amount, 'band': band,
                     'out_index_left': label_to_idx[ll],
                     'out_index_right': label_to_idx[rr]})
    for i, pair in enumerate(layout['height_pairs']):
        ll, rr = pair.split('/')
        band = height_bands[i] if i < len(height_bands) else (F1, INF)
        plan.append({'left': ll, 'right': rr, 'kind': 'height',
                     'amount': amount, 'band': band,
                     'out_index_left': label_to_idx[ll],
                     'out_index_right': label_to_idx[rr]})
    return plan


def _band_weight(freqs_hz: np.ndarray, band: tuple) -> np.ndarray:
    """Magnitude of a Butterworth band filter for ``(low_hz, high_hz)``."""
    lo, hi = band
    if hi == float('inf'):
        return _hp_butterworth(freqs_hz, lo)
    if lo == 0.0:
        return _lp_butterworth(freqs_hz, hi)
    return _band(freqs_hz, lo, hi)


def _allocate_layout_simple(z: np.ndarray, primary: np.ndarray,
                             ambient: np.ndarray, gate: np.ndarray,
                             plan: list, centre_amount: float,
                             freqs_hz: np.ndarray,
                             labels: tuple,
                             height_amount: float = 0.0) -> np.ndarray:
    """Generic v2 allocation honouring the single-budget partition.

    Each emitted pair gets the full ``surround`` amount (not divided by
    pair count); the band tile controls how the spectrum is partitioned
    so the sum of all emitted ambient pairs equals ``surround * ambient``
    at every frequency — the same one-budget invariant that the
    5.1-side reference satisfies with a single full-spectrum pair.

    The LoRo fold identity is preserved exactly: each pair subtracts
    ``K * surround * actual_weight`` from the front residual and
    contributes the same shape to its pair of output channels.

    Heights always start at 1 kHz (highpass weight). Their effective
    weight is ``height_amount * W_height_base`` — linear in the user's
    height parameter. Floor pairs receive ``(1 - height_amount) *
    total_heights_base / N_floor`` of redistribution at every frequency
    so the sum across all pairs equals 1 at every frequency.
    """
    n_chan = len(labels)
    out = np.zeros(z.shape[:-1] + (n_chan,), dtype=z.dtype)
    c = centre_amount * gate * (primary[..., 0] + primary[..., 1]) / (2 * K)
    label_to_idx = {l: i for i, l in enumerate(labels)}
    fc_index = label_to_idx.get('FC', None)
    if fc_index is not None and centre_amount > 0:
        out[..., fc_index] = c
    # A fixed surround budget is partitioned between floor and height.
    # Each group has complementary smooth weights, including DC. Heights
    # take a linear, high-passed share; the floor retains its complement.
    floor_pairs = [p for p in plan if p['kind'] == 'surround']
    height_pairs = [p for p in plan if p['kind'] == 'height']
    floor_low = _lp_butterworth(freqs_hz, 1000.0)
    top_front = _lp_butterworth(freqs_hz, 4000.0)
    height_share = (height_amount * _hp_butterworth(freqs_hz, 1000.0)
                    if height_pairs else np.zeros_like(freqs_hz))
    weights = {}
    for i, p in enumerate(floor_pairs):
        partition = (np.ones_like(freqs_hz) if len(floor_pairs) == 1
                     else floor_low if i == 0 else 1.0 - floor_low)
        weights[id(p)] = (1.0 - height_share) * partition
    for i, p in enumerate(height_pairs):
        partition = (np.ones_like(freqs_hz) if len(height_pairs) == 1
                     else top_front if i == 0 else 1.0 - top_front)
        weights[id(p)] = height_share * partition
    sub_l = np.zeros_like(z[..., 0])
    sub_r = np.zeros_like(z[..., 1])
    for p in plan:
        l_chan = p['out_index_left']
        r_chan = p['out_index_right']
        weight = weights[id(p)]
        out[..., l_chan] = p['amount'] * ambient[..., 0] * weight
        out[..., r_chan] = p['amount'] * ambient[..., 1] * weight
        sub_l = sub_l + K * p['amount'] * ambient[..., 0] * weight
        sub_r = sub_r + K * p['amount'] * ambient[..., 1] * weight
    lfe_index = label_to_idx.get('LFE', None)
    if lfe_index is not None:
        out[..., lfe_index] = 0
    out[..., 0] = z[..., 0] - K * c - sub_l
    out[..., 1] = z[..., 1] - K * c - sub_r
    return out


def _process_chunk_to_6ch(x: np.ndarray, centre: float, surround: float,
                          size: int, hop: int) -> np.ndarray:
    """Single contiguous block: ``(frames, 2) → (frames, 6)`` float64.

    The block is treated as a stand-alone signal: scipy's
    ``boundary='zeros', padded=True`` and the matching ``boundary=True``
    inverse provide the smoothing context on both ends.
    """
    z, primary, ambient, gate = _analyse_block(x, size, hop)
    six = _allocate(z, primary, ambient, gate, centre, surround)
    return _istft_block(six, size, hop)


def _process_chunk_layout(x: np.ndarray, plan: list, centre_amount: float,
                          freqs_hz: np.ndarray, size: int, hop: int,
                          labels: tuple,
                          height_amount: float = 0.0) -> np.ndarray:
    """Generic v2 block processor; returns ``(samples, channels)``."""
    z, primary, ambient, gate = _analyse_block(x, size, hop)
    six_stft = _allocate_layout_simple(
        z, primary, ambient, gate, plan, centre_amount, freqs_hz, labels,
        height_amount=height_amount)
    return _istft_block(six_stft, size, hop)


def _round_to_hop(samples: int, hop: int) -> int:
    """Round ``samples`` up to the nearest hop multiple (never smaller)."""
    if hop <= 0:
        raise ValueError('hop must be positive')
    return max(hop, ((samples + hop - 1) // hop) * hop)


def _ensure_real_number(name: str, value) -> float:
    if isinstance(value, bool):
        raise ValueError('upmix {n} must be a real number, not bool'
                         .format(n=name))
    if not isinstance(value, (int, float)):
        raise ValueError('upmix {n} must be a real number'.format(n=name))
    fv = float(value)
    if not math.isfinite(fv):
        raise ValueError('upmix {n} must be finite'.format(n=name))
    return fv


def upmix_pcm_to_5_1_side(stereo: np.ndarray, sample_rate: int,
                          *, centre: float = DEFAULT_CENTRE,
                          surround: float = DEFAULT_SURROUND,
                          block_samples: int = BLOCK_SAMPLES_DEFAULT
                          ) -> tuple:
    """Stereo PCM → 5.1-side allocation.

    Returns ``(six_channel, applied_gain)`` where ``six_channel`` is a
    float64 ``(frames, 6)`` array in OUTPUT_LABELS order and
    ``applied_gain`` is the linear static common attenuation used to keep
    the peak ≤ ``PEAK_CEILING``. The LFE column is exactly silent.
    """
    array = np.asarray(stereo)
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError('upmix input must be (frames, 2)')
    if array.size == 0:
        raise ValueError('upmix input must contain samples')
    if not np.isfinite(array).all():
        raise ValueError('upmix input contains non-finite samples')
    if not isinstance(sample_rate, int) or sample_rate not in SUPPORTED_RATES:
        raise ValueError('upmix sample rate must be 44.1/48/88.2/96/176.4/192 kHz')
    centre_v = _ensure_real_number('centre', centre)
    surround_v = _ensure_real_number('surround', surround)
    if not (CENTRE_RANGE[0] <= centre_v <= CENTRE_RANGE[1]):
        raise ValueError('upmix centre must be in [{lo}, {hi}]'
                         .format(lo=CENTRE_RANGE[0], hi=CENTRE_RANGE[1]))
    if not (SURROUND_RANGE[0] <= surround_v <= SURROUND_RANGE[1]):
        raise ValueError('upmix surround must be in [{lo}, {hi}]'
                         .format(lo=SURROUND_RANGE[0], hi=SURROUND_RANGE[1]))

    n = array.shape[0]
    if array.dtype != np.float64:
        array = array.astype(np.float64)

    size = _frame_size(sample_rate)
    hop = size // 4
    # Halo radius measured in STFT frames; each frame is `hop` samples.
    halo_samples = HALO_FRAMES * hop

    block_samples = _round_to_hop(int(block_samples), hop)

    if n <= block_samples:
        six = _process_chunk_to_6ch(array, centre_v, surround_v, size, hop)
        # Crop to the original input length; ISTFT may have produced
        # ``size`` samples when the input was shorter.
        six = six[:n]
    else:
        out = np.zeros((n, 6), dtype=np.float64)
        pos = 0
        while pos < n:
            end = min(n, pos + block_samples)
            halo_start = max(0, pos - halo_samples)
            halo_end = min(n, end + halo_samples)
            block = array[halo_start:halo_end]
            # No manual zero pad: scipy boundary='zeros' handles the
            # boundary condition at file start / end.
            block_six = _process_chunk_to_6ch(
                block, centre_v, surround_v, size, hop)
            # Crop offset within the unified buffer is the difference
            # between the requested output start and the buffered start.
            crop_start = pos - halo_start
            crop_end = crop_start + (end - pos)
            # Boundary='zeros' pads the input; the inverse may have
            # produced fewer samples than the buffered length, or
            # truncated leading zeros at file start — never shift the
            # content beyond the natural alignment.
            if crop_end > block_six.shape[0]:
                crop_end = block_six.shape[0]
            if crop_start >= crop_end:
                # Nothing to copy for this segment (e.g. extreme tail).
                pos = end
                continue
            out[pos:end] = block_six[crop_start:crop_end]
            pos = end
        six = out

    # Static common attenuation — never boost.
    peak = float(np.max(np.abs(six), initial=0))
    if peak > PEAK_CEILING:
        gain = PEAK_CEILING / peak
    else:
        gain = 1.0
    six[..., LFE_INDEX] = 0.0
    return six * gain, gain


# ---------------------------------------------------------------------------
# Generic v2 upmix entry — supports every implemented layout from the
# catalogue with smooth complementary spectral partitions and a shared
# ambient budget. The 5.1-side v1 wrapper above remains source-compatible.
# ---------------------------------------------------------------------------

GENERIC_ALGORITHM_ID = "upmix-cov-wiener-generic-v2"
GENERIC_ALGORITHM_VERSION = "experimental-v2"


def upmix_pcm(stereo: np.ndarray, layout_id: str, sample_rate: int,
              *, centre: float = 0.0, surround: float = 0.4,
              height: float = 0.2, block_samples: int = BLOCK_SAMPLES_DEFAULT
              ) -> tuple:
    """Generic stereo PCM → multi-channel upmix for any implemented
    layout. Returns ``(output, applied_gain, meta)``.

    ``output`` is ``(frames, channels)`` float64 in ``layout['labels']``
    order; ``applied_gain`` is the linear static common attenuation used
    to keep the peak ≤ ``PEAK_CEILING``; ``meta`` is a dict with the
    inferred / requested amounts, the per-pair filter selection, the
    centre / surround / height amounts actually used, the algorithm id
    and the WAV mask of the emitted layout.
    """
    import layout_catalog  # local import: keep module path lazy.

    array = np.asarray(stereo)
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError('upmix input must be (frames, 2)')
    if array.size == 0:
        raise ValueError('upmix input must contain samples')
    if not np.isfinite(array).all():
        raise ValueError('upmix input contains non-finite samples')
    if not isinstance(sample_rate, int) or sample_rate not in SUPPORTED_RATES:
        raise ValueError('upmix sample rate must be 44.1/48/96 kHz')

    catalogue = layout_catalog.get_catalog()
    if not layout_catalog.is_implemented(catalogue, layout_id):
        raise ValueError('upmix layout {l} not implemented'.format(l=layout_id))
    layout = layout_catalog.find(catalogue, layout_id)

    centre_v = _ensure_real_number('centre', centre)
    surround_v = _ensure_real_number('surround', surround)
    height_v = _ensure_real_number('height', height)
    if not (CENTRE_RANGE[0] <= centre_v <= CENTRE_RANGE[1]):
        raise ValueError('upmix centre must be in [{lo}, {hi}]'
                         .format(lo=CENTRE_RANGE[0], hi=CENTRE_RANGE[1]))
    if not (SURROUND_RANGE[0] <= surround_v <= SURROUND_RANGE[1]):
        raise ValueError('upmix surround must be in [{lo}, {hi}]'
                         .format(lo=SURROUND_RANGE[0], hi=SURROUND_RANGE[1]))
    cat_ranges = catalogue['ranges']
    if not (cat_ranges['height'][0] <= height_v <= cat_ranges['height'][1]):
        raise ValueError('upmix height must be in [{lo}, {hi}]'
                         .format(lo=cat_ranges['height'][0],
                                 hi=cat_ranges['height'][1]))
    if not layout['has_centre'] and centre_v > 0:
        raise ValueError('layout {l} has no centre; centre must be 0'
                         .format(l=layout_id))
    if not layout['has_height'] and height_v > 0:
        raise ValueError('layout {l} has no height; height must be 0'
                         .format(l=layout_id))

    plan = _build_pair_plan(layout)
    surround_pairs = [p for p in plan if p['kind'] == 'surround']
    height_pairs = [p for p in plan if p['kind'] == 'height']
    # Single-budget scheme: every pair carries the full surround amount.
    # The band tile in the plan ensures the sum of every pair's emission
    # equals ``surround * ambient`` at every frequency, so the LoRo fold
    # residual is identical regardless of how many pairs share the
    # budget. Heights get a separate height share that participates in
    # the same band-partitioned scheme (they always live above the 1
    # kHz floor boundary so the highpass requirement holds).
    for p in surround_pairs:
        p['amount'] = surround_v
    for p in height_pairs:
        p['amount'] = surround_v

    n = array.shape[0]
    if array.dtype != np.float64:
        array = array.astype(np.float64)
    size = _frame_size(sample_rate)
    hop = size // 4
    halo_samples = HALO_FRAMES * hop
    freqs_hz = np.fft.rfftfreq(size, d=1.0 / sample_rate)
    block_samples = _round_to_hop(int(block_samples), hop)

    labels = tuple(layout['labels'])
    if n <= block_samples:
        out = _process_chunk_layout(
            array, plan, centre_v, freqs_hz, size, hop, labels,
            height_amount=height_v)
        out = out[:n]
    else:
        out = np.zeros((n, len(labels)), dtype=np.float64)
        pos = 0
        while pos < n:
            end = min(n, pos + block_samples)
            halo_start = max(0, pos - halo_samples)
            halo_end = min(n, end + halo_samples)
            block = array[halo_start:halo_end]
            block_out = _process_chunk_layout(
                block, plan, centre_v, freqs_hz, size, hop, labels,
                height_amount=height_v)
            crop_start = pos - halo_start
            crop_end = crop_start + (end - pos)
            if crop_end > block_out.shape[0]:
                crop_end = block_out.shape[0]
            if crop_start < crop_end:
                out[pos:end] = block_out[crop_start:crop_end]
            pos = end

    peak = float(np.max(np.abs(out), initial=0))
    if peak > PEAK_CEILING:
        gain = PEAK_CEILING / peak
    else:
        gain = 1.0
    # LFE silent (when present).
    if 'LFE' in labels:
        out[..., labels.index('LFE')] = 0.0
    out = out * gain
    meta = {
        'algorithm': GENERIC_ALGORITHM_ID,
        'version': GENERIC_ALGORITHM_VERSION,
        'experimental': True,
        'layout_id': layout_id,
        'layout_name': layout['name'],
        'channels': len(labels),
        'labels': list(labels),
        'centre': centre_v,
        'surround': surround_v,
        'height': height_v,
        'per_pair': [
            {'left': p['left'], 'right': p['right'],
             'kind': p['kind'], 'amount': p['amount'],
             'partition': ('full' if len(height_pairs if p['kind'] == 'height'
                                            else surround_pairs) == 1
                           else 'low' if p is (height_pairs if p['kind'] == 'height'
                                                       else surround_pairs)[0] else 'high'),
             'partition_cutoff_hz': 4000.0 if p['kind'] == 'height' else 1000.0}
            for p in plan
        ],
        'sample_ceiling_db': 20.0 * math.log10(PEAK_CEILING),
        'applied_gain_linear': float(gain),
        'applied_gain_db': 20.0 * math.log10(max(gain, 1e-30)),
        'loro_compatible_allocation': True,
        'lfe_silent': True,
        'height_share_of_ambience': height_v,
        'height_highpass_hz': 1000.0,
    }
    return out, gain, meta


def lfe_silent(six_channel: np.ndarray) -> bool:
    """Truthful check that the LFE column carries no energy."""
    return bool(np.max(np.abs(six_channel[..., LFE_INDEX])) == 0.0)


# ---------------------------------------------------------------------------
# Optional research-only real-covariance comparator. Not exposed to the App.
# ---------------------------------------------------------------------------

def upmix_pcm_to_5_1_side_real(stereo: np.ndarray, sample_rate: int,
                               *, centre: float = DEFAULT_CENTRE,
                               surround: float = DEFAULT_SURROUND,
                               block_samples: int = BLOCK_SAMPLES_DEFAULT
                               ) -> np.ndarray:
    """Real-coherence variant for lab comparison only. App UI never reaches
    this; it is here so future diagnostics can be run against the same
    chunked scratch without modifying the shipping path."""
    array = np.asarray(stereo, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError('upmix input must be (frames, 2)')
    n = array.shape[0]
    centre_v = _ensure_real_number('centre', centre)
    surround_v = _ensure_real_number('surround', surround)
    if not (CENTRE_RANGE[0] <= centre_v <= CENTRE_RANGE[1]):
        raise ValueError('upmix centre must be in [{lo}, {hi}]'
                         .format(lo=CENTRE_RANGE[0], hi=CENTRE_RANGE[1]))
    if not (SURROUND_RANGE[0] <= surround_v <= SURROUND_RANGE[1]):
        raise ValueError('upmix surround must be in [{lo}, {hi}]'
                         .format(lo=SURROUND_RANGE[0], hi=SURROUND_RANGE[1]))

    size = _frame_size(sample_rate)
    hop = size // 4
    halo_samples = HALO_FRAMES * hop
    block_samples = _round_to_hop(int(block_samples), hop)

    if n <= block_samples:
        z = _stft_block(array, size, hop)
        cov = _smooth_complex(z[..., :, None] * z[..., None, :].conj(),
                              COVARIANCE_FRAMES, axis=0)
        gp_r = _smooth_complex(_real_primary_filter(cov), PRIMARY_FRAMES,
                               axis=0)
        primary = np.einsum('tfij,tfj->tfi', gp_r, z)
        ambient = z - primary
        power = np.maximum(cov.diagonal(axis1=-2, axis2=-1).real, 0)
        denom = np.sqrt(power[..., 0] * power[..., 1])
        rho_real = np.divide(cov[..., 0, 1].real, denom,
                             out=np.zeros_like(denom), where=denom > 1e-30)
        ild = 10 * np.log10((power[..., 0] + 1e-30) / (power[..., 1] + 1e-30))
        gate = np.clip((rho_real - REAL_GATE_FLOOR) / 0.3, 0, 1) * \
            np.clip(1 - np.abs(ild) / ILD_GATE_CAP_DB, 0, 1)
        six = _allocate(z, primary, ambient, gate, centre_v, surround_v)
        return _istft_block(six, size, hop)[:n]

    out = np.zeros((n, 6), dtype=np.float64)
    pos = 0
    while pos < n:
        end = min(n, pos + block_samples)
        halo_start = max(0, pos - halo_samples)
        halo_end = min(n, end + halo_samples)
        block = array[halo_start:halo_end]
        z = _stft_block(block, size, hop)
        cov = _smooth_complex(z[..., :, None] * z[..., None, :].conj(),
                              COVARIANCE_FRAMES, axis=0)
        gp_r = _smooth_complex(_real_primary_filter(cov), PRIMARY_FRAMES,
                               axis=0)
        primary = np.einsum('tfij,tfj->tfi', gp_r, z)
        ambient = z - primary
        power = np.maximum(cov.diagonal(axis1=-2, axis2=-1).real, 0)
        denom = np.sqrt(power[..., 0] * power[..., 1])
        rho_real = np.divide(cov[..., 0, 1].real, denom,
                             out=np.zeros_like(denom), where=denom > 1e-30)
        ild = 10 * np.log10((power[..., 0] + 1e-30) / (power[..., 1] + 1e-30))
        gate = np.clip((rho_real - REAL_GATE_FLOOR) / 0.3, 0, 1) * \
            np.clip(1 - np.abs(ild) / ILD_GATE_CAP_DB, 0, 1)
        six = _allocate(z, primary, ambient, gate, centre_v, surround_v)
        block_six = _istft_block(six, size, hop)
        crop_start = pos - halo_start
        crop_end = crop_start + (end - pos)
        if crop_end > block_six.shape[0]:
            crop_end = block_six.shape[0]
        if crop_start < crop_end:
            out[pos:end] = block_six[crop_start:crop_end]
        pos = end
    return out
