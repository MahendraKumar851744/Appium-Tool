from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from appium_tool.device_selection import (
    AndroidDeviceCoordinator,
    DeviceSelectionError,
    apk_requirement_profile,
    evaluate_device,
)
from appium_tool.exploration.models import ApkMetadata


def metadata(
    *,
    native_abis: tuple[str, ...] = ("arm64-v8a",),
    min_sdk: str | None = "24",
    target_sdk: str | None = "36",
    supports_16kb_page_size: bool | None = True,
) -> ApkMetadata:
    return ApkMetadata(
        path=Path("application.apk"),
        sha256="a" * 64,
        size_bytes=100,
        package="com.example.application",
        min_sdk=min_sdk,
        target_sdk=target_sdk,
        native_abis=native_abis,
        supports_16kb_page_size=supports_16kb_page_size,
        inspector="aapt.exe",
    )


class FakeRuntime:
    def __init__(self, devices=None, avds=None):
        self.devices = list(devices or [])
        self.avds = list(avds or [])

    def status(self):
        return {
            "android": {
                "sdk_root": "C:\\Android\\Sdk",
                "avds": self.avds,
                "tools": {
                    "adb": {
                        "available": True,
                        "path": "adb.exe",
                    },
                    "emulator": {
                        "available": True,
                        "path": "emulator.exe",
                    },
                },
            },
            "devices": self.devices,
        }


class StubCoordinator(AndroidDeviceCoordinator):
    def __init__(self, *args, avd_details=None, booted=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.avd_details = avd_details or {}
        self.booted = booted or {}
        self.started = []

    def _inspect_avd(self, avd_name, requirements):
        return dict(self.avd_details[avd_name])

    def _start_and_verify_avd(
        self,
        avd_name,
        requirements,
        *,
        timeout_seconds,
    ):
        self.started.append((avd_name, timeout_seconds))
        value = self.booted[avd_name]
        if isinstance(value, Exception):
            raise value
        return dict(value)


class DeviceSelectionTests(unittest.TestCase):
    def test_apk_requirements_capture_abis_and_sdk_levels(self):
        requirements = apk_requirement_profile(metadata())

        self.assertEqual(requirements["native_abis"], ["arm64-v8a"])
        self.assertEqual(requirements["minimum_api_level"], 24)
        self.assertEqual(requirements["target_api_level"], 36)
        self.assertFalse(requirements["abi_independent"])

    def test_apk_without_native_libraries_is_abi_independent(self):
        requirements = apk_requirement_profile(
            metadata(native_abis=())
        )

        self.assertEqual(requirements["native_abis"], [])
        self.assertTrue(requirements["abi_independent"])

    def test_device_requires_boot_api_and_advertised_abi(self):
        requirements = apk_requirement_profile(metadata())
        incompatible = evaluate_device(
            requirements,
            {
                "serial": "emulator-5554",
                "state": "device",
                "boot_completed": True,
                "abi_list": "x86,armeabi-v7a",
                "api_level": 35,
            },
        )
        compatible = evaluate_device(
            requirements,
            {
                "serial": "emulator-5556",
                "state": "device",
                "boot_completed": True,
                "abi_list": "x86_64,arm64-v8a",
                "api_level": 35,
            },
        )

        self.assertFalse(incompatible["compatible"])
        self.assertIn(
            "no APK ABI is advertised by the device",
            incompatible["rejection_reasons"],
        )
        self.assertTrue(compatible["compatible"])
        self.assertEqual(compatible["matching_abis"], ["arm64-v8a"])

    def test_16kb_device_rejects_unaligned_native_apk(self):
        requirements = apk_requirement_profile(
            metadata(supports_16kb_page_size=False)
        )
        assessment = evaluate_device(
            requirements,
            {
                "serial": "emulator-5554",
                "state": "device",
                "boot_completed": True,
                "abi_list": "arm64-v8a",
                "api_level": 36,
                "page_size_bytes": 16_384,
            },
        )

        self.assertFalse(assessment["compatible"])
        self.assertFalse(assessment["page_size_compatible"])

    def test_connected_device_is_selected_using_apk_not_pinned_profile(self):
        requirements = apk_requirement_profile(metadata())
        runtime = FakeRuntime(
            devices=[
                {
                    "serial": "emulator-5556",
                    "state": "device",
                    "boot_completed": True,
                    "compatible": False,
                    "abi_list": "x86_64,arm64-v8a",
                    "api_level": 35,
                    "is_emulator": True,
                }
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            coordinator = AndroidDeviceCoordinator(
                project_root=directory,
                runtime_manager=runtime,
            )
            result = coordinator.ensure_device(requirements)

        self.assertEqual(result["device_id"], "emulator-5556")
        self.assertEqual(
            result["selection_source"],
            "compatible_connected_device",
        )
        self.assertTrue(result["verification"]["verified"])

    def test_requested_incompatible_device_returns_full_inventory(self):
        requirements = apk_requirement_profile(metadata())
        runtime = FakeRuntime(
            devices=[
                {
                    "serial": "emulator-5554",
                    "state": "device",
                    "boot_completed": True,
                    "abi_list": "x86",
                    "api_level": 35,
                }
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            coordinator = AndroidDeviceCoordinator(
                project_root=directory,
                runtime_manager=runtime,
            )

            with self.assertRaises(DeviceSelectionError) as caught:
                coordinator.ensure_device(
                    requirements,
                    requested_device_id="emulator-5554",
                )

        self.assertEqual(
            caught.exception.details["selected_device"]["device_id"],
            "emulator-5554",
        )

    def test_existing_avd_candidates_are_boot_verified_in_order(self):
        requirements = apk_requirement_profile(metadata())
        runtime = FakeRuntime(
            avds=["Old_x86", "Modern_x86_64"],
        )
        rejected = DeviceSelectionError("No advertised ARM64")
        verified = {
            "device_id": "emulator-5556",
            "state": "device",
            "boot_completed": True,
            "api_level": 35,
            "supported_abis": ["arm64-v8a", "x86_64"],
            "matching_abis": ["arm64-v8a"],
            "api_compatible": True,
            "compatible": True,
        }
        avd_details = {
            "Old_x86": {
                "avd_name": "Old_x86",
                "potentially_compatible": True,
            },
            "Modern_x86_64": {
                "avd_name": "Modern_x86_64",
                "potentially_compatible": True,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            coordinator = StubCoordinator(
                project_root=directory,
                runtime_manager=runtime,
                avd_details=avd_details,
                booted={
                    "Old_x86": rejected,
                    "Modern_x86_64": verified,
                },
            )
            result = coordinator.ensure_device(
                requirements,
                options={
                    "auto_start_avd": True,
                    "allow_provision": False,
                    "boot_timeout_seconds": 120,
                },
            )

        self.assertEqual(
            coordinator.started,
            [("Old_x86", 120), ("Modern_x86_64", 120)],
        )
        self.assertEqual(result["device_id"], "emulator-5556")
        self.assertEqual(result["attempts"][0]["status"], "rejected")
        self.assertEqual(result["attempts"][1]["status"], "compatible")

    def test_arm64_provisioning_uses_verified_x86_64_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = AndroidDeviceCoordinator(
                project_root=directory,
                runtime_manager=FakeRuntime(),
            )
            profile = coordinator._provision_profile(
                apk_requirement_profile(metadata())
            )

        self.assertEqual(profile["emulator_abi"], "x86_64")
        self.assertGreaterEqual(profile["api_level"], 35)
        self.assertIn("arm64_v8a", profile["avd_name"])


if __name__ == "__main__":
    unittest.main()
