#!/usr/bin/env python3
"""Regression tests for the argument-translation functions in plex-runner.py.

Run:  python3 runner/test_translations.py
These import the runner module (which needs runner.token and creates runner/logs), but start no server and
run no transcodes. Add a test here for every new rewrite rule or bug fix.
"""
import importlib.util, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = [sys.argv[0]]
# Hermetic: never depend on (or write into) the real .env, token or log directory.
import tempfile
_tmp = tempfile.mkdtemp(prefix="plex-runner-test-")
os.environ["PLEX_MEDIA_DIR"] = os.path.join(_tmp, "media"); os.makedirs(os.environ["PLEX_MEDIA_DIR"], exist_ok=True)
os.environ["BRIDGE_TOKEN"] = "test-token"; os.environ["PLEX_RUNNER_LOGDIR"] = os.path.join(_tmp, "logs")
_spec = importlib.util.spec_from_file_location("plex_runner", os.path.join(HERE, "plex-runner.py"))
R = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(R)


class Decoders(unittest.TestCase):
    def test_aac_lc_mapped_before_input_only(self):
        a = "-codec:0 h264 -codec:1 aac_lc -i F -codec:1 aac_lc".split()
        out, ch = R.map_decoders(a)
        self.assertEqual(out[3], "aac"); self.assertEqual(out[-1], "aac_lc")   # output side untouched
        self.assertEqual(ch, ["aac_lc->aac"])

    def test_unknown_decoder_untouched(self):
        a = "-codec:0 msmpeg4 -i F".split()
        self.assertEqual(R.map_decoders(a), (a, []))


class HwEncode(unittest.TestCase):
    A = ("-codec:0 h264 -i F -codec:0 libx264 -crf:0 16 -maxrate:0 3419k -bufsize:0 6838k -r:0 24 "
         "-preset:0 veryfast -x264opts:0 subme=2 -force_key_frames:0 expr:x -codec:1 aac -b:1 256k -f dash dash").split()

    def test_swap_drops_x264_options_and_sets_bitrate(self):
        out, streams = R.use_hw_encoder(self.A)
        self.assertEqual(streams, ["0"])
        self.assertIn("h264_videotoolbox", out); self.assertNotIn("libx264", out)
        for gone in ("-crf:0", "-preset:0", "-x264opts:0"): self.assertNotIn(gone, out)
        i = out.index("h264_videotoolbox"); self.assertEqual(out[i + 1:i + 3], ["-allow_sw", "0"])
        self.assertEqual(out[out.index("-b:0") + 1], str(int(3419000 * 0.85)))
        for kept in ("-maxrate:0", "-bufsize:0", "-force_key_frames:0", "aac"): self.assertIn(kept, out)

    def test_copy_untouched(self):
        a = "-codec:0 h264 -i F -codec:0 copy -f dash dash".split()
        self.assertEqual(R.use_hw_encoder(a), (a, []))

    def test_hw_decode_only_for_h264_hevc(self):
        a = "-codec:0 hevc -i F -codec:0 h264_videotoolbox".split()
        out, ok = R.use_hw_decoder(a)
        self.assertTrue(ok); self.assertEqual(out[out.index("-hwaccel") + 1], "videotoolbox")
        self.assertLess(out.index("-hwaccel"), out.index("-i"))
        self.assertFalse(R.use_hw_decoder("-codec:0 vp9 -i F".split())[1])


class MinSize(unittest.TestCase):
    CHAIN = "[0:2]scale=720:300[0];[0:0]scale=w=720:h=300:force_divisible_by=4[1];[1]format=pix_fmts=yuv420p|nv12[2];[2][0]overlay[3]"

    def test_upscale_video_and_subtitle_canvas_together(self):
        out, note = R.fit_hw_min_size(["-filter_complex", self.CHAIN], ["0"])
        self.assertEqual(note, "720x300->864x360")
        self.assertIn("[0:2]scale=864:360[0]", out[1]); self.assertIn("scale=w=864:h=360", out[1])

    def test_label_variants(self):
        for lab in ("[0:0]", "[0:1]", "[0:#0x1e0]"):
            self.assertIsNotNone(R.fit_hw_min_size(["-filter_complex", lab + "scale=w=352:h=263:x[0]"], ["0"])[1])

    def test_large_outputs_and_audio_chains_untouched(self):
        self.assertIsNone(R.fit_hw_min_size(["[0:0]scale=w=1280:h=720:x[0]"], ["0"])[1])
        self.assertIsNone(R.fit_hw_min_size(["[0:1] aresample=async=1:osr=48000[2]"], ["0"])[1])

    def test_results_are_multiples_of_four_and_big_enough(self):
        for w, h in ((320, 240), (352, 263), (512, 288), (426, 240)):
            out, note = R.fit_hw_min_size(["[0:0]scale=w=%d:h=%d:x[0]" % (w, h)], ["0"])
            w2, h2 = map(int, note.split("->")[1].split("x"))
            self.assertGreaterEqual(w2, R.HW_MIN_W); self.assertGreaterEqual(h2, R.HW_MIN_H)
            self.assertEqual((w2 % 4, h2 % 4), (0, 0))


class Fallback(unittest.TestCase):
    def test_software_fallback_removes_hwaccel_and_allows_sw(self):
        a = "-codec:0 h264 -hwaccel videotoolbox -i F -codec:0 h264_videotoolbox -allow_sw 0".split()
        self.assertEqual(R.software_fallback(a), "-codec:0 h264 -i F -codec:0 h264_videotoolbox -allow_sw 1".split())


class Validation(unittest.TestCase):
    def test_rejects_paths_protocols(self):
        self.assertIn("path not allowed", R.check_args(["-i", "/etc/passwd"]))
        self.assertIn("protocol not allowed", R.check_args(["-i", "file:///etc/hosts"]))
        self.assertIn("protocol not allowed", R.check_args(["-i", "http://evil.example/x"]))

    def test_allows_media_and_pms_urls(self):
        self.assertIsNone(R.check_args(["-i", os.path.join(R.MEDIA, "x.mkv"), "-progressurl", "http://127.0.0.1:32400/video/:/transcode/session/a/b/progress"]))

    def test_redaction(self):
        self.assertNotIn("SECRET", R.redact("http://x/?X-Plex-Token=SECRET&a=1"))
        self.assertNotIn("SECRET", R.redact("X_PLEX_TOKEN=SECRET"))

    def test_env_allowlist(self):
        env = R.build_env({"X_PLEX_TOKEN": "t", "PLEX_UID": "1", "PATH": "/evil", "LD_PRELOAD": "x", "FOO": "bar"})
        self.assertIn("X_PLEX_TOKEN", env); self.assertNotIn("LD_PRELOAD", env); self.assertNotIn("FOO", env)
        self.assertEqual(env["PATH"], "/usr/bin:/bin"); self.assertNotIn("PLEX_UID", env)


class Diff(unittest.TestCase):
    def test_diff_reports_replacements(self):
        d = R.argv_diff(["-a", "libx264", "-b"], ["-a", "h264_videotoolbox", "-allow_sw", "0", "-b"])
        self.assertEqual(d[0]["op"], "replace"); self.assertEqual(d[0]["from"], ["libx264"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
