from __future__ import annotations

import base64
import json
from pathlib import Path

from appium_tool.exploration.llm_context import build_llm_context
from appium_tool.exploration.models import JsonObject


FUTURE_CAPTURE_ADDITIONS = (
    "Visual analysis: colors, typography, spacing, icons, OCR, contrast, and "
    "per-element image crops.",
    "Transient UI: animations, loading states, toasts, snackbars, popups, "
    "bottom sheets, and short time-window observations.",
    "Deeper semantics: input types, hints, password state, accessibility "
    "importance, drawing order, and guaranteed parent/child relationships.",
    "Scrollable content: direction, position, hidden content indicators, and "
    "safe full-page stitched captures.",
    "Framework-specific evidence: WebView DOM, Jetpack Compose semantics, "
    "Flutter/React Native detection, and custom-drawn UI handling.",
    "Action grounding: recommended actions, preferred/fallback locators, "
    "obstruction checks, and safe interaction coordinates.",
    "Environment context: dark mode, display/font scaling, layout direction, "
    "network, battery, time format, permissions, and default-app state.",
    "Evidence quality: collector provenance, authoritative versus inferred "
    "values, confidence, and explicit unavailable reasons for every section.",
)


def write_screen_viewer(path: Path, document: JsonObject) -> None:
    """Write a self-contained visual viewer for one canonical screen document."""
    payload = base64.b64encode(
        json.dumps(document, ensure_ascii=False, default=str).encode("utf-8")
    ).decode("ascii")
    llm_context_payload = base64.b64encode(
        build_llm_context(document).encode("utf-8")
    ).decode("ascii")
    future_items = "\n".join(f"<li>{item}</li>" for item in FUTURE_CAPTURE_ADDITIONS)
    path.write_text(
        _template(payload, llm_context_payload, future_items),
        encoding="utf-8",
    )


