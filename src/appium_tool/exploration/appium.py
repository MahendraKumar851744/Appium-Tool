from __future__ import annotations

import io

import os

import re

import json

import time

import hashlib

import requests

import subprocess

from pathlib import Path

from typing import Any, Callable

from xml.etree import ElementTree

from PIL import Image, ImageStat

from appium_tool.exploration.models import ApkMetadata, JsonObject, ScreenCapture, SessionInfo


TRUE_VALUES = {"true", "1", "yes"}

SYSTEM_PACKAGES = {

    "android",

    "com.android.permissioncontroller",

    "com.android.systemui",

    "com.google.android.permissioncontroller",

}

ANDROID_ABIS = {

    "armeabi",

    "armeabi-v7a",

    "arm64-v8a",

    "x86",

    "x86_64",

    "mips",

    "mips64",

    "riscv64",

}

BOUNDS_PATTERN = re.compile(

    r"^\[(?P<left>-?\d+),(?P<top>-?\d+)\]"

    r"\[(?P<right>-?\d+),(?P<bottom>-?\d+)\]$"

)


def _as_bool(value: str | None) -> bool:

    return (value or "").strip().lower() in TRUE_VALUES


def _bounds(value: str | None) -> JsonObject | None:

    match = BOUNDS_PATTERN.match(value or "")

    if not match:

        return None

    bounds = {name: int(number) for name, number in match.groupdict().items()}

    return {

        **bounds,

        "width": bounds["right"] - bounds["left"],

        "height": bounds["bottom"] - bounds["top"],

        "center": {

            "x": (bounds["left"] + bounds["right"]) // 2,

            "y": (bounds["top"] + bounds["bottom"]) // 2,

        },

    }


def parse_hierarchy(
    
    xml_source: str,
    
    *,
    
    app_package: str | None = None,

) -> list[JsonObject]:

    """Flatten a UiAutomator hierarchy into stable, evidence-rich element records."""

    root = ElementTree.fromstring(xml_source)

    records: list[JsonObject] = []

    def visit(node: ElementTree.Element, xpath: str, depth: int) -> None:

        attributes = dict(node.attrib)

        class_name = attributes.get("class", node.tag)

        package = attributes.get("package", "")

        number = len(records) + 1

        if not app_package or package == app_package:

            source = "app"

        elif package in SYSTEM_PACKAGES or "permissioncontroller" in package:

            source = "system"

        elif package:

            source = "external"

        else:

            source = "unknown"

        record: JsonObject = {

            "id": f"element_{number:04d}",

            "number": number,

            "depth": depth,

            "xpath": xpath,

            "class": class_name,

            "package": package,

            "source": source,

            "resource_id": attributes.get("resource-id", ""),

            "text": attributes.get("text", ""),

            "content_description": attributes.get("content-desc", ""),

            "bounds": _bounds(attributes.get("bounds")),

            "bounds_raw": attributes.get("bounds", ""),

        }

        boolean_attributes = (

            "clickable",

            "long-clickable",

            "checkable",

            "checked",

            "enabled",

            "focusable",

            "focused",

            "scrollable",

            "selected",

            "password",

            "displayed",

        )

        for name in boolean_attributes:

            record[name.replace("-", "_")] = _as_bool(attributes.get(name))

        editable = class_name.endswith(("EditText", "AutoCompleteTextView"))

        record["editable"] = editable

        if record["clickable"]:

            record["interaction"] = "tap"

        elif record["long_clickable"]:

            record["interaction"] = "long_press"

        elif record["scrollable"]:

            record["interaction"] = "scroll"

        elif record["checkable"]:

            record["interaction"] = "toggle"

        elif editable:

            record["interaction"] = "type_text"

        else:

            record["interaction"] = None

        known_keys = {

            "class",

            "package",

            "resource-id",

            "text",

            "content-desc",

            "bounds",

            *boolean_attributes,

        }

        record["extra_attributes"] = {

            key: value for key, value in attributes.items() if key not in known_keys

        }

        records.append(record)

        sibling_counts: dict[str, int] = {}

        for child in node:

            child_class = child.attrib.get("class", child.tag)

            sibling_counts[child_class] = sibling_counts.get(child_class, 0) + 1

            visit(

                child,

                f"{xpath}/{child_class}[{sibling_counts[child_class]}]",

                depth + 1,

            )

    root_class = root.attrib.get("class", root.tag)

    visit(root, f"/{root_class}[1]", 0)

    return records


def summarize(elements: list[JsonObject]) -> JsonObject:
    
    return {
    
        "total_elements": len(elements),
    
        "visible_elements": sum(bool(item["displayed"]) for item in elements),
    
        "app_elements": sum(item["source"] == "app" for item in elements),
    
        "system_elements": sum(item["source"] == "system" for item in elements),
    
        "external_elements": sum(item["source"] == "external" for item in elements),
    
        "unknown_elements": sum(item["source"] == "unknown" for item in elements),
    
        "clickable_elements": sum(bool(item["clickable"]) for item in elements),
    
        "editable_elements": sum(bool(item["editable"]) for item in elements),
    
        "scrollable_elements": sum(bool(item["scrollable"]) for item in elements),
    
        "text_elements": sum(bool(item["text"]) for item in elements),
    
        "content_description_elements": sum(
    
            bool(item["content_description"]) for item in elements
    
        ),
    
        "resource_id_elements": sum(bool(item["resource_id"]) for item in elements),
    
        "interactive_element_ids": [
    
            item["id"] for item in elements if item["interaction"]
    
        ],
    
    }


""" 

`ApkInspector` examines an APK before installation and extracts details such as its package name, 
launch activity, Android version requirements and supported CPU architectures. 
This helps the workflow select a compatible emulator and launch the correct application.

"""

