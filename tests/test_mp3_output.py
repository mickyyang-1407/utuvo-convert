"""Generated fixtures only; independently decode real MP3 bitstreams."""
import hashlib
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
import worker


class MP3Output(unittest.TestCase):
    def setUp(self):
        self.owner = tempfile.TemporaryDirectory(prefix='utuvo-mp3-')
        self.root = Path(self.owner.name)

    def tearDown(self):
        self.owner.cleanup()

    def source(self, rate=48000, channels=2, frames=None):
        n = frames or rate
        t = np.arange(n) / rate
        signal = np.column_stack([.1*np.sin(2*np.pi*(440+220*c)*t) for c in range(channels)])
        p = self.root / f'signal-{rate}-{channels}-{n}.wav'
        sf.write(p, signal, rate, subtype='PCM_24')
        return p

    def req(self, source, tag, **extra):
        return dict(input=str(source), out_dir=str(self.root/tag), format='MP3',
                    depth='24', target='stereo', **extra)

    def oracle(self, report, rate, channels, bitrate, expected_frames):
        p = report['output']
        probe = subprocess.run([shutil.which('ffprobe') or 'ffprobe', '-v', 'error',
            '-show_streams', '-of', 'json', p], capture_output=True, text=True, check=True)
        stream = json.loads(probe.stdout)['streams'][0]
        self.assertEqual(stream['codec_name'], 'mp3')
        self.assertEqual(int(stream['sample_rate']), rate)
        self.assertEqual(stream['channels'], channels)
        self.assertEqual(int(stream['bit_rate']), bitrate*1000)
        decoded = subprocess.run([shutil.which('ffmpeg') or 'ffmpeg', '-v', 'error', '-nostdin',
            '-i', p, '-f', 'f32le', '-'], capture_output=True, check=True)
        samples = np.frombuffer(decoded.stdout, dtype='<f4').reshape(-1, channels)
        self.assertEqual(len(samples), expected_frames)
        self.assertTrue(np.isfinite(samples).all())
        self.assertGreater(np.max(np.abs(samples)), .04)
        self.assertLess(np.max(np.abs(samples)), 1)
        sidecar = json.loads(Path(p + '.conversion.json').read_text())
        self.assertEqual(sidecar['codec']['bitrate_kbps'], bitrate)
        self.assertEqual(sidecar['codec']['bitrate_mode'], 'CBR')
        self.assertIsNone(sidecar['depth'])
        self.assertEqual(sidecar['normalization'], 'off')
        self.assertIn('PASS', sidecar['qc'])
        self.assertFalse(any(x.name.startswith('.conversion-') for x in Path(p).parent.iterdir()))

    def test_all_bitrates_rates_and_mono_stereo(self):
        for channels in (1, 2):
            for rate in (44100, 48000):
                source = self.source(rate, channels)
                before = hashlib.sha256(source.read_bytes()).hexdigest()
                for bitrate in worker.MP3_BITRATES:
                    with self.subTest(channels=channels, rate=rate, bitrate=bitrate):
                        req = self.req(source, f'{channels}-{rate}-{bitrate}', mp3_bitrate=bitrate)
                        req['target'] = 'mono' if channels == 1 else 'stereo'
                        report = worker.convert(req)
                        self.oracle(report, rate, channels, bitrate, rate)
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_high_rate_source_auto_and_explicit_src(self):
        source = self.source(96000)
        auto = worker.convert(self.req(source, 'auto'))
        self.oracle(auto, 48000, 2, 320, 48000)
        self.assertTrue(any('48000 Hz' in x for x in auto['warnings']))
        selected = worker.convert(self.req(source, 'selected', rate=44100))
        self.oracle(selected, 44100, 2, 320, 44100)

    def test_invalid_settings_refuse_before_publication(self):
        source = self.source()
        for tag, extra in [('rate', {'rate':96000}), ('bitrate', {'mp3_bitrate':999}),
                           ('fractional', {'mp3_bitrate':192.5}), ('bool', {'mp3_bitrate':True}),
                           ('depth', {'depth':'float32'})]:
            req = self.req(source, tag)
            req.update(extra)
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                worker.convert(req)
            self.assertFalse(Path(req['out_dir']).exists())
        source = self.source(channels=6)
        req = self.req(source, 'six', layout='5.1-side')
        req['target'] = '5.1-side'
        with self.assertRaisesRegex(ValueError, 'mono/stereo'):
            worker.convert(req)
        self.assertFalse(Path(req['out_dir']).exists())

    def test_collision_preserves_existing_mp3_and_report(self):
        source = self.source()
        req = self.req(source, 'collision')
        report = worker.convert(req)
        files = [Path(report['output']), Path(report['output']+'.conversion.json')]
        before = [p.read_bytes() for p in files]
        with self.assertRaisesRegex(ValueError, 'destination exists'):
            worker.convert(req)
        self.assertEqual([p.read_bytes() for p in files], before)

    def test_lossy_overflow_is_not_silently_clipped(self):
        with self.assertRaisesRegex(ValueError, 'MP3 re-encode'):
            worker.adm_compat.refuse_lossy_peak(np.array([[1.01, -.1]]), 'MP3')

    def test_short_gapless_output(self):
        source = self.source(frames=257)
        report = worker.convert(self.req(source, 'short'))
        self.oracle(report, 48000, 2, 320, 257)


if __name__ == '__main__':
    unittest.main(verbosity=2)
