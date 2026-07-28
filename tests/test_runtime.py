from __future__ import annotations

import tempfile
import threading
import time
import unittest
import sys
from pathlib import Path

from appium_tool.runtime import RuntimeManager


def wait_for_job(manager: RuntimeManager, job_id: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = manager.get_job(job_id)
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("Runtime job did not reach a terminal state.")


class RuntimeManagerTests(unittest.TestCase):
    def test_managed_command_has_a_hard_timeout(self):
        output = []
        started = time.monotonic()

        exit_code = RuntimeManager._run_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            Path.cwd(),
            output.append,
            1,
        )

        self.assertEqual(exit_code, 124)
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(
            any("exceeded its 1-second timeout" in line for line in output)
        )

    def test_runtime_readiness_requires_abi_compatible_booted_device(self):
        incompatible = [
            {
                "state": "device",
                "boot_completed": True,
                "compatible": False,
            }
        ]
        compatible = [
            *incompatible,
            {
                "state": "device",
                "boot_completed": True,
                "compatible": True,
            },
        ]

        self.assertFalse(RuntimeManager._has_ready_device(incompatible))
        self.assertTrue(RuntimeManager._has_ready_device(compatible))

    def test_provision_runs_setup_and_doctor_and_persists_success(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {
                "provisioned": False,
                "android": {"ready": True},
            }
            commands = []

            def runner(command, _cwd, output, _timeout_seconds):
                commands.append(command)
                output(f"completed {command[0]}")
                if command[0] == "doctor.ps1":
                    state["provisioned"] = True
                return 0

            manager = RuntimeManager(
                directory,
                command_runner=runner,
                status_provider=lambda: dict(state),
            )
            manager._powershell_script = lambda name: [name]

            submitted, reused = manager.provision({})
            completed = wait_for_job(manager, submitted["job_id"])

            self.assertFalse(reused)
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["progress"], 100)
            self.assertEqual(
                commands,
                [["setup.ps1"], ["doctor.ps1"]],
            )
            self.assertTrue(
                (
                    Path(directory)
                    / ".runtime"
                    / "jobs"
                    / f"{submitted['job_id']}.json"
                ).is_file()
            )

    def test_active_provision_job_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            entered = threading.Event()
            release = threading.Event()

            def runner(_command, _cwd, _output, _timeout_seconds):
                entered.set()
                release.wait(timeout=2)
                return 1

            manager = RuntimeManager(
                directory,
                command_runner=runner,
                status_provider=lambda: {
                    "provisioned": True,
                    "android": {"ready": True},
                },
            )
            manager._powershell_script = lambda name: [name]

            first, first_reused = manager.provision({"force": True})
            self.assertTrue(entered.wait(timeout=1))
            second, second_reused = manager.provision({"force": True})
            release.set()
            wait_for_job(manager, first["job_id"])

            self.assertFalse(first_reused)
            self.assertTrue(second_reused)
            self.assertEqual(first["job_id"], second["job_id"])

    def test_full_provision_refuses_implicit_android_license_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = RuntimeManager(
                directory,
                command_runner=lambda *_args: 0,
                status_provider=lambda: {
                    "provisioned": False,
                    "android": {"ready": False},
                },
            )
            manager._powershell_script = lambda name: [name]

            submitted, _ = manager.provision({})
            completed = wait_for_job(manager, submitted["job_id"])

            self.assertEqual(completed["status"], "failed")
            self.assertIn(
                "licenses must be accepted explicitly",
                completed["error"],
            )

    def test_start_job_requires_provisioned_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = RuntimeManager(
                directory,
                status_provider=lambda: {
                    "provisioned": False,
                    "ready": False,
                },
            )

            submitted, reused = manager.start({})
            completed = wait_for_job(manager, submitted["job_id"])

            self.assertFalse(reused)
            self.assertEqual(completed["status"], "failed")
            self.assertIn("Complete provisioning first", completed["error"])

    def test_appium_only_start_does_not_launch_fixed_emulator(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {
                "provisioned": True,
                "ready": False,
                "appium": {"server_ready": False},
                "devices": [
                    {
                        "serial": "emulator-5580",
                        "state": "device",
                        "boot_completed": True,
                    }
                ],
            }
            commands = []

            def runner(command, _cwd, _output, _timeout_seconds):
                commands.append(command)
                state["appium"] = {"server_ready": True}
                return 0

            manager = RuntimeManager(
                directory,
                command_runner=runner,
                status_provider=lambda: dict(state),
            )
            manager._powershell_script = lambda name: [name]

            submitted, reused = manager.start(
                {
                    "start_emulator": False,
                    "device_id": "emulator-5580",
                }
            )
            completed = wait_for_job(manager, submitted["job_id"])

            self.assertFalse(reused)
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(commands, [["start-appium-background.ps1"]])


if __name__ == "__main__":
    unittest.main()
