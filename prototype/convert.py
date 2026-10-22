#!/usr/bin/env python3
"""Offline, bounded WAV proof; stdlib only. Never starts an audio device."""
import argparse, array, json, math, pathlib, struct, subprocess, sys, tempfile
ROOT = pathlib.Path(__file__).resolve().parent.parent
LAYOUTS = {
 'mono': ['FC'], 'stereo': ['FL','FR'],
 'quad': ['FL','FR','BL','BR'],
 '5.0-side': ['FL','FR','FC','SL','SR'],
 '5.1-rear': ['FL','FR','FC','LFE','BL','BR'],
 '5.1-side': ['FL','FR','FC','LFE','SL','SR'],
 '7.0': ['FL','FR','FC','BL','BR','SL','SR'],
 '7.1': ['FL','FR','FC','LFE','BL','BR','SL','SR'],
 '5.1.2': ['FL','FR','FC','LFE','SL','SR','TFL','TFR'],
 '5.1.4': ['FL','FR','FC','LFE','SL','SR','TFL','TFR','TBL','TBR'],
 '7.1.2': ['FL','FR','FC','LFE','BL','BR','SL','SR','TFL','TFR'],
 '7.1.4-rear-first': ['FL','FR','FC','LFE','BL','BR','SL','SR','TFL','TFR','TBL','TBR'],
 '7.1.4-side-first': ['FL','FR','FC','LFE','SL','SR','BL','BR','TFL','TFR','TBL','TBR'],
}
BITS = {'FL':0,'FR':1,'FC':2,'LFE':3,'BL':4,'BR':5,'SL':9,'SR':10,'TFL':12,'TFR':14,'TBL':15,'TBR':17}
LIMIT = 128 * 1024 * 1024

