"""Tests for the FEX whole-app translation fallback in the proton launcher.

Run with: python3 -m unittest discover -s tests
"""

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_proton():
    loader = importlib.machinery.SourceFileLoader("proton", os.path.join(REPO_ROOT, "proton"))
    spec = importlib.util.spec_from_loader("proton", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


proton = load_proton()


class FakeProton:
    """A Proton whose bin/lib/dist dirs live under a temporary root."""

    def __init__(self, root):
        self.root = root
        self.bin_dir = os.path.join(root, "bin") + "/"
        self.lib_dir = os.path.join(root, "lib") + "/"
        self.dist_dir = os.path.join(root, "dist") + "/"
        self.wine_bin = self.bin_dir + "wine"
        os.makedirs(self.bin_dir, exist_ok=True)

    def path(self, d):
        return os.path.join(self.root, d)


class FullTranslationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        proton.g_compatdata = proton.CompatData(self.tmp.name)
        proton.g_proton = FakeProton(self.tmp.name)
        self.session = proton.Session()
        self.session.log_file = None
        self._saved_game_id = os.environ.pop("SteamGameId", None)

    def tearDown(self):
        if self._saved_game_id is not None:
            os.environ["SteamGameId"] = self._saved_game_id
        self.tmp.cleanup()

    def write_compiler(self, bitness=64, path=None):
        compiler = path or (proton.g_proton.bin_dir
                            + proton.FEX_OFFLINE_COMPILER + str(bitness) + ".exe")
        with open(compiler, "w") as f:
            f.write("")
        return compiler

    def test_cache_dir_is_next_to_the_prefix(self):
        self.assertEqual(self.session.fex.cache_dir(), self.tmp.name + "/fex-cache/")

    def test_cache_dir_is_given_to_fex_as_a_windows_path(self):
        # FEX reads FEX_APP_CACHE_LOCATION verbatim and prefixes it with \??\,
        # so the value must be a Windows path without a prefix of its own.
        self.assertEqual(self.session.fex.cache_dir_windows(),
                         "Z:" + self.tmp.name.replace("/", "\\") + "\\fex-cache\\")

    def test_default_is_call_by_call(self):
        self.assertFalse(self.session.fex.should_translate_whole_app())

    def test_marker_enables_full_translation(self):
        self.session.fex.mark_full_translation()
        self.assertTrue(os.path.exists(self.session.fex.full_translation_marker()))
        self.assertTrue(self.session.fex.should_translate_whole_app())

    def test_env_var_forces_full_translation(self):
        self.session.env["PROTON_FEX_FULL_TRANSLATION"] = "1"
        self.assertTrue(self.session.fex.should_translate_whole_app())

    def test_env_var_zero_disables_full_translation(self):
        self.session.fex.mark_full_translation()
        self.session.env["PROTON_FEX_FULL_TRANSLATION"] = "0"
        self.assertFalse(self.session.fex.should_translate_whole_app())

    def test_full_translation_config_sets_code_caching(self):
        config = self.session.generate_fex_app_config(full_translation=True)["Config"]
        self.assertEqual(config["EnableCodeCachingWIP"], "1")
        self.assertEqual(config["Multiblock"], "1")

    def test_default_config_does_not_set_code_caching(self):
        config = self.session.generate_fex_app_config()["Config"]
        self.assertNotIn("EnableCodeCachingWIP", config)

    def test_offline_compiler_lookup(self):
        self.assertIsNone(self.session.fex.offline_compiler_path())
        compiler = self.write_compiler(64)
        self.assertEqual(self.session.fex.offline_compiler_path(), compiler)

    def test_offline_compiler_prefers_the_matching_guest_bitness(self):
        compiler32 = self.write_compiler(32)
        compiler64 = self.write_compiler(64)
        self.assertEqual(self.session.fex.offline_compiler_path(True), compiler64)
        self.assertEqual(self.session.fex.offline_compiler_path(False), compiler32)

    def test_offline_compiler_found_in_the_dist(self):
        dist_bin = os.path.join(proton.g_proton.dist_dir, "bin")
        os.makedirs(dist_bin)
        compiler = self.write_compiler(64, path=os.path.join(dist_bin, "FEXOfflineCompiler64.exe"))
        self.assertEqual(self.session.fex.offline_compiler_path(), compiler)

    def test_cache_generation_is_skipped_without_code_maps(self):
        calls = []
        original = proton.subprocess.call
        proton.subprocess.call = lambda *a, **k: calls.append(a)
        try:
            self.session.fex.save_code_cache()
        finally:
            proton.subprocess.call = original
        self.assertEqual(calls, [])

    def test_cache_generation_skipped_without_compiler(self):
        new_dir = os.path.join(self.session.fex.cache_dir(), "codemap", "new")
        os.makedirs(new_dir)
        with open(os.path.join(new_dir, "app.bin"), "w") as f:
            f.write("x")
        calls = []
        original = proton.subprocess.call
        proton.subprocess.call = lambda *a, **k: calls.append(a)
        try:
            self.session.fex.save_code_cache()
        finally:
            proton.subprocess.call = original
        self.assertEqual(calls, [])

    def test_cache_generation_invokes_offline_compiler(self):
        new_dir = os.path.join(self.session.fex.cache_dir(), "codemap", "new")
        os.makedirs(new_dir)
        with open(os.path.join(new_dir, "app.bin"), "w") as f:
            f.write("x")
        self.write_compiler()

        calls = []
        original = proton.subprocess.call
        proton.subprocess.call = lambda *a, **k: calls.append(a)
        try:
            self.session.fex.save_code_cache()
        finally:
            proton.subprocess.call = original

        self.assertEqual(len(calls), 1)
        argv = calls[0][0]
        self.assertEqual(argv[0], proton.g_proton.wine_bin)
        compiler = self.session.fex.offline_compiler_path()
        self.assertEqual(argv[1], "Z:" + compiler.replace("/", "\\"))
        self.assertEqual(argv[2], "process-all")


class CacheVersionTest(unittest.TestCase):
    """The saved translation is rebuilt when the FEX shipped with Proton changes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        proton.g_compatdata = proton.CompatData(self.tmp.name)
        proton.g_proton = FakeProton(self.tmp.name)
        self.session = proton.Session()
        self.session.log_file = None
        self._saved_game_id = os.environ.pop("SteamGameId", None)
        self.version_dir = os.path.join(proton.g_proton.dist_dir, "share", "fex-emu")
        os.makedirs(self.version_dir, exist_ok=True)

    def tearDown(self):
        if self._saved_game_id is not None:
            os.environ["SteamGameId"] = self._saved_game_id
        self.tmp.cleanup()

    def set_runtime_version(self, text):
        with open(os.path.join(self.version_dir, "version"), "w") as f:
            f.write(text)

    def test_no_runtime_version_means_nothing_to_compare(self):
        self.assertFalse(self.session.fex.cache_is_stale())

    def test_empty_cache_without_a_stamp_is_not_stale(self):
        self.set_runtime_version("FEX-2610")
        self.assertFalse(self.session.fex.cache_is_stale())

    def test_unstamped_cache_with_content_is_stale(self):
        # A cache built before stamps existed must not be trusted blindly.
        self.set_runtime_version("FEX-2610")
        os.makedirs(self.session.fex.cache_dir(), exist_ok=True)
        with open(self.session.fex.cache_dir() + "cache", "w") as f:
            f.write("x")
        self.assertTrue(self.session.fex.cache_is_stale())

    def test_cache_is_fresh_once_stamped(self):
        self.set_runtime_version("FEX-2610")
        os.makedirs(self.session.fex.cache_dir(), exist_ok=True)
        self.session.fex.stamp_cache()
        self.assertFalse(self.session.fex.cache_is_stale())

    def test_a_new_fex_version_makes_the_cache_stale(self):
        self.set_runtime_version("FEX-2607")
        os.makedirs(self.session.fex.cache_dir(), exist_ok=True)
        self.session.fex.stamp_cache()
        self.assertFalse(self.session.fex.cache_is_stale())

        self.set_runtime_version("FEX-2610")
        self.assertTrue(self.session.fex.cache_is_stale())

    def test_prepare_drops_a_stale_cache(self):
        self.set_runtime_version("FEX-2607")
        os.makedirs(self.session.fex.cache_dir(), exist_ok=True)
        self.session.fex.stamp_cache()

        self.set_runtime_version("FEX-2610")
        self.session.fex.prepare()
        self.assertFalse(os.path.exists(self.session.fex.cache_dir() + "version"))

    def test_prepare_keeps_a_fresh_cache(self):
        self.set_runtime_version("FEX-2610")
        os.makedirs(self.session.fex.cache_dir(), exist_ok=True)
        self.session.fex.stamp_cache()
        self.session.fex.prepare()
        self.assertTrue(os.path.exists(self.session.fex.cache_dir() + "version"))


class MarkerFileTest(unittest.TestCase):
    def test_marker_records_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            proton.g_compatdata = proton.CompatData(tmp)
            session = proton.Session()
            session.log_file = None
            session.fex.mark_full_translation()
            with open(session.fex.full_translation_marker()) as f:
                self.assertIn("translated as a whole", f.read())


class PostRunTest(unittest.TestCase):
    """The run() hook that decides between saving a cache and giving up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        proton.g_compatdata = proton.CompatData(self.tmp.name)
        proton.g_proton = FakeProton(self.tmp.name)
        proton.g_proton.host_pe_arch = "aarch64-windows"
        self.session = proton.Session()
        self.session.log_file = None
        self.session.remote_debug_cmd = None
        self.session.cmdlineappend = []
        self._saved_argv = sys.argv
        sys.argv = ["proton", "run"]
        self._saved_game_id = os.environ.pop("SteamGameId", None)

    def tearDown(self):
        sys.argv = self._saved_argv
        if self._saved_game_id is not None:
            os.environ["SteamGameId"] = self._saved_game_id
        self.tmp.cleanup()

    def run_with(self, rc):
        original_machine = proton.platform.machine
        proton.platform.machine = lambda: "aarch64"
        self.session.run_proc = lambda *a, **k: rc
        try:
            return self.session.run()
        finally:
            proton.platform.machine = original_machine

    def test_failure_marks_full_translation(self):
        rc = self.run_with(1)
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(self.session.fex.full_translation_marker()))

    def test_success_does_not_mark_full_translation(self):
        rc = self.run_with(0)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.session.fex.full_translation_marker()))

    def test_failure_does_not_save_a_cache(self):
        saved = []
        self.session.fex.save_code_cache = lambda: saved.append(True)
        self.run_with(1)
        self.assertEqual(saved, [])

    def test_full_translation_saves_a_cache_instead_of_marking(self):
        self.session.fex.mark_full_translation()
        saved = []
        self.session.fex.save_code_cache = lambda: saved.append(True)
        self.run_with(0)
        self.assertEqual(saved, [True])


class PeBitnessTest(unittest.TestCase):
    """Reading the machine field of a Windows PE."""

    def write_pe(self, machine):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.exe")
            data = bytearray(0x100)
            data[0:2] = b"MZ"
            data[0x3c:0x40] = (0x80).to_bytes(4, "little")
            data[0x80:0x84] = b"PE\0\0"
            data[0x84:0x86] = machine.to_bytes(2, "little")
            with open(path, "wb") as f:
                f.write(bytes(data))
            return proton.pe_is_64bit(path)

    def test_i386_is_32bit(self):
        self.assertFalse(self.write_pe(0x014c))

    def test_amd64_is_64bit(self):
        self.assertTrue(self.write_pe(0x8664))

    def test_arm64_is_64bit(self):
        self.assertTrue(self.write_pe(0xaa64))

    def test_non_pe_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notes.txt")
            with open(path, "w") as f:
                f.write("hello")
            self.assertIsNone(proton.pe_is_64bit(path))

    def test_missing_file(self):
        self.assertIsNone(proton.pe_is_64bit("/nonexistent/app.exe"))


if __name__ == "__main__":
    unittest.main()
