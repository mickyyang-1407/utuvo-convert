"""A valid full-scale PCM batch must survive MP3 reconstruction overshoot."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import worker


class MP3PeakRegression(unittest.TestCase):
    def test_loud_batch_preserves_level_and_warns(self):
        with tempfile.TemporaryDirectory(prefix='utuvo-mp3-peak-') as folder:
            root = Path(folder)
            t = np.arange(48000) / 48000
            signal = np.column_stack([.98*np.where(np.sin(2*np.pi*f*t)>0,1.,-1.) for f in (997,1117)])
            for bitrate in (128,320):
                for index in (1,2):
                    source = root / f'square-{index}.wav'
                    sf.write(source, signal, 48000, subtype='PCM_24')
                    before = source.read_bytes()
                    report = worker.convert(dict(input=str(source),out_dir=str(root/str(bitrate)),
                        format='MP3',depth='24',target='stereo',mp3_bitrate=bitrate))
                    decoded, rate = sf.read(report['output'],always_2d=True)
                    self.assertEqual(decoded.shape,signal.shape)
                    self.assertEqual(rate,48000)
                    self.assertTrue(np.isfinite(decoded).all())
                    self.assertGreater(np.max(np.abs(decoded)),1)
                    self.assertGreater(report['sample_peak'],1)
                    self.assertEqual(report['normalization'],'off')
                    self.assertTrue(any('source level unchanged' in w for w in report['warnings']))
                    self.assertEqual(source.read_bytes(),before)
                    self.assertEqual(json.loads(Path(report['output']+'.conversion.json').read_text()),report)

    def test_quiet_output_and_invalid_source_keep_existing_policy(self):
        with tempfile.TemporaryDirectory(prefix='utuvo-mp3-peak-policy-') as folder:
            root=Path(folder)
            t=np.arange(48000)/48000
            signal=.1*np.sin(2*np.pi*440*t)
            source=root/'quiet.wav'
            sf.write(source,signal,48000,subtype='PCM_24')
            report=worker.convert(dict(input=str(source),out_dir=str(root/'quiet'),
                format='MP3',depth='24',target='stereo'))
            self.assertFalse(any('MP3 decoded peak' in w for w in report['warnings']))
            source=root/'overload.wav'
            sf.write(source,signal*20,48000,subtype='FLOAT')
            with self.assertRaisesRegex(ValueError,'output exceeds 0 dBFS'):
                worker.convert(dict(input=str(source),out_dir=str(root/'invalid'),
                    format='MP3',depth='24',target='stereo'))
            self.assertFalse(list((root/'invalid').glob('*.mp3')))
            with self.assertRaisesRegex(ValueError,'Vorbis re-encode'):
                worker.adm_compat.refuse_lossy_peak(np.array([[1.01]]),'OGG')


if __name__ == '__main__':
    unittest.main(verbosity=2)
