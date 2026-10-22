#!/usr/bin/env python3
"""Project-local offline worker. stdout is newline JSON; no hardware/output API."""
import json, math, os, pathlib, shutil, signal, struct, subprocess, sys, tempfile, warnings
from contextlib import contextmanager
warnings.filterwarnings('ignore',message='pkg_resources is deprecated.*')
warnings.filterwarnings('ignore',message="Running interpreter doesn't sufficiently support.*")
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from math import gcd
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'prototype'))
sys.path.insert(0,str(ROOT/'backend'))
import convert as proof
import adm_compat
import upmix as upmix_engine
import layout_catalog
import upmix_stream
MAX_DECODE_BYTES=512*1024*1024
FORMATS={'WAV':('wav','PCM_24'),'AIFF':('aiff','PCM_24'),'FLAC':('flac','PCM_24'),'OGG':('ogg','VORBIS'),'MP3':('mp3','MPEG_LAYER_III')}
MP3_BITRATES = (128, 192, 256, 320)
MP3_RATES = (44100, 48000)
EAR_SYSTEM={'stereo':'0+2+0','5.1-side':'0+5+0','7.1.4-rear-first':'4+7+0'}


UPMIX_KEYS_V1=frozenset(('mode','centre','surround'))
UPMIX_KEYS_V2=frozenset(('mode','layout','centre','surround','height'))
UPMIX_MODE_V1='experimental-stereo-to-5.1-side-v1'
UPMIX_MODE_V2='upmix-generic-v2'

# REPAIR_DELTA #7 — SIGTERM during the tiny commit window defers termination
# until the done / failed event is emitted, so a committed pair is recorded
# as completed; before commit, normal cancellation cleans up the tempdir.
# Before commit, the flag stays False and KeyboardInterrupt propagates.
_DEFERRED_TERMINATE = None
_COMMITTING = False
_CLI_RUN = False


def emit(**v):
    print(json.dumps(v), flush=True)


def wave_chunks(path):
    chunks={}; mask=0
    with open(path,'rb') as f:
        h=f.read(12)
        if len(h)<12 or h[:4] not in (b'RIFF',b'RF64',b'BW64') or h[8:]!=b'WAVE': return {},0
        end=os.path.getsize(path)
        while f.tell()+8<=end:
            tag,size=struct.unpack('<4sI',f.read(8))
            if size==0xffffffff: break # BW64 data uses ds64; AXML/CHNA read through EAR below.
            if f.tell()+size>end: raise ValueError('truncated WAV chunk')
            if tag in (b'axml',b'chna',b'fmt '):
                if size>adm_compat.MAX_METADATA_BYTES: raise ValueError('metadata chunk over 128 MiB')
                chunks[tag]=f.read(size)
            else: f.seek(size,1)
            if size&1: f.seek(1,1)
        fmt=chunks.get(b'fmt ',b'')
        if len(fmt)>=40 and struct.unpack_from('<H',fmt)[0]==65534: mask=struct.unpack_from('<I',fmt,20)[0]
    return chunks,mask


def adm_info(path):
    # Bounded offline-only ADM adapter with pack-ref compatibility.
    # The adapter closes the file handle on context exit; we don't need
    # to do it manually.
    with adm_compat.load_adm_with_pack_compat(str(path)) as f:
        f.adm.validate()
        from ear.core.select_items import select_rendering_items
        items=select_rendering_items(f.adm)
        progs=[{'id':p.id,'name':p.audioProgrammeName} for p in f.adm.audioProgrammes]
        types={}
        for ac in f.adm.audioChannelFormats:
            if not ac.is_common_definition: types[ac.type.name]=types.get(ac.type.name,0)+1
        info={'programmes':progs,'object_channels':types.get('Objects',0),
              'direct_speaker_channels':types.get('DirectSpeakers',0),
              'render_items':len(items),'type_counts':types}
        info['pack_compat'] = getattr(f.adm, '_pack_compat_notes', None)
        return info


def probe(path):
    path=pathlib.Path(path)
    if not path.is_file(): raise ValueError('file not found')
    with sf.SoundFile(path) as f:
        info={'path':str(path),'name':path.name,'format':f.format,'subtype':f.subtype,
              'channels':f.channels,'sample_rate':f.samplerate,
              'frames':len(f),'duration':len(f)/f.samplerate}
    chunks,mask=wave_chunks(path)
    axml = chunks.get(b'axml', b'')
    has_adm_xml = any(marker in axml for marker in (b'audioFormatExtended', b'audioProgramme', b'audioTrackUID'))
    is_adm=b'chna' in chunks or has_adm_xml or path.suffix.lower() == '.adm'
    if path.suffix.lower() in ('.wav','.bw64','.bwf'):
        try:
            index=adm_compat.wave_chunks_with_index(str(path))
            axml = index.get('axml') or b''
            is_adm=is_adm or index.get('chna') is not None or any(marker in axml for marker in (b'audioFormatExtended', b'audioProgramme', b'audioTrackUID'))
        except Exception:
            if is_adm: raise
    if is_adm:
        adm_summary=adm_info(path)
        # REPAIR_DELTA #3 — probe must describe ADM permitted targets.
        adm_summary['permitted_targets'] = adm_compat.permitted_targets_for_adm(
            info['frames'], info['sample_rate'], info['channels'])
        info.update(adm=True,layout='ADM metadata',summary=adm_summary)
    else:
        layout='mono' if info['channels']==1 else 'stereo' if info['channels']==2 else 'unknown'
        if mask:
            labels=[k for k,v in sorted(proof.BITS.items(),key=lambda x:x[1]) if mask&(1<<v)]
            layout=next((k for k,v in proof.LAYOUTS.items() if labels==v and sum(1<<proof.BITS[x] for x in v)==mask),'unknown')
        info.update(adm=False,layout=layout,summary={'metadata':'Descriptive tags are not copied; exported layout is recorded in sidecar.'})
        # REPAIR_DELTA #3 — restore the PCM 512 MiB decoded safety cap for
        # non-ADM sources. EAR returns float64 samples; 8 bytes / sample.
        if info['frames']*info['channels']*8 > MAX_DECODE_BYTES:
            raise ValueError('current local limit: 512 MiB decoded PCM; streaming expansion pending')
    # Issue #6 — empty / zero-frame refusal before any work.
    adm_compat.refuse_empty_frames(info)
    return info


