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

    def write_compiler(self):
        compiler = proton.g_proton.bin_dir + proton.FEX_OFFLINE_COMPILER + "64"
        with open(compiler, "w") as f:
            f.write("")
        return compiler

    def test_cache_dir_is_next_to_the_prefix(self):
        self.assertEqual(self.session.fex_cache_dir(), self.tmp.name + "/fex-cache/")

    def test_default_is_call_by_call(self):
        self.assertFalse(self.session.should_translate_whole_app())

    def test_marker_enables_full_translation(self):
        self.session.mark_full_translation()
        self.assertTrue(os.path.exists(self.session.fex_full_translation_marker()))
        self.assertTrue(self.session.should_translate_whole_app())

    def test_env_var_forces_full_translation(self):
        self.session.env["PROTON_FEX_FULL_TRANSLATION"] = "1"
        self.assertTrue(self.session.should_translate_whole_app())

    def test_env_var_zero_disables_full_translation(self):
        self.session.mark_full_translation()
        self.session.env["PROTON_FEX_FULL_TRANSLATION"] = "0"
        self.assertFalse(self.session.should_translate_whole_app())

    def test_app_profile_enables_full_translation(self):
        proton.fex_full_translation_profiles["1234"] = True
        try:
            os.environ["SteamGameId"] = "1234"
            self.assertTrue(self.session.should_translate_whole_app())
        finally:
            del proton.fex_full_translation_profiles["1234"]

    def test_full_translation_config_sets_code_caching(self):
        config = self.session.generate_fex_app_config(full_translation=True)["Config"]
        self.assertEqual(config["EnableCodeCachingWIP"], "1")
        self.assertEqual(config["EnableLazyCodeCachingWIP"], "1")
        self.assertEqual(config["Multiblock"], "1")

    def test_default_config_does_not_set_code_caching(self):
        config = self.session.generate_fex_app_config()["Config"]
        self.assertNotIn("EnableCodeCachingWIP", config)

    def test_offline_compiler_lookup(self):
        self.assertIsNone(self.session.fex_offline_compiler_path())
        compiler = self.write_compiler()
        self.assertEqual(self.session.fex_offline_compiler_path(), compiler)

    def test_cache_generation_is_skipped_without_code_maps(self):
        calls = []
        original = proton.subprocess.call
        proton.subprocess.call = lambda *a, **k: calls.append(a)
        try:
            self.session.generate_fex_code_cache()
        finally:
            proton.subprocess.call = original
        self.assertEqual(calls, [])

    def test_cache_generation_skipped_without_compiler(self):
        new_dir = os.path.join(self.session.fex_cache_dir(), "codemap", "new")
        os.makedirs(new_dir)
        with open(os.path.join(new_dir, "app.bin"), "w") as f:
            f.write("x")
        calls = []
        original = proton.subprocess.call
        proton.subprocess.call = lambda *a, **k: calls.append(a)
        try:
            self.session.generate_fex_code_cache()
        finally:
            proton.subprocess.call = original
        self.assertEqual(calls, [])

    def test_cache_generation_invokes_offline_compiler(self):
        new_dir = os.path.join(self.session.fex_cache_dir(), "codemap", "new")
        os.makedirs(new_dir)
        with open(os.path.join(new_dir, "app.bin"), "w") as f:
            f.write("x")
        self.write_compiler()

        calls = []
        original = proton.subprocess.call
        proton.subprocess.call = lambda *a, **k: calls.append(a)
        try:
            self.session.generate_fex_code_cache()
        finally:
            proton.subprocess.call = original

        self.assertEqual(len(calls), 1)
        argv = calls[0][0]
        self.assertEqual(argv[0], proton.g_proton.wine_bin)
        self.assertTrue(argv[1].startswith("Z:"))
        self.assertEqual(argv[2], "process-all")


class MarkerFileTest(unittest.TestCase):
    def test_marker_records_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            proton.g_compatdata = proton.CompatData(tmp)
            session = proton.Session()
            session.log_file = None
            session.mark_full_translation()
            with open(session.fex_full_translation_marker()) as f:
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
        self.assertTrue(os.path.exists(self.session.fex_full_translation_marker()))

    def test_success_does_not_mark_full_translation(self):
        rc = self.run_with(0)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.session.fex_full_translation_marker()))


if __name__ == "__main__":
    unittest.main()
