"""Synthetic dynamic ADM: one PCM track moves left→right; same PCM, different metadata."""
from pathlib import Path
import json, subprocess, sys
import numpy as np, soundfile as sf
root=Path(__file__).resolve().parents[1]; dest=root/'fixtures'; dest.mkdir(exist_ok=True)
sr=48000; t=np.arange(sr)/sr; pcm=.1*np.sin(2*np.pi*1000*t)
sf.write(dest/'object-pcm.wav',pcm,sr,subtype='PCM_24')
for name,left,right in [('moving',30,-30),('left',30,30),('right',-30,-30)]:
 meta={'name':'Synthetic '+name,'items':[{'type':'Objects','name':'tone object','channels':[1],'blocks':[{'rtime':'0','duration':'0.5','position':{'azimuth':left,'elevation':0,'distance':1},'jumpPosition':{'flag':True}},{'rtime':'0.5','duration':'0.5','position':{'azimuth':right,'elevation':0,'distance':1},'jumpPosition':{'flag':True}}]}]}
 path=dest/(name+'.json'); path.write_text(json.dumps(meta))
 subprocess.run([str(root/'.runtime/bin/ear-utils'),'make_test_bwf','-i',str(dest/'object-pcm.wav'),'-m',str(path),str(dest/(name+'-adm.wav'))],check=True)
print(dest)