def render_adm(path, target, progress, programme=None, memory_cap=MAX_DECODE_BYTES):
    """Render ADM through EAR, bounded by ``memory_cap`` for the running
    sum of per-block decoded samples."""
    from ear.cmdline.render_file import OfflineRenderDriver
    from ear.fileio.adm import timing_fixes
    system=EAR_SYSTEM.get(target)
    if not system: raise ValueError('ADM supports stereo, 5.1-side, 7.1.4 or binaural')
    driver=OfflineRenderDriver(system,None,0,True,False,programme_id=programme)
    layout,upmix,n=driver.load_output_layout()
    notes=[]
    blocks=[]
    count=0
    with warnings.catch_warnings(record=True) as captured:
        with adm_compat.load_adm_with_pack_compat(str(path)) as f:
            f.adm.validate()
            timing_fixes.check_blockFormat_timings(f.adm, fix=False)
            sr=f.sample_rate
            source_frames=f._reader.__len__()
            for block in driver.render_input_file(f, layout, upmix):
                block=np.asarray(block)
                if not np.isfinite(block).all():
                    raise ValueError('nonfinite ADM render')
                # Bound chunk-length growth: refuse if accumulated frames
                # exceed the safety limit. EAR may return unexpected
                # lengths; we cap them here.
                count += len(block)
                est_running = count * n * 8
                if est_running > memory_cap:
                    raise ValueError(
                        'ADM render exceeded {m} MiB running buffer; '
                        '512 MiB safety cap binds'.format(m=memory_cap // (1024*1024)))
                blocks.append(block)
                progress(min(.55, .1 + .45 * count / max(1, source_frames)))
        notes.extend(str(w.message) for w in captured)
    data=np.concatenate(blocks) if blocks else np.zeros((0,n))
    if target=='7.1.4-rear-first': data=data[:,[0,1,2,3,6,7,4,5,8,9,10,11]]
    return data, sr, notes


def binaural(data, sr, layout):
    if layout not in ('stereo','5.1-side','7.1.4-side-first','7.1.4-rear-first'):
        raise ValueError('Apple HRTF supports stereo, 5.1-side or explicit 7.1.4 in this build')
    with tempfile.TemporaryDirectory(prefix='offline-hrtf-') as td:
        td=pathlib.Path(td); data.astype('<f4').tofile(td/'input.raw')
        p=subprocess.run([str(ROOT/'.build/spatial-helper'),
                          str(td/'input.raw'),str(td/'output.raw'),str(sr),layout],
                         capture_output=True, text=True, check=True)
        out=np.fromfile(td/'output.raw',dtype='<f4').reshape(-1,2)
        return out,json.loads(p.stdout)


def encode(path, data, sr, labels, fmt, depth, mp3_bitrate=320):
    """Encode to ``fmt``/``depth``. Returns the actual on-disk column
    order (canonical for WAV; explicit for AIFF)."""
    if fmt=='WAV':
        # Issue #5 — WAV requires canonical ascending-bit label order.
        # Reorder sample columns to match; AIFF keeps explicit order.
        encoding='float32' if depth=='float32' else 'pcm'+depth
        canonical, perm = adm_compat.canonical_wav_labels(labels, proof.BITS)
        reordered = data[:, perm]
        adm_compat.vectorized_write_wav(path, reordered, sr, canonical,
                                         encoding, proof.BITS)
        return canonical
    # Non-WAV: validate early (REPAIR_DELTA #6).
    n_ch = len(labels)
    if fmt == 'FLAC' and (n_ch > 8 or depth in ('32', 'float32')):
        raise ValueError('FLAC supports <=8 channels, 16/24-bit in this build')
    if fmt == 'OGG' and n_ch > 2:
        raise ValueError('OGG Vorbis export validated only for mono/stereo')
    if fmt == 'AIFF' and depth == 'float32':
        raise ValueError('AIFF PCM selected; choose 16/24/32-bit')
    codec_options = {}
    if fmt == 'MP3':
        if n_ch > 2 or sr not in MP3_RATES or mp3_bitrate not in MP3_BITRATES:
            raise ValueError('MP3 requires mono/stereo, 44.1/48 kHz and 128/192/256/320 kbps')
        # libsndfile 1.2.2 maps MPEG-1 CBR as 320 - compression * 288.
        codec_options = {'bitrate_mode': 'CONSTANT', 'compression_level': (320 - mp3_bitrate) / 288}
    subtype='MPEG_LAYER_III' if fmt=='MP3' else 'VORBIS' if fmt=='OGG' else 'PCM_'+depth
    # Bound each codec call: large Vorbis single-write buffers can crash libsndfile.
    with sf.SoundFile(str(path), mode='x', samplerate=sr, channels=len(labels),
                      format=fmt, subtype=subtype, **codec_options) as encoded:
        for start in range(0, len(data), 65536):
            encoded.write(data[start:start+65536])
    return labels


@contextmanager
def _streamed_upmix_stage(data, layout_id, sample_rate, labels, fmt, depth,
                          expected_frames, outdir, centre, surround, height,
                          block_samples, progress):
    """Render generic v2 into a bounded staged stream, then decode-QC it.

    The temporary stage is context-owned, so exceptions and cancellation
    remove partial codecs. WAV uses the canonical extensible writer; FLAC
    receives the RFC 9639 WAVEFORMATEXTENSIBLE_CHANNEL_MASK comment.
    """
    catalogue = layout_catalog.get_catalog()
    layout = layout_catalog.find(catalogue, layout_id)
    channels = layout['channels']
    encoding = 'float32' if depth == 'float32' else 'pcm' + depth
    if fmt == 'WAV':
        canonical_labels, permutation = adm_compat.canonical_wav_labels(
            labels, proof.BITS)
    else:
        canonical_labels, permutation = list(labels), list(range(len(labels)))
    mask = sum(1 << proof.BITS[label] for label in canonical_labels)
    subtype = 'FLOAT' if depth == 'float32' else 'PCM_' + depth
    with tempfile.TemporaryDirectory(prefix='.upmix-stream-', dir=outdir) as td:
        path = pathlib.Path(td) / ('stream.' + FORMATS[fmt][0])
        stream_gain, stream_info = upmix_stream.compute_global_gain(
            data, layout_id, sample_rate,
            centre=centre, surround=surround, height=height,
            block_samples=block_samples,
            progress=lambda fraction: progress(.68 + .12 * fraction))
        blocks_written = 0
        block_total = max(1, math.ceil(
            len(data) / upmix_engine._round_to_hop(
                int(block_samples), upmix_engine._frame_size(sample_rate) // 4)))
        if fmt == 'WAV':
            writer = adm_compat.StreamingWAVWriter(
                path, sample_rate, canonical_labels, encoding, proof.BITS,
                expected_frames)
            try:
                for _start, _end, block in upmix_stream.stream_pass2(
                        data, layout_id, sample_rate, stream_gain,
                        centre=centre, surround=surround, height=height,
                        block_samples=block_samples):
                    writer.write(block[:, permutation])
                    blocks_written += 1
                    progress(.81 + .13 * blocks_written / block_total)
                writer.close()
            except BaseException:
                writer.close(abort=True)
                raise
        else:
            with sf.SoundFile(str(path), mode='x', samplerate=sample_rate,
                              channels=channels, format=fmt,
                              subtype=subtype) as encoded:
                for _start, _end, block in upmix_stream.stream_pass2(
                        data, layout_id, sample_rate, stream_gain,
                        centre=centre, surround=surround, height=height,
                        block_samples=block_samples):
                    encoded.write(block[:, permutation])
                    blocks_written += 1
                    progress(.81 + .13 * blocks_written / block_total)
            if fmt == 'FLAC':
                adm_compat.rewrite_flac_channel_mask(path, mask)
        progress(.95)
        peak_qc = 0.0
        lfe_peak = 0.0
        lfe_index = (canonical_labels.index('LFE')
                     if 'LFE' in canonical_labels else None)
        expected_format = 'WAVEX' if fmt == 'WAV' else fmt
        expected_subtype = subtype
        with sf.SoundFile(str(path), mode='r') as qc:
            if qc.samplerate != sample_rate or qc.channels != channels:
                raise ValueError('streamed QC rate/channel mismatch')
            if qc.format != expected_format or qc.subtype != expected_subtype:
                raise ValueError(
                    'streamed QC codec/depth mismatch: {f}/{s} != {ef}/{es}'.format(
                        f=qc.format, s=qc.subtype,
                        ef=expected_format, es=expected_subtype))
            if len(qc) != expected_frames:
                raise ValueError(
                    'streamed QC frame count {a} != expected {e}; refused'.format(
                        a=len(qc), e=expected_frames))
            frames_checked = 0
            while True:
                block = qc.read(4096, dtype='float64', always_2d=True)
                if len(block) == 0:
                    break
                frames_checked += len(block)
                if not np.isfinite(block).all():
                    raise ValueError('streamed QC: non-finite samples')
                peak_qc = max(peak_qc, float(np.max(np.abs(block))))
                if lfe_index is not None:
                    lfe_peak = max(
                        lfe_peak,
                        float(np.max(np.abs(block[:, lfe_index]), initial=0.0)))
                progress(.95 + .04 * min(1.0, frames_checked / max(1, expected_frames)))
        if fmt == 'WAV':
            wav = adm_compat.read_wav_extensible_info(path)
            expected_bits = 32 if depth == 'float32' else int(depth)
            expected_subformat = 3 if depth == 'float32' else 1
            if (wav['channels'] != channels or wav['sample_rate'] != sample_rate
                    or wav['bits'] != expected_bits
                    or wav['valid_bits'] != expected_bits
                    or wav['subformat'] != expected_subformat
                    or wav['channel_mask'] != mask):
                raise ValueError('streamed WAV extensible header/mask QC failed')
        elif fmt == 'FLAC' and adm_compat.read_flac_channel_mask(path) != mask:
            raise ValueError('streamed FLAC channel-mask QC failed')
        if frames_checked != expected_frames:
            raise ValueError('streamed QC read a different frame count')
        if peak_qc > upmix_engine.PEAK_CEILING + 1e-9:
            raise ValueError(
                'streamed QC peak {p:.6f} > -1 dBFS ceiling; refused'.format(
                    p=peak_qc))
        if lfe_peak != 0.0:
            raise ValueError('streamed QC LFE must remain silent')
        progress(.99)
        yield path, canonical_labels, stream_gain, stream_info, blocks_written, peak_qc


# ---------------------------------------------------------------------------
# REPAIR_DELTA #7 — publication exception-safe transaction.
# Both os.link calls live inside one exception-safe critical section. On
# failure / KeyboardInterrupt we compare the candidate's lstat dev/ino
# against the staged source; if it matches, the link was created by THIS
# attempt and we own it; otherwise it's a preexisting collision sentinel
# and we MUST NOT delete it. The function returns success / failure but
# is allowed to raise KeyboardInterrupt when no link was created.
# ---------------------------------------------------------------------------


def _stage_identity(staged):
    """Snapshot (dev, ino) of the staged file BEFORE linking."""
    st = os.lstat(staged)
    return (st.st_dev, st.st_ino)


def _publish_pair(meta_staged, audio_staged, sidecar, output):
    """Publish the (sidecar, output) pair from the (meta_staged,
    audio_staged) tempdir. Returns ``True`` on success.

    Both ``os.link`` calls happen inside one transaction. If anything
    goes wrong (including KeyboardInterrupt between the two calls) we
    verify dev/ino match the staged inode before deleting anything we
    just created. Preexisting files with the same names are NEVER
    removed by this transaction.
    """
    global _DEFERRED_TERMINATE
    meta_own = (os.lstat(meta_staged).st_dev, os.lstat(meta_staged).st_ino)
    audio_own = (os.lstat(audio_staged).st_dev, os.lstat(audio_staged).st_ino)
    if sidecar.exists() or output.exists():
        raise ValueError('destination exists; choose another folder/name, '
                         'overwrite forbidden')
    # Defer SIGTERM during the tiny commit window. The first os.link
    # actually creates an inode we own; between that point and the
    # second link, we MUST clear the defer flag so a deferred SIGTERM
    # doesn't run after we've already published one half.
    published = []
    try:
        os.link(meta_staged, sidecar)
        published.append(('sidecar', sidecar, meta_own))
        os.link(audio_staged, output)
        published.append(('output', output, audio_own))
    except BaseException:
        # Walk the half-published links; only delete those whose
        # dev/ino still matches what WE created (proves we own them).
        for path, own in ((sidecar, meta_own), (output, audio_own)):
            try:
                st = os.lstat(path)
                if (st.st_dev, st.st_ino) == own:
                    os.unlink(path)
            except FileNotFoundError:
                pass
            except Exception:
                pass
        raise
    return True


def _deferred_sigterm_handler(signum, frame):
    global _DEFERRED_TERMINATE
    if _COMMITTING:
        _DEFERRED_TERMINATE = signum
    else:
        raise KeyboardInterrupt('conversion cancelled')


def _parse_upmix_config(request):
    """Validate and normalise the optional ``upmix`` block from a convert
    request. Returns a dict with the validated parameters plus a ``reason``
    string (empty on success). Strict refusals only — the block is opt-in
    and silent for ordinary requests.

    v1: ``mode`` = ``experimental-stereo-to-5.1-side-v1`` (default for
    backwards compatibility); accepts ``centre`` + ``surround``.
    v2: ``mode`` = ``upmix-generic-v2``; requires ``layout`` (one of the
    implemented catalogue ids) and accepts ``centre``, ``surround`` and
    ``height``. Only implemented layouts are accepted; reference /
    explicit-input-only ids are refused.
    """
    raw = request.get('upmix')
    if raw is None:
        return {'enabled': False, 'mode': None, 'layout': None,
                'centre': None, 'surround': None, 'height': None,
                'reason': ''}
    if not isinstance(raw, dict):
        return {'enabled': False, 'reason': 'upmix must be a JSON object'}
    # Mode selection. Default to v1 for backwards compatibility; the
    # absence of an explicit ``mode`` does not change behaviour for
    # requests that only carry ``centre`` / ``surround``.
    mode = raw.get('mode')
    if mode is None:
        # Infer from presence of ``layout``: explicit layout implies v2.
        if 'layout' in raw:
            mode = UPMIX_MODE_V2
        else:
            mode = UPMIX_MODE_V1
    if mode not in (UPMIX_MODE_V1, UPMIX_MODE_V2):
        return {'enabled': False,
                'reason': 'upmix mode must be {a} or {b}'.format(
                    a=UPMIX_MODE_V1, b=UPMIX_MODE_V2)}
    allowed = UPMIX_KEYS_V1 if mode == UPMIX_MODE_V1 else UPMIX_KEYS_V2
    bad = sorted(k for k in raw if k not in allowed)
    if bad:
        return {'enabled': False,
                'reason': 'upmix keys must be in {allowed}; got {got}'.format(
                    allowed=sorted(allowed), got=bad)}
    result = {'enabled': True, 'mode': mode, 'layout': None,
              'centre': None, 'surround': None, 'height': None,
              'reason': ''}
    if mode == UPMIX_MODE_V2:
        layout_id = raw.get('layout')
        if not isinstance(layout_id, str) or not layout_id:
            return {'enabled': False,
                    'reason': 'upmix v2 requires layout id'}
        catalogue = layout_catalog.get_catalog()
        if not layout_catalog.is_implemented(catalogue, layout_id):
            return {'enabled': False,
                    'reason': 'upmix layout {l} not implemented'.format(
                        l=layout_id)}
        result['layout'] = layout_id
    defaults = layout_catalog.get_catalog()['defaults']
    ranges = layout_catalog.get_catalog()['ranges']

    def _read_amount(name, default):
        if name not in raw:
            return default
        v = raw[name]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError('upmix {n} must be a real number'.format(n=name))
        fv = float(v)
        if not math.isfinite(fv):
            raise ValueError('upmix {n} must be finite'.format(n=name))
        lo, hi = ranges[name]
        if not (lo <= fv <= hi):
            raise ValueError(
                'upmix {n} must be in [{lo}, {hi}]'.format(
                    n=name, lo=lo, hi=hi))
        return fv
    try:
        result['centre'] = _read_amount('centre', defaults['centre'])
        result['surround'] = _read_amount('surround', defaults['surround'])
        result['height'] = _read_amount('height', defaults['height'])
    except ValueError as exc:
        return {'enabled': False, 'reason': str(exc)}
    return result


def convert(request, progress=lambda x:None):
    global _COMMITTING
    path=pathlib.Path(request['input']); info=probe(path)
    fmt=request.get('format','WAV'); target=request.get('target','stereo'); depth=request.get('depth','24')
    if fmt not in FORMATS: raise ValueError('unsupported output format')
    rate=int(request.get('rate',0)) or info['sample_rate']
    mp3_bitrate = request.get('mp3_bitrate', 320)
    if fmt == 'MP3':
        if type(mp3_bitrate) is not int or mp3_bitrate not in MP3_BITRATES:
            raise ValueError('MP3 bitrate must be 128, 192, 256 or 320 kbps')
        if not int(request.get('rate', 0)) and rate not in MP3_RATES:
            rate = 48000
        if rate not in MP3_RATES:
            raise ValueError('MP3 supports 44.1/48 kHz; select a supported output sample rate')
    if rate not in (44100,48000,88200,96000,176400,192000) and rate!=info['sample_rate']:
        raise ValueError('unsupported output sample rate')

    upmix_cfg = _parse_upmix_config(request)
    if upmix_cfg['reason']:
        raise ValueError(upmix_cfg['reason'])
    upmix_enabled = upmix_cfg['enabled']
    upmix_centre = upmix_cfg['centre']
    upmix_surround = upmix_cfg['surround']
    upmix_height = upmix_cfg['height']
    upmix_mode = upmix_cfg['mode']
    upmix_layout_id = upmix_cfg['layout']
    # v1 eligibility: PCM stereo + 2 channels + target 5.1-side.
    # v2 eligibility: PCM stereo + 2 channels + target is an implemented
    # layout id; the Upmix DSP then replaces the matrix mix entirely.
    upmix_eligible = (not info['adm']
                      and (request.get('layout') or info['layout']) == 'stereo'
                      and info['channels'] == 2)
    v1_target_ok = upmix_mode == UPMIX_MODE_V1 and target == '5.1-side'
    v2_target_ok = (upmix_mode == UPMIX_MODE_V2
                    and target == upmix_layout_id)
    if upmix_enabled and not (upmix_eligible and (v1_target_ok or v2_target_ok)):
        raise ValueError(
            'upmix requires PCM stereo source (2 actual channels); '
            'v1 needs target 5.1-side, v2 needs target == upmix.layout')
    if upmix_enabled and rate not in upmix_engine.SUPPORTED_RATES:
        raise ValueError(
            'upmix output rate must be one of 44.1, 48, 88.2, 96, '
            '176.4 or 192 kHz')

    # Validate the final container contract before rendering or staging.
    if info['adm']:
        final_channels = {'stereo':2, '5.1-side':6,
                          '7.1.4-rear-first':12, 'binaural':2}.get(target)
    else:
        final_channels = 2 if target == 'binaural' else len(proof.LAYOUTS.get(target, []))
    if not final_channels:
        raise ValueError('unsupported conversion target')
    adm_compat.validate_depth_container(fmt, depth, final_channels)

    outdir=pathlib.Path(request['out_dir']); outdir.mkdir(parents=True,exist_ok=True)
    ext=FORMATS[fmt][0]
    output=outdir/(path.stem+'-'+target+'.'+ext)
    sidecar=output.with_suffix(output.suffix+'.conversion.json')
    # The collision check stays as a hard guard; _publish_pair enforces
    # the same invariant atomically.
    if output.exists() or sidecar.exists():
        raise ValueError('destination exists; choose another folder/name, overwrite forbidden')

    progress(.05)
    notes=[]
    stream_tmp_owner = None
    renderer='PCM format conversion'
    tail=0
    layout=None
    src_frames = info['frames']
    src_sr = info['sample_rate']
    src_n_ch = info['channels']

    if info['adm']:
        # REPAIR_DELTA #3 — per-target gate with float64 (8 bytes), real
        # tail of round(.25*source_rate), output resampling rate, 12ch
        # intermediate, mono duplication, upsampled PCM output.
        elig, elig_reason, _ = adm_compat.target_eligibility(
            src_frames, src_sr, src_n_ch, target,
            out_rate=rate, source_layout=None)
        if not elig:
            raise ValueError(elig_reason)
        render_target='7.1.4-rear-first' if target=='binaural' else target
        data, sr, render_notes = render_adm(
            path, render_target, progress, request.get('programme') or None)
        notes.extend(render_notes)
        # Capture timing-fixes / pack-compat notes that EAR warned about.
        layout=render_target
        renderer='EBU EAR 2.1.0 ADM metadata renderer'
        upmix_record = None
    else:
        layout=request.get('layout') or info['layout']
        if layout not in proof.LAYOUTS or len(proof.LAYOUTS[layout])!=src_n_ch:
            raise ValueError('explicit source channel layout required; channel count is not a layout')
        if info['layout']!='unknown' and info['layout']!=layout:
            explicit_orders = {
                item['id'] for item in layout_catalog.get_catalog()['input_orders']}
            declared = proof.LAYOUTS.get(info['layout'], [])
            selected = proof.LAYOUTS.get(layout, [])
            same_mask = (declared and selected
                         and sum(1 << proof.BITS[x] for x in declared)
                         == sum(1 << proof.BITS[x] for x in selected))
            if layout not in explicit_orders or not same_mask:
                raise ValueError('selected layout conflicts with source channel mask')
        # REPAIR_DELTA #3 — per-target gate for PCM path: float64 source
        # bytes + per-target downmix / mono-duplication estimate. The
        # generic v2 upmix streaming path replaces the gate with its own
        # streaming-aware memory check below.
        # Generic v2 is always the bounded stream path. The old request
        # flag is ignored for compatibility; backend v1 remains unchanged.
        upmix_streaming_requested = (upmix_enabled
                                     and upmix_mode == UPMIX_MODE_V2)
        elig, elig_reason, _ = adm_compat.target_eligibility(
            src_frames, src_sr, src_n_ch, target,
            out_rate=rate, source_layout=layout,
            adm_eligible=False)
        if not elig and not upmix_streaming_requested:
            raise ValueError(elig_reason)
        if upmix_streaming_requested:
            source_bytes = info['frames'] * src_n_ch * 8
            if source_bytes > MAX_DECODE_BYTES:
                raise ValueError(
                    'source decoded PCM exceeds 512 MiB safety cap; refused')
            if rate != src_sr:
                estimated_stereo_frames = (
                    info['frames'] * rate + src_sr - 1) // src_sr
                estimated_stereo_bytes = estimated_stereo_frames * 2 * 8
                if estimated_stereo_bytes > MAX_DECODE_BYTES:
                    raise ValueError(
                        'stereo sample-rate conversion exceeds 512 MiB '
                        'safety cap; refused')
        data, sr = sf.read(path, dtype='float64', always_2d=True)
        if upmix_streaming_requested and data.nbytes > MAX_DECODE_BYTES:
            raise ValueError(
                'decoded source PCM exceeds 512 MiB safety cap; refused')
        if len(data) != info['frames']:
            notes.append('Source header frames are estimated; full decoder returned {actual} frames rather than {header}.'.format(actual=len(data), header=info['frames']))
        src_frames = len(data)
        upmix_record = None
        if upmix_enabled:
            # Upmix replaces the normal matrix mix entirely. The
            # six-column (or wider) layout is produced by the upmix stage;
            # LoRo-fold compatibility comes from the in-DSP allocation,
            # not from the proof.matrix_for path.
            if upmix_mode == UPMIX_MODE_V1:
                layout = '5.1-side'
                renderer = ('Experimental stereo → 5.1-side covariance / '
                            'Wiener upmix (v1), no normalization')
            else:
                layout = upmix_layout_id
                renderer = ('Experimental stereo → {n} covariance / Wiener '
                            'upmix (v2), no normalization'.format(
                                n=layout))
            target = layout
        elif target!='binaural' and target!=layout:
            if layout=='mono' and target=='stereo':
                data=np.repeat(data,2,axis=1); layout='stereo'; renderer='Mono duplicated to L/R, no normalization'
            else:
                # Known multichannel PCM layouts, including explicit
                # side-first 7.1.4, may fold to Stereo through the named
                # LoRo matrix. Other target changes remain unsupported.
                # Matrix construction raises for any unimplemented pair.
                m, g = proof.matrix_for(proof.LAYOUTS[layout], proof.LAYOUTS.get(target,[]))
                m=np.asarray(m)/g
                data = adm_compat.native_mix(data.astype('f4'), m.astype('f4'))
                layout=target
                renderer='Monitor precise sum, LoRo matrix, no normalization'

    if target=='binaural':
        data, sp=binaural(data, sr, layout)
        tail=sp['tail_frames']
        layout='stereo'
        renderer+=' → Apple AUSpatialMixer HRTF (250ms tail)'
    # Pass source-layout labels to encode; encode reports the actual
    # on-disk column order back via the return value.
    labels=proof.LAYOUTS[layout]
    source_layout = info['layout'] if info['adm'] else request.get('layout') or info['layout']
    serialization_layout = layout
    progress(.65)

    # Output resampling. Estimate upsampled PCM output before allocating
    # the full target rate ndarray; refuse if the result would exceed
    # the 512 MiB safety cap.
    if upmix_enabled:
        # Channel count depends on the selected layout (v1: 6; v2: 4-12).
        catalogue = layout_catalog.get_catalog()
        if upmix_mode == UPMIX_MODE_V1:
            n_channels = 6
            label_list = list(proof.LAYOUTS['5.1-side'])
        else:
            v2_layout = layout_catalog.find(catalogue, upmix_layout_id)
            n_channels = v2_layout['channels']
            label_list = list(v2_layout['labels'])
        # Memory gate: refuse before any upmix / SRC allocation. Output
        # frame count is exact from the source rate (no double-ratio).
        # The 512 MiB cap is for the in-memory allocation; the streaming
        # v2 path bypasses it because the renderer never holds the full
        # N-channel buffer in memory.
        final_frames = (src_frames * rate + src_sr - 1) // src_sr
        streaming_check = (upmix_mode == UPMIX_MODE_V2)
        if (not streaming_check
                and final_frames * n_channels * 8 > MAX_DECODE_BYTES):
            raise ValueError(
                'upmix output at {r} Hz exceeds 512 MiB safety cap '
                '({n} MiB estimated); refused'.format(
                    r=rate, n=final_frames * n_channels * 8 // (1024 * 1024)))
        # RIFF size precheck for WAV. Refuse if the RIFF container would
        # exceed 4 GiB; no RF64 fallback is implemented here. The check
        # runs BEFORE any allocation so a too-large request is rejected
        # before the tempdir is staged.
        if fmt == 'WAV':
            encoding = 'float32' if depth == 'float32' else 'pcm' + depth
            try:
                adm_compat.wav_riff_size_bytes(
                    final_frames, n_channels, encoding)
            except ValueError as exc:
                raise ValueError(
                    'WAV RIFF output exceeds 4 GiB limit; refused: {e}'.format(
                        e=exc)) from exc
        upmix_sr = rate
        if sr != rate:
            estimated_stereo_bytes = (
                (len(data) * rate + sr - 1) // sr) * 2 * 8
            if upmix_streaming_requested and estimated_stereo_bytes > MAX_DECODE_BYTES:
                raise ValueError(
                    'stereo sample-rate conversion exceeds 512 MiB '
                    'safety cap; refused')
            divisor = gcd(rate, sr)
            data = resample_poly(data, rate // divisor, sr // divisor, axis=0)
            if upmix_streaming_requested and data.nbytes > MAX_DECODE_BYTES:
                raise ValueError(
                    'stereo sample-rate conversion exceeds 512 MiB '
                    'safety cap; refused')
            sr = rate
        notes.append('Upmix applied at {r} Hz (requested supported rate)'.format(r=rate))
        # Streaming v2 path: pass1 estimates the global peak, pass2
        # streams hop-aligned blocks directly to a staged temp file
        # using a sequential sf.SoundFile writer. The full N-channel
        # buffer is never materialised; memory stays bounded.
        streaming = upmix_mode == UPMIX_MODE_V2
        if streaming:
            block_samples = request.get(
                'upmix_block_samples', upmix_engine.BLOCK_SAMPLES_DEFAULT)
            with _streamed_upmix_stage(
                    data, upmix_layout_id, upmix_sr, label_list, fmt, depth,
                    final_frames, outdir, upmix_centre, upmix_surround,
                    upmix_height, block_samples, progress) as streamed:
                stg_path, labels, stream_gain, stream_info, blocks_written, peak_qc = streamed
                stream_peak = stream_info['peak']
                true_peak_4x = stream_info['estimated_true_peak_4x']
                combined_gain = stream_gain
                upmix_meta = {
                    'algorithm': upmix_engine.GENERIC_ALGORITHM_ID,
                    'version': upmix_engine.GENERIC_ALGORITHM_VERSION,
                    'experimental': True,
                    'centre': upmix_centre,
                    'surround': upmix_surround,
                    'height': upmix_height,
                    'lfe_silent': True,
                    'loro_compatible_allocation': True,
                    'sample_ceiling_db': 20.0 * math.log10(
                        upmix_engine.PEAK_CEILING),
                    'streaming': True,
                    'blocks_written': blocks_written,
                    'estimated_true_peak_4x': true_peak_4x,
                    'estimated_peak_bound_4x': stream_info['peak_bound_4x'],
                    'estimated_true_peak_after_gain': true_peak_4x * stream_gain,
                    'true_peak_target_dbfs': stream_info['peak_target_dbfs'],
                    'true_peak_method': stream_info['true_peak_method'],
                }
                data = None  # downstream uses `tmp` only.
                sr = rate
                upmix_record = dict(upmix_meta)
                upmix_record['applied_gain_linear'] = float(combined_gain)
                upmix_record['applied_gain_db'] = 20.0 * math.log10(
                    max(combined_gain, 1e-30))
                upmix_record['mode'] = upmix_mode
                upmix_record['upmix_sr'] = upmix_sr
                upmix_record['output_sr'] = rate
                upmix_record['layout_id'] = upmix_layout_id
                upmix_record['layout_name'] = layout_catalog.find(
                    catalogue, upmix_layout_id)['name']
                notes.append(
                    'Streaming Upmix: 4x estimated peak {p:.6f}, blocks {b}'.format(
                        p=stream_info['peak_bound_4x'], b=blocks_written))
                # Move the validated stream into the ordinary transaction
                # directory while its stage owner is active.
                stream_tmp_owner = tempfile.TemporaryDirectory(
                    prefix='.conversion-', dir=outdir)
                tmp_dir = pathlib.Path(stream_tmp_owner.name)
                tmp = tmp_dir / ('encoded.' + FORMATS[fmt][0])
                try:
                    os.replace(str(stg_path), str(tmp))
                except BaseException:
                    stream_tmp_owner.cleanup()
                    raise
        else:
            if upmix_mode == UPMIX_MODE_V1:
                six, applied_gain = upmix_engine.upmix_pcm_to_5_1_side(
                    data, upmix_sr, centre=upmix_centre,
                    surround=upmix_surround)
                upmix_meta = {
                    'algorithm': upmix_engine.ALGORITHM_ID,
                    'version': upmix_engine.ALGORITHM_VERSION,
                    'experimental': True,
                    'centre': upmix_centre,
                    'surround': upmix_surround,
                    'lfe_silent': True,
                    'loro_compatible_allocation': True,
                    'sample_ceiling_db': 20.0 * math.log10(
                        upmix_engine.PEAK_CEILING),
                    'per_pair': [
                        {'left': 'SL', 'right': 'SR',
                         'kind': 'surround', 'amount': upmix_surround,
                         'filter': 'full'}
                    ],
                }
            else:
                six, applied_gain, upmix_meta = upmix_engine.upmix_pcm(
                    data, upmix_layout_id, upmix_sr,
                    centre=upmix_centre, surround=upmix_surround,
                    height=upmix_height)
            # Combined static common attenuation: SRC can nudge a sample that
            # was at the ceiling slightly above it; apply the additional gain
            # (always ≤ 1) and record the combined value.
            combined_gain = applied_gain
            if six.shape[0]:
                peak_after_src = float(np.max(np.abs(six), initial=0))
                if peak_after_src > upmix_engine.PEAK_CEILING:
                    extra = upmix_engine.PEAK_CEILING / peak_after_src
                    six = six * extra
                    combined_gain = applied_gain * extra
            data = six
            sr = rate
            # Labels are the layout's native order (same as the upmix output).
            labels = label_list
            upmix_record = dict(upmix_meta)
            upmix_record['applied_gain_linear'] = float(combined_gain)
            upmix_record['applied_gain_db'] = 20.0 * math.log10(max(combined_gain, 1e-30))
            upmix_record['mode'] = upmix_mode
            upmix_record['upmix_sr'] = upmix_sr
            upmix_record['output_sr'] = rate
            if 'lfe_silent' not in upmix_record:
                upmix_record['lfe_silent'] = True
            if 'loro_compatible_allocation' not in upmix_record:
                upmix_record['loro_compatible_allocation'] = True
    elif rate != sr:
        divisor=gcd(rate, sr)
        est_frames = (len(data) * rate + sr - 1) // sr
        est_bytes = est_frames * len(labels) * 8
        if est_bytes > MAX_DECODE_BYTES:
            raise ValueError(
                'output at {r} Hz exceeds 512 MiB safety cap ({n} MiB); '
                'refused'.format(r=rate, n=est_bytes // (1024 * 1024)))
        data=resample_poly(data, rate//divisor, sr//divisor, axis=0); sr=rate
        notes.append('Polyphase SRC scipy.signal.resample_poly')

    if data is not None and (not np.isfinite(data).all()
                              or np.max(np.abs(data), initial=0) > 1):
        raise ValueError('output exceeds 0 dBFS; normalization is off. Reduce source/render gain before conversion.')

    # Strict output / sidecar collision check (parent remains guard).
    if output.exists() or sidecar.exists():
        if data is None and tmp is not None:
            if stream_tmp_owner is not None:
                stream_tmp_owner.cleanup()
        raise ValueError('destination exists; choose another folder/name, overwrite forbidden')

    # Streaming v2 path: cleanup logic is handled inside the streaming
    # branch above. Now wire into the staging block.
    _streaming_active = (data is None and tmp is not None
                          and tmp.exists())
    if _streaming_active:
        tmp_dir = pathlib.Path(tmp.parent)
        meta = tmp_dir / 'report.json'
    else:
        tmp_dir = None
        tmp = None
        meta = None
    _tmpdir_ctx = (tempfile.TemporaryDirectory(prefix='.conversion-', dir=outdir)
                   if not _streaming_active else stream_tmp_owner)
    with _tmpdir_ctx as td_ctx:
        if not _streaming_active:
            tmp_dir = pathlib.Path(td_ctx)
            tmp = tmp_dir / ('encoded.'+ext)
            meta = tmp_dir / 'report.json'
        report_frames = (len(data) if data is not None
                          else int(math.ceil(src_frames * rate / src_sr)))
        report_peak = (float(np.max(np.abs(data), initial=0))
                       if data is not None else 0.0)
        report = {'source':str(path),'output':str(output),'renderer':renderer,
                  'format':fmt,'depth':None if fmt == 'MP3' else depth,'sample_rate':sr,
                  'frames':report_frames,'duration':report_frames/sr,
                  'labels':labels,'source_layout':source_layout,
                  'source_frames':src_frames, 'source_header_frames':info['frames'],
                  'tail_frames_at_source_rate':tail,
                  'sample_peak':report_peak,
                  'normalization':'off',
                  'metadata':'ADM consumed to PCM render; no reverse ADM export' if info['adm'] else 'tags not copied',
                  'warnings':list(notes),
                  'qc':'Pending decode'}
        if fmt == 'MP3':
            report['codec'] = {'name': 'MPEG Layer III', 'encoder': 'libsndfile/LAME',
                               'bitrate_mode': 'CBR', 'bitrate_kbps': mp3_bitrate}
            if not int(request.get('rate', 0)) and info['sample_rate'] not in MP3_RATES:
                report['warnings'].append('MP3 source-rate mode resolves to 48000 Hz for this source; polyphase SRC applied')
        if upmix_record is not None:
            report['upmix'] = upmix_record
            if upmix_record.get('applied_gain_linear', 1.0) < 1.0:
                report['warnings'].append(
                    'upmix applied one static common attenuation to keep peak ≤ '
                    '-1 dBFS; normalisation is off by user choice')
        meta.write_text(json.dumps(report, indent=2))
        # Encode the audio to the staged path unless streaming already
        # wrote the file directly. Catch encoding failures so the
        # staging tempdir still gets cleaned up.
        if data is None:
            # Streaming path: the file is already at ``tmp`` and the
            # sequential QC during streaming has validated rate /
            # channels / frames / peak / LFE silence. Reflect the
            # actual peak into the report.
            report['sample_peak'] = peak_qc
            progress(.99)
        else:
            try:
                labels = encode(tmp, data, sr, labels, fmt, depth, mp3_bitrate)
            except Exception:
                raise
            progress(.85)
            # Decode finished file for channels / rate / frame count /
            # nonfinite and lossy peak QC.
            decoded, qrate = sf.read(tmp, dtype='float64', always_2d=True)
            if qrate != sr or decoded.shape[1] != len(labels):
                raise ValueError('final decoded format QC failed')
            if abs(len(decoded) - len(data)) > (512 if fmt == 'OGG' else 0):
                raise ValueError('final decoded duration QC failed')
            if not np.isfinite(decoded).all():
                raise ValueError('final decoder returned nonfinite samples')
            # MP3 reconstruction can overshoot full scale for valid PCM input.
            # Keep the source level unchanged; report playback headroom instead
            # of rejecting a valid bitstream or secretly normalizing/clamping it.
            decoded_peak = float(np.max(np.abs(decoded), initial=0))
            if fmt == 'MP3' and decoded_peak > 1.0:
                report['warnings'].append(
                    'MP3 decoded peak {p:.6f} ({db:+.2f} dBFS): lossy '
                    'reconstruction overshoot; source level unchanged. '
                    'Playback may clip; reduce source gain for more headroom.'
                    .format(p=decoded_peak, db=20 * math.log10(decoded_peak)))
            else:
                adm_compat.refuse_lossy_peak(decoded, fmt)
            report['labels'] = labels
            report['sample_peak'] = float(np.max(np.abs(decoded), initial=0))
        if fmt == 'WAV':
            source_labels = proof.LAYOUTS[serialization_layout]
            if list(labels) != list(source_labels):
                report['warnings'].append(
                    'WAV columns reordered to canonical ascending-bit mask '
                    'order: {src} -> {dst}'.format(src=source_labels, dst=list(labels)))
        report['qc'] = 'Decoded final file: duration/rate/channels/finite samples PASS'
        meta.write_text(json.dumps(report, indent=2))

        # CLI cancellation is deferred through publication AND the done event.
        # Imported library callers retain their own signal policy.
        if _CLI_RUN:
            _COMMITTING = True
        _publish_pair(meta, tmp, sidecar, output)
    progress(1)
    return report


def main():
    global _CLI_RUN, _COMMITTING, _DEFERRED_TERMINATE
    _CLI_RUN = True
    signal.signal(signal.SIGTERM, _deferred_sigterm_handler)
    req=json.load(sys.stdin)
    if req['action']=='probe':
        for path in req['paths']:
            try: emit(event='probe',info=probe(path))
            except Exception as e: emit(event='unsupported',path=path,error=str(e))
    else:
        try:
            report=convert(req,lambda v:emit(event='progress',value=v))
            emit(event='done',report=report)
        except Exception as e:
            emit(event='failed',error=str(e)); return 1
        finally:
            _COMMITTING = False
            _DEFERRED_TERMINATE = None
    return 0
if __name__=='__main__': sys.exit(main())
