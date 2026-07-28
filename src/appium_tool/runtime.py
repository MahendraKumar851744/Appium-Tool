from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import requests

from appium_tool.types import JsonObject


PROFILE = {
    "name": "appium-arm32-api30",
    "android_api_level": 30,
    "build_tools_version": "35.0.0",
    "avd_name": "Appium_Arm32_API30",
    "required_abi": "armeabi-v7a",
    "appium_version": "3.5.2",
    "uiautomator2_version": "8.1.1",
}
TERMINAL_JOB_STATUSES = {"succeeded", "failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeErrorBase(RuntimeError):
    pass


class RuntimeValidationError(RuntimeErrorBase):
    pass


class RuntimeJobNotFoundError(RuntimeErrorBase):
    pass


class RuntimeConflictError(RuntimeErrorBase):
    pass


class RuntimeManager:
    """Administrative boundary for the pinned local Android/Appium runtime."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        command_runner: Callable[
            [list[str], Path, Callable[[str], None], int], int
        ]
        | None = None,
        status_provider: Callable[[], JsonObject] | None = None,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.runtime_root = self.project_root / ".runtime"
        self.jobs_root = self.runtime_root / "jobs"
        self.command_runner = command_runner or self._run_command
        self.status_provider = status_provider
        self._jobs: dict[str, JsonObject] = {}
        self._lock = threading.Lock()
        self._load_jobs()

    def status(self) -> JsonObject:
        if self.status_provider is not None:
            return self.status_provider()

        sdk_root = self._sdk_root()
        tools = {
            "adb": self._file_status(
                sdk_root / "platform-tools" / "adb.exe" if sdk_root else None
            ),
            "emulator": self._file_status(
                sdk_root / "emulator" / "emulator.exe" if sdk_root else None
            ),
            "sdkmanager": self._file_status(
                sdk_root
                / "cmdline-tools"
                / "latest"
                / "bin"
                / "sdkmanager.bat"
                if sdk_root
                else None
            ),
        }
        commands = {
            name: self._command_status(executable, version_args)
            for name, executable, version_args in (
                ("python", "python.exe", ["--version"]),
                ("node", "node.exe", ["--version"]),
                ("npm", "npm.cmd", ["--version"]),
                ("java", "java.exe", ["-version"]),
                ("git", "git.exe", ["--version"]),
            )
        }
        android = self._android_status(sdk_root, tools)
        appium = self._appium_status()
        devices = self._devices(tools["adb"].get("path"))
        project = {
            "python_environment": self._file_status(
                self.runtime_root / "python" / "Scripts" / "python.exe"
            ),
            "setup_script": self._file_status(
                self.project_root / "scripts" / "setup.ps1"
            ),
            "bootstrap_script": self._file_status(
                self.project_root / "scripts" / "bootstrap-windows.ps1"
            ),
        }
        prerequisites_ready = all(item["available"] for item in commands.values())
        device_ready = self._has_ready_device(devices)
        provisioned = (
            prerequisites_ready
            and android["ready"]
            and appium["installed"]
            and appium["driver_installed"]
            and project["python_environment"]["available"]
        )
        return {
            "contract": "appium.runtime_status",
            "schema_version": 1,
            "checked_at": utc_now(),
            "profile": PROFILE,
            "platform": {
                "supported": os.name == "nt",
                "name": os.name,
            },
            "prerequisites": commands,
            "android": android,
            "appium": appium,
            "devices": devices,
            "project": project,
            "managed_processes": {
                "appium": self._managed_process("appium.pid", {"node", "node.exe"}),
                "emulator": self._managed_process(
                    "emulator.pid", {"emulator", "emulator.exe"}
                ),
            },
            "provisioned": provisioned,
            "ready": provisioned and appium["server_ready"] and device_ready,
        }

    def provision(self, payload: JsonObject) -> tuple[JsonObject, bool]:
        options = self._provision_options(payload)
        with self._lock:
            active = self._active_job("provision")
            if active is not None:
                return dict(active), True
            if self._active_job("start") is not None:
                raise RuntimeConflictError(
                    "Runtime startup is still active."
                )
            job = self._new_job("provision", options)
            self._start_job(job, lambda: self._provision_task(job, options))
            return dict(job), False

    def start(self, payload: JsonObject) -> tuple[JsonObject, bool]:
        options = self._start_options(payload)
        with self._lock:
            active = self._active_job("start")
            if active is not None:
                if active.get("request") != options:
                    raise RuntimeConflictError(
                        "Runtime startup is already active with different options."
                    )
                return dict(active), True
            if self._active_job("provision") is not None:
                raise RuntimeConflictError(
                    "Runtime provisioning is still active."
                )
            job = self._new_job("start", options)
            self._start_job(job, lambda: self._start_task(job, options))
            return dict(job), False

    def stop(self, payload: JsonObject) -> JsonObject:
        allowed = {"stop_appium", "stop_emulator"}
        if not isinstance(payload, dict):
            raise RuntimeValidationError("Request body must be a JSON object.")
        unexpected = sorted(set(payload) - allowed)
        if unexpected:
            raise RuntimeValidationError(
                f"Unsupported runtime stop fields: {', '.join(unexpected)}."
            )
        options = {
            "stop_appium": payload.get("stop_appium", True),
            "stop_emulator": payload.get("stop_emulator", True),
        }
        for name, value in options.items():
            if not isinstance(value, bool):
                raise RuntimeValidationError(f"'{name}' must be a boolean.")
        with self._lock:
            if self._active_job("provision") is not None:
                raise RuntimeConflictError(
                    "Runtime provisioning is still active."
                )
            if self._active_job("start") is not None:
                raise RuntimeConflictError(
                    "Runtime startup is still active."
                )

        stopped = []
        skipped = []
        for name, enabled, expected_names in (
            ("appium", options["stop_appium"], {"node", "node.exe"}),
            (
                "emulator",
                options["stop_emulator"],
                {"emulator", "emulator.exe"},
            ),
        ):
            if not enabled:
                skipped.append({"process": name, "reason": "disabled"})
                continue
            outcome = self._stop_managed_process(
                name,
                f"{name}.pid",
                expected_names,
            )
            (stopped if outcome["stopped"] else skipped).append(outcome)
        return {
            "contract": "appium.runtime_stop_result",
            "schema_version": 1,
            "status": "completed",
            "stopped": stopped,
            "skipped": skipped,
            "checked_at": utc_now(),
        }

    def get_job(self, job_id: str) -> JsonObject:
        if not isinstance(job_id, str) or not job_id.startswith("job_"):
            raise RuntimeJobNotFoundError("Invalid runtime job identifier.")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise RuntimeJobNotFoundError(
                    f"Runtime job '{job_id}' does not exist."
                )
            return dict(job)

    def _provision_task(self, job: JsonObject, options: JsonObject) -> None:
        current = self.status()
        if current.get("provisioned") and not options["force"]:
            commands = [
                self._powershell_script("doctor.ps1"),
            ]
        elif current.get("android", {}).get("ready"):
            commands = [
                self._powershell_script("setup.ps1"),
                self._powershell_script("doctor.ps1"),
            ]
        else:
            if not options["accept_android_licenses"]:
                raise RuntimeValidationError(
                    "Android SDK licenses must be accepted explicitly before "
                    "full provisioning."
                )
            bootstrap = self._powershell_script("bootstrap-windows.ps1")
            if options["install_missing_prerequisites"]:
                bootstrap.append("-InstallMissingPrerequisites")
            bootstrap.append("-AcceptAndroidLicenses")
            commands = [bootstrap]

        for index, command in enumerate(commands, start=1):
            self._update_job(
                job,
                step=f"command_{index}_of_{len(commands)}",
                progress=int(((index - 1) / len(commands)) * 100),
            )
            script_name = Path(command[-1]).name.casefold()
            timeout_seconds = (
                3600
                if script_name == "bootstrap-windows.ps1"
                else 1800
                if script_name == "setup.ps1"
                else 180
            )
            exit_code = self.command_runner(
                command,
                self.project_root,
                lambda line: self._job_output(job, line),
                timeout_seconds,
            )
            if exit_code != 0:
                raise RuntimeError(
                    f"Provisioning command failed with exit code {exit_code}."
                )
        final_status = self.status()
        if not final_status.get("provisioned"):
            raise RuntimeError(
                "Provisioning commands completed but the runtime is not ready."
            )
        self._succeed_job(job, result={"runtime": final_status})

    def _start_task(
        self,
        job: JsonObject,
        options: JsonObject,
    ) -> None:
        current = self.status()
        if not current.get("provisioned"):
            raise RuntimeError(
                "Runtime is not provisioned. Complete provisioning first."
            )
        commands = []

        if options["start_emulator"]:
            commands.append(
                (
                    "start_emulator",
                    20,
                    self._powershell_script("start-emulator.ps1"),
                    240,
                )
            )

        commands.append(
            (
                "start_appium",
                70,
                self._powershell_script("start-appium-background.ps1"),
                150,
            )
        )

        for step, progress, command, timeout_seconds in commands:
            self._update_job(
                job,
                step=step,
                progress=progress,
            )
            exit_code = self.command_runner(
                command,
                self.project_root,
                lambda line: self._job_output(job, line),
                timeout_seconds,
            )
            if exit_code != 0:
                raise RuntimeError(
                    f"Runtime start command failed with exit code {exit_code}."
                )
        final_status = self.status()
        if not self._startup_ready(final_status, options):
            raise RuntimeError(
                "Start commands completed but the runtime did not become ready."
            )
        self._succeed_job(job, result={"runtime": final_status})

    def _start_job(
        self,
        job: JsonObject,
        target: Callable[[], None],
    ) -> None:
        def run() -> None:
            self._update_job(
                job,
                status="running",
                started_at=utc_now(),
                step="starting",
                progress=1,
            )
            try:
                target()
            except Exception as error:
                self._update_job(
                    job,
                    status="failed",
                    completed_at=utc_now(),
                    step="failed",
                    error=str(error),
                )

        thread = threading.Thread(
            target=run,
            name=f"runtime-{job['job_id']}",
            daemon=True,
        )
        thread.start()

    def _new_job(self, operation: str, request: JsonObject) -> JsonObject:
        job_id = f"job_{uuid4().hex}"
        job = {
            "contract": "appium.runtime_job",
            "schema_version": 1,
            "job_id": job_id,
            "operation": operation,
            "status": "queued",
            "step": "queued",
            "progress": 0,
            "created_at": utc_now(),
            "started_at": None,
            "completed_at": None,
            "request": request,
            "output": [],
            "result": None,
            "error": None,
            "status_endpoint": f"/api/v1/admin/jobs/{job_id}",
        }
        self._jobs[job_id] = job
        self._persist_job(job)
        return job

    def _succeed_job(self, job: JsonObject, *, result: JsonObject) -> None:
        self._update_job(
            job,
            status="succeeded",
            completed_at=utc_now(),
            step="completed",
            progress=100,
            result=result,
        )

    def _update_job(self, job: JsonObject, **changes: Any) -> None:
        with self._lock:
            job.update(changes)
            self._persist_job(job)

    def _job_output(self, job: JsonObject, line: str) -> None:
        cleaned = line.strip()
        if not cleaned:
            return
        with self._lock:
            output = list(job.get("output") or [])
            output.append(cleaned)
            job["output"] = output[-200:]
            self._persist_job(job)

    def _active_job(self, operation: str) -> JsonObject | None:
        return next(
            (
                job
                for job in self._jobs.values()
                if job["operation"] == operation
                and job["status"] not in TERMINAL_JOB_STATUSES
            ),
            None,
        )

    def _persist_job(self, job: JsonObject) -> None:
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        path = self.jobs_root / f"{job['job_id']}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(job, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _load_jobs(self) -> None:
        if not self.jobs_root.is_dir():
            return
        for path in self.jobs_root.glob("job_*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") not in TERMINAL_JOB_STATUSES:
                job.update(
                    {
                        "status": "failed",
                        "step": "interrupted",
                        "completed_at": utc_now(),
                        "error": "Backend restarted before the job completed.",
                    }
                )
                self._persist_job(job)
            self._jobs[str(job["job_id"])] = job

    def _powershell_script(self, name: str) -> list[str]:
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
        if not executable:
            raise RuntimeError("PowerShell is required for runtime management.")
        path = (self.project_root / "scripts" / name).resolve()
        scripts_root = (self.project_root / "scripts").resolve()
        if path.parent != scripts_root or not path.is_file():
            raise RuntimeError(f"Managed runtime script is missing: {name}")
        return [
            executable,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
        ]

    @staticmethod
    def _run_command(
        command: list[str],
        cwd: Path,
        output: Callable[[str], None],
        timeout_seconds: int,
    ) -> int:
        creationflags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0
        )
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        assert process.stdout is not None

        def read_output() -> None:
            try:
                for line in process.stdout:
                    output(line)
            except (OSError, ValueError):
                return

        reader = threading.Thread(
            target=read_output,
            name=f"runtime-command-output-{process.pid}",
            daemon=True,
        )
        reader.start()
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            output(
                f"Managed command exceeded its {timeout_seconds}-second timeout."
            )
            if os.name == "nt":
                try:
                    subprocess.run(
                        [
                            "taskkill.exe",
                            "/PID",
                            str(process.pid),
                            "/T",
                            "/F",
                        ],
                        capture_output=True,
                        timeout=2,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    output("Timed out while terminating the managed process tree.")
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                output("Managed process did not acknowledge termination promptly.")
            exit_code = 124
        reader.join(timeout=0.5)
        return exit_code

    def _android_status(
        self,
        sdk_root: Path | None,
        tools: JsonObject,
    ) -> JsonObject:
        platform = (
            sdk_root / "platforms" / f"android-{PROFILE['android_api_level']}"
            if sdk_root
            else None
        )
        build_tools = (
            sdk_root / "build-tools" / PROFILE["build_tools_version"]
            if sdk_root
            else None
        )
        system_image = (
            sdk_root
            / "system-images"
            / f"android-{PROFILE['android_api_level']}"
            / "google_apis_playstore"
            / "x86"
            if sdk_root
            else None
        )
        avds = self._avds(tools["emulator"].get("path"))
        components = {
            "platform": self._file_status(platform, directory=True),
            "build_tools": self._file_status(build_tools, directory=True),
            "system_image": self._file_status(system_image, directory=True),
        }
        ready = (
            sdk_root is not None
            and all(item["available"] for item in tools.values())
            and all(item["available"] for item in components.values())
            and PROFILE["avd_name"] in avds
        )
        return {
            "sdk_root": str(sdk_root) if sdk_root else None,
            "available": sdk_root is not None,
            "tools": tools,
            "components": components,
            "avds": avds,
            "required_avd": PROFILE["avd_name"],
            "ready": ready,
        }

    def _appium_status(self) -> JsonObject:
        package = self.project_root / "tooling" / "appium" / "package.json"
        appium_entry = (
            self.project_root
            / "tooling"
            / "appium"
            / "node_modules"
            / "appium"
            / "build"
            / "lib"
            / "main.js"
        )
        driver_package = (
            self.project_root
            / "tooling"
            / "appium"
            / "node_modules"
            / "appium-uiautomator2-driver"
            / "package.json"
        )
        versions: JsonObject = {}
        if package.is_file():
            try:
                dependencies = json.loads(
                    package.read_text(encoding="utf-8")
                ).get("devDependencies", {})
                versions = {
                    "configured_appium": dependencies.get("appium"),
                    "configured_uiautomator2": dependencies.get(
                        "appium-uiautomator2-driver"
                    ),
                }
            except (OSError, json.JSONDecodeError):
                pass
        server_ready = False
        server_error = None
        try:
            response = requests.get("http://127.0.0.1:4723/status", timeout=2)
            payload = response.json()
            server_ready = bool(payload.get("value", {}).get("ready"))
        except Exception as error:
            server_error = str(error)
        return {
            "installed": appium_entry.is_file(),
            "entry_point": str(appium_entry),
            "driver_installed": driver_package.is_file(),
            "driver_package": str(driver_package),
            "server_url": "http://127.0.0.1:4723",
            "server_ready": server_ready,
            "server_error": server_error,
            **versions,
        }

    def _devices(self, adb_path: Any) -> list[JsonObject]:
        if not adb_path:
            return []
        result = self._capture([str(adb_path), "devices"])
        devices = []
        for line in result.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            boot_completed = False
            abi_list = None
            api_level = None
            android_version = None
            model = None
            page_size_bytes = None
            avd_name = None
            features: list[str] = []
            is_emulator = serial.startswith("emulator-")
            if state == "device":
                boot_completed = (
                    self._capture(
                        [
                            str(adb_path),
                            "-s",
                            serial,
                            "shell",
                            "getprop",
                            "sys.boot_completed",
                        ]
                    ).strip()
                    == "1"
                )
                abi_list = self._capture(
                    [
                        str(adb_path),
                        "-s",
                        serial,
                        "shell",
                        "getprop",
                        "ro.product.cpu.abilist",
                    ]
                ).strip()
                api_level = self._integer(
                    self._capture(
                        [
                            str(adb_path),
                            "-s",
                            serial,
                            "shell",
                            "getprop",
                            "ro.build.version.sdk",
                        ]
                    ).strip()
                )
                android_version = self._capture(
                    [
                        str(adb_path),
                        "-s",
                        serial,
                        "shell",
                        "getprop",
                        "ro.build.version.release",
                    ]
                ).strip()
                model = self._capture(
                    [
                        str(adb_path),
                        "-s",
                        serial,
                        "shell",
                        "getprop",
                        "ro.product.model",
                    ]
                ).strip()
                page_size_bytes = self._integer(
                    self._capture(
                        [
                            str(adb_path),
                            "-s",
                            serial,
                            "shell",
                            "getconf",
                            "PAGE_SIZE",
                        ]
                    ).strip()
                )
                avd_name = self._capture(
                    [
                        str(adb_path),
                        "-s",
                        serial,
                        "shell",
                        "getprop",
                        "ro.boot.qemu.avd_name",
                    ]
                ).strip() or None
                features = [
                    line.partition(":")[2].strip()
                    for line in self._capture(
                        [
                            str(adb_path),
                            "-s",
                            serial,
                            "shell",
                            "pm",
                            "list",
                            "features",
                        ]
                    ).splitlines()
                    if line.startswith("feature:")
                ]
            devices.append(
                {
                    "serial": serial,
                    "state": state,
                    "boot_completed": boot_completed,
                    "abi_list": abi_list,
                    "api_level": api_level,
                    "android_version": android_version,
                    "model": model,
                    "is_emulator": is_emulator,
                    "page_size_bytes": page_size_bytes,
                    "avd_name": avd_name,
                    "features": features,
                    "compatible": bool(
                        abi_list
                        and PROFILE["required_abi"] in abi_list.split(",")
                    ),
                }
            )
        return devices

    @staticmethod
    def _integer(value: Any) -> int | None:

        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _avds(self, emulator_path: Any) -> list[str]:
        if not emulator_path:
            return []
        output = self._capture([str(emulator_path), "-list-avds"])
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _sdk_root(self) -> Path | None:
        candidates = [
            os.environ.get("ANDROID_SDK_ROOT"),
            os.environ.get("ANDROID_HOME"),
            (
                str(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
                if os.environ.get("LOCALAPPDATA")
                else None
            ),
            (
                str(
                    Path(os.environ["USERPROFILE"])
                    / "AppData"
                    / "Local"
                    / "Android"
                    / "Sdk"
                )
                if os.environ.get("USERPROFILE")
                else None
            ),
        ]
        for value in candidates:
            if value:
                path = Path(value).expanduser()
                if path.is_dir():
                    return path.resolve()
        return None

    @staticmethod
    def _command_status(executable: str, arguments: list[str]) -> JsonObject:
        path = shutil.which(executable)
        if not path:
            return {"available": False, "path": None, "version": None}
        version = RuntimeManager._capture([path, *arguments]).strip().splitlines()
        return {
            "available": True,
            "path": path,
            "version": version[0] if version else None,
        }

    @staticmethod
    def _file_status(
        path: Path | None,
        *,
        directory: bool = False,
    ) -> JsonObject:
        available = bool(
            path and (path.is_dir() if directory else path.is_file())
        )
        return {"available": available, "path": str(path) if path else None}

    @staticmethod
    def _capture(command: list[str]) -> str:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            return (completed.stdout or "") + (completed.stderr or "")
        except (OSError, subprocess.TimeoutExpired):
            return ""

    @staticmethod
    def _has_ready_device(devices: list[JsonObject]) -> bool:
        return any(
            item.get("state") == "device"
            and item.get("boot_completed")
            and item.get("compatible")
            for item in devices
        )

    @staticmethod
    def _startup_ready(
        status: JsonObject,
        options: JsonObject,
    ) -> bool:
        if options["start_emulator"]:
            return bool(status.get("ready"))

        if not (
            status.get("provisioned")
            and status.get("appium", {}).get("server_ready")
        ):
            return False

        device_id = options.get("device_id")

        if not device_id:
            return True

        return any(
            item.get("serial") == device_id
            and item.get("state") == "device"
            and item.get("boot_completed")
            for item in status.get("devices", [])
        )

    def _managed_process(
        self,
        pid_name: str,
        expected_names: set[str],
    ) -> JsonObject:
        pid_path = self.runtime_root / pid_name
        pid = self._read_pid(pid_path)
        if pid is None:
            return {"managed": False, "running": False, "pid": None}
        process_name = self._process_name(pid)
        return {
            "managed": True,
            "running": process_name is not None,
            "pid": pid,
            "process_name": process_name,
            "identity_valid": bool(
                process_name and process_name.casefold() in expected_names
            ),
        }

    def _stop_managed_process(
        self,
        name: str,
        pid_name: str,
        expected_names: set[str],
    ) -> JsonObject:
        pid_path = self.runtime_root / pid_name
        pid = self._read_pid(pid_path)
        if pid is None:
            return {
                "process": name,
                "stopped": False,
                "reason": "no_managed_pid",
            }
        process_name = self._process_name(pid)
        if process_name is None:
            pid_path.unlink(missing_ok=True)
            return {
                "process": name,
                "pid": pid,
                "stopped": False,
                "reason": "not_running",
            }
        if process_name.casefold() not in expected_names:
            raise RuntimeConflictError(
                f"Refusing to stop PID {pid}: expected managed {name}, "
                f"found '{process_name}'."
            )
        completed = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if completed.returncode != 0 and self._process_name(pid) is not None:
            return {
                "process": name,
                "pid": pid,
                "process_name": process_name,
                "stopped": False,
                "reason": "termination_failed",
                "error": (completed.stderr or completed.stdout or "").strip(),
            }
        pid_path.unlink(missing_ok=True)
        return {
            "process": name,
            "pid": pid,
            "process_name": process_name,
            "stopped": True,
        }

    @staticmethod
    def _process_name(pid: int) -> str | None:
        if os.name != "nt":
            return None
        output = RuntimeManager._capture(
            [
                "tasklist.exe",
                "/FI",
                f"PID eq {pid}",
                "/FO",
                "CSV",
                "/NH",
            ]
        ).strip()
        if not output or output.startswith("INFO:"):
            return None
        try:
            return next(iter(csv.reader([output])))[0]
        except (csv.Error, StopIteration, IndexError):
            return None

    @staticmethod
    def _read_pid(path: Path) -> int | None:
        try:
            value = int(path.read_text(encoding="utf-8").strip())
            return value if value > 0 else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _provision_options(payload: JsonObject) -> JsonObject:
        allowed = {
            "install_missing_prerequisites",
            "accept_android_licenses",
            "force",
        }
        if not isinstance(payload, dict):
            raise RuntimeValidationError("Request body must be a JSON object.")
        unexpected = sorted(set(payload) - allowed)
        if unexpected:
            raise RuntimeValidationError(
                f"Unsupported provisioning fields: {', '.join(unexpected)}."
            )
        options = {
            "install_missing_prerequisites": payload.get(
                "install_missing_prerequisites", False
            ),
            "accept_android_licenses": payload.get(
                "accept_android_licenses", False
            ),
            "force": payload.get("force", False),
        }
        for name, value in options.items():
            if not isinstance(value, bool):
                raise RuntimeValidationError(f"'{name}' must be a boolean.")
        return options

    @staticmethod
    def _empty_payload(payload: JsonObject, operation: str) -> None:
        if not isinstance(payload, dict) or payload:
            raise RuntimeValidationError(
                f"{operation} request must be an empty JSON object."
            )

    @staticmethod
    def _start_options(payload: JsonObject) -> JsonObject:
        if not isinstance(payload, dict):
            raise RuntimeValidationError(
                "Runtime start request must be a JSON object."
            )

        allowed = {"start_emulator", "device_id"}
        unexpected = sorted(set(payload) - allowed)

        if unexpected:
            raise RuntimeValidationError(
                "Unsupported runtime start fields: "
                + ", ".join(unexpected)
                + "."
            )

        start_emulator = payload.get("start_emulator", True)

        if not isinstance(start_emulator, bool):
            raise RuntimeValidationError(
                "'start_emulator' must be a boolean."
            )

        device_id = payload.get("device_id")

        if device_id is not None and (
            not isinstance(device_id, str)
            or not device_id.strip()
        ):
            raise RuntimeValidationError(
                "'device_id' must be a non-empty string or null."
            )

        if start_emulator and device_id is not None:
            raise RuntimeValidationError(
                "'device_id' is only supported when 'start_emulator' is false."
            )

        return {
            "start_emulator": start_emulator,
            "device_id": (
                device_id.strip()
                if isinstance(device_id, str)
                else None
            ),
        }

