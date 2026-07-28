import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from appium_tool.exploration.actions import (
    ActionExecutor,
    ActionRequest,
    SessionManager,
)
from appium_tool.exploration.appium import AdbSystemProbe, ApkInspector, AppiumExplorer
from appium_tool.exploration.context_export import ScreenContextExporter
from appium_tool.exploration.llm_context import build_llm_context, load_llm_context
from appium_tool.exploration.models import ApkMetadata, ScreenCapture, SessionInfo
from appium_tool.exploration.service import ExplorationService
from appium_tool.exploration.storage import SessionStore


class FakeExplorer:
    def __init__(self, apk: ApkMetadata, capture: ScreenCapture):
        self.apk = apk
        self.capture = capture
        self.closed = False

    def start(self, apk_path):
        self.received_path = Path(apk_path)
        return self.apk, SessionInfo(
            session_id="session-1",
            server_url="http://127.0.0.1:4723",
            device_name="Test Android",
            udid="emulator-5554",
            capabilities={"platformName": "Android"},
        )

    def observe(self):
        return self.capture

    def close(self):
        self.closed = True


class FakeDriver:
    session_id = "session-1"
    current_package = "example.app"
    current_activity = "example.app.MainActivity"
    orientation = "PORTRAIT"
    contexts = ["NATIVE_APP"]
    capabilities = {
        "platformName": "Android",
        "statBarHeight": 72,
        "navigationBarHeight": 96,
        "pixelRatio": 3,
        "viewportRect": {"left": 0, "top": 72, "width": 1080, "height": 1752},
    }
    page_source = """<hierarchy>
      <node class="android.widget.FrameLayout" package="example.app"
            bounds="[0,0][1080,1920]" displayed="true" enabled="true">
        <node class="android.widget.Button" package="example.app"
              text="Continue" resource-id="example.app:id/continue"
              bounds="[100,200][500,300]" displayed="true" enabled="true"
              clickable="true"/>
        <node class="android.widget.Button"
              package="com.android.permissioncontroller" text="Allow"
              bounds="[100,1400][500,1500]" displayed="true" enabled="true"
              clickable="true"/>
      </node>
    </hierarchy>"""

    def __init__(self):
        self.page_source = type(self).page_source
        self.current_package = type(self).current_package
        self.current_activity = type(self).current_activity
        self.changed = False
        self.activated_packages = []

    def get_screenshot_as_png(self):
        stream = BytesIO()
        color = "black" if self.changed else "white"
        Image.new("RGB", (108, 228), color).save(stream, format="PNG")
        return stream.getvalue()

    def get_window_size(self):
        return {"width": 1080, "height": 1920}

    def is_keyboard_shown(self):
        return False

    def find_element(self, strategy, value):
        if strategy == "id" and value in {
            "example.app:id/continue",
            "example.app:id/allow",
        }:
            return FakeElement(self, value)
        raise RuntimeError(f"not found with {strategy}: {value}")

    def activate_app(self, package_id):
        self.activated_packages.append(package_id)

    def quit(self):
        self.closed = True


class FakeElement:
    def __init__(self, driver, resource_id):
        self.driver = driver
        self.resource_id = resource_id
        self.id = f"remote-{resource_id.rsplit('/', 1)[-1]}"
        self.value = ""

    def click(self):
        self.driver.changed = True
        self.driver.current_activity = "example.app.NextActivity"
        self.driver.page_source = self.driver.page_source.replace(
            'text="Continue"',
            'text="Finished"',
        )

    def send_keys(self, value):
        self.value += value

    def clear(self):
        self.value = ""

    def get_attribute(self, name):
        return "false"


