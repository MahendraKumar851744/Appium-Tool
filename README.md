# Appium Tool

Appium Tool is a standalone, deterministic automation service for Android
applications. It exposes the same Appium capabilities through:

- a canonical REST API;
- generated MCP tools for n8n and other MCP clients;
- one generic action executor used by both interfaces.

The repository intentionally excludes workflow engines and LLM providers.
An external orchestrator owns reasoning and workflow state; this service owns
Android runtime provisioning, APK/device operations, live sessions, screen
observations, action delivery, monitoring, and artifacts.

## Architecture

```text
n8n / MCP client ──┐
                   ├── ToolRegistry ── SafetyPolicy ── SessionManager
REST client ───────┘                                      │
                                                 generic ActionExecutor
                                                          │
                                                       Appium
```

`ToolRegistry` is the source of truth for tool names, descriptions, JSON
schemas, and risk classifications. MCP tools and REST discovery are generated
from it. Adding an action to the registry never duplicates Appium execution
logic.

## Setup

Python 3.10+, Node.js, the Android SDK, and a compatible Android device or AVD
are required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Set two different, long random tokens in the environment:

```powershell
$env:APPIUM_TOOL_SERVICE_TOKEN = "service-token"
$env:APPIUM_TOOL_ADMIN_TOKEN = "admin-token"
appium-tool
```

The service listens on `http://127.0.0.1:8000` by default:

- REST: `http://127.0.0.1:8000/api/v1`
- MCP Streamable HTTP: `http://127.0.0.1:8000/mcp`
- health: `http://127.0.0.1:8000/health`

Every protected request uses:

```http
Authorization: Bearer <token>
```

The service token can open sessions and invoke normal tools. The admin token
is required for runtime/APK administration and system/destructive tools.

## Session lifecycle

Open an already-installed package:

```http
POST /api/v1/sessions
Authorization: Bearer <service-token>
Content-Type: application/json

{
  "package_id": "com.example.app",
  "device_id": "emulator-5554"
}
```

The result contains `session_id`, `screen_id`, screen metadata, and artifact
references. Session IDs replace the older exploration-run terminology at the
service boundary.

```http
GET    /api/v1/sessions/{session_id}
DELETE /api/v1/sessions/{session_id}
POST   /api/v1/sessions/{session_id}/actions
```

Sessions are serialized with one lock per session and expire after 30 minutes
of inactivity by default. Set `APPIUM_TOOL_SESSION_TTL_SECONDS` to change it.
Expiry closes the underlying Appium driver.

## Tools

Discover every generated tool and its schema:

```http
GET /api/v1/tools
```

Invoke a named tool through REST:

```http
POST /api/v1/tools/tap/invoke
Authorization: Bearer <service-token>
Content-Type: application/json

{
  "session_id": "session_...",
  "screen_id": "screen_...",
  "target": {"element_id": "element_..."}
}
```

The generic compatibility endpoint remains available:

```http
POST /api/v1/sessions/{session_id}/actions

{
  "action": "tap",
  "screen_id": "screen_...",
  "target": {"element_id": "element_..."},
  "parameters": {},
  "completion": {}
}
```

## Safety policy

Tools are classified as `read_only`, `safe`, `controlled`, `destructive`, or
`system`.

- Read-only and safe tools require the service or admin token.
- Controlled tools additionally require `"confirm": true`.
- Destructive and system tools require `"confirm": true` and the admin token.
- Runtime provisioning, runtime control, install, and uninstall endpoints
  always require the admin token.
- APK paths must be inside `APPIUM_TOOL_APK_ROOTS`.
- Clean install and uninstall remain explicit operations because they remove
  application data.

## Runtime and APK APIs

```text
GET  /api/v1/runtime
POST /api/v1/runtime/provision
POST /api/v1/runtime/start
POST /api/v1/runtime/stop
GET  /api/v1/runtime/jobs/{job_id}

POST   /api/v1/apps/preflight
POST   /api/v1/apps/prepare-device
POST   /api/v1/apps/install
GET    /api/v1/apps/{package_id}
DELETE /api/v1/apps/{package_id}
```

## Tests

```powershell
pytest
```

Completed and pushed everything finally.
