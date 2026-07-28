from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from appium_tool.exploration.models import ApkMetadata


JsonObject = dict[str, Any]

KNOWN_ABIS = {
    "arm64-v8a",
    "armeabi-v7a",
    "armeabi",
    "x86",
    "x86_64",
}


class DeviceSelectionError(RuntimeError):

    def __init__(

        self,

        message: str,

        *,

        details: JsonObject | None = None,

    ) -> None:

        super().__init__(message)

        self.details = details or {}


def apk_requirement_profile(metadata: ApkMetadata) -> JsonObject:

    native_abis = sorted(

        {
            str(value).strip()
            for value in metadata.native_abis
            if str(value).strip() in KNOWN_ABIS
        }

    )

    min_api = _integer(metadata.min_sdk)

    target_api = _integer(metadata.target_sdk)

    return {

        "contract": "appium.apk_runtime_requirements",

        "schema_version": 1,

        "apk_path": (
            str(metadata.path)
            if metadata.path is not None
            else None
        ),

        "sha256": metadata.sha256,

        "package_id": metadata.package,

        "launch_activity": metadata.launch_activity,

        "minimum_api_level": min_api,

        "target_api_level": target_api,

        "native_abis": native_abis,

        "abi_independent": not native_abis,

        "required_features": sorted(

            set(metadata.required_features)

        ),

        "supports_16kb_page_size": (

            metadata.supports_16kb_page_size

        ),

        "selection_rules": {

            "requires_booted_device": True,

            "requires_api_at_least": min_api,

            "requires_advertised_abi_intersection": native_abis,

            "native_bridge_must_be_advertised_by_device": True,

            "reject_16kb_device_when_apk_is_incompatible": True,

            "requires_device_features": sorted(

                set(metadata.required_features)

            ),

        },

        "inspection": {

            "inspector": metadata.inspector,

            "warnings": list(metadata.warnings),

        },

    }


def evaluate_device(

    requirements: JsonObject,

    device: JsonObject,

) -> JsonObject:

    required_abis = set(requirements.get("native_abis") or [])

    device_abis = {

        item.strip()
        for item in str(device.get("abi_list") or "").split(",")
        if item.strip()

    }

    minimum_api = _integer(requirements.get("minimum_api_level"))

    device_api = _integer(device.get("api_level"))

    page_size = _integer(device.get("page_size_bytes"))

    supports_16kb = requirements.get(

        "supports_16kb_page_size"

    )

    required_features = set(

        requirements.get("required_features") or []

    )

    device_features = set(device.get("features") or [])

    state_ready = (

        device.get("state") == "device"

        and device.get("boot_completed") is True

    )

    abi_match = (

        sorted(required_abis.intersection(device_abis))

        if required_abis

        else ["abi_independent"]

    )

    api_compatible = (

        minimum_api is None

        or (

            device_api is not None

            and device_api >= minimum_api

        )

    )

    page_size_compatible = not (

        page_size == 16_384

        and supports_16kb is False

    )

    features_missing = sorted(

        required_features - device_features

    )

    features_compatible = not features_missing

    reasons: list[str] = []

    if not state_ready:

        reasons.append("device is not connected and fully booted")

    if required_abis and not abi_match:

        reasons.append(

            "no APK ABI is advertised by the device"

        )

    if not api_compatible:

        reasons.append(

            (

                f"device API {device_api} is below required "

                f"API {minimum_api}"

            )

        )

    if not page_size_compatible:

        reasons.append(

            "device uses 16 KB pages but APK native libraries "

            "are not 16 KB compatible"

        )

    if features_missing:

        reasons.append(

            "device is missing required features: "

            + ", ".join(features_missing)

        )

    compatible = (

        state_ready

        and bool(abi_match)

        and api_compatible

        and page_size_compatible

        and features_compatible

    )

    return {

        "device_id": device.get("serial"),

        "state": device.get("state"),

        "boot_completed": device.get("boot_completed"),

        "api_level": device_api,

        "android_version": device.get("android_version"),

        "model": device.get("model"),

        "is_emulator": device.get("is_emulator"),

        "avd_name": device.get("avd_name"),

        "supported_abis": sorted(device_abis),

        "matching_abis": abi_match,

        "api_compatible": api_compatible,

        "page_size_bytes": page_size,

        "page_size_compatible": page_size_compatible,

        "required_features": sorted(required_features),

        "missing_features": features_missing,

        "features_compatible": features_compatible,

        "compatible": compatible,

        "rejection_reasons": reasons,

    }


