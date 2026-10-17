import sys
import threading
import unittest

from cerberus import config, guard, hardware
from cerberus.core import Cerberus
from cerberus.safety import Denied

from .helpers import FakeOllama, Isolated

COOL = {"temp_c": 60.0, "power_w": 100.0, "power_limit_w": 360.0, "util_pct": 10.0, "vram_used_mb": 1000.0, "vram_total_mb": 16000.0}
REAL = {"gpu": guard._probe_gpu, "nvml": guard._probe_nvml, "board": guard._probe_board, "cpu": guard._probe_cpu}   # before Isolated swaps them


def gpu_at(temp):
    return lambda: {**COOL, "temp_c": float(temp)}


class GuardBase(unittest.TestCase):
    def setUp(self):
        iso = Isolated()
        self.root = iso.__enter__()
        self.addCleanup(iso.__exit__, None, None, None)
        saved = hardware.pressure
        self.addCleanup(setattr, hardware, "pressure", saved)
        hardware.pressure = lambda: {"low": False, "free_gb": 16.0}

    def heat(self, temp):
        guard._probe_gpu = gpu_at(temp)
        guard.reset_cache()


class ThresholdTests(GuardBase):
    def test_defaults_and_clamping_and_cannot_be_turned_off(self):
        self.assertEqual(guard.thresholds()["gpu_hot"], 86)
        config.update_settings({"guard": {"gpu_hot": 200, "gpu_warm": 5, "child_procs": 0, "min_disk_gb": "junk", "enabled": False}})
        th = guard.thresholds()
        self.assertEqual(th["gpu_hot"], 92)                     # clamped to the safe range
        self.assertEqual(th["gpu_warm"], 60)
        self.assertEqual(th["child_procs"], 4)
        self.assertEqual(th["min_disk_gb"], 5.0)                 # junk falls back to the default
        self.assertNotIn("enabled", th)

    def test_warm_line_always_sits_below_the_hot_line(self):
        config.update_settings({"guard": {"gpu_hot": 70, "gpu_warm": 88}})
        th = guard.thresholds()
        self.assertLessEqual(th["gpu_warm"], th["gpu_hot"] - 4)


class StatusTests(GuardBase):
    def test_unreadable_card_is_reported_honestly_not_as_cool(self):
        st = guard.status(force=True)
        self.assertEqual(st["level"], "unknown")
        self.assertFalse(st["temp_known"])
        self.assertFalse(st["blocked"])
        self.assertTrue(any("NOT" in n for n in st["notes"]))

    def test_ok_warm_hot(self):
        self.heat(60)
        self.assertEqual(guard.status(force=True)["level"], "ok")
        self.heat(80)
        st = guard.status(force=True)
        self.assertEqual((st["level"], st["throttle"], st["blocked"]), ("warm", True, False))
        self.heat(90)
        st = guard.status(force=True)
        self.assertEqual((st["level"], st["blocked"]), ("hot", True))

    def test_hysteresis_keeps_it_hot_until_it_really_cooled(self):
        self.heat(90)
        guard.status(force=True)
        guard._probe_gpu = gpu_at(84)                            # below the hot line but not 6 degrees below
        self.assertEqual(guard.status(force=True)["level"], "hot")
        guard._probe_gpu = gpu_at(78)
        self.assertNotEqual(guard.status(force=True)["level"], "hot")

    def test_low_memory_throttles(self):
        hardware.pressure = lambda: {"low": True, "free_gb": 0.5}
        st = guard.status(force=True)
        self.assertTrue(st["memory_low"])
        self.assertTrue(st["throttle"])
        self.assertTrue(guard.should_throttle())

    def test_failed_probe_is_not_retried_every_moment(self):
        calls = []
        guard._probe_gpu = lambda: calls.append(1)
        guard.reset_cache()
        guard.status(), guard.status(), guard.status()
        self.assertEqual(len(calls), 1)


