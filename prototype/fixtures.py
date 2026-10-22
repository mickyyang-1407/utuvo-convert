import math, pathlib, sys
import convert as c
p=pathlib.Path(sys.argv[1] if len(sys.argv)>1 else 'fixtures'); p.mkdir(parents=True,exist_ok=True)
rate=48000
samples=[]
for f in range(4800):
    x=.25*math.sin(2*math.pi*1000*f/rate); samples += [x,-x]
c.write_wav(p/'stereo-tone.wav',samples,rate,c.LAYOUTS['stereo'],'float32')
c.write_wav(p/'stereo-silence.wav',[0]*9600,rate,c.LAYOUTS['stereo'],'pcm16')
samples=[0.0]*(4800*12)
for ch in range(12): samples[(100+ch*300)*12+ch]=.5
c.write_wav(p/'714-impulses.wav',samples,rate,c.LAYOUTS['7.1.4-rear-first'],'float32')
print('Generated synthetic tone, silence, per-channel impulse fixtures')