def read_wav(path, labels):
    if pathlib.Path(path).stat().st_size > LIMIT: raise ValueError('proof limit: 128 MiB input')
    data = pathlib.Path(path).read_bytes()
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE': raise ValueError('only RIFF WAV; no RF64/BW64/ADM')
    if len(data) < 12 or struct.unpack_from('<I', data, 4)[0] + 8 != len(data): raise ValueError('invalid RIFF size')
    chunks = {}; offset = 12
    while offset < len(data):
        if offset + 8 > len(data): raise ValueError('truncated chunk')
        tag, size = struct.unpack_from('<4sI', data, offset); offset += 8
        if offset + size > len(data): raise ValueError('truncated chunk payload')
        if tag in chunks: raise ValueError('duplicate chunk')
        chunks[tag] = data[offset:offset+size]; offset += size + (size & 1)
    if set(chunks) - {b'fmt ',b'data',b'fact',b'JUNK',b'PAD '}: raise ValueError('metadata-bearing WAV refused: no silent metadata loss')
    fmt = chunks[b'fmt ']
    code, ch, rate, byte_rate, align, bits = struct.unpack_from('<HHIIHH', fmt)
    mask = 0
    if code == 65534:
        if len(fmt) < 40: raise ValueError('short extensible fmt')
        cb, valid, mask = struct.unpack_from('<HHI',fmt,16)
        if cb != 22 or valid != bits: raise ValueError('unsupported valid-bit packing')
        if fmt[28:40] != bytes.fromhex('00001000800000aa00389b71'): raise ValueError('unknown subformat GUID')
        code = struct.unpack_from('<I',fmt,24)[0]
    if code not in (1,3) or bits not in (16,24,32) or (code == 3 and bits != 32): raise ValueError('PCM16/24/32 or float32 only')
    if ch != len(labels) or align != ch * (bits//8) or byte_rate != rate*align or not 8000 <= rate <= 192000: raise ValueError('format/layout mismatch')
    # Extensible masks encode ascending bit order. Do not ignore conflicting labels.
    if mask:
        declared = [name for name, bit in sorted(BITS.items(), key=lambda kv:kv[1]) if mask & (1 << bit)]
        if mask != sum(1 << BITS[x] for x in declared) or declared != labels: raise ValueError('channel mask conflicts with explicit layout')
    raw = chunks[b'data']
    if len(raw) % align: raise ValueError('partial frame')
    width = bits//8
    if code == 3: samples = list(struct.unpack('<'+'f'*(len(raw)//4),raw))
    else: samples = [int.from_bytes(raw[i:i+width],'little',signed=True)/(2**(bits-1)) for i in range(0,len(raw),width)]
    if not all(math.isfinite(x) and abs(x) <= 1 for x in samples): raise ValueError('nonfinite or out-of-range input')
    return samples,rate

def write_wav(path, samples, rate, labels, encoding):
    bits = 32 if encoding == 'float32' else int(encoding[3:]); code = 3 if encoding == 'float32' else 1
    if any(not math.isfinite(x) or abs(x) > 1 for x in samples): raise ValueError('clipping: output refused')
    if code == 3: raw = struct.pack('<'+'f'*len(samples),*samples)
    else:
        scale = 2**(bits-1)
        # Deterministic nearest-even, no dither. Proof setting, not mastering default.
        raw = b''.join(max(-scale,min(scale-1,round(x*scale))).to_bytes(bits//8,'little',signed=True) for x in samples)
    # Explicit extensible labels; only canonical ascending mask order can be serialized.
    if labels != sorted(labels,key=lambda x:BITS[x]): raise ValueError('output must use canonical mask order')
    mask = sum(1 << BITS[x] for x in labels); align = len(labels)*bits//8
    fmt = struct.pack('<HHIIHHHHII',65534,len(labels),rate,rate*align,align,bits,22,bits,mask,code)+bytes.fromhex('00001000800000aa00389b71')
    chunks = b'fmt '+struct.pack('<I',len(fmt))+fmt
    if code == 3: chunks += b'fact'+struct.pack('<II',4,len(samples)//len(labels))
    chunks += b'data'+struct.pack('<I',len(raw))+raw+(b'\0' if len(raw)&1 else b'')
    with open(path,'xb') as f: f.write(b'RIFF'+struct.pack('<I',len(chunks)+4)+b'WAVE'+chunks)

def matrix_for(src,dst):
    m = [[0.0]*len(src) for _ in dst]
    def put(s,d,g):
        if s in src and d in dst: m[dst.index(d)][src.index(s)] += g
    if src == dst:
        for s in src: put(s,s,1)
    elif dst == LAYOUTS['stereo']:
        put('FL','FL',1); put('FR','FR',1)
        for d in dst: put('FC',d,math.sqrt(.5))
        for s in ('BL','SL','TFL','TBL'): put(s,'FL',math.sqrt(.5))
        for s in ('BR','SR','TFR','TBR'): put(s,'FR',math.sqrt(.5))
        # LFE omitted. LoRo only, no LtRt phase encoding or bass management.
    elif len(src) == 12 and len(dst) == 6:
        for s in ('FL','FR','FC','LFE'): put(s,s,1)
        left,right = dst[4:]
        for s in ('SL','BL'): put(s,left,1)
        for s in ('SR','BR'): put(s,right,1)
        # Local experimental preset matching existing engine's stated assumption.
        for s in ('TFL','TBL'): put(s,left,10**(-3/20))
        for s in ('TFR','TBR'): put(s,right,10**(-3/20))
    else: raise ValueError('unsupported mapping; upmix/LtRt/binaural unavailable')
    # Global, deterministic worst-case row-sum headroom, includes identity mapping.
    bound = max(sum(abs(x) for x in row) for row in m)
    gain = 1 / max(1,bound) * (10**(-1/20) if bound > 1 else 1)
    return [[x*gain for x in row] for row in m], gain

def mix(samples, m):
    helper = ROOT/'.build/mix-helper'
    if not helper.exists(): raise ValueError('run prototype/build.sh first')
    with tempfile.TemporaryDirectory(prefix='utuvo-convert-') as td:
        td = pathlib.Path(td); (td/'in.raw').write_bytes(struct.pack('<'+'f'*len(samples),*samples)); (td/'matrix.json').write_text(json.dumps({'matrix':m}))
        subprocess.run([str(helper),str(td/'in.raw'),str(td/'out.raw'),str(td/'matrix.json')],check=True)
        raw = (td/'out.raw').read_bytes(); return list(struct.unpack('<'+'f'*(len(raw)//4),raw))

def resample(samples, channels, source_rate, target_rate):
    if source_rate == target_rate: return samples
    n = len(samples)//channels; count = round(n*target_rate/source_rate)
    cutoff = min(1,target_rate/source_rate)*.90; radius = 32
    output = []
    for f in range(count):
        pos = f*source_rate/target_rate; start = math.floor(pos)-radius+1
        taps = []
        for k in range(start,start+2*radius):
            delta = pos-k
            if abs(delta) >= radius: continue
            sinc = cutoff if delta == 0 else math.sin(math.pi*cutoff*delta)/(math.pi*delta)
            w = sinc*(.5+.5*math.cos(math.pi*delta/radius)); taps.append((min(n-1,max(0,k)),w))
        norm = sum(w for k,w in taps)
        for c in range(channels): output.append(sum(samples[k*channels+c]*w for k,w in taps)/norm)
    return output

def convert(source, destination, layout, target, encoding='pcm24', rate=None):
    src,dst = LAYOUTS[layout],LAYOUTS[target]
    if pathlib.Path(destination).exists(): raise ValueError('output exists; overwrite forbidden')
    samples,sr = read_wav(source,src); m,gain = matrix_for(src,dst)
    output = mix(samples,m); target_rate = rate or sr
    if target_rate not in (44100,48000,96000,192000): raise ValueError('output rate subset: 44.1/48/96/192 kHz')
    output = resample(output,len(dst),sr,target_rate)
    write_wav(destination,output,target_rate,dst,encoding)
    return {'input':str(source),'output':str(destination),'input_labels':src,'output_labels':dst,'matrix_out_in':m,'global_gain':gain,'sample_rate':target_rate,'frames':len(output)//len(dst),'sample_peak':max(map(abs,output),default=0),'encoding':encoding,'dither':'none','renderer':'custom PCM matrix; not Atmos/ADM, LtRt or binaural'}

def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('inputs',nargs='+',type=pathlib.Path); p.add_argument('--out-dir',required=True,type=pathlib.Path)
    p.add_argument('--layout',choices=LAYOUTS,required=True); p.add_argument('--target',choices=LAYOUTS,required=True); p.add_argument('--encoding',choices=['pcm16','pcm24','pcm32','float32'],default='pcm24'); p.add_argument('--rate',type=int)
    a = p.parse_args(); a.out_dir.mkdir(parents=True,exist_ok=True); failed = False
    for i, source in enumerate(a.inputs):
        dest = a.out_dir/(source.stem+'-'+a.target+'.wav')
        try: print(json.dumps(convert(source,dest,a.layout,a.target,a.encoding,a.rate)))
        except Exception as e: failed=True; print(json.dumps({'input':str(source),'error':str(e)}),file=sys.stderr)
    return int(failed)
if __name__ == '__main__': sys.exit(main())