class AndroidDeviceCoordinator:

    """Select, start, or provision a device for one inspected APK."""

    def __init__(

        self,

        *,

        project_root: str | Path,

        runtime_manager: Any,

        runner: Callable[..., subprocess.CompletedProcess[str]] = (
            subprocess.run
        ),

        process_launcher: Callable[..., subprocess.Popen[Any]] = (
            subprocess.Popen
        ),

        sleeper: Callable[[float], None] = time.sleep,

    ) -> None:

        self.project_root = Path(project_root).expanduser().resolve()

        self.runtime_manager = runtime_manager

        self.runner = runner

        self.process_launcher = process_launcher

        self.sleeper = sleeper

    def inventory(

        self,

        requirements: JsonObject,

    ) -> JsonObject:

        runtime = self.runtime_manager.status()

        device_evaluations = [

            evaluate_device(requirements, device)

            for device in runtime.get("devices", [])

            if isinstance(device, dict)

        ]

        avds = self._avd_inventory(runtime, requirements)

        candidates = sorted(

            (

                item
                for item in avds
                if item["potentially_compatible"]

            ),

            key=lambda item: self._avd_score(

                requirements,

                item,

            ),

            reverse=True,

        )

        return {

            "contract": "appium.device_compatibility_inventory",

            "schema_version": 1,

            "requirements": requirements,

            "connected_devices": device_evaluations,

            "configured_avds": avds,

            "compatible_connected_devices": [

                item
                for item in device_evaluations
                if item["compatible"]

            ],

            "candidate_avds": candidates,

        }

    def ensure_device(

        self,

        requirements: JsonObject,

        *,

        requested_device_id: str | None = None,

        options: JsonObject | None = None,

    ) -> JsonObject:

        settings = self._options(options)

        inventory = self.inventory(requirements)

        connected = inventory["connected_devices"]

        if requested_device_id:

            selected = next(

                (

                    item
                    for item in connected
                    if item.get("device_id") == requested_device_id

                ),

                None,

            )

            if selected is None:

                raise DeviceSelectionError(

                    (

                        f"Requested Android device "

                        f"'{requested_device_id}' is not connected."

                    ),

                    details=inventory,

                )

            if not selected["compatible"]:

                raise DeviceSelectionError(

                    (

                        f"Requested Android device "

                        f"'{requested_device_id}' is incompatible "

                        "with the APK."

                    ),

                    details={

                        **inventory,

                        "selected_device": selected,

                    },

                )

            return self._selection_result(

                requirements,

                selected,

                source="requested_connected_device",

                inventory=inventory,

            )

        compatible = list(

            inventory["compatible_connected_devices"]

        )

        if compatible:

            selected = max(

                compatible,

                key=lambda item: self._device_score(

                    requirements,

                    item,

                ),

            )

            return self._selection_result(

                requirements,

                selected,

                source="compatible_connected_device",

                inventory=inventory,

            )

        attempts: list[JsonObject] = []

        if settings["auto_start_avd"]:

            for avd in inventory["candidate_avds"]:

                attempt = {

                    "avd_name": avd["avd_name"],

                    "status": "starting",

                    "static_assessment": avd,

                }

                attempts.append(attempt)

                try:

                    device = self._start_and_verify_avd(

                        avd["avd_name"],

                        requirements,

                        timeout_seconds=settings[

                            "boot_timeout_seconds"

                        ],

                    )

                    attempt["status"] = "compatible"

                    attempt["device"] = device

                    return self._selection_result(

                        requirements,

                        device,

                        source="started_existing_avd",

                        inventory=inventory,

                        avd_name=avd["avd_name"],

                        attempts=attempts,

                    )

                except DeviceSelectionError as error:

                    attempt["status"] = "rejected"

                    attempt["error"] = str(error)

                    attempt["details"] = error.details

        if settings["allow_provision"]:

            profile = self._provision_profile(requirements)

            try:

                provisioned = self._provision_avd(profile)

            except DeviceSelectionError as error:

                raise DeviceSelectionError(

                    "No existing AVD passed verification and automatic "

                    "provisioning failed.",

                    details={

                        **inventory,

                        "attempts": attempts,

                        "provisioning_profile": profile,

                        "provisioning_error": {

                            "message": str(error),

                            "details": error.details,

                        },

                    },

                ) from error

            attempts.append(

                {

                    "avd_name": provisioned["avd_name"],

                    "status": "provisioned",

                    "profile": provisioned,

                }

            )

            device = self._start_and_verify_avd(

                provisioned["avd_name"],

                requirements,

                timeout_seconds=settings["boot_timeout_seconds"],

            )

            attempts[-1]["status"] = "compatible"

            attempts[-1]["device"] = device

            return self._selection_result(

                requirements,

                device,

                source="provisioned_compatible_avd",

                inventory=inventory,

                avd_name=provisioned["avd_name"],

                attempts=attempts,

            )

        raise DeviceSelectionError(

            "No compatible Android device is available for the APK.",

            details={

                **inventory,

                "attempts": attempts,

                "provisioning_allowed": settings["allow_provision"],

                "resolution": (

                    "Connect a compatible physical device, configure a "

                    "compatible AVD, provide another APK ABI variant, or "

                    "enable automatic provisioning."

                ),

            },

        )

    def _avd_inventory(

        self,

        runtime: JsonObject,

        requirements: JsonObject,

    ) -> list[JsonObject]:

        names = runtime.get("android", {}).get("avds", [])

        running_avds = {

            str(device.get("avd_name"))

            for device in runtime.get("devices", [])

            if device.get("avd_name")

        }

        results = [

            self._inspect_avd(str(name), requirements)

            for name in names

        ]

        sdk_root_value = runtime.get("android", {}).get("sdk_root")

        sdk_root = (

            Path(str(sdk_root_value))

            if sdk_root_value

            else None

        )

        for result in results:

            image = str(result.get("system_image") or "")

            image_path = (

                sdk_root

                / Path(

                    image.replace("\\", os.sep).replace("/", os.sep)

                )

                if sdk_root is not None and image

                else None

            )

            available = (

                image_path.is_dir()

                if (

                    image_path is not None

                    and sdk_root is not None

                    and sdk_root.is_dir()

                )

                else None

            )

            result["system_image_path"] = (

                str(image_path)

                if image_path is not None

                else None

            )

            result["system_image_available"] = available

            result["already_running"] = (

                result["avd_name"] in running_avds

            )

            if result["already_running"]:

                result["potentially_compatible"] = False

            if available is False:

                result["potentially_compatible"] = False

                result["rejection_reason"] = (

                    "configured AVD system image is missing"

                )

        return results

    def _inspect_avd(

        self,

        avd_name: str,

        requirements: JsonObject,

    ) -> JsonObject:

        config = self._avd_config(avd_name)

        abi = config.get("abi.type")

        image = config.get("image.sysdir.1", "")

        api_match = re.search(r"android-(\d+)", image)

        api_level = int(api_match.group(1)) if api_match else None

        required_abis = set(requirements.get("native_abis") or [])

        minimum_api = _integer(requirements.get("minimum_api_level"))

        exact = bool(

            not required_abis

            or (

                isinstance(abi, str)

                and abi in required_abis

            )

        )

        translation_candidate = self._translation_candidate(

            required_abis,

            abi,

            image,

        )

        api_compatible = (

            minimum_api is None

            or (

                api_level is not None

                and api_level >= minimum_api

            )

        )

        is_16kb_image = "ps16k" in image.casefold()

        page_size_compatible = not (

            is_16kb_image

            and requirements.get(

                "supports_16kb_page_size"

            ) is False

        )

        return {

            "avd_name": avd_name,

            "configured_abi": abi,

            "api_level": api_level,

            "system_image": image,

            "exact_abi_candidate": exact,

            "native_translation_candidate": translation_candidate,

            "api_compatible": api_compatible,

            "page_size": (

                "16kb"

                if is_16kb_image

                else "4kb_or_unknown"

            ),

            "page_size_compatible": page_size_compatible,

            "potentially_compatible": (

                api_compatible

                and page_size_compatible

                and (exact or translation_candidate)

            ),

            "requires_boot_verification": True,

        }

    @staticmethod
    def _translation_candidate(

        required_abis: set[str],

        avd_abi: Any,

        system_image: str,

    ) -> bool:

        if not required_abis:

            return False

        is_google_image = "google_apis" in system_image

        if not is_google_image:

            return False

        return (

            avd_abi == "x86"

            and bool(

                required_abis.intersection(

                    {"armeabi", "armeabi-v7a"}

                )

            )

        ) or (

            avd_abi == "x86_64"

            and "arm64-v8a" in required_abis

        )

    def _start_and_verify_avd(

        self,

        avd_name: str,

        requirements: JsonObject,

        *,

        timeout_seconds: int,

    ) -> JsonObject:

        runtime = self.runtime_manager.status()

        emulator = self._tool(runtime, "emulator")

        adb = self._tool(runtime, "adb")

        before = {

            str(item.get("serial"))
            for item in runtime.get("devices", [])
            if item.get("serial")

        }

        port = self._available_emulator_port(before)

        command = [

            emulator,

            "-avd",

            avd_name,

            "-port",

            str(port),

            "-netdelay",

            "none",

            "-netspeed",

            "full",

        ]

        stdout_path, stderr_path = self._emulator_log_paths(

            avd_name

        )

        stdout_stream = stdout_path.open("w", encoding="utf-8")

        stderr_stream = stderr_path.open("w", encoding="utf-8")

        try:

            process = self.process_launcher(

                command,

                cwd=str(self.project_root),

                stdout=stdout_stream,

                stderr=stderr_stream,

                creationflags=(

                    getattr(

                        subprocess,

                        "CREATE_NEW_PROCESS_GROUP",

                        0,

                    )

                    if os.name == "nt"

                    else 0

                ),

            )

        except OSError as error:

            stdout_stream.close()

            stderr_stream.close()

            raise DeviceSelectionError(

                f"Could not start AVD '{avd_name}': {error}",

                details={"command": command},

            ) from error

        finally:

            stdout_stream.close()

            stderr_stream.close()

        self._record_emulator_process(avd_name, process.pid)

        serial = f"emulator-{port}"

        deadline = time.monotonic() + timeout_seconds

        last_probe: JsonObject = {

            "device_id": serial,

            "state": "starting",

        }

        while time.monotonic() < deadline:

            if process.poll() is not None:

                raise DeviceSelectionError(

                    f"AVD '{avd_name}' exited before boot completed.",

                    details={

                        "avd_name": avd_name,

                        "exit_code": process.returncode,

                        "command": command,

                        "stdout_log": str(stdout_path),

                        "stderr_log": str(stderr_path),

                        "stdout": self._log_tail(stdout_path),

                        "stderr": self._log_tail(stderr_path),

                    },

                )

            probe = self._probe_device(adb, serial)

            if probe is not None:

                last_probe = evaluate_device(

                    requirements,

                    probe,

                )

                if last_probe["compatible"]:

                    return last_probe

                if (

                    last_probe["boot_completed"]

                    and last_probe["state"] == "device"

                ):

                    self._stop_emulator(adb, serial)

                    raise DeviceSelectionError(

                        (

                            f"AVD '{avd_name}' booted but is "

                            "incompatible with the APK."

                        ),

                        details={

                            "avd_name": avd_name,

                            "device": last_probe,

                        },

                    )

            self.sleeper(3)

        self._stop_emulator(adb, serial)

        raise DeviceSelectionError(

            f"Timed out waiting for AVD '{avd_name}' to boot.",

            details={

                "avd_name": avd_name,

                "timeout_seconds": timeout_seconds,

                "last_probe": last_probe,

            },

        )

    def _probe_device(

        self,

        adb: str,

        serial: str,

    ) -> JsonObject | None:

        state_result = self._run(

            [adb, "-s", serial, "get-state"],

            timeout=10,

        )

        if (

            state_result.returncode != 0

            or (state_result.stdout or "").strip() != "device"

        ):

            return None

        def prop(name: str) -> str:

            result = self._run(

                [adb, "-s", serial, "shell", "getprop", name],

                timeout=10,

            )

            return (result.stdout or "").strip()

        page_size_result = self._run(

            [adb, "-s", serial, "shell", "getconf", "PAGE_SIZE"],

            timeout=10,

        )

        features_result = self._run(

            [adb, "-s", serial, "shell", "pm", "list", "features"],

            timeout=20,

        )

        return {

            "serial": serial,

            "state": "device",

            "boot_completed": prop("sys.boot_completed") == "1",

            "abi_list": prop("ro.product.cpu.abilist"),

            "api_level": _integer(

                prop("ro.build.version.sdk")

            ),

            "android_version": prop("ro.build.version.release"),

            "model": prop("ro.product.model"),

            "is_emulator": True,

            "avd_name": prop("ro.boot.qemu.avd_name") or None,

            "page_size_bytes": _integer(

                (page_size_result.stdout or "").strip()

            ),

            "features": [

                line.partition(":")[2].strip()

                for line in (

                    features_result.stdout or ""

                ).splitlines()

                if line.startswith("feature:")

            ],

        }

    def _provision_profile(

        self,

        requirements: JsonObject,

    ) -> JsonObject:

        native_abis = set(requirements.get("native_abis") or [])

        minimum_api = (

            _integer(requirements.get("minimum_api_level"))

            or 24

        )

        if native_abis.intersection(

            {"armeabi", "armeabi-v7a", "x86"}

        ):

            api_level = max(30, minimum_api)

            emulator_abi = "x86"

        else:

            api_level = max(35, minimum_api)

            emulator_abi = "x86_64"

        image = (

            f"system-images;android-{api_level};"

            f"google_apis_playstore;{emulator_abi}"

        )

        abi_name = (

            "_".join(sorted(native_abis))

            if native_abis

            else "universal"

        )

        safe_abi = re.sub(r"[^A-Za-z0-9_]+", "_", abi_name)

        return {

            "avd_name": (

                f"Appium_Auto_{safe_abi}_API{api_level}"

            ),

            "api_level": api_level,

            "emulator_abi": emulator_abi,

            "system_image": image,

            "device_profile": "pixel_6",

        }

    def _provision_avd(

        self,

        profile: JsonObject,

    ) -> JsonObject:

        runtime = self.runtime_manager.status()

        sdk_root = runtime.get("android", {}).get("sdk_root")

        if not sdk_root:

            raise DeviceSelectionError(

                "Android SDK is unavailable for AVD provisioning."

            )

        sdk = Path(str(sdk_root))

        sdkmanager = (

            sdk

            / "cmdline-tools"

            / "latest"

            / "bin"

            / ("sdkmanager.bat" if os.name == "nt" else "sdkmanager")

        )

        avdmanager = (

            sdk

            / "cmdline-tools"

            / "latest"

            / "bin"

            / ("avdmanager.bat" if os.name == "nt" else "avdmanager")

        )

        for tool in (sdkmanager, avdmanager):

            if not tool.is_file():

                raise DeviceSelectionError(

                    f"Required Android SDK tool is missing: {tool}"

                )

        install = self._run(

            [

                str(sdkmanager),

                f"--sdk_root={sdk}",

                f"platforms;android-{profile['api_level']}",

                str(profile["system_image"]),

            ],

            timeout=1800,

        )

        if install.returncode != 0:

            raise DeviceSelectionError(

                "Android system-image provisioning failed.",

                details=self._command_details(install),

            )

        runtime = self.runtime_manager.status()

        if profile["avd_name"] not in runtime.get(

            "android",

            {},

        ).get("avds", []):

            create = self._run(

                [

                    str(avdmanager),

                    "create",

                    "avd",

                    "--name",

                    str(profile["avd_name"]),

                    "--package",

                    str(profile["system_image"]),

                    "--device",

                    str(profile["device_profile"]),

                    "--force",

                ],

                timeout=180,

                input_text="no\n",

            )

            if create.returncode != 0:

                raise DeviceSelectionError(

                    "Android AVD creation failed.",

                    details=self._command_details(create),

                )

        return dict(profile)

    def _avd_config(self, avd_name: str) -> dict[str, str]:

        for root in self._avd_roots():

            ini_path = root / f"{avd_name}.ini"

            ini = self._properties(ini_path)

            configured_path = ini.get("path")

            candidates = [

                Path(configured_path) / "config.ini"

                if configured_path

                else None,

                root / f"{avd_name}.avd" / "config.ini",

            ]

            for candidate in candidates:

                if candidate is not None and candidate.is_file():

                    return self._properties(candidate)

        return {}

    @staticmethod
    def _properties(path: Path) -> dict[str, str]:

        if not path.is_file():

            return {}

        values: dict[str, str] = {}

        for line in path.read_text(

            encoding="utf-8",

            errors="replace",

        ).splitlines():

            key, separator, value = line.partition("=")

            if separator:

                values[key.strip()] = value.strip()

        return values

    @staticmethod
    def _avd_roots() -> list[Path]:

        roots = []

        if os.environ.get("ANDROID_AVD_HOME"):

            roots.append(Path(os.environ["ANDROID_AVD_HOME"]))

        if os.environ.get("ANDROID_SDK_HOME"):

            roots.append(

                Path(os.environ["ANDROID_SDK_HOME"])

                / ".android"

                / "avd"

            )

        if os.environ.get("USERPROFILE"):

            roots.append(

                Path(os.environ["USERPROFILE"])

                / ".android"

                / "avd"

            )

        return roots

    @staticmethod
    def _available_emulator_port(

        existing_serials: set[str],

    ) -> int:

        used = {

            int(match.group(1))

            for serial in existing_serials

            if (

                match := re.fullmatch(

                    r"emulator-(\d+)",

                    serial,

                )

            )

        }

        return next(

            port

            for port in range(5554, 5682, 2)

            if port not in used

        )

    def _record_emulator_process(

        self,

        avd_name: str,

        pid: int,

    ) -> None:

        root = (

            self.project_root

            / ".runtime"

            / "device-selection"

        )

        root.mkdir(parents=True, exist_ok=True)

        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", avd_name)

        (root / f"{safe_name}.pid").write_text(

            str(pid),

            encoding="utf-8",

        )

    def _emulator_log_paths(

        self,

        avd_name: str,

    ) -> tuple[Path, Path]:

        root = (

            self.project_root

            / ".runtime"

            / "device-selection"

        )

        root.mkdir(parents=True, exist_ok=True)

        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", avd_name)

        return (

            root / f"{safe_name}.stdout.log",

            root / f"{safe_name}.stderr.log",

        )

    @staticmethod
    def _log_tail(path: Path) -> str:

        try:

            return path.read_text(

                encoding="utf-8",

                errors="replace",

            )[-4000:]

        except OSError:

            return ""

    def _run(

        self,

        command: list[str],

        *,

        timeout: int,

        input_text: str | None = None,

    ) -> subprocess.CompletedProcess[str]:

        try:

            return self.runner(

                command,

                input=input_text,

                capture_output=True,

                text=True,

                encoding="utf-8",

                errors="replace",

                timeout=timeout,

                check=False,

            )

        except (OSError, subprocess.TimeoutExpired) as error:

            raise DeviceSelectionError(

                f"Android device command failed: {error}",

                details={"command": command},

            ) from error

    def _stop_emulator(self, adb: str, serial: str) -> None:

        try:

            self._run(

                [adb, "-s", serial, "emu", "kill"],

                timeout=20,

            )

        except DeviceSelectionError:

            pass

    @staticmethod
    def _tool(runtime: JsonObject, name: str) -> str:

        tool = (

            runtime.get("android", {})

            .get("tools", {})

            .get(name, {})

        )

        if not tool.get("available") or not tool.get("path"):

            raise DeviceSelectionError(

                f"Android SDK tool '{name}' is unavailable."

            )

        return str(tool["path"])

    @staticmethod
    def _device_score(

        requirements: JsonObject,

        device: JsonObject,

    ) -> tuple[int, int, int]:

        target = (

            _integer(requirements.get("target_api_level"))

            or _integer(requirements.get("minimum_api_level"))

            or 1

        )

        api = _integer(device.get("api_level")) or 0

        return (

            len(device.get("matching_abis") or []),

            -abs(api - target),

            1 if device.get("is_emulator") else 0,

        )

    @staticmethod
    def _avd_score(

        requirements: JsonObject,

        avd: JsonObject,

    ) -> tuple[int, int, int]:

        target = (

            _integer(requirements.get("target_api_level"))

            or _integer(requirements.get("minimum_api_level"))

            or 1

        )

        api = _integer(avd.get("api_level")) or 0

        standard_pages = (

            1

            if avd.get("page_size") != "16kb"

            else 0

        )

        return (

            1 if avd.get("exact_abi_candidate") else 0,

            -abs(api - target),

            standard_pages,

        )

    @staticmethod
    def _selection_result(

        requirements: JsonObject,

        selected: JsonObject,

        *,

        source: str,

        inventory: JsonObject,

        avd_name: str | None = None,

        attempts: list[JsonObject] | None = None,

    ) -> JsonObject:

        return {

            "contract": "appium.device_selection_result",

            "schema_version": 1,

            "status": "ready",

            "requirements": requirements,

            "device_id": selected["device_id"],

            "selected_device": selected,

            "selection_source": source,

            "avd_name": avd_name,

            "verification": {

                "boot_completed": selected["boot_completed"],

                "api_compatible": selected["api_compatible"],

                "matching_abis": selected["matching_abis"],

                "verified": selected["compatible"],

            },

            "attempts": list(attempts or []),

            "inventory": inventory,

        }

    @staticmethod
    def _options(value: JsonObject | None) -> JsonObject:

        options = dict(value or {})

        allowed = {

            "auto_start_avd",

            "allow_provision",

            "boot_timeout_seconds",

        }

        unexpected = sorted(set(options) - allowed)

        if unexpected:

            raise DeviceSelectionError(

                (

                    "Unsupported device-selection options: "

                    f"{', '.join(unexpected)}."

                )

            )

        auto_start = options.get("auto_start_avd", True)

        allow_provision = options.get("allow_provision", False)

        timeout = options.get("boot_timeout_seconds", 300)

        if not isinstance(auto_start, bool):

            raise DeviceSelectionError(

                "'auto_start_avd' must be a boolean."

            )

        if not isinstance(allow_provision, bool):

            raise DeviceSelectionError(

                "'allow_provision' must be a boolean."

            )

        if (

            not isinstance(timeout, int)

            or isinstance(timeout, bool)

            or not 30 <= timeout <= 900

        ):

            raise DeviceSelectionError(

                (

                    "'boot_timeout_seconds' must be an integer "

                    "between 30 and 900."

                )

            )

        return {

            "auto_start_avd": auto_start,

            "allow_provision": allow_provision,

            "boot_timeout_seconds": timeout,

        }

    @staticmethod
    def _command_details(

        completed: subprocess.CompletedProcess[str],

    ) -> JsonObject:

        return {

            "exit_code": completed.returncode,

            "stdout": (completed.stdout or "").strip()[-4000:],

            "stderr": (completed.stderr or "").strip()[-4000:],

            "command": list(completed.args),

        }


def _integer(value: Any) -> int | None:

    try:

        result = int(value)

    except (TypeError, ValueError):

        return None

    return result if result >= 0 else None