class FakeSystemProbe:
    def collect(self, udid):
        return {
            "device": {
                "serial": udid,
                "android_version": "11",
                "api_level": 30,
                "supported_abis": ["x86", "armeabi-v7a"],
            },
            "display": {
                "physical_size": {"width": 108, "height": 228},
                "physical_density_dpi": 440,
                "rotation": 0,
            },
            "window": {
                "system_ui_flags": {
                    "hex": "0x2010",
                    "value": 8208,
                    "decoded": ["light_status_bar", "light_navigation_bar"],
                },
                "insets": {
                    "status_bar": {
                        "visible": True,
                        "bounds": {
                            "left": 0,
                            "top": 0,
                            "right": 108,
                            "bottom": 7,
                            "width": 108,
                            "height": 7,
                            "center": {"x": 54, "y": 3},
                        },
                    },
                    "navigation_bar": {
                        "visible": True,
                        "bounds": {
                            "left": 0,
                            "top": 214,
                            "right": 108,
                            "bottom": 228,
                            "width": 108,
                            "height": 14,
                            "center": {"x": 54, "y": 221},
                        },
                    },
                    "ime": {
                        "visible": False,
                        "bounds": {
                            "left": 0,
                            "top": 0,
                            "right": 0,
                            "bottom": 0,
                            "width": 0,
                            "height": 0,
                            "center": {"x": 0, "y": 0},
                        },
                    },
                },
            },
            "navigation": {"mode_value": 0, "mode": "three_button"},
            "input_method": {"shown": False, "inset_visible": False},
        }

    def collect_action_health(self, udid, package, *, since_epoch):
        return {
            "process_alive": True,
            "process_ids": [1234],
            "crash_detected": False,
            "anr_detected": False,
            "recent_error_log": "",
        }


def observation(apk: ApkMetadata):
    element = {
        "id": "element_0001",
        "number": 1,
        "depth": 0,
        "xpath": "/hierarchy[1]",
        "class": "hierarchy",
        "package": apk.package,
        "source": "app",
        "resource_id": "",
        "text": "",
        "content_description": "",
        "bounds": None,
        "bounds_raw": "",
        "clickable": False,
        "long_clickable": False,
        "checkable": False,
        "checked": False,
        "enabled": True,
        "focusable": False,
        "focused": False,
        "scrollable": False,
        "selected": False,
        "password": False,
        "displayed": True,
        "editable": False,
        "interaction": None,
        "extra_attributes": {},
    }
    return {
        "contract": "appium.screen_capture",
        "schema_version": 1,
        "screen_id": "screen_1234567890abcdef",
        "fingerprint": "1" * 64,
        "captured_at": "2026-07-24T00:00:00+00:00",
        "apk": apk.to_dict(),
        "session": {"session_id": "session-1"},
        "screen": {
            "package": apk.package,
            "activity": apk.launch_activity,
            "orientation": "PORTRAIT",
            "window_size": {"width": 1080, "height": 1920},
            "contexts": ["NATIVE_APP"],
            "app_in_foreground": True,
        },
        "system": {
            "keyboard_visible": False,
            "status_bar": {"present": False, "elements": [], "bounds": []},
            "navigation_bar": {"present": False, "elements": [], "bounds": []},
            "permission_prompt": {"present": False, "elements": []},
            "dialog": {"present": False, "elements": []},
            "system_element_count": 0,
            "system_packages": [],
        },
        "stability": {"stable": True, "samples": 3, "duration_ms": 1000},
        "hashes": {
            "screenshot_sha256": "2" * 64,
            "hierarchy_sha256": "3" * 64,
        },
        "summary": {
            "total_elements": 1,
            "visible_elements": 1,
            "interactive_element_ids": [],
        },
        "elements": [element],
        "collection_errors": [],
    }


class ApkInspectorTests(unittest.TestCase):
    def test_parses_aapt_badging(self):
        parsed = ApkInspector.parse_badging(
            "\n".join(
                [
                    "package: name='example.app' versionCode='42' versionName='1.2'",
                    "sdkVersion:'29'",
                    "targetSdkVersion:'35'",
                    "application-label:'Example'",
                    "launchable-activity: name='example.app.MainActivity'",
                    "uses-feature: name='android.hardware.camera'",
                    "native-code: 'arm64-v8a' 'armeabi-v7a'",
                ]
            )
        )
        self.assertEqual(parsed["package"], "example.app")
        self.assertEqual(parsed["version_code"], "42")
        self.assertEqual(parsed["launch_activity"], "example.app.MainActivity")
        self.assertEqual(parsed["native_abis"], ["arm64-v8a", "armeabi-v7a"])
        self.assertEqual(
            parsed["required_features"],
            ["android.hardware.camera"],
        )

    def test_parses_adb_insets_and_system_ui_flags(self):
        parsed = AdbSystemProbe.parse_insets(
            "\n".join(
                [
                    "InsetsSource type=ITYPE_STATUS_BAR frame=[0,0][1080,66] visible=true",
                    "InsetsSource type=ITYPE_NAVIGATION_BAR frame=[0,2148][1080,2280] visible=true",
                    "InsetsSource type=ITYPE_IME frame=[0,0][0,0] visible=false",
                ]
            )
        )
        self.assertEqual(parsed["status_bar"]["bounds"]["height"], 66)
        self.assertEqual(parsed["navigation_bar"]["bounds"]["top"], 2148)
        self.assertFalse(parsed["ime"]["visible"])
        self.assertEqual(AdbSystemProbe.navigation_mode("0"), "three_button")
        self.assertIn(
            "light_status_bar",
            AdbSystemProbe.decode_system_ui_flags(0x2010),
        )


