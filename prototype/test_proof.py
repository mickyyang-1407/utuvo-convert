import hashlib, json, math, pathlib, struct, tempfile, unittest
import convert as c
class Proof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root=pathlib.Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def wav(self,name,samples,rate,labels,encoding='float32'):
        p=self.root/name; c.write_wav(p,samples,rate,labels,encoding); return p
    def test_each_714_channel_independent_impulse_and_order(self):
        # Independent constants: not derived from implementation's matrix.
        g=10**(-3/20); expected_gain=10**(-1/20)/(2+2*g)
        for layout in ('7.1.4-rear-first','7.1.4-side-first'):
            labels=c.LAYOUTS[layout]
            for target in ('5.1-rear','5.1-side'):
                for slot,label in enumerate(labels):
                    samples=[0.0]*12*24; samples[slot+12*7]=.5
                    # Serialize custom order as maskless PCM, requiring explicit choice.
                    raw=struct.pack('<'+'f'*len(samples),*samples)
                    fmt=struct.pack('<HHIIHH',3,12,48000,48000*48,48,32)
                    blob=b'fmt '+struct.pack('<I',16)+fmt+b'data'+struct.pack('<I',len(raw))+raw
                    inp=self.root/'in.wav'; inp.write_bytes(b'RIFF'+struct.pack('<I',len(blob)+4)+b'WAVE'+blob)
                    out=self.root/'out.wav'; report=c.convert(inp,out,layout,target,'float32')
                    decoded,sr=c.read_wav(out,c.LAYOUTS[target]); out.unlink()
                    expected=[0.0]*6
                    dest={'FL':0,'FR':1,'FC':2,'LFE':3,'BL':4,'SL':4,'TFL':4,'TBL':4,'BR':5,'SR':5,'TFR':5,'TBR':5}[label]
                    expected[dest]=.5*expected_gain*(g if label.startswith('T') else 1)
                    for f in range(24):
                        for ch in range(6): self.assertAlmostEqual(decoded[f*6+ch],expected[ch] if f==7 else 0,places=7)
                    self.assertAlmostEqual(report['global_gain'],expected_gain)
    def test_loro_lfe_silence_and_opposition(self):
        labels=c.LAYOUTS['5.1-side']
        samples=[]
        for f in range(480): samples += [0,0,0,.5,0,0]
        inp=self.wav('lfe.wav',samples,48000,labels); out=self.root/'out.wav'
        c.convert(inp,out,'5.1-side','stereo','float32'); result,_=c.read_wav(out,c.LAYOUTS['stereo']); self.assertEqual(max(map(abs,result)),0)
        m,g=c.matrix_for(labels,c.LAYOUTS['stereo'])
        output=c.mix([.5,0,0,0,-.5/math.sqrt(.5),0],m)
        self.assertAlmostEqual(output[0],0,places=7); self.assertEqual(output[1],0)
        self.assertEqual(c.mix([0]*60,m),[0]*20)
    def test_headroom_coherent_and_precision(self):
        m,g=c.matrix_for(c.LAYOUTS['7.1.4-rear-first'],c.LAYOUTS['stereo'])
        output=c.mix([1]*12,m)
        self.assertLessEqual(max(map(abs,output)),10**(-1/20)+1e-7)
        # Double accumulator preserves residual lost by sequential Float summation.
        self.assertEqual(c.mix([1e8,1,-1e8],[[1,1,1]]),[1])
    def test_integer_roundtrip_and_source_integrity(self):
        for bits in (16,24,32):
            samples=[-.75,-.25,0,.25,.75,1,-1,0]
            inp=self.wav('in'+str(bits)+'.wav',samples,48000,c.LAYOUTS['stereo'],'pcm'+str(bits)); before=hashlib.sha256(inp.read_bytes()).hexdigest()
            out=self.root/('out'+str(bits)+'.wav'); c.convert(inp,out,'stereo','stereo','pcm'+str(bits))
            decoded,sr=c.read_wav(out,c.LAYOUTS['stereo'])
            for actual,expected in zip(decoded,samples): self.assertLessEqual(abs(actual-expected),2**(-(bits-1)))
            self.assertEqual(hashlib.sha256(inp.read_bytes()).hexdigest(),before)
            with self.assertRaises(ValueError): c.convert(inp,out,'stereo','stereo')
    def test_resample_duration_tone_and_antialias(self):
        sr=48000; n=4800
        for freq in (1000,18000):
            samples=[.25*math.sin(2*math.pi*freq*f/sr) for f in range(n)]
            res=c.resample(samples,1,sr,44100 if freq==1000 else 16000)
            target=44100 if freq==1000 else 16000
            self.assertEqual(len(res),round(n*target/sr))
            rms=math.sqrt(sum(x*x for x in res[100:-100])/len(res[100:-100]))
            if freq==1000: self.assertAlmostEqual(rms,.25/math.sqrt(2),delta=.001)
            else: self.assertLess(rms,.001)
        zero=c.resample([0]*480,2,48000,96000); self.assertEqual(zero,[0]*960)
    def test_conversion_resample_and_duration(self):
        n=4800; labels=c.LAYOUTS['stereo']
        signal=[]
        for f in range(n):
            x=.2*math.sin(2*math.pi*1000*f/48000); signal += [x,-x]
        inp=self.wav('tone.wav',signal,48000,labels)
        for rate in (44100,96000,192000):
            out=self.root/('tone'+str(rate)+'.wav')
            report=c.convert(inp,out,'stereo','stereo','pcm24',rate)
            decoded,sr=c.read_wav(out,labels)
            self.assertEqual(sr,rate); self.assertEqual(len(decoded)//2,round(n*rate/48000))
            self.assertLessEqual(abs(report['frames']/rate-n/48000),.5/rate)
            self.assertLess(max(abs(decoded[i]+decoded[i+1]) for i in range(0,len(decoded),2)),1e-7)
        with self.assertRaises(ValueError): c.convert(inp,self.root/'fail.wav','stereo','stereo','float32',16000)

    def test_reject_layout_metadata_and_clipping(self):
        inp=self.wav('in.wav',[0]*24,48000,c.LAYOUTS['7.1.4-rear-first'])
        with self.assertRaises(ValueError): c.read_wav(inp,c.LAYOUTS['7.1.4-side-first'])
        b=inp.read_bytes()+b'axml'+struct.pack('<I',2)+b'xx'; b=b[:4]+struct.pack('<I',len(b)-8)+b[8:]; inp.write_bytes(b)
        with self.assertRaises(ValueError): c.read_wav(inp,c.LAYOUTS['7.1.4-rear-first'])
        with self.assertRaises(ValueError): c.write_wav(self.root/'bad.wav',[1.01],48000,['FC'],'pcm16')
if __name__=='__main__': unittest.main(verbosity=2)