"""
AAPT stands for Android Asset Packaging Tool.

It is an Android SDK tool used to inspect APK files and extract following metadata without requiring the application to be installed.

1. Package name
2. Version code
3. Version name
4. Minimum Android version
5. Target Android version
6. App's Launch Activity
7. List Permissions
8. View package metadata like:
    a. App name
    b. Icon
    c. Supported screen sizes
    d. Supported CPU architectures
"""
class ApkInspector:
   
    def __init__(
   
        self,
   
        sdk_root: Path | None = None,
   
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
   
    ) -> None:
   
        self.sdk_root = sdk_root
   
        self.runner = runner

    def inspect(self, apk_path: str | Path) -> ApkMetadata:
   
        apk = Path(apk_path).expanduser().resolve()
   
        if not apk.is_file():
   
            raise FileNotFoundError(f"APK not found: {apk}")
   
        if apk.suffix.lower() != ".apk":
   
            raise ValueError(f"Expected an .apk file: {apk}")

        digest = hashlib.sha256()
   
        with apk.open("rb") as stream:
   
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
   
                digest.update(chunk)

        aapt = self._find_aapt()
   
        if aapt is None:
   
            return ApkMetadata(
   
                path=apk,
   
                sha256=digest.hexdigest(),
   
                size_bytes=apk.stat().st_size,
   
                warnings=("Android aapt was not found; APK manifest metadata is unavailable.",),
   
            )

        """

        Running the command:  aapt dump badging app.apk

        Output like:

        -----------------------------------------------
        package: name='com.demo.app'
        versionCode='12'
        versionName='1.5'

        sdkVersion:'24'
        targetSdkVersion:'34'

        launchable-activity:
        name='com.demo.MainActivity'

        native-code:
        'arm64-v8a'
        'x86_64'
        -----------------------------------------------

        """

        result = self.runner(
   
            [str(aapt), "dump", "badging", str(apk)],
   
            capture_output=True,
   
            text=True,
   
            encoding="utf-8",
   
            errors="replace",
   
            check=False,
   
        )
   
        if result.returncode != 0:
   
            reason = (result.stderr or result.stdout).strip()
   
            raise RuntimeError(f"Unable to inspect APK metadata with aapt: {reason}")
   
        values = self.parse_badging(result.stdout)
   
        native_abis = tuple(values.get("native_abis", []))
   
        supports_16kb = self._supports_16kb_page_size(aapt, apk, native_abis)
   
        return ApkMetadata(
   
            path=apk,
   
            sha256=digest.hexdigest(),
   
            size_bytes=apk.stat().st_size,
   
            package=values.get("package"),
   
            launch_activity=values.get("launch_activity"),
   
            label=values.get("label"),
   
            version_name=values.get("version_name"),
   
            version_code=values.get("version_code"),
   
            min_sdk=values.get("min_sdk"),
   
            target_sdk=values.get("target_sdk"),
   
            native_abis=native_abis,
   
            required_features=tuple(
   
                values.get("required_features", [])
   
            ),
   
            supports_16kb_page_size=supports_16kb,
   
            inspector=str(aapt),
   
        )

    """
    zipalign is an Android SDK command-line tool that optimizes an APK so it uses memory more efficiently when installed and running on a device.

    An APK is essentially a ZIP archive.

    zipalign aligns uncompressed data within the APK on specific byte boundaries (typically 4-byte boundaries).

    This allows Android to access resources directly from the APK using memory mapping (mmap) instead of copying them into RAM.

    """

    """

    Purpose:

    Determine whether native libraries inside the APK are correctly aligned for devices that use 16 KB memory pages.

    Modern Android devices (especially newer ARM64 devices) may use 16 KB memory pages instead of the traditional 4 KB.

    If native libraries (.so files) are properly aligned, Android can memory-map them efficiently instead of copying them into RAM.

    """
    def _supports_16kb_page_size(

        self,

        aapt: Path,

        apk: Path,

        native_abis: tuple[str, ...],

    ) -> bool | None:

        if not native_abis:

            return True

        executable = (

            "zipalign.exe"

            if os.name == "nt"

            else "zipalign"

        )

        zipalign = aapt.with_name(executable)

        if not zipalign.is_file():

            return None

        result = self.runner(

            [

                str(zipalign),

                "-c",

                "-P",

                "16",

                "-v",

                "4",

                str(apk),

            ],

            capture_output=True,

            text=True,

            encoding="utf-8",

            errors="replace",

            check=False,

        )

        return result.returncode == 0

    """
    The primary usecase of the function is to find the executable path of 'aapt'
    """
    def _find_aapt(self) -> Path | None:
    
        roots: list[Path | None] = [self.sdk_root]
    
        roots.extend(
    
            Path(value)
    
            for value in (
    
                os.environ.get("ANDROID_SDK_ROOT"),
    
                os.environ.get("ANDROID_HOME"),
    
            )
    
            if value
    
        )
    
        if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
    
            roots.append(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
    
        executable = "aapt.exe" if os.name == "nt" else "aapt"
    
        for root in roots:
    
            if root is None or not root.is_dir():
    
                continue
    
            build_tools = root / "build-tools"
    
            if not build_tools.is_dir():
    
                continue
    
            versions = sorted(
    
                (path for path in build_tools.iterdir() if path.is_dir()),
    
                reverse=True,
    
            )
    
            for version in versions:
    
                candidate = version / executable
    
                if candidate.is_file():
    
                    return candidate
    
        return None

    @staticmethod
    def parse_badging(output: str) -> JsonObject:

        def value(pattern: str) -> str | None:

            match = re.search(pattern, output, re.MULTILINE)

            return match.group(1) if match else None

        native_line = value(r"^native-code:\s*(.+)$") or ""

        return {

            "package": value(r"^package:\s+name='([^']+)'"),

            "version_code": value(r"^package:.*versionCode='([^']+)'"),

            "version_name": value(r"^package:.*versionName='([^']+)'"),

            "min_sdk": value(r"^sdkVersion:'([^']+)'"),

            "target_sdk": value(r"^targetSdkVersion:'([^']+)'"),

            "label": value(r"^application-label(?:-[^:]+)?:'([^']*)'"),

            "launch_activity": value(r"^launchable-activity:\s+name='([^']+)'"),

            "native_abis": [

                item

                for item in re.findall(r"'([^']+)'", native_line)

                if item in ANDROID_ABIS

            ],

            "required_features": sorted(

                set(

                    re.findall(

                        r"^uses-feature:\s+name='([^']+)'",

                        output,

                        re.MULTILINE,

                    )


                )

            ),

        }
        

""" 

`AdbSystemProbe` uses Android Debug Bridge (`adb`) to collect information directly from the connected Android device.

It helps the workflow:

- Take screenshots.
- Check whether the app is running.
- Read device, screen and system information.
- Detect crashes or unresponsive applications.
- Verify whether an Appium action actually affected the device.


**Appium performs actions, while `AdbSystemProbe` independently checks what is really happening on the Android device.**

"""
class AdbSystemProbe:
    
    """Read-only Android system probe used to enrich an Appium observation."""

    def __init__(
      
        self,
      
        adb_path: Path | None = None,
      
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    
    ) -> None:
    
        self.adb_path = adb_path or self._find_adb()
    
        self.runner = runner

    def collect(self, udid: str) -> JsonObject:
    
        if self.adb_path is None:
    
            raise RuntimeError("ADB was not found.")
    
        properties = {
    
            name: self._shell(udid, "getprop", name)
    
            for name in (
    
                "ro.build.version.release",
    
                "ro.build.version.sdk",
    
                "ro.build.fingerprint",
    
                "ro.product.manufacturer",
    
                "ro.product.model",
    
                "ro.product.brand",
    
                "ro.product.device",
    
                "ro.product.cpu.abilist",
    
                "persist.sys.locale",
    
                "ro.product.locale",
    
                "persist.sys.timezone",
    
            )
    
        }
    
        window_output = self._shell(udid, "dumpsys", "window", "displays")
    
        input_output = self._shell(udid, "dumpsys", "input_method")
    
        size = self._parse_wm(self._shell(udid, "wm", "size"), "size")
    
        density = self._parse_wm(
    
            self._shell(udid, "wm", "density"),
    
            "density",
    
        )
    
        insets = self.parse_insets(window_output)
    
        flags = self._hex_value(window_output, r"mLastSystemUiFlags=(0x[0-9a-fA-F]+)")
    
        navigation_value = self._setting(udid, "secure", "navigation_mode")
    
        return {
      
            "captured_via": "adb",
      
            "device": {
      
                "serial": udid,
      
                "manufacturer": properties["ro.product.manufacturer"],
      
                "model": properties["ro.product.model"],
      
                "brand": properties["ro.product.brand"],
      
                "device": properties["ro.product.device"],
      
                "android_version": properties["ro.build.version.release"],
      
                "api_level": self._integer(properties["ro.build.version.sdk"]),
      
                "build_fingerprint": properties["ro.build.fingerprint"],
      
                "supported_abis": [
      
                    value
      
                    for value in properties["ro.product.cpu.abilist"].split(",")
      
                    if value
      
                ],
      
                "locale": properties["persist.sys.locale"]
      
                or properties["ro.product.locale"],
      
                "timezone": properties["persist.sys.timezone"],
      
            },
      
            "display": {
      
                "physical_size": size.get("physical"),
      
                "override_size": size.get("override"),
      
                "physical_density_dpi": density.get("physical"),
      
                "override_density_dpi": density.get("override"),
      
                "rotation": self._integer_value(
      
                    window_output,
      
                    r"\bmRotation=(\d+)",
      
                ),
      
                "display_frames": self._display_frames(window_output),
      
                "auto_rotation": self._setting_bool(
      
                    udid,
      
                    "system",
      
                    "accelerometer_rotation",
      
                ),
      
                "user_rotation": self._setting_int(
      
                    udid,
      
                    "system",
      
                    "user_rotation",
      
                ),
      
                "font_scale": self._setting_float(
      
                    udid,
      
                    "system",
      
                    "font_scale",
      
                ),
      
            },
      
            "window": {
      
                "current_focus": self._line_value(
      
                    window_output,
      
                    r"mCurrentFocus=(.+)",
      
                ),
      
                "focused_app": self._line_value(
      
                    window_output,
      
                    r"mFocusedApp=(.+)",
      
                ),
      
                "system_ui_flags": {
      
                    "hex": f"0x{flags:x}" if flags is not None else None,
      
                    "value": flags,
      
                    "decoded": self.decode_system_ui_flags(flags),
      
                },
      
                "insets": insets,
      
            },
      
            "navigation": {
      
                "mode_value": self._integer(navigation_value),
      
                "mode": self.navigation_mode(navigation_value),
      
            },
      
            "input_method": {
      
                "shown": self._bool_value(
      
                    input_output,
      
                    r"\bmInputShown=(true|false)",
      
                ),
      
                "current_id": self._line_value(
      
                    input_output,
      
                    r"\bmCurId=([^\s]+)",
      
                ),
      
                "inset_visible": insets.get("ime", {}).get("visible"),
      
            },
      
        }

    def screenshot(self, udid: str) -> bytes:

        if self.adb_path is None:

            raise RuntimeError("ADB was not found.")

        creation_flags = (

            subprocess.CREATE_NO_WINDOW

            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")

            else 0

        )

        result = self.runner(

            [

                str(self.adb_path),

                "-s",

                udid,

                "exec-out",

                "screencap",

                "-p",

            ],

            capture_output=True,

            timeout=30,

            creationflags=creation_flags,

            check=False,

        )

        screenshot = bytes(result.stdout or b"")

        if result.returncode != 0 or not screenshot.startswith(

            b"\x89PNG\r\n\x1a\n"

        ):

            error = result.stderr or b""

            if isinstance(error, bytes):

                error = error.decode("utf-8", errors="replace")

            raise RuntimeError(

                "ADB screenshot capture failed: "

                + str(error).strip()

            )

        return screenshot

    def collect_action_health(
    
        self,
    
        udid: str,
    
        package: str,
    
        *,
    
        since_epoch: float,
    
    ) -> JsonObject:
    
        """Collect process and recent crash/ANR evidence for one action window."""
    
        try:
    
            pid_output = self._shell(udid, "pidof", package)
    
        except RuntimeError:
    
            pid_output = ""
    
        process_ids = [
    
            int(value)
    
            for value in pid_output.split()
    
            if value.strip().isdigit()
    
        ]
    
        try:
    
            log_output = self._shell(
    
                udid,
    
                "logcat",
    
                "-d",
    
                "-v",
    
                "epoch",
    
                "AndroidRuntime:E",
    
                "ActivityManager:E",
    
                "*:S",
    
            )
    
        except RuntimeError:
    
            log_output = ""
    
        recent_lines: list[str] = []
    
        for line in log_output.splitlines():
    
            match = re.match(r"^\s*(\d+(?:\.\d+)?)\s+", line)
    
            if match and float(match.group(1)) >= since_epoch:
    
                recent_lines.append(line)
    
        recent_log = "\n".join(recent_lines)
    
        package_log = package.lower() in recent_log.lower()
    
        return {
    
            "process_alive": bool(process_ids),
    
            "process_ids": process_ids,
    
            "crash_detected": bool(
    
                package_log
    
                and (
    
                    "fatal exception" in recent_log.lower()
    
                    or "force finishing activity" in recent_log.lower()
                )
    
            ),
    
            "anr_detected": bool(
    
                package_log and f"anr in {package}".lower() in recent_log.lower()
            ),
    
            "recent_error_log": recent_log[-8000:] if recent_log else "",
    
        }

    def _shell(self, udid: str, *arguments: str) -> str:
    
        assert self.adb_path is not None
    
        creation_flags = (
    
            subprocess.CREATE_NO_WINDOW
    
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
    
            else 0
    
        )
    
        result = self.runner(
    
            [str(self.adb_path), "-s", udid, "shell", *arguments],
    
            capture_output=True,
    
            text=True,
    
            encoding="utf-8",
    
            errors="replace",
    
            timeout=10,
    
            creationflags=creation_flags,
    
            check=False,
    
        )
    
        if result.returncode != 0:
    
            reason = (result.stderr or result.stdout).strip()
    
            raise RuntimeError(f"ADB shell command failed: {reason}")
    
        return result.stdout.strip()

    def _setting(self, udid: str, namespace: str, name: str) -> str:

        value = self._shell(udid, "settings", "get", namespace, name)

        return "" if value == "null" else value

    def _setting_int(self, udid: str, namespace: str, name: str) -> int | None:

        return self._integer(self._setting(udid, namespace, name))

    def _setting_float(

        self,

        udid: str,

        namespace: str,

        name: str,

    ) -> float | None:

        value = self._setting(udid, namespace, name)

        try:

            return float(value)

        except (TypeError, ValueError):

            return None

    def _setting_bool(

        self,

        udid: str,

        namespace: str,

        name: str,

    ) -> bool | None:

        value = self._setting_int(udid, namespace, name)

        return None if value is None else bool(value)

    @staticmethod
    def parse_insets(output: str) -> JsonObject:

        result: JsonObject = {}

        pattern = re.compile(

            r"InsetsSource type=ITYPE_(?P<name>[A-Z_]+) "

            r"frame=\[(?P<left>-?\d+),(?P<top>-?\d+)\]"

            r"\[(?P<right>-?\d+),(?P<bottom>-?\d+)\] "

            r"visible=(?P<visible>true|false)"

        )

        names = {

            "STATUS_BAR": "status_bar",

            "NAVIGATION_BAR": "navigation_bar",

            "IME": "ime",

            "DISPLAY_CUTOUT": "display_cutout",

        }

        for match in pattern.finditer(output):

            key = names.get(match.group("name"))

            if key is None or key in result:

                continue

            bounds = {

                field: int(match.group(field))

                for field in ("left", "top", "right", "bottom")

            }

            result[key] = {

                "visible": match.group("visible") == "true",

                "bounds": {

                    **bounds,

                    "width": bounds["right"] - bounds["left"],

                    "height": bounds["bottom"] - bounds["top"],

                    "center": {

                        "x": (bounds["left"] + bounds["right"]) // 2,

                        "y": (bounds["top"] + bounds["bottom"]) // 2,

                    },

                },

            }

        return result


    @staticmethod
    def decode_system_ui_flags(value: int | None) -> list[str]:

        if value is None:

            return []

        flags = {

            0x00000001: "low_profile",

            0x00000002: "hide_navigation",

            0x00000004: "fullscreen",

            0x00000010: "light_navigation_bar",

            0x00000100: "layout_stable",

            0x00000200: "layout_hide_navigation",

            0x00000400: "layout_fullscreen",

            0x00000800: "immersive",

            0x00001000: "immersive_sticky",

            0x00002000: "light_status_bar",

        }

        return [name for flag, name in flags.items() if value & flag]

    @staticmethod
    def navigation_mode(value: str) -> str | None:

        return {

            "0": "three_button",

            "1": "two_button",

            "2": "gesture",

        }.get(value)


    @staticmethod
    def _parse_wm(output: str, kind: str) -> JsonObject:

        result: JsonObject = {}

        if kind == "size":

            pattern = re.compile(

                r"^(Physical|Override) size:\s*(\d+)x(\d+)$",

                re.MULTILINE,

            )

            for match in pattern.finditer(output):

                result[match.group(1).lower()] = {

                    "width": int(match.group(2)),

                    "height": int(match.group(3)),

                }

        else:

            pattern = re.compile(

                r"^(Physical|Override) density:\s*(\d+)$",

                re.MULTILINE,

            )

            for match in pattern.finditer(output):

                result[match.group(1).lower()] = int(match.group(2))

        return result


    @staticmethod
    def _display_frames(output: str) -> JsonObject | None:

        match = re.search(r"DisplayFrames w=(\d+) h=(\d+) r=(\d+)", output)

        if not match:

            return None

        return {

            "width": int(match.group(1)),

            "height": int(match.group(2)),

            "rotation": int(match.group(3)),

        }


    @staticmethod
    def _find_adb() -> Path | None:

        executable = "adb.exe" if os.name == "nt" else "adb"

        roots = [

            Path(value)

            for value in (

                os.environ.get("ANDROID_SDK_ROOT"),

                os.environ.get("ANDROID_HOME"),

            )

            if value

        ]

        if os.name == "nt" and os.environ.get("LOCALAPPDATA"):

            roots.append(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")

        for root in roots:

            candidate = root / "platform-tools" / executable

            if candidate.is_file():

                return candidate

        return None


    @staticmethod
    def _integer(value: Any) -> int | None:

        try:

            return int(value)

        except (TypeError, ValueError):

            return None


    @classmethod
    def _integer_value(cls, output: str, pattern: str) -> int | None:

        match = re.search(pattern, output)

        return cls._integer(match.group(1)) if match else None

    @staticmethod
    def _hex_value(output: str, pattern: str) -> int | None:

        match = re.search(pattern, output)

        return int(match.group(1), 16) if match else None


    @staticmethod
    def _line_value(output: str, pattern: str) -> str | None:

        match = re.search(pattern, output)

        return match.group(1).strip() if match else None

    @staticmethod
    def _bool_value(output: str, pattern: str) -> bool | None:

        match = re.search(pattern, output)

        return match.group(1) == "true" if match else None


class AppiumExplorer:
    
    """Deterministic Appium boundary for starting and observing one Android app."""

    def __init__(
      
        self,
      
        *,
      
        server_url: str = "http://127.0.0.1:4723",
      
        device_name: str = "Android",
      
        udid: str | None = None,
      
        keep_data: bool = False,
      
        stability_timeout: float = 15.0,
      
        stability_interval: float = 0.5,
      
        inspector: ApkInspector | None = None,
      
        system_probe: AdbSystemProbe | None = None,
     
        driver_factory: Callable[[str, JsonObject], Any] | None = None,
    
    ) -> None:
       
        self.server_url = server_url.rstrip("/")
       
        self.device_name = device_name
       
        self.udid = udid
       
        self.keep_data = keep_data
       
        self.stability_timeout = stability_timeout
       
        self.stability_interval = stability_interval
       
        self.inspector = inspector or ApkInspector()
       
        self.system_probe = system_probe or AdbSystemProbe()
       
        self.driver_factory = driver_factory
       
        self.driver: Any | None = None
       
        self.apk: ApkMetadata | None = None
       
        self.session: SessionInfo | None = None
       
        self.launch_source = "apk"

    def start(self, apk_path: str | Path) -> tuple[ApkMetadata, SessionInfo]:
        
        self.launch_source = "apk"
        
        self.apk = self.inspector.inspect(apk_path)
        
        self._require_server()
        
        capabilities = self._capabilities(self.apk)
        
        self._create_session_with_adb_recovery(capabilities)
        
        self._wait_until_stable()
        
        return self.apk, self.session

    def start_package(self, package_id: str) -> tuple[ApkMetadata, SessionInfo]:
       
        """Start an Appium session and activate an already-installed package."""
       
        self.launch_source = "installed_package"
       
        self.apk = ApkMetadata(
       
            path=None,
       
            sha256="",
       
            size_bytes=0,
       
            package=package_id,
       
            inspector="package_id",
       
            warnings=("APK metadata is unavailable for package-only launch.",),
       
        )
       
        self._require_server()
       
        capabilities = self._package_capabilities(package_id)
       
        self._create_session_with_adb_recovery(capabilities)
       
        self.driver.activate_app(package_id)
       
        self._wait_until_stable()

        return self.apk, self.session

    def _create_session(self, capabilities: JsonObject) -> None:
        
        self.driver = (
        
            self.driver_factory(self.server_url, capabilities)
        
            if self.driver_factory
        
            else self._create_driver(capabilities)
        
        )
        
        actual_capabilities = self._json_safe(
        
            dict(getattr(self.driver, "capabilities", {}) or {})
        
        )
        
        self.session = SessionInfo(
        
            session_id=str(getattr(self.driver, "session_id", "")),
        
            server_url=self.server_url,
        
            device_name=self.device_name,
        
            udid=self.udid or actual_capabilities.get("udid"),
        
            capabilities=actual_capabilities,
        
        )


    def _create_session_with_adb_recovery(

        self,

        capabilities: JsonObject,

        *,

        max_attempts: int = 3,

    ) -> None:

        for attempt in range(1, max_attempts + 1):

            try:

                self._create_session(capabilities)

                return

            except Exception as error:

                self.driver = None

                self.session = None

                if (

                    attempt >= max_attempts

                    or not self._is_transient_adb_session_error(error)

                ):

                    raise

                time.sleep(3)

        raise RuntimeError("Appium session recovery attempts were exhausted.")

    @staticmethod
    def _is_transient_adb_session_error(error: Exception) -> bool:

        message = str(error).lower()

        return any(

            signal in message

            for signal in (

                "error: closed",

                "device offline",

                "no connected devices have been detected",

                "could not find online devices",

                "device unauthorized",

            )

        )


    def _capture_screenshot(self) -> bytes:

        if self.driver is None or self.session is None:

            raise RuntimeError("Appium session has not been started.")

        adb_screenshot = getattr(self.system_probe, "screenshot", None)

        if callable(adb_screenshot) and self.session.udid:

            return bytes(adb_screenshot(self.session.udid))

        return bytes(self.driver.get_screenshot_as_png())


    def observe(self) -> ScreenCapture:
        
        if self.driver is None or self.apk is None or self.session is None:
        
            raise RuntimeError("Appium session has not been started.")

        hierarchy, stability = self._wait_until_stable()
        
        screenshot = self._capture_screenshot()
        
        collection_errors: list[JsonObject] = []

        current_package = self._safe(
        
            "current_package",
        
            lambda: self.driver.current_package,
        
            collection_errors,
        
        )
        
        current_activity = self._safe(
        
            "current_activity",
        
            lambda: self.driver.current_activity,
        
            collection_errors,
        
        )
        
        orientation = self._safe(
        
            "orientation",
        
            lambda: self.driver.orientation,
        
            collection_errors,
        
        )
        
        window_size = self._safe(
        
            "window_size",
        
            lambda: self.driver.get_window_size(),
        
            collection_errors,
        
            {},
        
        )
        
        contexts = self._safe(
        
            "contexts",
        
            lambda: list(self.driver.contexts),
        
            collection_errors,
        
            [],
        
        )
        
        keyboard_visible = self._safe(
        
            "keyboard_visible",
        
            lambda: bool(self.driver.is_keyboard_shown()),
        
            collection_errors,
        
            None,
        
        )
        
        active_udid = self.session.udid or self.udid
        
        adb_system = (
        
            self._safe(
        
                "adb_system",
        
                lambda: self.system_probe.collect(str(active_udid)),
        
                collection_errors,
        
                {},
        
            )
        
            if active_udid
        
            else {}
        
        )

        elements = parse_hierarchy(hierarchy, app_package=self.apk.package)
        
        summary = summarize(elements)
        
        system_state = self._system_state(
        
            elements,
        
            current_package=current_package,
        
            keyboard_visible=keyboard_visible,
        
            window_size=window_size,
        
            capabilities=self.session.capabilities,
        
            adb_system=adb_system,
        
        )
        
        captured_at = self._timestamp()
        
        screenshot_sha = hashlib.sha256(screenshot).hexdigest()
        
        hierarchy_sha = hashlib.sha256(hierarchy.encode("utf-8")).hexdigest()
        
        screenshot_details = self._image_details(screenshot, system_state)
        
        fingerprint_payload = {
        
            "package": current_package,
        
            "activity": current_activity,
        
            "orientation": orientation,
        
            "hierarchy_sha256": hierarchy_sha,
        
            "screenshot_sha256": screenshot_sha,
        
            "system": system_state,
        
        }
        
        fingerprint = hashlib.sha256(
        
            json.dumps(
        
                fingerprint_payload,
        
                sort_keys=True,
        
                separators=(",", ":"),
        
                default=str,
        
            ).encode("utf-8")
        
        ).hexdigest()
        
        screen_id = f"screen_{fingerprint[:16]}"
        
        observation: JsonObject = {
        
            "contract": "appium.screen_capture",
        
            "schema_version": 1,
        
            "screen_id": screen_id,
        
            "fingerprint": fingerprint,
        
            "captured_at": captured_at,
        
            "input": {
        
                "launch_source": self.launch_source,
        
                "apk_path": (
        
                    str(self.apk.path) if self.apk.path is not None else None
        
                ),
        
                "package_id": self.apk.package,
        
                "server_url": self.server_url,
        
                "device_name": self.device_name,
        
                "udid": active_udid,
        
                "keep_data": self.keep_data,

            },
            
            "apk": self.apk.to_dict(),
            
            "session": self.session.to_dict(),
            
            "device": adb_system.get("device", {}),
            
            "display": {
            
                **adb_system.get("display", {}),
            
                "appium_window_size": window_size,
            
                "appium_orientation": orientation,
            
                "appium_pixel_ratio": self.session.capabilities.get("pixelRatio"),
            
                "appium_viewport": self.session.capabilities.get("viewportRect"),
            
            },
            
            "screen": {
            
                "package": current_package,
            
                "activity": current_activity,
            
                "orientation": orientation,
            
                "window_size": window_size,
            
                "contexts": contexts,
            
                "app_in_foreground": current_package == self.apk.package,
            
            },
            
            "system": system_state,
            
            "stability": stability,
            
            "hashes": {
            
                "screenshot_sha256": screenshot_sha,
            
                "hierarchy_sha256": hierarchy_sha,
            
            },
            
            "screenshot": screenshot_details,
           
            "hierarchy": {
           
                "format": "xml",
           
                "encoding": "utf-8",
           
                "character_count": len(hierarchy),
           
                "byte_count": len(hierarchy.encode("utf-8")),
           
                "node_count": len(elements),
           
                "sha256": hierarchy_sha,
           
                "raw_xml": hierarchy,
           
            },
           
            "summary": summary,
           
            "elements": elements,
           
            "collection_errors": collection_errors,
        
        }
        
        return ScreenCapture(
        
            observation=observation,
        
            hierarchy_xml=hierarchy,
        
            screenshot_png=screenshot,
        )

    def close(self) -> None:
       
        driver, self.driver = self.driver, None
       
        if driver is not None:
       
            driver.quit()


    def _require_server(self) -> None:
       
        try:
       
            response = requests.get(f"{self.server_url}/status", timeout=5)
       
            response.raise_for_status()
       
            payload = response.json()
       
        except Exception as error:
       
            raise RuntimeError(
       
                f"Appium is not ready at {self.server_url}: {error}"
       
            ) from error
       
        if not payload.get("value", {}).get("ready"):
       
            raise RuntimeError(f"Appium reported that it is not ready: {payload}")


    def _capabilities(self, apk: ApkMetadata) -> JsonObject:
        
        capabilities = self._base_capabilities()
        
        if apk.path is None:
        
            raise RuntimeError("APK path is required for APK-based launch.")
        
        capabilities["appium:app"] = str(apk.path)
        
        if apk.package:
        
            capabilities["appium:appPackage"] = apk.package
        
        if apk.launch_activity:
        
            capabilities["appium:appActivity"] = apk.launch_activity
        
        return capabilities


    def _package_capabilities(self, package_id: str) -> JsonObject:
        
        capabilities = self._base_capabilities()
        
        capabilities.update(
          
            {
        
                "appium:appPackage": package_id,
        
                "appium:autoLaunch": False,
        
                "appium:noReset": True,
        
            }
        
        )
        
        return capabilities



    """ 
  
    `_base_capabilities` tells Appium which Android device to use and how to control it. 
  
    Without these settings, Appium would not know how to start the testing session.
    
    """
    def _base_capabilities(self) -> JsonObject:
        
        capabilities: JsonObject = {
        
            "platformName": "Android",
        
            "appium:automationName": "UiAutomator2",
        
            "appium:deviceName": self.device_name,
        
            "appium:autoGrantPermissions": False,
        
            "appium:noReset": self.keep_data,
        
            "appium:fullReset": False,
        
            "appium:newCommandTimeout": 180,
        
            "appium:disableWindowAnimation": True,
        
            "appium:adbExecTimeout": 120_000,
        
            "appium:androidInstallTimeout": 180_000,
        
            "appium:uiautomator2ServerInstallTimeout": 120_000,
        
            "appium:uiautomator2ServerLaunchTimeout": 120_000,
        
            "appium:appWaitDuration": 120_000,
        
            "appium:appWaitActivity": "*",
        
        }
        
        if self.udid:
        
            capabilities["appium:udid"] = self.udid
        
        return capabilities


    def _create_driver(self, capabilities: JsonObject) -> Any:
        
        try:
        
            from appium import webdriver
        
            from appium.options.android import UiAutomator2Options
        
        except ImportError as error:
        
            raise RuntimeError(
        
                "The Appium Python client is missing. Run .\\scripts\\setup.ps1."
        
            ) from error
        
        options = UiAutomator2Options().load_capabilities(capabilities)
        
        return webdriver.Remote(command_executor=self.server_url, options=options)


    def _wait_until_stable(self) -> tuple[str, JsonObject]:
        
        if self.driver is None:
        
            raise RuntimeError("Appium session has not been started.")
        
        started = time.monotonic()
        
        deadline = started + max(0.1, self.stability_timeout)
        
        previous_hash: str | None = None
        
        stable_samples = 0
        
        samples = 0
        
        latest_source = ""

        while True:
        
            latest_source = str(self.driver.page_source)
        
            samples += 1
        
            current_hash = hashlib.sha256(latest_source.encode("utf-8")).hexdigest()
        
            if current_hash == previous_hash:
        
                stable_samples += 1
        
            else:
        
                stable_samples = 0
        
            if stable_samples >= 2:
        
                return latest_source, {
        
                    "stable": True,
        
                    "samples": samples,
        
                    "duration_ms": round((time.monotonic() - started) * 1000),
        
                }
        
            if time.monotonic() >= deadline:
        
                return latest_source, {
        
                    "stable": False,
        
                    "samples": samples,
        
                    "duration_ms": round((time.monotonic() - started) * 1000),
        
                    "reason": "timeout",
        
                }
        
            previous_hash = current_hash
        
            time.sleep(max(0.05, self.stability_interval))


    @staticmethod
    def _system_state(
        
        elements: list[JsonObject],
        
        *,
        
        current_package: Any,
        
        keyboard_visible: Any,
        
        window_size: Any,
        
        capabilities: JsonObject,
        
        adb_system: JsonObject,
        
    ) -> JsonObject:
        
        system_elements = [item for item in elements if item["source"] == "system"]
        
        permission_elements = [
        
            item
        
            for item in system_elements
        
            if "permissioncontroller" in str(item["package"]).lower()
        
        ]
        
        dialog_elements = [
        
            item
        
            for item in elements
        
            if "dialog" in str(item["class"]).lower()
        
            or "alert" in str(item["class"]).lower()
        
        ]

        def bar(

            name: str,
           
            *,
           
            reported_height: Any = None,
           
            bottom: bool = False,
        
        ) -> JsonObject:
        
            matches = [
        
                item
        
                for item in system_elements
        
                if name in str(item["resource_id"]).lower()
        
                or name in str(item["content_description"]).lower()
        
            ]
        
            bounds = [item["bounds"] for item in matches if item["bounds"]]
        
            try:
        
                height = int(reported_height or 0)
        
                width = int((window_size or {}).get("width", 0))
        
                screen_height = int((window_size or {}).get("height", 0))
        
                if not bounds and height > 0 and width > 0:
        
                    top = max(0, screen_height - height) if bottom else 0
        
                    bounds = [
        
                        {
        
                            "left": 0,
        
                            "top": top,
        
                            "right": width,
        
                            "bottom": top + height,
        
                            "width": width,
        
                            "height": height,
        
                            "center": {"x": width // 2, "y": top + height // 2},
        
                        }
        
                    ]
        
            except (TypeError, ValueError):
        
                height = None
        
            return {
        
                "present": bool(matches or bounds),
        
                "elements": [item["id"] for item in matches],
        
                "bounds": bounds,
        
                "reported_height": height,
        
            }

        fallback_status = bar(
        
            "statusbar",
        
            reported_height=capabilities.get("statBarHeight")
        
            or capabilities.get("statusBarHeight"),
        
        )
        
        fallback_navigation = bar(
        
            "navigationbar",
        
            reported_height=capabilities.get("navigationBarHeight"),
        
            bottom=True,
        
        )
        
        insets = adb_system.get("window", {}).get("insets", {})
        
        decoded_flags = (
        
            adb_system.get("window", {})
        
            .get("system_ui_flags", {})
        
            .get("decoded", [])
        )

        def authoritative_bar(
       
            name: str,
       
            fallback: JsonObject,
       
            light_flag: str,
       
        ) -> JsonObject:
       
            inset = insets.get(name)
       
            if not inset:
       
                return {**fallback, "source": "appium"}
       
            bounds = inset.get("bounds")
       
            return {
       
                "present": bool(inset.get("visible") and bounds),
       
                "visible": inset.get("visible"),
       
                "elements": fallback["elements"],
       
                "bounds": [bounds] if bounds else [],
       
                "reported_height": bounds.get("height") if bounds else 0,
       
                "dark_icons": light_flag in decoded_flags,
       
                "light_background": light_flag in decoded_flags,
       
                "source": "adb_insets",
       
            }

        status_bar = authoritative_bar(
       
            "status_bar",
       
            fallback_status,
       
            "light_status_bar",
       
        )
       
        navigation_bar = authoritative_bar(
       
            "navigation_bar",
       
            fallback_navigation,
       
            "light_navigation_bar",
       
        )
       
        adb_input = adb_system.get("input_method", {})
       
        keyboard_from_adb = adb_input.get("inset_visible")
       
        if keyboard_from_adb is None:
       
            keyboard_from_adb = adb_input.get("shown")
        
        return {
       
            "current_package": current_package,
       
            "keyboard_visible": (
       
                keyboard_from_adb
        
                if keyboard_from_adb is not None
        
                else keyboard_visible
        
            ),
        
            "status_bar": status_bar,
        
            "navigation_bar": navigation_bar,
        
            "viewport": capabilities.get("viewportRect"),
        
            "pixel_ratio": capabilities.get("pixelRatio"),
        
            "navigation": adb_system.get("navigation", {}),
        
            "window": adb_system.get("window", {}),
        
            "input_method": adb_input,
        
            "permission_prompt": {
        
                "present": bool(permission_elements),
        
                "elements": [item["id"] for item in permission_elements],
        
            },
        
            "dialog": {
        
                "present": bool(dialog_elements),
        
                "elements": [item["id"] for item in dialog_elements],
        
            },
        
            "system_element_count": len(system_elements),
        
            "system_packages": sorted(
        
                {str(item["package"]) for item in system_elements if item["package"]}
        
            ),
        
        }
        
        
    @staticmethod
    def _image_details(screenshot: bytes, system_state: JsonObject) -> JsonObject:
        
        with Image.open(io.BytesIO(screenshot)) as image:
        
            image.load()
        
            regions: JsonObject = {}
            
            for name in ("status_bar", "navigation_bar"):
            
                bar = system_state.get(name, {})
            
                bounds_list = bar.get("bounds", [])
            
                if not bounds_list:
            
                    continue
            
                bounds = bounds_list[0]
            
                crop_box = (
            
                    max(0, int(bounds["left"])),
            
                    max(0, int(bounds["top"])),
            
                    min(image.width, int(bounds["right"])),
            
                    min(image.height, int(bounds["bottom"])),
            
                )
            
                if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
            
                    continue
            
                region = image.crop(crop_box).convert("RGB")
            
                mean = tuple(round(value) for value in ImageStat.Stat(region).mean)
            
                luminance = round(
            
                    0.2126 * mean[0] + 0.7152 * mean[1] + 0.0722 * mean[2],
            
                    2,
                )
            
                regions[name] = {
            
                    "bounds": bounds,
            
                    "average_color": {
            
                        "red": mean[0],
            
                        "green": mean[1],
            
                        "blue": mean[2],
            
                        "hex": f"#{mean[0]:02x}{mean[1]:02x}{mean[2]:02x}",
            
                    },
            
                    "luminance": luminance,
            
                    "light_background": luminance >= 128,
            
                }
            
            return {
            
                "format": image.format or "PNG",
            
                "mime_type": "image/png",
            
                "mode": image.mode,
            
                "width": image.width,
            
                "height": image.height,
            
                "byte_count": len(screenshot),
            
                "sha256": hashlib.sha256(screenshot).hexdigest(),
            
                "regions": regions,
            
            }


    @staticmethod
    def _safe(
        
        name: str,
        
        function: Callable[[], Any],
        
        errors: list[JsonObject],
        
        fallback: Any = None,
    
    ) -> Any:
    
        try:
    
            return function()
    
        except Exception as error:
    
            errors.append({"field": name, "error": str(error)})
    
            return fallback

    @staticmethod
    def _json_safe(value: Any) -> JsonObject:

        return json.loads(json.dumps(value, default=str))

    @staticmethod
    def _timestamp() -> str:
        
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()

