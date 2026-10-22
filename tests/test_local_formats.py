"""Qualify the local formats listed by the UI, using generated signals only."""
import hashlib, json, pathlib, sys, tempfile, unittest
import numpy as np
import soundfile as sf
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import worker

class LocalFormats(unittest.TestCase):
    def setUp(self):
        self.stage = tempfile.TemporaryDirectory(prefix="utuvo-formats-")
        self.root = pathlib.Path(self.stage.name)
        t = np.arange(4800) / 48000
        self.signal = np.column_stack((.12*np.sin(2*np.pi*440*t), .09*np.sin(2*np.pi*997*t)))

    def tearDown(self):
        self.stage.cleanup()

    def test_input_containers_to_wav(self):
        for fmt, subtype, extension in (("WAV","PCM_24","wav"),("AIFF","PCM_24","aiff"),
                                       ("FLAC","PCM_24","flac"),("OGG","VORBIS","ogg"),
                                       ("CAF","PCM_24","caf"),("MP3","MPEG_LAYER_III","mp3")):
            with self.subTest(format=fmt):
                source = self.root / ("signal." + extension)
                sf.write(source, self.signal, 48000, format=fmt, subtype=subtype)
                digest = hashlib.sha256(source.read_bytes()).digest()
                decoded, sr = sf.read(source, always_2d=True)
                info = worker.probe(source)
                self.assertEqual(info["layout"], "stereo")
                report = worker.convert(dict(input=str(source),out_dir=str(self.root/fmt),target="stereo",format="WAV",depth="24"))
                output, rate = sf.read(report["output"], always_2d=True)
                self.assertEqual(output.shape, decoded.shape)
                self.assertEqual(rate, sr)
                self.assertLessEqual(np.max(np.abs(output-decoded)), 2**-23)
                self.assertEqual(digest, hashlib.sha256(source.read_bytes()).digest())
                self.assertEqual(report["normalization"], "off")
                self.assertEqual(report["labels"], ["FL", "FR"])

    def test_export_depths_and_rates(self):
        source = self.root / "source.wav"
        sf.write(source, self.signal, 48000, subtype="PCM_24")
        for fmt, depths in (("WAV",("16","24","32","float32")),("AIFF",("16","24","32")),("FLAC",("16","24")),("OGG",("24",))):
            for depth in depths:
                with self.subTest(format=fmt,depth=depth):
                    report = worker.convert(dict(input=str(source),out_dir=str(self.root/(fmt+depth)),target="stereo",format=fmt,depth=depth))
                    data, sr = sf.read(report["output"], always_2d=True)
                    self.assertEqual(data.shape, self.signal.shape)
                    self.assertTrue(np.isfinite(data).all())
                    self.assertGreater(np.max(np.abs(data)), .05)
                    self.assertLess(np.max(np.abs(data)), 1)
                    self.assertEqual(sr, 48000)
                    subtype = sf.info(report["output"]).subtype
                    self.assertEqual(subtype, "VORBIS" if fmt=="OGG" else "FLOAT" if depth=="float32" else "PCM_"+depth)
        for rate in (44100,48000,88200,96000,176400,192000):
            report = worker.convert(dict(input=str(source),out_dir=str(self.root/str(rate)),target="stereo",format="WAV",depth="24",rate=rate))
            self.assertEqual(report["frames"], round(4800*rate/48000))
            self.assertEqual(sf.info(report["output"]).samplerate, rate)

if __name__ == "__main__":
    unittest.main(verbosity=2)