def _template(
    payload: str,
    llm_context_payload: str,
    future_items: str,
) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Appium screen content</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #172033;
      --muted: #667085;
      --line: #e3e8ef;
      --soft: #f5f7fa;
      --panel: #ffffff;
      --accent: #635bff;
      --good: #087f5b;
      --warn: #b54708;
      --shadow: 0 10px 30px rgba(19, 33, 68, .08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: #eef2f7;
      color: var(--ink);
      font: 14px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
    }}
    header {{
      color: white;
      background: linear-gradient(125deg, #111827, #273469 60%, #635bff);
      padding: 42px max(24px, calc((100vw - 1440px) / 2));
    }}
    h1 {{ margin: 0 0 8px; font-size: clamp(26px, 4vw, 42px); }}
    h2 {{ margin: 0 0 18px; font-size: 21px; }}
    h3 {{ margin: 0 0 12px; font-size: 15px; }}
    .subtitle {{ opacity: .8; overflow-wrap: anywhere; }}
    main {{ max-width: 1440px; margin: 0 auto; padding: 24px; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: var(--shadow);
      padding: 20px;
      margin-bottom: 20px;
    }}
    .workflow {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
      margin-bottom: 22px;
    }}
    .workflow a {{
      display: flex;
      gap: 12px;
      align-items: center;
      min-height: 72px;
      padding: 12px;
      color: inherit;
      text-decoration: none;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: var(--shadow);
    }}
    .workflow .number {{
      display: grid;
      place-items: center;
      flex: 0 0 34px;
      height: 34px;
      color: white;
      background: var(--accent);
      border-radius: 50%;
      font-weight: 800;
    }}
    .workflow strong, .workflow small {{ display: block; }}
    .workflow small {{ color: var(--muted); }}
    .stage-title {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin: 30px 0 14px;
    }}
    .stage-title span {{
      padding: 5px 10px;
      color: white;
      background: var(--accent);
      border-radius: 999px;
      font-weight: 750;
    }}
    .stage-title h2 {{ margin: 0; font-size: 25px; }}
    .badges, .metrics, .columns, .gallery {{ display: grid; gap: 12px; }}
    .badges {{ display: flex; flex-wrap: wrap; margin-top: 18px; }}
    .badge {{
      border: 1px solid rgba(255,255,255,.25);
      border-radius: 999px;
      padding: 5px 10px;
      background: rgba(255,255,255,.1);
    }}
    .metrics {{ grid-template-columns: repeat(auto-fit, minmax(135px, 1fr)); }}
    .metric {{ background: var(--soft); border-radius: 10px; padding: 14px; }}
    .metric strong {{ display: block; font-size: 22px; overflow-wrap: anywhere; }}
    .metric span, .muted {{ color: var(--muted); }}
    .columns {{ grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); }}
    .gallery {{ grid-template-columns: minmax(260px, .8fr) minmax(320px, 1.2fr); }}
    .screen-shot {{
      display: block;
      width: min(100%, 430px);
      max-height: 760px;
      object-fit: contain;
      margin: auto;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #111;
    }}
    .crop {{
      display: block;
      width: 100%;
      min-height: 48px;
      object-fit: contain;
      border: 1px solid var(--line);
      border-radius: 9px;
      margin: 8px 0 18px;
      background: #eee;
    }}
    dl {{ display: grid; grid-template-columns: minmax(120px, .75fr) 1.5fr; gap: 0; margin: 0; }}
    dt, dd {{ margin: 0; padding: 8px 0; border-bottom: 1px solid var(--line); }}
    dt {{ color: var(--muted); padding-right: 14px; }}
    dd {{ font-weight: 550; overflow-wrap: anywhere; }}
    .table-wrap {{ overflow: auto; max-height: 650px; border: 1px solid var(--line); border-radius: 10px; }}
    table {{ width: 100%; border-collapse: collapse; white-space: nowrap; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ position: sticky; top: 0; background: #eef2f7; z-index: 1; }}
    td.wrap {{ min-width: 180px; max-width: 340px; white-space: normal; overflow-wrap: anywhere; }}
    tbody tr:hover {{ background: #f8f9ff; }}
    input, select {{
      width: min(100%, 360px);
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      padding: 9px 11px;
      margin: 0 8px 12px 0;
      background: white;
    }}
    details {{ border: 1px solid var(--line); border-radius: 10px; margin-top: 10px; }}
    summary {{ cursor: pointer; padding: 12px; font-weight: 650; }}
    pre {{
      margin: 0;
      padding: 16px;
      overflow: auto;
      max-height: 600px;
      background: #101828;
      color: #d7e0ff;
      border-radius: 0 0 9px 9px;
      font: 12px/1.55 ui-monospace, SFMono-Regular, Consolas, monospace;
    }}
    .ok {{ color: var(--good); }}
    .warning {{ color: var(--warn); }}
    .future {{ border-left: 5px solid var(--accent); }}
    .future li {{ margin-bottom: 9px; }}
    .context-toolbar {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 12px;
    }}
    button {{
      cursor: pointer;
      border: 0;
      border-radius: 8px;
      padding: 9px 14px;
      color: white;
      background: var(--accent);
      font-weight: 700;
    }}
    .context-output {{
      max-height: 780px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      border-radius: 10px;
    }}
    .placeholder {{
      padding: 24px;
      color: var(--muted);
      text-align: center;
      border: 2px dashed #cbd5e1;
      border-radius: 10px;
      background: var(--soft);
    }}
    code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
    @media (max-width: 820px) {{
      .gallery {{ grid-template-columns: 1fr; }}
      .workflow {{ grid-template-columns: 1fr; }}
      main {{ padding: 14px; }}
      dl {{ grid-template-columns: 1fr; }}
      dt {{ padding-bottom: 0; border-bottom: 0; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Appium screen content</h1>
    <div class="subtitle" id="identity"></div>
    <div class="badges" id="badges"></div>
  </header>
  <main>
    <nav class="workflow" aria-label="Screen understanding workflow">
      <a href="#step1"><span class="number">1</span><span>
        <strong>Captured evidence</strong><small>Appium and Android ground truth</small>
      </span></a>
      <a href="#step2"><span class="number">2</span><span>
        <strong>LLM context</strong><small>Compact, reasoning-friendly summary</small>
      </span></a>
      <a href="#step3"><span class="number">3</span><span>
        <strong>LLM output</strong><small>Reserved for the next workflow stage</small>
      </span></a>
    </nav>

    <div class="stage-title" id="step1">
      <span>Step 1</span><h2>Captured screen evidence</h2>
    </div>
    <section class="panel">
      <h2>Capture overview</h2>
      <div class="metrics" id="metrics"></div>
    </section>

    <section class="panel">
      <h2>Visual evidence</h2>
      <div class="gallery">
        <div><img id="fullScreenshot" class="screen-shot" alt="Full captured screen"></div>
        <div id="systemCrops"></div>
      </div>
    </section>

    <div class="columns" id="detailSections"></div>

    <section class="panel">
      <h2>Elements</h2>
      <input id="elementSearch" type="search" placeholder="Search text, ID, class, boundsâ€¦">
      <select id="sourceFilter"><option value="">All sources</option></select>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>ID</th><th>Source</th><th>Interaction</th><th>Class</th>
            <th>Text / description</th><th>Resource ID</th><th>State</th>
            <th>Bounds</th><th>XPath</th>
          </tr></thead>
          <tbody id="elementRows"></tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>Evidence and diagnostics</h2>
      <div id="diagnostics"></div>
      <details><summary>Complete canonical JSON</summary><pre id="rawJson"></pre></details>
      <details><summary>Raw UI hierarchy XML</summary><pre id="rawHierarchy"></pre></details>
    </section>

    <div class="stage-title" id="step2">
      <span>Step 2</span><h2>LLM-ready screen context</h2>
    </div>
    <section class="panel">
      <div class="context-toolbar">
        <div>
          <strong>Deterministic context generated from Step 1</strong>
          <div class="muted" id="contextStats"></div>
        </div>
        <button id="copyContext" type="button">Copy context</button>
      </div>
      <pre class="context-output" id="llmContext"></pre>
    </section>

    <div class="stage-title" id="step3">
      <span>Step 3</span><h2>LLM output</h2>
    </div>
    <section class="panel">
      <div class="placeholder">
        <strong>Reserved for the future LLM response.</strong>
        <div>This stage will show the model's screen understanding, observations,
          and traversal decision after Step 2 is connected to the LLM.</div>
      </div>
    </section>

    <section class="panel future">
      <h2>Future capture additions</h2>
      <p class="muted">Static roadmap â€” deliberately not part of the current capture yet.</p>
      <ol>{future_items}</ol>
    </section>
  </main>
  <script>
    const DATA = JSON.parse(new TextDecoder().decode(
      Uint8Array.from(atob("{payload}"), c => c.charCodeAt(0))
    ));
    const LLM_CONTEXT = new TextDecoder().decode(
      Uint8Array.from(atob("{llm_context_payload}"), c => c.charCodeAt(0))
    );
    const byId = id => document.getElementById(id);
    const get = (obj, path, fallback = "â€”") => {{
      const value = path.split(".").reduce((current, key) =>
        current == null ? undefined : current[key], obj);
      return value === undefined || value === null || value === "" ? fallback : value;
    }};
    const text = value => Array.isArray(value) ? value.join(", ") :
      typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "â€”");
    const escapeHtml = value => text(value).replace(/[&<>"']/g, char => ({{
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
    }})[char]);
    const imagePath = value => {{
      if (!value) return "";
      const parts = String(value).replaceAll("\\\\", "/").split("/");
      const index = parts.indexOf("screens");
      return index >= 0 ? parts.slice(index + 2).join("/") : parts.at(-1);
    }};
    const section = (title, data) => {{
      const entries = Object.entries(data || {{}});
      const rows = entries.map(([key, value]) =>
        `<dt>${{escapeHtml(key.replaceAll("_", " "))}}</dt><dd>${{escapeHtml(value)}}</dd>`
      ).join("");
      return `<section class="panel"><h2>${{escapeHtml(title)}}</h2><dl>${{rows}}</dl></section>`;
    }};

    byId("identity").textContent =
      `${{get(DATA, "screen.package")}} Â· ${{get(DATA, "screen.activity")}} Â· ${{get(DATA, "captured_at")}}`;
    const badgeValues = [
      `Contract: ${{get(DATA, "contract")}} v${{get(DATA, "schema_version")}}`,
      get(DATA, "screen.orientation"),
      get(DATA, "device.model"),
      `Android ${{get(DATA, "device.android_version")}} / API ${{get(DATA, "device.api_level")}}`,
      get(DATA, "stability.stable", false) ? "Stable" : "Not stable"
    ];
    byId("badges").innerHTML = badgeValues.map(value => `<span class="badge">${{escapeHtml(value)}}</span>`).join("");

    const metrics = {{
      "Elements": get(DATA, "summary.total_elements"),
      "Clickable": get(DATA, "summary.clickable_elements"),
      "Text nodes": get(DATA, "summary.text_elements"),
      "System nodes": get(DATA, "summary.system_elements"),
      "Screenshot": `${{get(DATA, "screenshot.width")}}Ã—${{get(DATA, "screenshot.height")}}`,
      "Hierarchy nodes": get(DATA, "hierarchy.node_count"),
      "Collection errors": (DATA.collection_errors || []).length,
      "Foreground": get(DATA, "screen.app_in_foreground")
    }};
    byId("metrics").innerHTML = Object.entries(metrics).map(([label, value]) =>
      `<div class="metric"><strong>${{escapeHtml(value)}}</strong><span>${{escapeHtml(label)}}</span></div>`
    ).join("");

    byId("fullScreenshot").src = imagePath(get(DATA, "artifacts.screenshots.full", ""));
    const cropNames = [["status_bar", "Status bar"], ["navigation_bar", "Navigation bar"]];
    byId("systemCrops").innerHTML = cropNames.map(([key, label]) => {{
      const region = get(DATA, `screenshot.regions.${{key}}`, null);
      const artifact = get(DATA, `artifacts.screenshots.${{key}}`, "");
      if (!region || !artifact) return "";
      return `<h3>${{label}}</h3>
        <img class="crop" src="${{escapeHtml(imagePath(artifact))}}" alt="${{label}}">
        <dl>
          <dt>Bounds</dt><dd>${{escapeHtml(region.bounds)}}</dd>
          <dt>Average color</dt><dd>${{escapeHtml(region.average_color)}}</dd>
          <dt>Luminance</dt><dd>${{escapeHtml(region.luminance)}}</dd>
        </dl>`;
    }}).join("");

    const status = get(DATA, "system.status_bar", {{}});
    const navigationBar = get(DATA, "system.navigation_bar", {{}});
    const detailSections = [
      ["Application", {{
        package: get(DATA, "screen.package"), activity: get(DATA, "screen.activity"),
        foreground: get(DATA, "screen.app_in_foreground"), contexts: get(DATA, "screen.contexts"),
        apk_label: get(DATA, "apk.label"), apk_version: get(DATA, "apk.version_name"),
        apk_sha256: get(DATA, "apk.sha256")
      }}],
      ["Device and display", {{
        serial: get(DATA, "device.serial"), manufacturer: get(DATA, "device.manufacturer"),
        model: get(DATA, "device.model"), Android: get(DATA, "device.android_version"),
        api_level: get(DATA, "device.api_level"), ABIs: get(DATA, "device.supported_abis"),
        locale: get(DATA, "device.locale"), timezone: get(DATA, "device.timezone"),
        physical_size: get(DATA, "display.physical_size"), density_dpi: get(DATA, "display.physical_density_dpi"),
        rotation: get(DATA, "display.rotation"), auto_rotation: get(DATA, "display.auto_rotation")
      }}],
      ["System UI", {{
        status_bar_visible: status.visible, status_bar_bounds: status.bounds,
        navigation_bar_visible: navigationBar.visible, navigation_bar_bounds: navigationBar.bounds,
        navigation_mode: get(DATA, "system.navigation.mode"),
        keyboard_visible: get(DATA, "system.keyboard_visible"),
        IME: get(DATA, "system.input_method.current_id"),
        dialog_present: get(DATA, "system.dialog.present"),
        permission_prompt: get(DATA, "system.permission_prompt.present")
      }}],
      ["Window and session", {{
        current_focus: get(DATA, "system.window.current_focus"),
        focused_app: get(DATA, "system.window.focused_app"),
        system_ui_flags: get(DATA, "system.window.system_ui_flags.decoded"),
        session_id: get(DATA, "session.session_id"),
        Appium_server: get(DATA, "session.server_url"),
        udid: get(DATA, "session.udid")
      }}],
      ["Stability and identity", {{
        stable: get(DATA, "stability.stable"), samples: get(DATA, "stability.samples"),
        elapsed_seconds: get(DATA, "stability.elapsed_seconds"),
        fingerprint: get(DATA, "fingerprint"),
        screenshot_sha256: get(DATA, "screenshot.sha256"),
        hierarchy_sha256: get(DATA, "hierarchy.sha256")
      }}]
    ];
    byId("detailSections").innerHTML = detailSections.map(args => section(...args)).join("");

    const elements = DATA.elements || [];
    [...new Set(elements.map(item => item.source))].sort().forEach(source => {{
      const option = document.createElement("option");
      option.value = source; option.textContent = source; byId("sourceFilter").append(option);
    }});
    const renderElements = () => {{
      const query = byId("elementSearch").value.toLowerCase();
      const source = byId("sourceFilter").value;
      const filtered = elements.filter(item =>
        (!source || item.source === source) &&
        (!query || JSON.stringify(item).toLowerCase().includes(query))
      );
      byId("elementRows").innerHTML = filtered.map(item => {{
        const states = ["clickable","long_clickable","scrollable","editable","checkable",
          "checked","enabled","focusable","focused","selected"]
          .filter(key => item[key]).join(", ");
        const label = [item.text, item.content_description].filter(Boolean).join(" / ");
        return `<tr>
          <td>${{escapeHtml(item.id)}}</td><td>${{escapeHtml(item.source)}}</td>
          <td>${{escapeHtml(item.interaction)}}</td><td class="wrap">${{escapeHtml(item.class)}}</td>
          <td class="wrap">${{escapeHtml(label)}}</td><td class="wrap">${{escapeHtml(item.resource_id)}}</td>
          <td class="wrap">${{escapeHtml(states)}}</td><td>${{escapeHtml(item.bounds_raw)}}</td>
          <td class="wrap">${{escapeHtml(item.xpath)}}</td></tr>`;
      }}).join("");
    }};
    byId("elementSearch").addEventListener("input", renderElements);
    byId("sourceFilter").addEventListener("change", renderElements);
    renderElements();

    const errors = DATA.collection_errors || [];
    byId("diagnostics").innerHTML = errors.length
      ? `<p class="warning"><strong>${{errors.length}} collection issue(s)</strong></p><pre>${{escapeHtml(errors)}}</pre>`
      : `<p class="ok"><strong>No collection errors.</strong></p>`;
    byId("rawJson").textContent = JSON.stringify(DATA, null, 2);
    byId("rawHierarchy").textContent = get(DATA, "hierarchy.raw_xml", "Not captured");
    byId("llmContext").textContent = LLM_CONTEXT;
    byId("contextStats").textContent =
      `${{LLM_CONTEXT.length.toLocaleString()}} characters Â· about ${{Math.ceil(LLM_CONTEXT.length / 4).toLocaleString()}} tokens`;
    byId("copyContext").addEventListener("click", async () => {{
      const button = byId("copyContext");
      try {{
        await navigator.clipboard.writeText(LLM_CONTEXT);
      }} catch (_) {{
        const temporary = document.createElement("textarea");
        temporary.value = LLM_CONTEXT;
        temporary.style.position = "fixed";
        temporary.style.opacity = "0";
        document.body.appendChild(temporary);
        temporary.select();
        document.execCommand("copy");
        temporary.remove();
      }}
      button.textContent = "Copied";
      setTimeout(() => button.textContent = "Copy context", 1400);
    }});
  </script>
</body>
</html>
"""