class MilestoneOneTests(unittest.TestCase):
    def test_context_export_supports_inline_and_stored_screens_with_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apk = ApkMetadata(
                path=Path("sample.apk"),
                sha256="a" * 64,
                size_bytes=3,
                package="example.app",
                launch_activity="example.app.MainActivity",
            )
            document = observation(apk)
            first = {
                **document["elements"][0],
                "id": "element_0001",
                "text": "Continue",
                "clickable": True,
                "interaction": "tap",
            }
            second = {
                **first,
                "id": "element_0002",
                "text": "Cancel",
                "bounds_raw": "[0,100][100,200]",
            }
            document["elements"] = [first, second]
            document["artifacts"] = {
                "screenshots": {"full": "screens/screen-id/screenshots/screen.png"}
            }
            exporter = ScreenContextExporter(root)

            inline = exporter.export(
                {
                    "screen": document,
                    "options": {
                        "max_actions": 1,
                        "include_device_context": False,
                        "include_system_context": False,
                        "include_capture_quality": False,
                    },
                }
            )

            self.assertEqual(inline["contract"], "appium.llm_screen_context")
            self.assertEqual(inline["source"]["type"], "inline")
            self.assertEqual(inline["coverage"]["actions"]["available"], 2)
            self.assertEqual(inline["coverage"]["actions"]["included"], 1)
            self.assertFalse(inline["coverage"]["actions"]["complete"])
            self.assertTrue(inline["coverage"]["truncated"])
            self.assertNotIn("## DEVICE CONTEXT", inline["text"])
            self.assertNotIn("## UI STATE", inline["text"])
            self.assertNotIn("## CAPTURE QUALITY", inline["text"])
            self.assertEqual(
                inline["visual_evidence"]["screenshot"],
                "screens/screen-id/screenshots/screen.png",
            )

            path = (
                root
                / "run_123"
                / "screens"
                / document["screen_id"]
                / "appium-result.json"
            )
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(document), encoding="utf-8")
            stored = exporter.export(
                {
                    "screen_ref": {
                        "session_id": "run_123",
                        "screen_id": document["screen_id"],
                    }
                }
            )

            self.assertEqual(stored["source"]["type"], "stored")
            self.assertEqual(stored["source"]["session_id"], "run_123")
            self.assertTrue(stored["coverage"]["actions"]["complete"])
            self.assertIn("Continue", stored["text"])
            self.assertIn("Cancel", stored["text"])

    def test_package_launch_creates_run_and_returns_reusable_screen_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = FakeDriver()
            captured_capabilities = {}
            explorer = AppiumExplorer(
                keep_data=True,
                stability_timeout=0.2,
                stability_interval=0.001,
                system_probe=FakeSystemProbe(),
                driver_factory=lambda _url, capabilities: (
                    captured_capabilities.update(capabilities) or driver
                ),
            )
            explorer._require_server = lambda: None
            launch_documents = []
            manager = SessionManager(
                Path(directory) / "artifacts",
                explorer_factory=lambda document: (
                    launch_documents.append(document) or explorer
                ),
            )
            try:
                result = manager.launch(
                    {
                        "package_id": "example.app",
                        "device_id": "emulator-5556",
                    }
                )

                self.assertEqual(result["status"], "opened")
                self.assertEqual(result["package_id"], "example.app")
                self.assertEqual(
                    launch_documents[0]["input"]["udid"],
                    "emulator-5556",
                )
                self.assertEqual(
                    result["screen_ref"],
                    {
                        "session_id": result["session_id"],
                        "screen_id": result["screen_id"],
                    },
                )
                self.assertEqual(driver.activated_packages, ["example.app"])
                self.assertNotIn("appium:app", captured_capabilities)
                self.assertFalse(captured_capabilities["appium:autoLaunch"])
                self.assertTrue(captured_capabilities["appium:noReset"])
                document = json.loads(
                    Path(result["screen"]["appium_result"]).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    document["input"]["launch_source"],
                    "installed_package",
                )
                self.assertEqual(
                    document["input"]["package_id"],
                    "example.app",
                )
                self.assertIn(result["session_id"], manager._sessions)
            finally:
                manager.close_all()

    def test_package_launch_retries_transient_adb_session_failure(self):
        driver = FakeDriver()
        attempts = []

        def create_driver(_url, _capabilities):
            attempts.append(len(attempts) + 1)

            if len(attempts) == 1:
                raise RuntimeError("adb command failed: error: closed")

            return driver

        explorer = AppiumExplorer(
            keep_data=True,
            stability_timeout=0.2,
            stability_interval=0.001,
            system_probe=FakeSystemProbe(),
            driver_factory=create_driver,
        )
        explorer._require_server = lambda: None

        with patch("appium_tool.exploration.appium.time.sleep"):
            apk, session = explorer.start_package("example.app")

        self.assertEqual(attempts, [1, 2])
        self.assertEqual(apk.package, "example.app")
        self.assertEqual(session.session_id, driver.session_id)
        self.assertEqual(driver.activated_packages, ["example.app"])

    def test_appium_observation_captures_app_and_system_state(self):
        apk = ApkMetadata(
            path=Path("sample.apk"),
            sha256="a" * 64,
            size_bytes=3,
            package="example.app",
            launch_activity="example.app.MainActivity",
        )
        driver = FakeDriver()
        explorer = AppiumExplorer(
            stability_timeout=1,
            stability_interval=0.001,
            system_probe=FakeSystemProbe(),
        )
        explorer.apk = apk
        explorer.driver = driver
        explorer.session = SessionInfo(
            session_id=driver.session_id,
            server_url="http://127.0.0.1:4723",
            device_name="Test Android",
            udid="emulator-5554",
            capabilities=driver.capabilities,
        )

        capture = explorer.observe()

        self.assertTrue(capture.observation["stability"]["stable"])
        self.assertEqual(capture.observation["summary"]["app_elements"], 2)
        self.assertEqual(capture.observation["summary"]["system_elements"], 1)
        self.assertTrue(
            capture.observation["system"]["permission_prompt"]["present"]
        )
        self.assertTrue(capture.observation["system"]["status_bar"]["present"])
        self.assertTrue(capture.observation["system"]["navigation_bar"]["present"])
        self.assertEqual(
            capture.observation["system"]["navigation"]["mode"],
            "three_button",
        )
        self.assertEqual(
            capture.observation["screenshot"]["regions"]["status_bar"][
                "average_color"
            ]["hex"],
            "#ffffff",
        )
        self.assertEqual(
            capture.observation["screen"]["window_size"],
            {"width": 1080, "height": 1920},
        )

        self.assertEqual(
            capture.observation["hierarchy"]["node_count"],
            len(capture.observation["elements"]),
        )
        self.assertGreater(capture.observation["hierarchy"]["character_count"], 0)

        context = build_llm_context(capture.observation)
        self.assertIn("# CURRENT ANDROID SCREEN CONTEXT", context)
        self.assertIn("## AVAILABLE ACTIONS (2)", context)
        self.assertIn('"Continue"', context)
        self.assertIn('"Allow"', context)
        self.assertIn("three_button", context)
        self.assertIn("enabled=yes", context)
        self.assertIn("checked=no", context)
        self.assertIn("Included: 2/2 actions", context)
        self.assertIn("Full-fidelity fallback: appium-result.json", context)
        self.assertNotIn("<hierarchy>", context)
        self.assertLess(
            len(context),
            len(json.dumps(capture.observation, default=str)),
        )

        repeated = json.loads(json.dumps(capture.observation))
        duplicate = next(
            item for item in repeated["elements"] if item["text"] == "Continue"
        )
        duplicate = {
            **duplicate,
            "id": "element_duplicate",
            "bounds_raw": "[100,400][500,500]",
            "bounds": {
                "left": 100,
                "top": 400,
                "right": 500,
                "bottom": 500,
            },
        }
        repeated["elements"].append(duplicate)
        repeated_context = build_llm_context(repeated)
        self.assertIn("## VISIBLE TEXT (3 occurrences)", repeated_context)
        self.assertIn("[element_duplicate]", repeated_context)

    def test_appium_prefers_direct_adb_screenshot(self):
        expected = FakeDriver().get_screenshot_as_png()

        class ScreenshotProbe(FakeSystemProbe):
            def screenshot(self, udid):
                self.screenshot_udid = udid
                return expected

        class DriverWithoutScreenshot:
            def get_screenshot_as_png(self):
                raise AssertionError("Appium screenshot transport must not be used.")

        probe = ScreenshotProbe()
        explorer = AppiumExplorer(system_probe=probe)
        explorer.driver = DriverWithoutScreenshot()
        explorer.session = SessionInfo(
            session_id="session-1",
            server_url="http://127.0.0.1:4723",
            device_name="Test Android",
            udid="emulator-5554",
            capabilities={},
        )

        screenshot = explorer._capture_screenshot()

        self.assertEqual(screenshot, expected)
        self.assertEqual(probe.screenshot_udid, "emulator-5554")

    def test_captures_and_persists_one_complete_screen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            apk_path = root / "sample.apk"
            apk_path.write_bytes(b"apk")
            apk = ApkMetadata(
                path=apk_path,
                sha256="a" * 64,
                size_bytes=3,
                package="example.app",
                launch_activity="example.app.MainActivity",
            )
            capture = ScreenCapture(
                observation=observation(apk),
                hierarchy_xml="<hierarchy />",
                screenshot_png=b"\x89PNG\r\n",
            )
            explorer = FakeExplorer(apk, capture)
            store = SessionStore(root / "artifacts")

            result = ExplorationService(explorer, store).capture_first_screen(apk_path)

            self.assertTrue(explorer.closed)
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.element_count, 1)
            expected = {
                "appium-result.json",
                "appium_screen_content.html",
                "screenshots",
            }
            self.assertEqual(
                {path.name for path in result.screen_root.iterdir()},
                expected,
            )
            self.assertEqual(
                {
                    path.name
                    for path in (result.screen_root / "screenshots").iterdir()
                },
                {"screen.png"},
            )
            manifest = json.loads(
                (result.artifact_root / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["screens"], [result.screen_id])
            unified = json.loads(result.document_path.read_text(encoding="utf-8"))
            self.assertEqual(unified["contract"], "appium.screen_capture")
            self.assertEqual(unified["hierarchy"]["raw_xml"], "<hierarchy />")
            self.assertEqual(
                unified["artifacts"]["appium_result"],
                f"screens/{result.screen_id}/appium-result.json",
            )
            self.assertEqual(
                unified["artifacts"]["screenshots"]["full"],
                f"screens/{result.screen_id}/screenshots/screen.png",
            )
            viewer = result.viewer_path.read_text(encoding="utf-8")
            self.assertIn("Appium screen content", viewer)
            self.assertIn("Step 1", viewer)
            self.assertIn("Step 2", viewer)
            self.assertIn("Step 3", viewer)
            self.assertIn("Copy context", viewer)
            self.assertIn("Future capture additions", viewer)
            context = load_llm_context(result.document_path)
            self.assertIn("example.app", context)
            self.assertNotIn("<hierarchy />", context)
            action_result = {
                "action_id": "action_1",
                "transition_id": "transition_action_1",
                "session_id": result.session_id,
                "status": "completed",
                "classification": "acknowledged_no_observable_change",
                "request": {"action": "tap"},
                "before": {"screen_id": result.screen_id},
                "after": {"screen_id": result.screen_id},
            }
            action_path = store.save_action_result(action_result)
            self.assertTrue(action_path.is_file())

            with closing(
                sqlite3.connect(result.artifact_root / "session.db")
            ) as database:
                run = database.execute(
                    "SELECT status FROM runs WHERE run_id = ?",
                    (result.session_id,),
                ).fetchone()
                screen_count = database.execute(
                    "SELECT COUNT(*) FROM screens WHERE run_id = ?",
                    (result.session_id,),
                ).fetchone()[0]
                element_count = database.execute(
                    "SELECT COUNT(*) FROM elements WHERE run_id = ?",
                    (result.session_id,),
                ).fetchone()[0]
                action_count = database.execute(
                    "SELECT COUNT(*) FROM actions WHERE run_id = ?",
                    (result.session_id,),
                ).fetchone()[0]
                transition_count = database.execute(
                    "SELECT COUNT(*) FROM transitions WHERE run_id = ?",
                    (result.session_id,),
                ).fetchone()[0]
            self.assertEqual(run, ("completed",))
            self.assertEqual(screen_count, 1)
            self.assertEqual(element_count, 1)
            self.assertEqual(action_count, 1)
            self.assertEqual(transition_count, 1)
            graph = json.loads(
                (result.artifact_root / "graph.json").read_text(encoding="utf-8")
            )
            self.assertEqual(graph["edges"][0]["action_id"], "action_1")

    def test_monitored_tap_reports_delivery_effect_health_and_timing(self):
        apk = ApkMetadata(
            path=Path("sample.apk"),
            sha256="a" * 64,
            size_bytes=3,
            package="example.app",
            launch_activity="example.app.MainActivity",
        )
        driver = FakeDriver()
        explorer = AppiumExplorer(
            stability_timeout=0.2,
            stability_interval=0.001,
            system_probe=FakeSystemProbe(),
        )
        explorer.apk = apk
        explorer.driver = driver
        explorer.session = SessionInfo(
            session_id=driver.session_id,
            server_url="http://127.0.0.1:4723",
            device_name="Test Android",
            udid="emulator-5554",
            capabilities=driver.capabilities,
        )
        before = explorer.observe()
        request = ActionRequest.from_dict(
            {
                "screen_id": before.screen_id,
                "action": "tap",
                "target": {"element_id": "element_0003"},
                "completion": {
                    "timeout_ms": 1000,
                    "interval_ms": 10,
                    "stable_samples": 2,
                },
            }
        )

        result, after = ActionExecutor(explorer).execute(
            "run_123",
            request,
            before.observation,
            before,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result["classification"],
            "succeeded_screen_changed",
        )
        self.assertTrue(result["delivery"]["acknowledged"])
        self.assertTrue(result["effect"]["activity_changed"])
        self.assertTrue(result["effect"]["hierarchy_changed"])
        self.assertEqual(result["app_health"]["status"], "healthy")
        self.assertTrue(result["stability"]["stable"])
        self.assertGreaterEqual(result["stability"]["samples"], 2)
        self.assertIn("total_ms", result["timings"])
        self.assertEqual(
            after.observation["screen"]["activity"],
            "example.app.NextActivity",
        )

    def test_acknowledged_action_with_crash_evidence_is_classified_as_crash(self):
        request = ActionRequest.from_dict(
            {
                "screen_id": "screen_123",
                "action": "back",
            }
        )
        classification = ActionExecutor._classification(
            {"acknowledged": True, "status": "acknowledged"},
            {"stable": True, "session_unresponsive": False},
            {"observable_change": True},
            {
                "crash_detected": True,
                "anr_detected": False,
                "process_alive": False,
            },
            request,
            {},
        )
        self.assertEqual(classification, "app_crashed")

    def test_volatile_screen_change_allows_unique_stable_target(self):
        request = ActionRequest.from_dict(
            {
                "screen_id": "screen_123",
                "action": "tap",
                "target": {"element_id": "element_0036"},
            }
        )
        expected = {
            "elements": [
                {
                    "id": "element_0036",
                    "package": "example.app",
                    "resource_id": "example.app:id/open",
                    "content_description": "",
                    "interaction": "tap",
                    "enabled": True,
                    "displayed": True,
                }
            ]
        }
        live = {
            "elements": [
                {
                    "id": "element_0037",
                    "package": "example.app",
                    "resource_id": "example.app:id/open",
                    "content_description": "",
                    "interaction": "tap",
                    "enabled": True,
                    "displayed": True,
                }
            ]
        }

        self.assertTrue(
            SessionManager._target_remains_resolvable(
                request,
                expected,
                live,
            )
        )


if __name__ == "__main__":
    unittest.main()