class EnsureSafeTests(GuardBase):
    def test_waits_for_the_card_to_cool(self):
        temps = iter([92, 90, 70, 70, 70])
        guard._probe_gpu = lambda: {**COOL, "temp_c": float(next(temps))}
        naps = []
        ok, why = guard.ensure_safe(sleep=naps.append, clock=lambda: 0.0)
        self.assertTrue(ok)
        self.assertGreaterEqual(len(naps), 1)

    def test_gives_up_with_a_clear_message(self):
        self.heat(95)
        t = [0.0]
        ok, why = guard.ensure_safe(max_wait=20, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        self.assertFalse(ok)
        self.assertIn("too hot", why)

    def test_cancel_stops_the_wait(self):
        self.heat(95)
        stop = threading.Event()
        stop.set()
        ok, why = guard.ensure_safe(cancel=stop, sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("Cancelled", why)

    def test_cool_card_passes_instantly(self):
        self.heat(50)
        self.assertEqual(guard.ensure_safe(sleep=lambda s: self.fail("should not wait")), (True, ""))


class FakeNvml:
    """Stands in for nvml.dll: fills the caller's ctypes buffers the way the real library does."""

    def __init__(self, temps, init_rc=0):
        self.temps, self.init_rc, self.shutdowns = temps, init_rc, 0

    def nvmlInit_v2(self): return self.init_rc
    def nvmlShutdown(self): self.shutdowns += 1
    def nvmlDeviceGetCount_v2(self, ref): ref._obj.value = len(self.temps); return 0
    def nvmlDeviceGetHandleByIndex_v2(self, i, ref): ref._obj.value = i + 1; return 0
    def nvmlDeviceGetTemperature(self, handle, sensor, ref): ref._obj.value = self.temps[handle.value - 1]; return 0
    def nvmlDeviceGetPowerUsage(self, handle, ref): ref._obj.value = 150_000; return 0
    def nvmlDeviceGetEnforcedPowerLimit(self, handle, ref): ref._obj.value = 360_000; return 0
    def nvmlDeviceGetUtilizationRates(self, handle, ref): ref._obj.gpu = 42; return 0
    def nvmlDeviceGetMemoryInfo(self, handle, ref): ref._obj.total, ref._obj.used = 16 * 1024 ** 3, 4 * 1024 ** 3; return 0


LHM_TREE = {"Text": "Sensor", "Children": [{"Text": "PC", "Children": [
    {"Text": "Intel Core Ultra 7", "ImageURL": "images_icon/cpu.png", "Children": [
        {"Text": "Temperatures", "Children": [
            {"Text": "CPU Package", "Value": "71,0 °C"},
            {"Text": "Core Max", "Value": "74.0 °C"},
            {"Text": "Core Average", "Value": "60.0 °C"},
            {"Text": "Distance to TjMax", "Value": "99.0 °C"}]},
        {"Text": "Load", "Children": [{"Text": "CPU Total", "Value": "12.0 %"}]}]},
    {"Text": "NVIDIA GeForce", "ImageURL": "images_icon/nvidia.png", "Children": [{"Text": "GPU Core", "Value": "95.0 °C"}]}]}]}


class ReadingTests(GuardBase):
    def test_nvml_reading_picks_the_hottest_card_and_always_shuts_down(self):
        lib = FakeNvml([55, 81])
        card = guard._nvml_read(lib)
        self.assertEqual(card["temp_c"], 81.0)
        self.assertEqual((card["power_w"], card["power_limit_w"], card["util_pct"]), (150.0, 360.0, 42.0))
        self.assertEqual((card["vram_used_mb"], card["vram_total_mb"]), (4096.0, 16384.0))
        self.assertEqual(lib.shutdowns, 1)

    def test_nvml_that_will_not_start_gives_none(self):
        self.assertIsNone(guard._nvml_read(FakeNvml([50], init_rc=999)))

    def test_gpu_probe_falls_back_to_nvidia_smi(self):
        for name, value in (("_probe_nvml", None), ("_probe_smi", {"temp_c": 61.0})):
            self.addCleanup(setattr, guard, name, getattr(guard, name))
            setattr(guard, name, lambda value=value: value)
        self.assertEqual(REAL["gpu"](), {"temp_c": 61.0})
        guard._probe_nvml = lambda: {"temp_c": 44.0}
        self.assertEqual(REAL["gpu"](), {"temp_c": 44.0})                # NVML wins when it works

    def test_libre_hardware_monitor_tree_gives_the_cpu_temperature_only(self):
        self.assertEqual(guard._lhm_cpu_temp(LHM_TREE), 74.0)       # not the 95 C graphics card, not the 99 "distance to TjMax"
        self.assertIsNone(guard._lhm_cpu_temp({"Text": "Sensor", "Children": []}))

    def test_libre_hardware_monitor_not_running_is_just_none(self):
        self.assertIsNone(REAL["cpu"]())                              # nothing listens on 8085 here (or it answers sensibly)

    def test_real_probes_on_this_machine_never_raise_and_are_sane(self):
        for name in ("nvml", "board"):
            reading = REAL[name]()
            if reading is not None:                                   # None is fine on a PC without that sensor
                self.assertTrue(-20 < reading["temp_c"] < 130, (name, reading))
        zones = REAL["board"]()
        if zones:
            self.assertTrue(all(-20 < t < 130 for t in zones["zones"].values()))
            self.assertTrue(0 < zones["passive_limit_pct"] <= 100)


class HeatSourceTests(GuardBase):
    def with_sources(self, gpu=None, cpu=None, board=None):
        guard._probe_gpu, guard._probe_cpu, guard._probe_board = (lambda: gpu), (lambda: cpu), (lambda: board)
        guard.reset_cache()
        return guard.status(force=True)

    def zones(self, temp, limit=100):
        return {"temp_c": temp, "zones": {"TZ00": temp}, "passive_limit_pct": limit}

    def test_board_sensors_alone_are_enough_to_know_heat(self):
        st = self.with_sources(board=self.zones(30))
        self.assertTrue(st["temp_known"])
        self.assertEqual(st["level"], "ok")
        self.assertEqual(st["hottest"]["label"], "motherboard sensor")
        self.assertFalse(any("NOT being watched" in n for n in st["notes"]))

    def test_a_hot_cpu_blocks_even_if_the_graphics_card_is_cool(self):
        st = self.with_sources(gpu=COOL, cpu={"temp_c": 97.0, "source": "x"})
        self.assertEqual((st["level"], st["blocked"], st["hottest"]["key"]), ("hot", True, "cpu"))
        self.assertTrue(any("processor is hot" in n for n in st["notes"]))

    def test_warm_cpu_throttles_but_does_not_block(self):
        st = self.with_sources(gpu=COOL, cpu={"temp_c": 88.0, "source": "x"})
        self.assertEqual((st["level"], st["throttle"], st["blocked"]), ("warm", True, False))

    def test_windows_slowing_itself_down_counts_as_warm(self):
        st = self.with_sources(gpu=COOL, board=self.zones(40, limit=70))
        self.assertEqual(st["level"], "warm")
        self.assertTrue(any("slowing" in n for n in st["notes"]))

    def test_worst_sensor_wins_and_each_keeps_its_own_hysteresis(self):
        self.with_sources(gpu=COOL, cpu={"temp_c": 97.0, "source": "x"})
        guard._probe_cpu = lambda: {"temp_c": 93.0, "source": "x"}           # under the hot line, not 6 below it
        self.assertEqual(guard.status(force=True)["level"], "hot")
        guard._probe_cpu = lambda: {"temp_c": 70.0, "source": "x"}
        self.assertEqual(guard.status(force=True)["level"], "warm")          # steps down one level at a time, like the card does
        self.assertEqual(guard.status(force=True)["level"], "ok")            # the cool graphics card never masked or extended it

    def test_cpu_thresholds_are_clamped_and_ordered(self):
        config.update_settings({"guard": {"cpu_hot": 500, "cpu_warm": 1}})
        th = guard.thresholds()
        self.assertEqual(th["cpu_hot"], 102)
        self.assertLessEqual(th["cpu_warm"], th["cpu_hot"] - 4)
        self.assertGreaterEqual(th["cpu_warm"], 60)

    def test_ensure_safe_names_the_part_that_is_too_hot(self):
        guard._probe_gpu, guard._probe_cpu, guard._probe_board = (lambda: COOL), (lambda: {"temp_c": 99.0, "source": "x"}), (lambda: None)
        guard.reset_cache()
        t = [0.0]
        ok, why = guard.ensure_safe(max_wait=10, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        self.assertFalse(ok)
        self.assertIn("processor is still too hot", why)


class DiskTests(GuardBase):
    def test_write_that_would_fill_the_drive_is_refused(self):
        free = guard.disk_free_gb(self.root)
        self.assertIsNotNone(free)
        config.update_settings({"guard": {"min_disk_gb": 5.0}})
        guard.assert_space(self.root / "x.txt", 1024)             # a tiny write is fine on a normal drive
        with self.assertRaises(Denied):
            guard.assert_space(self.root / "x.txt", int((free + 1) * 1024 ** 3))

    def test_file_tools_use_the_check(self):
        from cerberus.tools.files import write_file
        orig = guard.assert_space

        def refuse(path, nbytes=0):
            raise Denied("disk nearly full")
        guard.assert_space = refuse
        self.addCleanup(setattr, guard, "assert_space", orig)
        with self.assertRaises(Denied):
            write_file(None, "a.txt", "hello")


class PipelineTests(GuardBase):
    def setUp(self):
        super().setUp()
        self.core = Cerberus(self.root / "t.db")
        self.core.ollama = self.fake = FakeOllama("A fine answer, long enough to be a proper paragraph for this test case.")
        self.addCleanup(self.core.db.close)

    def run_mode(self):
        return list(self.core.orchestrator.run("Write me a paragraph about the report", "writing", {}, "local"))

    def test_warm_card_drops_to_light_work(self):
        self.heat(80)
        evs = self.run_mode()
        self.assertEqual(evs[0]["thinking"], "quick")
        self.assertTrue(any(e["type"] == "council" and e["phase"] == "skipped" for e in evs))

    def test_hot_card_stops_the_run_with_a_plain_message(self):
        self.heat(95)
        orig = guard.ensure_safe
        guard.ensure_safe = lambda cancel=None, **k: (False, "The graphics card is still too hot (95 C).")
        self.addCleanup(setattr, guard, "ensure_safe", orig)
        evs = self.run_mode()
        self.assertEqual(evs[-2]["type"] if evs[-1]["type"] == "done" else evs[-1]["type"], "error")
        err = next(e for e in evs if e["type"] == "error")
        self.assertIn("too hot", err["message"])
        self.assertFalse(any(e["type"] == "final" for e in evs))

    def test_cool_card_runs_normally(self):
        self.heat(55)
        evs = self.run_mode()
        self.assertTrue(any(e["type"] == "final" for e in evs))

    def test_scheduled_agent_refuses_to_start_on_a_hot_card(self):
        self.heat(95)
        orig = guard.ensure_safe
        guard.ensure_safe = lambda cancel=None, **k: (False, "too hot to start")
        self.addCleanup(setattr, guard, "ensure_safe", orig)
        a = self.core.agents.create("Hotbox", "tester", "x", [], "say hi", "manual")
        self.core.agents._work(self.core.db.one("SELECT * FROM agents WHERE id=?", (a["id"],)), "schedule")
        row = self.core.db.one("SELECT last_status, last_output FROM agents WHERE id=?", (a["id"],))
        self.assertEqual(row["last_status"], "error")
        self.assertIn("too hot", row["last_output"])


@unittest.skipUnless(sys.platform == "win32", "job objects are Windows-only")
class ChildRunTests(GuardBase):
    PY = sys.executable

    def test_normal_run_is_captured_and_limited(self):
        res = guard.child_run([self.PY, "-c", "print('hi')"], str(self.root), 30)
        self.assertEqual((res["code"], res["out"].strip(), res["timed_out"]), (0, "hi", False))
        self.assertTrue(res["limited"])

    def test_memory_hog_is_stopped_by_the_ceiling(self):
        code = "x = bytearray(1200 * 1024 * 1024)\nprint('allocated')"
        res = guard.child_run([self.PY, "-c", code], str(self.root), 60, mem_gb=0.5)
        self.assertNotIn("allocated", res["out"])
        self.assertNotEqual(res["code"], 0)

    def test_timeout_ends_the_whole_tree(self):
        marker = self.root / "grandchild.pid"
        code = ("import subprocess, sys, time, pathlib\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                f"pathlib.Path(r'{marker}').write_text(str(p.pid))\n"
                "time.sleep(120)\n")
        res = guard.child_run([self.PY, "-c", code], str(self.root), 3)
        self.assertTrue(res["timed_out"])
        from cerberus import winwatch
        pid = int(marker.read_text())
        for _ in range(20):
            if not winwatch.alive(pid):
                break
            threading.Event().wait(0.25)
        self.assertFalse(winwatch.alive(pid), "the grandchild survived the timeout")

    def test_fork_bomb_is_capped(self):
        code = ("import subprocess, sys\n"
                "ok = 0\n"
                "kids = []\n"
                "for _ in range(30):\n"
                "    try:\n"
                "        kids.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'])); ok += 1\n"
                "    except OSError:\n"
                "        break\n"
                "print('spawned', ok)\n"
                "[k.kill() for k in kids]\n")
        res = guard.child_run([self.PY, "-c", code], str(self.root), 60, procs=6)
        spawned = int(res["out"].split()[-1]) if "spawned" in res["out"] else 0
        self.assertLess(spawned, 30)


if __name__ == "__main__":
    unittest.main()
