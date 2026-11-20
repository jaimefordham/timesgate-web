const root = document.querySelector("#app");
let state, config, revision, page = "Screens", selected = "", dirty = false, polling = false;
let selectedPanel = 0, activityFailures = false;
let statusStale = false;
const pendingControls = new Set();
const activityDetails = new Set();
const objectUrls = [];
const lightingDrafts = new Map();
const loadedProfiles = new Map();
const controlDrafts = new Map();
const themes = {graphite: "Graphite", porcelain: "Porcelain", carbon: "Carbon", paper: "Paper"};
const widgetNames = {
  unmanaged: "Unmanaged / native", media: "Image / animated GIF",
  clock: "Clock", text: "Text", strava: "Strava cycling stats",
  weather: "Weather", countdown: "Countdown", rss: "RSS / Atom news", custom: "Custom design",
};

function screenTransport(device, screen) {
  if (screen.kind === "unmanaged") return "unmanaged";
  if (screen.kind === "media") return "gif";
  if (screen.kind === "clock" && device.clock_renderer === "native") return "native";
  return device.widget_transport || "gif";
}

function node(tag, attrs = {}, ...children) {
  const result = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) result.addEventListener(key.slice(2), value);
    else if (key === "class") result.className = value;
    else if (key === "checked") result.checked = value;
    else result.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child != null) result.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return result;
}
const hint = text => node("p", {class: "hint"}, text);
const card = (title, ...content) => node("section", {class: "card"}, node("h2", {}, title), ...content);
const row = (...content) => node("div", {class: "row"}, ...content);
const button = (text, action, primary = false) => node("button", {
  type: "button", class: primary ? "primary" : "",
  onclick: async event => {
    const control = event.currentTarget;
    control.disabled = true;
    try { await action(); } catch (error) { notify(error.message, true); }
    finally { control.disabled = false; }
  },
}, text);
function notify(text, error = false) {
  const notice = document.querySelector("#notice");
  notice.textContent = text; notice.className = error ? "error" : ""; notice.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { notice.hidden = true; }, 12000);
}
async function request(path, method = "GET", data, binary = false) {
  const response = await fetch("/api/" + path, {
    method, credentials: "same-origin", headers: {
      ...(data != null && !(data instanceof Blob) ? {"Content-Type": "application/json"} : {}),
    },
    body: data instanceof Blob ? data : data == null ? undefined : JSON.stringify(data),
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try { const body = await response.json(); message = body.error || body.detail || message; }
    catch { /* Proxies can return non-JSON errors. */ }
    if (response.status === 401 && state) login();
    throw Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return binary ? response.blob() : response.json();
}
function mark() {
  dirty = true;
  updateUnsavedConfiguration();
  updateScreenDrafts();
}
function updateUnsavedConfiguration() {
  const profileDirty = config?.devices.some(device => {
    const profile = loadedProfiles.get(device.id);
    return profile && (profileModified(device, profile)
      || !state.profiles.some(item => item.id === profile.id));
  });
  const status = document.querySelector("#save-state");
  if (status) status.textContent = dirty ? "Unsaved changes"
    : profileDirty ? "Unsaved profile changes"
    : page === "Lighting" ? "Preview locally, then Apply lighting" : "Saved configuration";
  const warning = document.querySelector("#unsaved-configuration");
  if (warning) warning.hidden = !dirty && !profileDirty;
  updateLoadedProfileStatus();
  fitDeviceStatusPanel();
}
function field(object, key, label, type = "text", options = null, changed = mark) {
  const control = type === "select" ? node("select")
    : type === "textarea" ? node("textarea") : node("input", {type});
  if (options) for (const [value, text] of Object.entries(options)) control.append(node("option", {value}, text));
  if (type === "checkbox") control.checked = !!object[key];
  else control.value = object[key] ?? "";
  control.addEventListener("input", () => {
    object[key] = type === "checkbox" ? control.checked
      : type === "number" ? Number(control.value) : control.value;
    changed();
  });
  return node("label", {}, node("span", {}, label), control);
}
function themePicker() {
  const preference = {theme: document.documentElement.dataset.theme || "graphite"};
  const picker = field(preference, "theme", "Theme", "select", themes, () => {
    document.documentElement.dataset.theme = preference.theme;
    try { localStorage.setItem("timesgate-theme", preference.theme); }
    catch (error) { notify("Theme applied, but your preference could not be saved: " + error.message, true); }
  });
  picker.classList.add("theme-picker");
  return picker;
}
function accentPicker(screen, group) {
  const choices = {original: {name: "Original"}, ...state.widget_accents};
  const selected = screen.accent ?? "original";
  const name = node("p", {class: "hint accent-current", "aria-live": "polite"}, choices[selected].name);
  const swatches = node("div", {class: "accent-swatches"});
  for (const [value, preset] of Object.entries(choices)) {
    const shape = preset.color
      ? `<circle cx="12" cy="12" r="8" fill="${preset.color}"/>`
      : '<path d="M6 18L18 6" stroke="#b9c9d6" stroke-width="3"/>';
    const image = "data:image/svg+xml," + encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><rect width="24" height="24" rx="12" fill="#08121e"/>${shape}</svg>`);
    swatches.append(node("label", {title: preset.name},
      node("input", {
        type: "radio", name: group, value, checked: value === selected, "aria-label": preset.name,
        onchange: event => {
          if (!event.currentTarget.checked) return;
          screen.accent = value; name.textContent = preset.name; mark();
        },
      }),
      node("img", {src: image, alt: "", width: "28", height: "28"})));
  }
  return node("fieldset", {class: "accent-picker"}, node("legend", {}, "Accent"), swatches, name);
}
function numberField(object, key, label, min, max, step = 1) {
  const labelNode = field(object, key, label, "number");
  const input = labelNode.querySelector("input");
  input.min = min; input.max = max; input.step = step;
  return labelNode;
}
function designEditor(screen) {
  screen.elements ??= [];
  screen.background ??= "#08121e";
  const metrics = {
    value: "Fixed value", cpu: "CPU %", ram: "RAM %", disk: "Disk %",
    gpu: "GPU %", cpu_temp: "CPU temperature (C)", gpu_temp: "GPU temperature (C)",
    download: "Download (bytes/s)", upload: "Upload (bytes/s)",
  };
  const editor = node("div", {class: "design-editor"},
    field(screen, "background", "Canvas background", "color"),
    hint("128 × 128 canvas. Layers draw in list order; later layers cover earlier ones. Image layers use the first frame of uploaded media."),
    hint("Text fields: {time}, {date}, {cpu}, {ram}, {disk}, {gpu}, {cpu_temp}, {gpu_temp}, {download}, {upload}. Metrics describe the server; unavailable values show N/A."));
  screen.elements.forEach((layer, index) => {
    const type = field(layer, "type", "Layer type", "select", {text: "Text", image: "Image", bar: "Bar"});
    type.querySelector("select").addEventListener("change", render);
    const item = node("details", {class: "design-layer", open: "", "data-layer": index},
      node("summary", {}, `Layer ${index + 1} · ${layer.type}`), type,
      row(numberField(layer, "x", "X", 0, 127), numberField(layer, "y", "Y", 0, 127)),
      row(numberField(layer, "width", "Width", 1, 128), numberField(layer, "height", "Height", 1, 128)));
    if (layer.type === "text") item.append(
      field(layer, "text", "Layer text", "textarea"), numberField(layer, "size", "Font size", 6, 64));
    if (layer.type !== "image") item.append(field(layer, "color", "Layer color", "color"));
    if (layer.type === "image") item.append(field(layer, "media_id", "Layer image", "select",
      {"": "Choose uploaded media", ...Object.fromEntries(state.media.map(media => [media.id, media.label]))}));
    if (layer.type === "bar") {
      const metric = field(layer, "metric", "Bar source", "select", metrics);
      metric.querySelector("select").addEventListener("change", render);
      item.append(metric);
      if (layer.metric === "value") item.append(field(layer, "value", "Bar value", "number"));
      item.append(numberField(layer, "maximum", "Bar maximum", 0.001, 1e15, "any"));
    }
    const move = offset => {
      const target = index + offset;
      [screen.elements[index], screen.elements[target]] = [screen.elements[target], screen.elements[index]];
      mark(); render();
    };
    const up = button("Move up", () => move(-1));
    const down = button("Move down", () => move(1));
    up.disabled = index === 0;
    down.disabled = index === screen.elements.length - 1;
    item.append(row(up, down, button("Remove layer", () => {
      screen.elements.splice(index, 1); mark(); render();
    })));
    editor.append(item);
  });
  const add = button("Add layer", () => {
    screen.elements.push({
      type: "text", x: 8, y: 8, width: 112, height: 24, size: 16, color: "#ffffff",
      text: "{time}", media_id: "", metric: "value", value: 50, maximum: 100,
    });
    mark(); render();
  });
  add.disabled = screen.elements.length >= 20;
  editor.append(add, hint("Up to 20 layers. Changes appear after Preview saves the configuration."));
  return editor;
}
function current() { return config.devices.find(item => item.id === selected) || config.devices[0]; }
async function load() {
  state = await request("state");
  statusStale = false;
  config = structuredClone(state.config); revision = state.revision; dirty = false;
  loadedProfiles.clear();
  for (const device of config.devices) {
    const profile = state.active_profiles[device.id];
    if (profile) {
      loadedProfiles.set(device.id, structuredClone(profile));
      lightingDrafts.set(device.id, structuredClone(profile.lighting));
    }
  }
  if (!config.devices.some(device => device.id === selected)) selected = config.devices[0].id;
}
async function save() {
  const profiles = config.devices.flatMap(device => {
    const loaded = loadedProfiles.get(device.id);
    if (!loaded) return [];
    const profile = state.profiles.find(item => item.id === loaded.id);
    if (!profile) throw Error("Loaded profile no longer exists; choose another profile before saving.");
    return [{...profileSnapshot(device, profile.name), id: profile.id}];
  });
  const result = await request("config", "PUT", {config, revision, profiles});
  revision = result.revision;
  await load(); render();
}
function login(setup = false) {
  state = null; config = null; revision = null; dirty = false;
  lightingDrafts.clear();
  loadedProfiles.clear();
  controlDrafts.clear();
  objectUrls.splice(0).forEach(url => URL.revokeObjectURL(url));
  const username = node("input", {
    name: "username", required: "", maxlength: "64", autocomplete: "username", value: "admin",
  });
  const password = node("input", {
    name: "password", type: "password", required: "", maxlength: "1024",
    autocomplete: setup ? "new-password" : "current-password",
    ...(setup ? {minlength: "8"} : {}),
  });
  const submit = node("button", {type: "submit", class: "primary"},
    setup ? "Create administrator" : "Sign in");
  const fields = [
    node("label", {}, node("span", {}, "Username"), username),
    node("label", {}, node("span", {}, "Password"), password),
  ];
  if (setup) fields.push(
    node("label", {}, node("span", {}, "Confirm password"),
      node("input", {
        name: "confirmation", type: "password", required: "", autocomplete: "new-password",
        minlength: "8", maxlength: "1024",
      })));
  root.replaceChildren(node("section", {class: "login card"},
    node("div", {class: "brand"}, "TIMESGATE / WEB"),
    node("h1", {}, setup ? "Create your administrator" : "Your five-screen workspace"),
    hint(setup
      ? "Choose a username using letters, numbers, dots, underscores or hyphens, and a password of at least 8 characters. The first visitor creates the administrator; keep access restricted to your trusted network during setup."
      : "Sign in with your administrator username and password."),
    node("form", {onsubmit: async event => {
      event.preventDefault();
      const form = event.currentTarget;
      if (setup && password.value !== form.elements.confirmation.value) {
        notify("Passwords do not match.", true); return;
      }
      submit.disabled = true;
      try {
        await request("auth/" + (setup ? "setup" : "login"), "POST", {
          username: username.value, password: password.value,
        });
        password.value = "";
        document.querySelector("#notice").hidden = true;
        await start();
      } catch (error) { notify(error.message, true); }
      finally { submit.disabled = false; }
    }}, ...fields, submit),
    hint("Saved settings and schedules stay on the server. Device media remains accessible without signing in."),
    themePicker()));
}
async function start() {
  try {
    const status = await request("auth/status");
    if (status.authenticated) { await load(); render(); }
    else login(status.setup_required);
  } catch (error) {
    root.replaceChildren(card("Unable to load Timesgate Web",
      hint("Check that the application server is available."), button("Try again", start)));
    notify(error.message, true);
  }
}
async function fillPreview(image, path) {
  try {
    const blob = await request(path, "GET", null, true);
    if (!image.isConnected) return;
    const url = URL.createObjectURL(blob); objectUrls.push(url); image.src = url;
    image.alt = "Screen preview";
  } catch (error) {
    if (image.isConnected) image.alt = error.message;
  }
}
function render() {
  objectUrls.splice(0).forEach(url => URL.revokeObjectURL(url));
  const main = node("main");
  const navButton = name => button(name, () => { page = name; render(); });
  const plugins = node("details", {class: "plugins"},
    node("summary", {}, "Plugins"),
    node("div", {class: "plugin-links"}, ...["Strava"].map(navButton)));
  plugins.open = root.querySelector("nav .plugins")?.open ?? false;
  root.replaceChildren(node("div", {class: "layout"},
    node("aside", {}, node("div", {class: "brand"}, "TIMESGATE / WEB"),
      hint(`Version ${state.version}`),
      node("div", {class: "tag"}, state.demo ? "DEMO · NO DEVICE WRITES" : "SINGLE-OWNER CONTROLLER"),
      node("nav", {"aria-label": "Sections"}, ...["Screens", "Lighting", "Profiles", "Tools", "Media", "Devices"].map(navButton),
        plugins, ...["Activity", "Settings"].map(navButton)),
      hint("Schedules run in the container, even when this browser is closed.")),
    node("div", {class: "workspace"},
      node("div", {class: "global-status-shell"},
      node("section", {class: "global-device-bar", "aria-label": "Global device status"},
        node("span", {class: "global-device-label"}, "Device status"),
        node("div", {id: "global-device-summary"}),
        node("details", {class: "global-device-disclosure", ontoggle: fitDeviceStatusPanel},
          node("summary", {}, "All devices",
            node("span", {id: "global-device-count", class: "pill"}),
            node("span", {"aria-hidden": "true"}, "\u25be")),
          node("section", {class: "device-status-panel", "aria-label": "All device status"},
            node("div", {class: "device-status-heading"},
              node("h2", {}, "All devices")),
            node("div", {id: "global-device-status"}),
            node("div", {class: "device-status-footer"},
              node("span", {id: "device-status-freshness", role: "status"}),
              button("View Activity", () => { page = "Activity"; render(); }))))),
      node("section", {id: "unsaved-configuration", class: "unsaved-configuration",
        "aria-label": "Unsaved configuration"},
        node("div", {role: "status"},
          node("strong", {}, "Unsaved configuration"),
          node("span", {}, "Save configuration to persist your configuration and loaded-profile edits. Lighting is sent only by Apply lighting.")),
        button("Save configuration", async () => { await save(); notify("Configuration and loaded profiles saved. Lighting was not sent."); }, true))),
      main)));
  [...root.querySelectorAll("nav button")].find(item => item.textContent === page)?.classList.add("active");
  main.append(node("header", {},
    node("div", {}, node("h1", {}, page), node("span", {id: "save-state", class: "muted"},
      page === "Lighting" ? "Preview locally, then Apply lighting" : dirty ? "Unsaved changes" : "Saved configuration")),
    row(themePicker(), button("Reload", async () => {
      if ((dirty || lightingDrafts.size || page === "Tools") && !confirm("Discard unsaved changes?")) return;
      lightingDrafts.clear();
      controlDrafts.clear();
      await load(); render();
    }), page === "Lighting" && !loadedProfiles.has(selected) ? null
      : button("Save configuration", async () => { await save(); notify("Configuration and loaded profiles saved. Lighting was not sent."); }, true))));
  if (state.demo) main.append(node("div", {class: "banner"}, "Demo mode uses sample weather, news and metrics; it never contacts Times Gate, Strava or widget sources."));
  if (!state.media_base_url && page !== "Lighting") main.append(node("div", {class: "banner warning"},
    "Set MEDIA_BASE_URL for GIF playback and native clock backgrounds. It must be reachable from the Times Gate. Direct JPEG widget uploads do not require it."));
  if (page === "Screens") screensPage(main);
  if (page === "Lighting") lightingPage(main);
  if (page === "Profiles") profilesPage(main);
  if (page === "Tools") toolsPage(main);
  if (page === "Media") mediaPage(main);
  if (page === "Devices") devicesPage(main);
  if (page === "Strava") stravaPage(main);
  if (page === "Activity") activityPage(main);
  if (page === "Settings") settingsPage(main);
  updateGlobalDeviceStatus();
  updateUnsavedConfiguration();
}
function deviceSelector(devices, label = "Device") {
  const selector = node("select", {"aria-label": label});
  for (const device of devices) selector.append(node("option", {value: device.id}, device.name));
  selector.value = devices.some(device => device.id === selected) ? selected : devices[0].id;
  selector.addEventListener("change", () => { selected = selector.value; selectedPanel = 0; render(); });
  return selector;
}
function applyStatus(latest) {
  for (const key of ["jobs", "schedules", "config", "lighting", "lighting_restoration",
    "strava", "controls", "device_schedules", "lighting_presets", "profiles", "active_profiles"]) state[key] = latest[key];
  statusStale = false;
  updateGlobalDeviceStatus();
  updateLightingStatus();
  updateDeviceStatus();
  updateScreenDrafts();
  updateUnsavedConfiguration();
  document.querySelector("#scheduled-updates")?.replaceChildren(scheduledUpdatesTable());
  document.querySelector("#job-list")?.replaceChildren(jobsTable());
}
function updateGlobalDeviceStatus() {
  const target = document.querySelector("#global-device-status");
  if (!target || !state) return;
  const summary = state.config.devices.map(device => {
    const runtime = state.controls[device.id];
    return {
      id: device.id, name: device.name,
      configured: Boolean(device.host && (device.hardware !== 402 || device.local_token_set)),
      available: Boolean(runtime), sleeping: runtime?.sleeping, paused: runtime?.paused,
      pushes: device.screens.filter(screen => screen.enabled).length,
      night: device.night, timezone: state.config.timezone,
      busy: pendingControls.has(device.id) || state.jobs.some(job => job.device_id === device.id
        && ["control", "device_schedule"].includes(job.operation)
        && ["pending", "running"].includes(job.status)),
    };
  });
  const active = summary.find(item => item.id === selected) || summary[0];
  const signature = JSON.stringify([statusStale, active.id, summary]);
  if (target.dataset.status === signature) return;
  target.dataset.status = signature;
  const needsAttention = item => statusStale || !item.available || !item.configured
    || item.sleeping || item.paused;
  const alerts = summary.filter(needsAttention).length;
  const count = document.querySelector("#global-device-count");
  count.textContent = alerts ? `${alerts} alert${alerts === 1 ? "" : "s"}`
    : `${summary.length} device${summary.length === 1 ? "" : "s"}`;
  count.classList.toggle("status-warning", alerts > 0);
  const selector = deviceSelector(state.config.devices, "Status device");
  selector.value = active.id;
  const chip = (label, value, warning = false, className = "") => node("span", {
    class: warning ? "status-warning" : className,
  }, label, node("strong", {}, value));
  const night = active.night.enabled
    ? `${active.night.start} - ${active.night.end}` : "Off";
  document.querySelector("#global-device-summary").replaceChildren(
    node("div", {class: "global-device-selection"},
      node("span", {class: `global-device-dot${needsAttention(active) ? " status-warning" : ""}`,
        "aria-hidden": "true"}), selector),
    node("div", {class: "global-device-chips"},
      chip("", `${active.pushes} automatic`),
      statusStale ? chip("", "Status stale", true)
        : !active.available ? chip("", "Status unavailable", true)
        : !active.configured ? chip("", "Connection not configured", true)
        : [
          chip("Sleep guard ", active.sleeping ? "Active" : "Off", active.sleeping),
          chip("", active.paused ? "Paused" : "Running", active.paused, "status-running"),
        ],
      node("span", {class: "global-device-night", title: `Saved night schedule in ${active.timezone}`},
        "Night ", node("strong", {}, night))));
  const freshness = document.querySelector("#device-status-freshness");
  freshness.textContent = statusStale ? "Status stale / refresh failed"
    : state.demo ? "Demo / local state only" : "Controller state / not live device feedback";
  freshness.classList.toggle("status-warning", statusStale);
  target.replaceChildren(...summary.map(item => {
    const blocked = item.sleeping || item.paused;
    const uncertain = statusStale || !item.available;
    const metric = (label, value, warning = false, title = "") => node("div", {
      class: `device-status-metric${warning ? " status-warning" : ""}`, title,
    }, node("span", {}, label), node("strong", {}, value));
    const activity = button("Activity", () => { selected = item.id; page = "Activity"; render(); });
    const actions = node("div", {class: "device-status-actions"}, activity);
    if (item.sleeping || item.paused) {
      const wake = Boolean(item.sleeping);
      const recover = button(wake ? "Wake displays" : "Restore / resume", () => {
        const device = state.config.devices.find(device => device.id === item.id);
        return sendControl(device, wake ? {action: "power", on: true} : {action: "resume"});
      });
      recover.className = "device-status-recover";
      recover.disabled = uncertain || !item.configured || item.busy;
      recover.title = statusStale ? "Wait for a successful status refresh before sending."
        : item.busy ? "A device control is pending or running."
        : wake ? "Release the sleep guard after wake acknowledgement."
        : "Release the pause and send configured managed screens, including unscheduled screens.";
      actions.prepend(recover);
    }
    const description = uncertain ? "Status unavailable"
      : !item.configured ? "Connection not configured"
      : blocked ? "Screen sends held" : "Screen sends permitted";
    const night = item.night.enabled
      ? `${item.night.mode === "sleep" ? "Sleep" : "Dim"} ${item.night.start} - ${item.night.end}` : "Off";
    return node("article", {
      class: `device-status-row${needsAttention(item) ? " device-status-attention" : ""}`,
      "aria-label": `${item.name} controller status`,
    },
    node("div", {class: "device-status-identity"},
      node("span", {class: "device-status-glyph", "aria-hidden": "true"},
        ...Array.from({length: 5}, () => node("span"))),
      node("div", {}, node("strong", {}, item.name), node("span", {}, description))),
    node("div", {class: "device-status-metrics"},
      metric("Screen schedules", `${item.pushes} automatic`),
      metric("Sleep guard", item.available ? item.sleeping ? "Active" : "Off" : "Unknown", item.sleeping),
      metric("Controller", item.available ? item.paused ? "Paused" : "Not paused" : "Unknown", item.paused),
      metric("Night schedule", night, false, `Saved schedule in ${item.timezone}`)),
    actions);
  }));
}
function fitDeviceStatusPanel() {
  const disclosure = document.querySelector(".global-device-disclosure");
  if (!disclosure?.open) return;
  const panel = disclosure.querySelector(".device-status-panel");
  const available = window.innerHeight - panel.getBoundingClientRect().top - 16;
  panel.style.maxHeight = `${Math.max(0, available)}px`;
}
window.addEventListener("resize", fitDeviceStatusPanel);
window.addEventListener("scroll", fitDeviceStatusPanel, {passive: true});
document.addEventListener("click", event => {
  const disclosure = document.querySelector(".global-device-disclosure");
  if (disclosure?.open && !disclosure.contains(event.target)) disclosure.open = false;
}, {capture: true});
document.addEventListener("keydown", event => {
  const disclosure = document.querySelector(".global-device-disclosure");
  if (event.key === "Escape" && disclosure?.open) {
    disclosure.open = false;
    disclosure.querySelector("summary").focus();
  }
});
async function sendControl(device, control) {
  const runtime = state.controls[device.id];
  const takeover = ["timer", "stopwatch", "scoreboard", "noise"].includes(control.action);
  if (control.action === "reboot" && !confirm(
    `Reboot ${device.name}? Displays will be interrupted and screen updates paused until you restore the layout.`)) return;
  if (takeover && !runtime?.paused && !confirm(
    `This experimental native tool may replace multiple screens on ${device.name}. Pause screen updates until you choose Restore layout?`)) return;
  if (pendingControls.has(device.id)) throw Error("A device control is already being queued.");
  pendingControls.add(device.id);
  updateGlobalDeviceStatus();
  try {
    const result = await request("device/control", "POST", {
      device_id: device.id, control, confirmed: takeover || control.action === "reboot",
    });
    notify(`Device control ${result.job.slice(0, 8)} queued. Check Activity; acceptance is not verified physical effect.`);
    const latest = await request("state");
    if (state) applyStatus(latest);
  } catch (error) {
    statusStale = true;
    throw error;
  } finally {
    pendingControls.delete(device.id);
    updateGlobalDeviceStatus();
  }
}
function updateDeviceStatus() {
  for (const target of document.querySelectorAll("[data-control-status]")) {
    const device = state.config.devices.find(item => item.id === target.dataset.controlStatus);
    const runtime = device && state.controls[device.id];
    if (!runtime) {
      target.replaceChildren(hint("Save this device connection before using controls."));
      continue;
    }
    const pushes = device.screens.filter(screen => screen.enabled).length;
    const job = state.jobs.find(item => item.device_id === device.id
      && ["control", "device_schedule"].includes(item.operation));
    const detailsOpen = target.querySelector(".control-status-details")?.open;
    target.replaceChildren(...[
      node("div", {class: "status-chips"},
        node("span", {class: `pill${runtime.sleeping ? " guard-status" : ""}`},
          runtime.sleeping ? "Sleep requested / send guard active" : "No sleep guard"),
        node("span", {class: "pill"}, `${pushes} automatic screen schedule(s)`),
        runtime.paused ? node("span", {class: "pill guard-status"}, "Controller paused") : null),
      job ? row(job.status === "done" ? node("span", {class: "pill"}, "Done (controller)") : ragBadge(job),
        runDetails(job) || hint("Device control is waiting.")) : null,
      node("details", {class: "control-status-details", ...(detailsOpen ? {open: ""} : {})},
        node("summary", {}, "Acknowledged settings and safety details"),
        hint(`Last control acknowledgement: ${when(runtime.acknowledged_at)}. Not live device feedback.`),
        hint(runtime.brightness == null ? "LCD brightness has not been acknowledged."
          : `Last acknowledged LCD brightness: ${runtime.brightness}%. RGB brightness is separate.`),
        runtime.simulated_at ? hint(`Last simulation: ${when(runtime.simulated_at)}. Demo guard changes are local only.`) : null,
        hint("Sleep blocks screen sends; Wake releases that guard after acknowledgement. Native tools/reboot stay paused until Restore layout. Restoration sends managed screens only, including unscheduled ones; native/unmanaged screens cannot be recovered.")),
    ].filter(child => child != null));
    const diagnostic = document.querySelector(`[data-diagnostic="${device.id}"]`);
    if (diagnostic) diagnostic.replaceChildren(...[
      hint(runtime.diagnostic?.message || "No settings query yet. No background health polling is performed."),
      runtime.diagnostic ? hint(`Checked: ${when(runtime.diagnostic.checked_at)}`) : null,
      ...Object.entries(runtime.diagnostic?.values || {}).map(([key, value]) => hint(`${key}: ${value}`)),
    ].filter(child => child != null));
  }
  for (const control of document.querySelectorAll("[data-send-device]")) {
    const runtime = state.controls[control.dataset.sendDevice];
    control.disabled = !runtime || runtime.sleeping || runtime.paused;
  }
}
function deviceBar(device) {
  const saved = state.config.devices.find(item => item.id === device.id);
  const configured = saved?.host && (saved.hardware !== 402 || saved.local_token_set);
  const night = device.night;
  const output = node("output", {}, `${night.day_brightness}%`);
  const slider = node("input", {
    type: "range", min: "0", max: "100", step: "1", value: night.day_brightness,
    "aria-label": "LCD brightness draft",
    oninput: event => {
      night.day_brightness = Number(event.target.value);
      output.textContent = `${night.day_brightness}%`;
      mark();
    },
  });
  const actions = [
    button("Apply LCD brightness", () => sendControl(device, {action: "brightness", value: night.day_brightness})),
    button("Sleep displays", () => sendControl(device, {action: "power", on: false})),
    button("Wake displays", () => sendControl(device, {action: "power", on: true})),
    button("Restore layout / resume", () => sendControl(device, {action: "resume"})),
    button("RGB lighting", () => { selected = device.id; page = "Lighting"; render(); }),
  ];
  actions.slice(0, 4).forEach(control => { control.disabled = !configured; });
  return node("section", {class: "card device-command-bar"},
    node("h2", {}, `${device.name} / Device controls`),
    hint("Experimental firmware controls; physical hardware 402 behaviour is unverified. Uses the saved connection."),
    node("label", {}, node("span", {}, "LCD brightness draft (not RGB)"), row(slider, output)),
    hint("Linked to Day LCD brightness. Apply sends it now; Save configuration persists it for the day schedule."),
    row(...actions),
    node("div", {"data-control-status": device.id}));
}
function updateScreenDrafts() {
  if (!config || !state) return;
  for (const label of document.querySelectorAll("[data-screen-draft]")) {
    const panel = Number(label.dataset.screenDraft);
    const device = current();
    const saved = state.config.devices.find(item => item.id === device.id);
    const changed = config.timezone !== state.config.timezone
      || screenTransport(device, device.screens[panel]) !== screenTransport(saved || {}, device.screens[panel])
      || JSON.stringify(device.screens[panel]) !== JSON.stringify(saved?.screens[panel]);
    label.textContent = changed ? "Preview changed / save to refresh" : "Saved preview / not live";
  }
  for (const target of document.querySelectorAll("[data-screen-rag]")) {
    const panel = Number(target.dataset.screenRag);
    const device = current();
    const schedule = state.schedules.find(item => item.device_id === device.id && item.panel === panel);
    target.replaceChildren(ragBadge(schedule?.last_run,
      device.screens[panel].enabled ? "Awaiting automatic run" : "Not scheduled"));
  }
}
function screensPage(main) {
  const selector = deviceSelector(config.devices);
  main.append(row(selector), hint("Set content for each screen. Automatic pushes start only after saving an enabled schedule. Native/unmanaged screens are left untouched."));
  const device = current();
  const savedDevice = state.config.devices.find(item => item.id === device.id);
  main.append(loadedProfileBanner(device), deviceBar(device));
  const overview = node("div", {class: "screen-overview", role: "group", "aria-label": "Five-screen overview"});
  const previews = [];
  device.screens.forEach((screen, panel) => {
    const image = node("img", {class: "preview", alt: screen.kind === "unmanaged" ? "Native screen / not managed" : "Save to preview", width: "128", height: "128"});
    const choice = button("", () => {
      const offset = overview.scrollLeft;
      selectedPanel = panel;
      render();
      const strip = document.querySelector(".screen-overview");
      strip.scrollLeft = offset;
      strip.querySelectorAll(".screen-choice")[panel].focus({preventScroll: true});
    });
    choice.className = "screen-choice";
    choice.setAttribute("aria-label", `Edit screen ${panel + 1}`);
    choice.setAttribute("aria-pressed", String(panel === selectedPanel));
    const accent = state.widget_accents[screen.accent]?.color;
    if (accent) choice.style.borderTopColor = accent;
    const schedule = state.schedules.find(item => item.device_id === device.id && item.panel === panel);
    choice.append(node("strong", {}, `SCREEN ${panel + 1}`),
      screen.kind === "unmanaged" ? node("span", {class: "native-preview"}, "Native") : image,
      node("span", {}, widgetNames[screen.kind]),
      node("span", {class: "hint"}, screen.title || (screen.enabled ? "Automatic pushes enabled" : "Automatic pushes off")),
      node("span", {class: "hint", "data-screen-draft": panel}),
      node("span", {"data-screen-rag": panel},
        ragBadge(schedule?.last_run, screen.enabled ? "Awaiting automatic run" : "Not scheduled")));
    overview.append(choice);
    previews.push([image, panel]);
  });
  main.append(overview);
  const grid = node("div", {class: "screen-editor"});
  device.screens.forEach((screen, panel) => {
    if (panel !== selectedPanel) return;
    const image = node("img", {class: "preview", alt: "Preview not loaded", width: "128", height: "128"});
    previews.push([image, panel]);
    const content = card(`SCREEN ${panel + 1}`,
      screen.kind === "unmanaged" ? hint("Native/unmanaged content is left untouched. Choose a widget or media to manage this screen.") : image,
      field(screen, "kind", "Content", "select", widgetNames));
    content.classList.add("screen");
    content.querySelector("select").addEventListener("change", () => {
      if (screen.kind !== "clock" && screen.schedule === "on_time_change") screen.schedule = "interval";
      render();
    });
    if (screen.kind === "media") {
      content.append(field(screen, "media_id", "Media", "select",
        {"": "Choose a file", ...Object.fromEntries(state.media.map(item => [item.id, item.label]))}));
    }
    if (["clock", "text", "strava", "weather", "countdown", "rss"].includes(screen.kind)) {
      content.append(field(screen, "title", "Heading"), accentPicker(screen, `accent-${device.id}-${panel}`));
    }
    if (screen.kind === "text") content.append(field(screen, "text", "Text (up to six displayed lines)", "textarea"));
    if (screen.kind === "strava") {
      content.append(
        field(screen, "strava_mode", "Period", "select", {ytd: "Year to date", "all-time": "All time"}),
        field(screen, "units", "Units", "select", {metric: "Metric", imperial: "Imperial"}));
      if (state.strava.fetched_at == null) content.append(hint(
        "Example data: no cached Strava statistics yet. Connect and refresh Strava under Plugins to preview your own totals. Sending requires real statistics."));
    }
    if (screen.kind === "weather") content.append(
      numberField(screen, "latitude", "Latitude", -90, 90, "any"),
      numberField(screen, "longitude", "Longitude", -180, 180, "any"),
      field(screen, "units", "Temperature", "select", {metric: "Celsius", imperial: "Fahrenheit"}),
      hint("Current conditions, cached for 10 minutes. Enter your location's coordinates."),
      node("p", {class: "hint"}, "Weather data: ",
        node("a", {href: "https://open-meteo.com/"}, "Open-Meteo"), " (CC BY 4.0)."));
    if (screen.kind === "countdown") content.append(
      field(screen, "target", "Target date and time",
        /(?:Z|[+-]\d\d:\d\d)$/.test(screen.target || "") ? "text" : "datetime-local"),
      hint(`Uses ${config.timezone} unless the target includes an explicit UTC offset. Updates only when previewed or pushed; it is not a self-ticking timer.`));
    if (screen.kind === "rss") {
      screen.news_seconds ??= 15; screen.news_size ??= 13;
      content.append(field(screen, "url", "Public RSS / Atom feed URL", "url"),
        numberField(screen, "news_seconds", "Headline rotation (seconds)", 10, 3600),
        numberField(screen, "news_size", "Headline font size", 8, 32),
        hint("Feeds are cached for five minutes. Each push samples the current headline; use a refresh interval no longer than the rotation time. Only public Internet URLs are accepted."));
    }
    if (screen.kind === "custom") content.append(designEditor(screen));
    if (screen.kind !== "unmanaged") {
      const native = screenTransport(device, screen) === "native";
      content.append(hint(native
        ? "Experimental native clock/date. Preview is an image-layout approximation; firmware font 2 and device time-format preferences determine the actual appearance. Send installs it and synchronizes device-wide time/timezone."
        : screenTransport(device, screen) === "jpeg"
          ? "Experimental direct JPEG upload. No media download; compression may slightly change the preview."
          : "GIF URL playback. The device must download the image or animation from the media server."));
      const schedule = field(screen, "schedule", "Schedule", "select", {
        interval: "Every interval", daily: "Daily at local time",
        ...(screen.kind === "clock" ? {on_time_change: "On time change"} : {}),
      });
      const timing = screen.schedule === "on_time_change" ? null : screen.schedule === "daily"
        ? field(screen, "daily_time", `Daily time (${config.timezone})`, "time")
        : field(screen, "refresh_seconds", "Refresh interval (seconds, minimum 10)", "number");
      const updateScheduleControls = () => {
        for (const label of [schedule, timing].filter(Boolean)) {
          label.querySelector("input, select").disabled = !screen.enabled || native;
        }
      };
      content.append(field(screen, "enabled", "Automatic pushes", "checkbox", null, () => {
        mark();
        updateScheduleControls();
      }), schedule);
      content.append(native
        ? hint("Automatic pushes install immediately after saving changes, then reinstall and sync time hourly. The device ticks between updates. Image schedule values are preserved but ignored in native mode. Off means manual Send / Restore layout only.")
        : timing || hint(`Pushes at each minute change (${config.timezone}), while automatic pushes are enabled.`));
      if (native && timing) content.append(timing);
      schedule.querySelector("select").addEventListener("change", render);
      updateScheduleControls();
      const send = button("Send", async () => {
        await save();
        const result = await request("send", "POST", {device_id: device.id, panel});
        notify(`Queued ${result.job.slice(0, 8)}. Check Activity for acknowledgement or errors.`);
      }, true);
      send.dataset.sendDevice = device.id;
      content.append(row(
        button("Preview", async () => {
          await save();
          const target = document.querySelector(".screen-editor .preview");
          await fillPreview(target, `preview/${device.id}/${panel}`);
        }),
        send));
      const due = state.schedules.find(item => item.device_id === device.id && item.panel === panel);
      if (due) content.append(hint(`Next push: ${when(due.next_due)}`));
    }
    grid.append(content);
  });
  main.append(grid);
  previews.forEach(([image, panel]) => {
    const screen = device.screens[panel];
    const unchanged = config.timezone === state.config.timezone
      && JSON.stringify(screen) === JSON.stringify(savedDevice?.screens[panel]);
    if (screen.kind !== "unmanaged" && unchanged) fillPreview(image, `preview/${device.id}/${panel}`);
    else if (screen.kind !== "unmanaged") image.alt = "Save or click Preview to see your changes";
  });
  updateDeviceStatus();
  updateScreenDrafts();
  main.append(hint("Image clocks need controller updates to keep time; native clocks tick on the device. Uploaded media always uses GIF downloads. An acknowledgement is not proof of rendering."));
}

function profileSnapshot(device, name) {
  return {
    device_id: device.id, name,
    screens: structuredClone(device.screens),
    night: structuredClone(device.night),
    lighting: structuredClone(lightingDrafts.get(device.id)
      || state.lighting[device.id]?.settings || state.lighting_defaults),
  };
}
function profileModified(device, profile) {
  const draft = profileSnapshot(device, profile.name);
  return JSON.stringify([draft.screens, draft.night, draft.lighting])
    !== JSON.stringify([profile.screens, profile.night, profile.lighting]);
}
function loadedProfileBanner(device) {
  return node("div", {
    class: "banner loaded-profile", "data-loaded-profile": device.id,
    role: "status", "aria-live": "polite",
  });
}
function updateLoadedProfileStatus() {
  if (!state || !config) return;
  for (const target of document.querySelectorAll("[data-loaded-profile]")) {
    const device = config.devices.find(item => item.id === target.dataset.loadedProfile);
    const profile = loadedProfiles.get(device?.id);
    const saved = profile && state.profiles.find(item => item.id === profile.id);
    const modified = profile && profileModified(device, profile);
    target.textContent = !profile ? "Loaded profile: none. Create or restore one in Profiles."
      : !saved ? `Loaded profile: ${profile.name} · Deleted — choose another profile before saving.`
      : `Loaded profile: ${saved.name}${modified ? " · Modified" : ""}`;
    target.classList.toggle("warning", !!profile && (!saved || modified));
  }
}
function profilesPage(main) {
  const device = current();
  const profiles = state.profiles.filter(profile => profile.device_id === device.id);
  const saved = state.config.devices.some(item => item.id === device.id);
  const lighting = lightingDrafts.get(device.id)
    || state.lighting[device.id]?.settings || state.lighting_defaults;
  const nameDraft = {name: ""};
  const create = button("Save new profile", async () => {
    const result = await request("profiles", "PUT", profileSnapshot(device, nameDraft.name));
    state.profiles = result.profiles;
    loadedProfiles.set(device.id, structuredClone(result.profile));
    lightingDrafts.set(device.id, structuredClone(result.profile.lighting));
    render(); notify("Profile saved on the server. No configuration saved or commands sent.");
  }, true);
  create.disabled = !saved || profiles.length >= 20;
  main.append(row(deviceSelector(config.devices)),
    loadedProfileBanner(device),
    hint("Profiles belong to this device. Restore loads a draft only; it does not save configuration or send device commands."),
    card("Current profile draft",
      node("ol", {}, ...device.screens.map(screen =>
        node("li", {}, `${widgetNames[screen.kind]}${screen.title ? " / " + screen.title : ""}${screen.enabled ? " / automatic" : ""}`))),
      hint(`Night schedule: ${device.night.enabled ? `${device.night.start} - ${device.night.end} (${device.night.mode})` : "off"}. Times use the application timezone, which is not changed by profiles.`),
      hint(`Lighting: ${lighting.color.toUpperCase()}, ${lighting.brightness}%, power ${lighting.on ? "on" : "off"}. Uses the current lighting preview, otherwise last acknowledged settings, otherwise defaults.`),
      row(button("Review screens", () => { page = "Screens"; render(); }),
        button("Review lighting", () => { page = "Lighting"; render(); }),
        button("Review night settings", () => { page = "Devices"; render(); })),
      hint("Save configuration updates each loaded profile together with screens and night settings and activates enabled schedules. Use Send for an immediate unscheduled screen update; Apply lighting sends lighting separately.")),
    card("Save this layout as a profile",
      row(field(nameDraft, "name", "Profile name", "text", null, () => {}), create),
      hint("Captures all five screen positions, content, accents, schedules, night settings and full lighting settings, including unsaved edits. Up to 20 profiles per device."),
      hint("Device identity, connection, credentials, Strava authorisation, UI theme, runtime sleep/tool state and lighting startup-restore approval are not included. Uploaded media is referenced, not copied."),
      saved ? null : hint("Save this device's configuration before creating profiles.")));

  const list = node("div", {class: "grid profile-cards"});
  for (const profile of profiles) {
    const rename = {name: profile.name};
    const clone = {name: `${profile.name.slice(0, 35)} copy`};
    list.append(card(profile.name,
      field(rename, "name", `Name for ${profile.name}`, "text", null, () => {}),
      row(button("Restore profile", async () => {
        if (!confirm(`Load "${profile.name}" into this device's editor, replacing its screen, night and lighting drafts? Saved settings remain unchanged until you explicitly save/apply.`)) return;
        const snapshot = await request(`profiles/${profile.id}`);
        if (snapshot.device_id !== device.id) throw Error("This profile belongs to another device.");
        device.screens = structuredClone(snapshot.screens);
        device.night = structuredClone(snapshot.night);
        lightingDrafts.set(device.id, structuredClone(snapshot.lighting));
        loadedProfiles.set(device.id, structuredClone(snapshot));
        mark(); render();
        notify(`"${snapshot.name}" loaded for review. Save configuration and Apply lighting only when ready.`);
      }, true), button("Rename", async () => {
        const result = await request(`profiles/${profile.id}`, "PATCH", rename);
        state.profiles = result.profiles; render(); notify("Profile renamed.");
      }), button("Update snapshot", async () => {
        if (!confirm(`Replace "${profile.name}" with this device's current screen, night and lighting drafts?`)) return;
        const result = await request("profiles", "PUT", {
          ...profileSnapshot(device, profile.name), id: profile.id,
        });
        if (loadedProfiles.get(device.id)?.id === profile.id) {
          loadedProfiles.set(device.id, structuredClone(result.profile));
        }
        state.profiles = result.profiles; render(); notify("Profile updated. Nothing sent.");
      }), button("Delete", async () => {
        if (!confirm(`Delete profile "${profile.name}"? Current settings and uploaded media will not be changed.`)) return;
        const result = await request(`profiles/${profile.id}`, "DELETE");
        if (loadedProfiles.get(device.id)?.id === profile.id) loadedProfiles.delete(device.id);
        state.profiles = result.profiles; render(); notify("Profile deleted.");
      })),
      row(field(clone, "name", `Clone name for ${profile.name}`, "text", null, () => {}),
        button("Clone profile", async () => {
          const result = await request(`profiles/${profile.id}/clone`, "POST", clone);
          state.profiles = result.profiles; render();
          notify(`"${result.profile.name}" cloned from the saved profile. Current drafts were not changed.`);
        }))));
  }
  main.append(card(`Saved profiles (${profiles.length}/20)`,
    profiles.length ? list : hint("No profiles saved for this device yet.")));
}
function mediaPage(main) {
  const upload = node("input", {type: "file", accept: ".gif,.png,.jpg,.jpeg,.webp", "aria-label": "Image or GIF"});
  main.append(card("Device-ready media", hint("Uploads become opaque, full-frame 128×128 GIFs to avoid motion trails. Timing is preserved to GIF's 10 ms precision, with a 20 ms minimum. Maximum 8 MiB / 300 frames / 4 megapixels."),
    upload, button("Upload and normalize", async () => {
      if (!upload.files[0]) throw Error("Choose an image or GIF");
      notify("Converting image…");
      const file = upload.files[0];
      await request("media?name=" + encodeURIComponent(file.name), "POST", file);
      const update = await request("state"); state.media = update.media;
      render(); notify("Media ready");
    }, true),
    hint("Anything sent to a device is served over unauthenticated HTTP on the media port. Keep that port on trusted networks; never publish sensitive images or statistics to the Internet.")));
  const grid = node("div", {class: "grid"});
  state.media.forEach(item => {
    const image = node("img", {alt: item.label});
    grid.append(node("section", {class: "card media-item"}, image, node("h3", {}, item.label),
      hint(`${item.frames} frame(s) · ${(item.duration_ms / 1000).toFixed(2)} seconds`)));
    setTimeout(() => fillPreview(image, "media/" + item.id), 0);
  });
  main.append(grid);
}
function devicesPage(main) {
  config.devices.forEach(device => {
    device.night ??= {
      enabled: false, start: "22:00", end: "07:00", mode: "dim", brightness: 10, day_brightness: 70,
    };
    const token = node("input", {type: "password", autocomplete: "new-password", inputmode: "numeric",
      placeholder: device.local_token_set ? "Saved — leave untouched to keep" : "LocalToken from device settings"});
    token.addEventListener("input", () => {
      const value = token.value.trim();
      device.local_token = value === "" ? null : /^\d+$/.test(value) ? Number(value) : value;
      mark();
    });
    const hardware = field(device, "hardware", "Hardware", "select", {400: "400 · port 80", 402: "402 · port 9000"});
    hardware.querySelector("select").addEventListener("input", event => { device.hardware = Number(event.target.value); });
    main.append(card(device.name,
      field(device, "name", "Name"), field(device, "host", "Private IPv4 address"),
      hardware, node("label", {}, node("span", {}, "LocalToken (masked; saved unencrypted on disk)"), token),
      hint("Changing the IP or hardware clears the saved token unless you enter a replacement. Only configured screens are managed."),
      field(device, "widget_transport", "Generated widget transport", "select", {
        gif: "GIF URL download (default)", jpeg: "Direct JPEG upload (experimental)",
      }),
      field(device, "clock_renderer", "Clock rendering", "select", {
        image: "Image widget (uses selected transport)", native: "Native clock / date (experimental)",
      }),
      hint("Save configuration to persist. Transport applies to generated widgets only; uploaded images and animated GIFs always keep GIF URL playback. Native clocks take priority over widget transport."),
      hint("Native clocks require a downloadable background GIF at setup, use firmware font 2, and synchronize device-wide time/timezone. Enabled clock schedules become immediate setup plus hourly refreshes; sleep/pause still apply. Firmware compatibility and visible output need physical verification. Failed writes are reported without immediate retries or transport fallback."),
      button("Remove device", () => {
        if (config.devices.length === 1) throw Error("Keep at least one device");
        if (!confirm("Remove this device and its schedules?")) return;
        config.devices = config.devices.filter(item => item.id !== device.id); mark(); render();
      })));
    const night = device.night;
    const times = row(field(night, "start", "Night starts", "time"), field(night, "end", "Day starts", "time"));
    const mode = field(night, "mode", "Night action", "select", {dim: "Dim LCDs", sleep: "Sleep displays"});
    const brightness = row(
      numberField(night, "brightness", "Night LCD brightness (%)", 0, 100),
      numberField(night, "day_brightness", "Day LCD brightness (%)", 0, 100));
    const update = () => {
      for (const control of [...times.querySelectorAll("input"), mode.querySelector("select"),
        ...brightness.querySelectorAll("input")]) control.disabled = !night.enabled;
      brightness.querySelector("input").disabled = !night.enabled || night.mode === "sleep";
    };
    mode.querySelector("select").addEventListener("change", update);
    main.append(card(`${device.name} / Scheduled night mode`,
      field(night, "enabled", "Enable night schedule", "checkbox", null, () => { mark(); update(); }),
      hint(`Uses ${config.timezone}. Save configuration to activate at the next boundary; no immediate command is sent.`),
      times, mode, brightness,
      hint("Sleep keeps RGB separate and blocks screen pushes. In sleep mode, the day boundary wakes displays before setting day brightness. Dim mode never wakes manually sleeping displays. Missed boundaries coalesce to the current period; failed commands wait for the next daily boundary."),
      button("Open device controls", () => { selected = device.id; page = "Tools"; render(); })));
    update();
  });
  main.append(button("Add device", () => {
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
      value => value.toString(16).padStart(2, "0")).join("");
    config.devices.push({id, name: "Times Gate", host: "",
      hardware: 402, widget_transport: "gif", clock_renderer: "image",
      screens: Array.from({length: 5}, () => ({
        kind: "unmanaged", enabled: false, title: "", text: "", media_id: "", refresh_seconds: 60,
        schedule: "interval", daily_time: "08:00", strava_mode: "ytd", units: "metric",
      }))});
    mark(); render();
  }));
}
function toolsPage(main) {
  const selector = deviceSelector(config.devices, "Tools device");
  const device = config.devices.find(item => item.id === selector.value);
  if (!state.controls[device.id]) {
    main.append(hint("The device configuration changed. Reload before using tools."));
    return;
  }
  main.append(row(selector), deviceBar(device),
    node("div", {class: "banner warning"},
      "Experimental native firmware tools. Controls are not physically verified on hardware 402. Tools may replace all five screens; per-screen targeting is deliberately not offered. Screen updates stay paused, even after an uncertain command, until you restore the layout."));
  if (!controlDrafts.has(device.id)) controlDrafts.set(device.id, {
    hour24: true, celsius: true, mirror: false, ...state.controls[device.id].preferences,
    minutes: 25, seconds: 0, red: 0, blue: 0,
  });
  const draft = controlDrafts.get(device.id);
  const control = values => sendControl(device, values);
  const grid = node("div", {class: "grid"});
  grid.append(
    card("Native display preference draft",
      field(draft, "hour24", "24-hour native clock", "checkbox", null, () => {}),
      field(draft, "celsius", "Native temperature in Celsius", "checkbox", null, () => {}),
      field(draft, "mirror", "Mirror mode", "checkbox", null, () => {}),
      button("Apply native preferences", () => control({
        action: "preferences", hour24: draft.hour24, celsius: draft.celsius, mirror: draft.mirror,
      })),
      hint("Draft values, not live feedback. Native clock/unit preferences do not change this app's rendered widget settings. Three commands are sent sequentially; partial acceptance is reported in Activity.")),
    card("Synchronise native time",
      hint(`Uses the controller's current UTC time and ${state.config.timezone}, including its current DST offset, not the container's local timezone.`),
      button("Sync device time", () => control({action: "sync_time"})),
      hint("Experimental timezone format; verify the native clock. This is a manual synchronisation, not continuous DST correction.")),
    card("Native timer",
      row(numberField(draft, "minutes", "Timer minutes", 0, 999), numberField(draft, "seconds", "Timer seconds", 0, 59)),
      row(button("Start timer", () => control({action: "timer", minutes: draft.minutes, seconds: draft.seconds, running: true})),
        button("Stop timer", () => control({action: "timer", running: false})))),
    card("Native stopwatch",
      row(...["start", "stop", "reset"].map(value =>
        button(`${value[0].toUpperCase() + value.slice(1)} stopwatch`, () => control({action: "stopwatch", state: value}))))),
    card("Native scoreboard",
      row(numberField(draft, "red", "Red score", 0, 999), numberField(draft, "blue", "Blue score", 0, 999)),
      button("Show scoreboard", () => control({action: "scoreboard", red: draft.red, blue: draft.blue}))),
    card("Noise meter and sound",
      row(button("Start noise meter", () => control({action: "noise", on: true})),
        button("Stop noise meter", () => control({action: "noise", on: false}))),
      button("Play short buzzer", () => control({action: "buzzer"})),
      hint("The buzzer is a separate, explicitly requested sound. Stopping a native tool does not resume managed screens; use Restore layout.")));
  main.append(grid,
    node("details", {class: "card advanced-controls"}, node("summary", {}, "Advanced diagnostics and reboot"),
      hint("Read settings is a one-off query. A rejected/unsupported query is unavailable, not proof the device is offline. Raw responses and credentials are never displayed."),
      button("Read device settings", () => control({action: "read_settings"})),
      node("div", {"data-diagnostic": device.id}),
      button("Reboot device", () => control({action: "reboot"})),
      hint("Reboot requires confirmation and pauses screen updates. Wait for the device to return, then Restore layout. Device reboot does not trigger app-start RGB restoration.")));
  const saved = state.config.devices.find(item => item.id === device.id);
  if (!saved?.host || (saved.hardware === 402 && !saved.local_token_set)) {
    for (const control of main.querySelectorAll(".grid button, .advanced-controls button")) control.disabled = true;
  }
  updateDeviceStatus();
}
function updateLightingStatus() {
  const status = document.querySelector("#lighting-status");
  if (!status) return;
  const deviceId = status.dataset.device;
  const applied = state.lighting[deviceId];
  const job = state.jobs.find(item =>
    ["lighting", "lighting_restore"].includes(item.operation) && item.device_id === deviceId);
  const restoration = state.lighting_restoration.find(item => item.device_id === deviceId);
  const restore = document.querySelector("#restore-lighting-on-startup");
  if (!restoration) {
    status.replaceChildren(hint("This device was removed. Reload Lighting to choose another device."));
    restore.disabled = true;
    document.querySelector("#apply-lighting").disabled = true;
    return;
  }
  status.replaceChildren(
    node("div", {class: "status-chips"},
      node("span", {class: "pill"}, applied && Object.keys(state.lighting_defaults).every(
        key => lightingDrafts.get(deviceId)?.[key] === applied.settings[key])
        ? "Preview matches last acknowledged settings" : "Lighting preview draft / not applied"),
      node("span", {class: `pill${restoration.blocked ? " guard-status" : ""}`},
        restoration.blocked ? "Restoration on hold" : restoration.enabled ? "Restoration enabled" : "Restoration off")),
    hint(applied
      ? `Last applied: ${when(applied.applied_at)} · ${applied.settings.color.toUpperCase()} · ${applied.settings.brightness}%. Acknowledged, not verified device state.`
      : "No acknowledged lighting settings saved for this device connection."));
  if (job) status.append(hint(`Latest ${job.operation === "lighting_restore" ? "startup restoration" : "manual lighting"} job: ${job.status}${job.message ? " — " + job.message : ""}`));
  if (restoration.blocked) status.append(hint(
    "Automatic restoration is on hold after a failed or interrupted attempt. Apply lighting successfully again to approve a future startup attempt."));
  if (!restore.dataset.saving) {
    restore.checked = restoration.enabled;
    restore.disabled = !restoration.eligible;
  }
  const device = state.config.devices.find(item => item.id === deviceId);
  document.querySelector("#apply-lighting").disabled = !device?.host
    || (device.hardware === 402 && !device.local_token_set)
    || (job?.operation === "lighting" && ["pending", "running"].includes(job.status));
}
function lightingPage(main) {
  const device = state.config.devices.find(item => item.id === selected) || state.config.devices[0];
  if (!lightingDrafts.has(device.id)) {
    lightingDrafts.set(device.id, structuredClone(state.lighting[device.id]?.settings || state.lighting_defaults));
  }
  const settings = lightingDrafts.get(device.id);
  const selector = node("select", {"aria-label": "Lighting device"});
  for (const item of state.config.devices) selector.append(node("option", {value: item.id}, item.name));
  selector.value = device.id;
  selector.addEventListener("change", () => { selected = selector.value; render(); });
  main.append(row(selector), loadedProfileBanner(device),
    hint(`Uses the saved connection: ${device.host || "not configured"}. Save connection changes in Devices first. Apply lighting does not change screen content or schedules.`),
    hint("With a loaded profile, Save configuration saves its lighting preview together with screen and night settings. Lighting is sent only by Apply lighting."));

  const back = node("div", {class: "lighting-glow", "aria-hidden": "true"});
  const edgeLeft = node("div", {class: "lighting-edge lighting-edge-left", "aria-hidden": "true"});
  const edgeRight = node("div", {class: "lighting-edge lighting-edge-right", "aria-hidden": "true"});
  const panels = node("div", {class: "lighting-panels"});
  const images = [];
  device.screens.forEach((screen, panel) => {
    const image = screen.kind === "unmanaged" ? null
      : node("img", {alt: `Screen ${panel + 1} preview`, width: "128", height: "128"});
    panels.append(node("div", {class: "lighting-panel"},
      image || node("span", {}, "Native"),
      node("span", {class: "lighting-screen-label"}, String(panel + 1))));
    if (image) images.push([image, panel]);
  });
  const front = node("div", {class: "lighting-front"},
    edgeLeft, panels, edgeRight);
  const keys = node("div", {class: "lighting-keys", "aria-hidden": "true"});
  const preview = node("div", {
    class: "lighting-device",
    "aria-label": "Illustrative five-screen lighting preview with left and right edge lights and rear backlight",
  }, back, front, keys);
  const percentage = node("output", {"aria-live": "polite"}, `${settings.brightness}%`);
  const colourValue = node("span", {class: "muted"}, settings.color.toUpperCase());
  const draw = () => {
    const intensity = settings.on ? settings.brightness / 100 : 0;
    back.style.backgroundColor = settings.color;
    back.style.opacity = settings.zone === 1 ? "0" : String(intensity * 0.6);
    for (const edge of [edgeLeft, edgeRight]) {
      edge.style.setProperty("--edge-color", settings.color);
      edge.style.opacity = settings.zone !== 2 ? String(intensity) : "0";
    }
    keys.style.backgroundColor = settings.color;
    keys.style.opacity = settings.keys ? String(intensity) : "0";
    percentage.textContent = `${settings.brightness}%`;
    colourValue.textContent = settings.color.toUpperCase();
    updateLightingStatus();
    updateUnsavedConfiguration();
  };
  const colour = field(settings, "color", "Lighting colour", "color", null, draw);
  const brightness = node("input", {
    type: "range", min: "0", max: "100", step: "1", value: settings.brightness,
    "aria-label": "Lighting brightness",
    oninput: event => { settings.brightness = Number(event.target.value); draw(); },
  });
  const zones = node("div", {class: "lighting-zones", role: "group", "aria-label": "Lighting zone"});
  ["All zones", "Edges", "Backlight"].forEach((label, index) => {
    const control = button(label, () => { settings.zone = index; render(); });
    control.setAttribute("aria-pressed", String(settings.zone === index));
    zones.append(control);
  });
  main.append(card("Lighting preview", preview,
    hint("Illustrative preview—not live device feedback. Edges are the curved left/right light strips; Backlight is the glow behind the device. Images use saved screen settings. Colours and intensity are approximate; animations and secondary-zone behaviour must be checked on the Times Gate."),
    row(colour, colourValue), zones,
    node("label", {}, node("span", {}, "Brightness (global)"), row(brightness, percentage)),
    row(
      field(settings, "on", "Lighting power (global)", "checkbox", null, draw),
      field(settings, "cycle", "Colour cycling", "checkbox", null, draw),
      field(settings, "keys", "Key lighting", "checkbox", null, draw)),
    hint("Power and brightness affect lighting globally. A zone change can affect other zones. Steady edge colour depends on firmware; use Solid backlight for a chosen static colour.")));

  const presets = node("div", {class: "lighting-presets"});
  for (const preset of Object.values(state.widget_accents)) {
    const control = button(preset.name, () => { settings.color = preset.color; render(); });
    const swatch = node("span", {class: "lighting-swatch", "aria-hidden": "true"});
    swatch.style.backgroundColor = preset.color;
    control.prepend(swatch);
    presets.append(control);
  }
  const accents = node("div", {class: "row"});
  device.screens.forEach((screen, panel) => {
    if (!["clock", "text", "strava", "weather", "countdown", "rss"].includes(screen.kind)) return;
    accents.append(button(`Use screen ${panel + 1} accent`, () => {
      settings.color = screen.accent === "original"
        ? screen.kind === "strava" ? "#fc4c02" : "#65e0c8"
        : state.widget_accents[screen.accent].color;
      render();
    }));
  });
  main.append(card("Colour swatches (colour only)", presets, accents,
    hint("Presets change only the preview colour. Widget colours are unchanged.")));
  const fullPresets = [
    {name: "Desk", settings: {...state.lighting_defaults, color: "#36D9FF", brightness: 55}, builtin: true},
    {name: "Evening", settings: {...state.lighting_defaults, color: "#FFD166", brightness: 20}, builtin: true},
    {name: "Night", settings: {...state.lighting_defaults, color: "#FF8A4C", brightness: 5, keys: false}, builtin: true},
    ...state.lighting_presets,
  ];
  const presetCards = node("div", {class: "lighting-preset-cards"});
  for (const preset of fullPresets) {
    const swatch = node("span", {class: "lighting-swatch", "aria-hidden": "true"});
    swatch.style.backgroundColor = preset.settings.color;
    const choose = button(`Preview ${preset.name}`, () => {
      lightingDrafts.set(device.id, structuredClone(preset.settings)); render();
    });
    const effectName = (preset.settings.zone === 2 ? state.lighting_effects.back
      : state.lighting_effects.edges)[preset.settings.effect].name;
    const item = node("section", {class: "lighting-preset-card"},
      row(swatch, node("strong", {}, preset.name)),
      hint(`${preset.settings.brightness}% / ${["All zones", "Edges", "Backlight"][preset.settings.zone]} / ${effectName} / Power ${preset.settings.on ? "on" : "off"} / Cycle ${preset.settings.cycle ? "on" : "off"} / Keys ${preset.settings.keys ? "on" : "off"}`),
      choose);
    if (!preset.builtin) item.append(row(
      button(`Update ${preset.name}`, async () => {
        if (!confirm(`Replace "${preset.name}" with all current lighting preview settings?`)) return;
        const result = await request("lighting/presets", "PUT", {
          id: preset.id, name: preset.name, settings: structuredClone(settings),
        });
        state.lighting_presets = result.lighting_presets; render(); notify("Lighting preset updated; nothing sent.");
      }),
      button(`Delete ${preset.name}`, async () => {
        if (!confirm(`Delete lighting preset "${preset.name}"?`)) return;
        const result = await request(`lighting/presets/${preset.id}`, "DELETE");
        state.lighting_presets = result.lighting_presets; render(); notify("Lighting preset deleted.");
      })));
    else item.append(hint("Built-in / preview only"));
    presetCards.append(item);
  }
  const presetDraft = {name: ""};
  main.append(card("Full lighting presets", presetCards,
    hint("Includes colour, brightness, zone, effect, power, cycling and keys. Shared across devices. Selecting or saving never sends; use Apply lighting."),
    row(field(presetDraft, "name", "Preset name", "text", null, () => {}),
      button("Save new lighting preset", async () => {
        const result = await request("lighting/presets", "PUT", {
          name: presetDraft.name, settings: structuredClone(settings),
        });
        state.lighting_presets = result.lighting_presets; render(); notify("Full lighting preset saved; nothing sent.");
      }))));
  const effects = settings.zone === 2 ? state.lighting_effects.back : state.lighting_effects.edges;
  const effectCards = node("div", {class: "lighting-effects"});
  effects.forEach((effect, index) => {
    const control = button("", () => {
      settings.effect = index;
      if (index === 5) settings.cycle = false;
      render();
    });
    control.setAttribute("aria-pressed", String(settings.effect === index));
    control.append(node("strong", {}, effect.name), node("span", {class: "hint"}, effect.description));
    effectCards.append(control);
  });
  const apply = node("button", {
    id: "apply-lighting", type: "button", class: "primary",
    onclick: async event => {
      event.currentTarget.disabled = true;
      try {
        const result = await request("lighting", "POST", {device_id: device.id, settings: structuredClone(settings)});
        if (!state) return;
        state.jobs.unshift({
          id: result.job, operation: "lighting", device_id: device.id, panel: null,
          status: "pending", created: Date.now() / 1000, message: "",
        });
        notify(`Lighting job ${result.job.slice(0, 8)} queued. Check Activity for its result.`);
        const latest = await request("state");
        if (!state) return;
        state.jobs = latest.jobs; state.lighting = latest.lighting;
        state.lighting_restoration = latest.lighting_restoration;
      } catch (error) { notify(error.message, true); }
      finally { if (state) updateLightingStatus(); }
    },
  }, "Apply lighting");
  main.append(card(settings.zone === 2 ? "Backlight effects" : "Edge / all-zone effects",
    effectCards,
    row(button("Solid backlight", () => {
      settings.zone = 2; settings.effect = 5; settings.cycle = false; render();
    }), button("Reset preview", () => { lightingDrafts.delete(device.id); render(); }), apply),
    hint("Preview changes are sent only by Apply lighting. Only acknowledged settings are saved; demo jobs do not save applied settings. Startup restoration is separately opt-in below; uncertain writes are never retried automatically."),
    node("div", {id: "lighting-status", "data-device": device.id})));
  const restore = node("input", {
    id: "restore-lighting-on-startup", type: "checkbox",
    onchange: async event => {
      const input = event.currentTarget;
      input.dataset.saving = "true";
      input.disabled = true;
      try {
        const result = await request("lighting/restore", "PUT", {device_id: device.id, enabled: input.checked});
        if (!state) return;
        state.lighting = result.lighting;
        state.lighting_restoration = result.lighting_restoration;
        if (!input.checked) notify("Startup restoration disabled.");
        else if (state.lighting_restoration.some(item => item.device_id === device.id && item.blocked)) {
          notify("Startup restoration enabled, but on hold until a manual Apply succeeds.");
        } else notify("Startup restoration enabled for the next controller start.");
      } catch (error) { notify(error.message, true); }
      finally {
        delete input.dataset.saving;
        if (state) updateLightingStatus();
      }
    },
  });
  main.append(card("Automatic restoration",
    node("label", {}, restore, node("span", {}, "Restore lighting when the controller starts")),
    hint("Saved immediately; off by default. Uses the last acknowledged settings for this exact device connection, not this preview. Apply lighting successfully before enabling. No device reconnect polling is performed."),
    hint("One attempt per controller start. A failed or interrupted restoration requires a fresh successful manual Apply before another automatic attempt. Turning this off or applying manually cancels a waiting restoration; an already-sent command cannot be recalled.")));
  draw();
  updateLightingStatus();
  for (const [image, panel] of images) fillPreview(image, `preview/${device.id}/${panel}`);
}
function stravaPage(main) {
  const settings = {client_id: state.strava.client_id, poll_minutes: state.strava.poll_minutes};
  const secret = node("input", {type: "password", autocomplete: "new-password",
    placeholder: state.strava.client_secret_set ? "Saved — leave blank to keep" : "Client secret"});
  main.append(card("Strava connection",
    node("p", {}, state.strava.connected ? "Connected" : "Not connected"),
    field(settings, "client_id", "Strava client ID", "text", null, () => {}),
    node("label", {}, node("span", {}, "Client secret"), secret),
    field(settings, "poll_minutes", "Poll interval (minutes, minimum 15)", "number", null, () => {}),
    row(button("Save Strava settings", async () => {
      if (secret.value.trim()) settings.client_secret = secret.value.trim();
      await request("strava", "PUT", settings);
      state.strava = await request("state").then(result => result.strava);
      render(); notify("Strava settings saved");
    }), button("Authorize with Strava", async () => {
      const result = await request("strava/authorize", "POST");
      window.location.assign(result.url);
    }, true)),
    hint(`OAuth redirect URI: ${state.strava.redirect_uri}`),
    hint("Register the redirect hostname as your Strava Authorization Callback Domain. Your browser must reach this application after authorizing. Client settings must be saved before authorization.")));
  main.append(card("Cycling totals",
    hint("Year-to-date and all-time distance, rides, moving time and elevation. Polling is independent of screen updates; screens use cached data and show the data timestamp."),
    node("p", {}, "Last successful fetch: ", when(state.strava.fetched_at)),
    node("p", {}, "Next permitted poll: ", when(state.strava.next_poll)),
    row(button("Queue statistics refresh", async () => {
      await request("strava/refresh", "POST"); notify("Refresh queued. Check Activity; API polling limits still apply.");
    }), button("Disconnect locally", async () => {
      if (!confirm("Remove stored authorization and cached statistics? Revoke access at Strava separately if needed.")) return;
      await request("strava", "DELETE"); state.strava = (await request("state")).strava; render();
    }))));
}
function when(value) { return value ? new Date(value * 1000).toLocaleString() : "Not available"; }
function timeLabel(value) {
  if (value == null) return "—";
  const delta = value - Date.now() / 1000;
  const [unit, scale] = Math.abs(delta) < 60 ? ["second", 1]
    : Math.abs(delta) < 3600 ? ["minute", 60] : Math.abs(delta) < 86400 ? ["hour", 3600] : ["day", 86400];
  return node("time", {datetime: new Date(value * 1000).toISOString(), title: when(value)},
    new Intl.RelativeTimeFormat(undefined, {numeric: "auto", style: "short"}).format(Math.round(delta / scale), unit),
    node("span", {class: "absolute-time"}, when(value)));
}
function runDetails(run) {
  if (!run?.message) return "";
  if (!["error", "interrupted"].includes(run.status)) return run.message;
  return node("details", {
    ...(activityDetails.has(run.id) ? {open: ""} : {}),
    ontoggle: event => {
      if (!event.currentTarget.isConnected) return;
      if (event.currentTarget.open) activityDetails.add(run.id);
      else activityDetails.delete(run.id);
    },
  }, node("summary", {}, run.status === "error" ? "Error details" : "Interruption details"), hint(run.message));
}
function ragBadge(run, emptyLabel = "Awaiting first run") {
  const statuses = {
    acknowledged: ["green", "Acknowledged"],
    error: ["red", "Failed"],
    pending: ["amber", "Queued"],
    running: ["amber", "Running"],
    interrupted: ["amber", "Interrupted"],
    cancelled: ["amber", "Cancelled"],
    demo: ["amber", "Demo only"],
  };
  const [colour, label] = run
    ? statuses[run.status] || ["amber", run.status]
    : ["amber", emptyLabel];
  return node("span", {class: `rag-status rag-${colour}`},
    `${colour[0].toUpperCase() + colour.slice(1)} — ${label}`);
}
function scheduledUpdatesTable() {
  const restorationRows = state.lighting_restoration.filter(item =>
    !activityFailures || (item.enabled && item.eligible && item.last_run?.status === "error")).map(item => {
    const device = state.config.devices.find(device => device.id === item.device_id);
    if (!device) return null;
    const run = item.last_run;
    return node("tr", {class: "lighting-restoration-row"},
      node("td", {}, `${device.name} / Lighting restoration`),
      node("td", {}, "RGB lighting"),
      node("td", {}, item.enabled ? "Controller startup (one attempt)" : "Disabled"),
      node("td", {}, item.blocked && item.enabled ? "Manual Apply required"
        : item.enabled ? "Next controller startup" : "—"),
      node("td", {}, run ? timeLabel(run.finished ?? run.created) : "—"),
      node("td", {}, !item.eligible ? ragBadge(null, "Apply lighting first")
        : !item.enabled ? ragBadge(null, "Disabled") : ragBadge(run, "Awaiting controller startup")),
      node("td", {}, runDetails(run),
        item.blocked && item.enabled ? hint("Apply lighting successfully before automatic restoration can run again.")
          : run ? null : item.enabled ? "Restores last acknowledged settings; no reconnect polling."
            : "Enable in Lighting after a successful manual Apply."));
  }).filter(Boolean);
  const groups = [];
  for (const device of state.config.devices) {
    const rows = [];
    for (const item of state.device_schedules.filter(item => item.device_id === device.id)) {
      if (activityFailures && item.last_run?.status !== "error") continue;
      const start = item.phase === "start";
      rows.push(node("tr", {},
        node("td", {}, `${device.name} / ${start ? "Night starts" : "Day starts"}`),
        node("td", {}, device.night.mode === "sleep" ? "Display sleep / wake" : "LCD dimming"),
        node("td", {}, `Daily at ${start ? device.night.start : device.night.end} (${state.config.timezone})`),
        node("td", {}, timeLabel(item.next_due)),
        node("td", {}, item.last_run ? timeLabel(item.last_run.finished ?? item.last_run.created) : "—"),
        node("td", {}, ragBadge(item.last_run)),
        node("td", {}, runDetails(item.last_run))));
    }
    for (const item of state.schedules.filter(item => item.device_id === device.id)) {
      if (activityFailures && item.last_run?.status !== "error") continue;
      const screen = device.screens[item.panel];
      const schedule = screenTransport(device, screen) === "native"
        ? "Hourly native clock setup / time sync"
        : screen.schedule === "daily"
        ? `Daily at ${screen.daily_time} (${state.config.timezone})`
        : screen.schedule === "on_time_change" ? "Every minute (on time change)"
          : `Every ${screen.refresh_seconds} seconds`;
      const runtime = state.controls[device.id];
      rows.push(node("tr", {},
        node("td", {}, `${device.name} / Screen ${item.panel + 1}`,
          screen.title ? node("div", {class: "muted"}, screen.title) : null),
        node("td", {}, widgetNames[screen.kind]),
        node("td", {}, schedule),
        node("td", {}, runtime?.sleeping || runtime?.paused ? "Held until wake / resume" : timeLabel(item.next_due)),
        node("td", {}, item.last_run ? timeLabel(item.last_run.finished ?? item.last_run.created) : "—"),
        node("td", {}, ragBadge(item.last_run)),
        node("td", {}, runDetails(item.last_run))));
    }
    if (rows.length) groups.push(node("tbody", {},
      node("tr", {class: "activity-group"}, node("th", {colspan: "7", scope: "rowgroup"}, device.name)), ...rows));
  }
  if (!restorationRows.length && !groups.length) return hint(
    activityFailures ? "No failed automatic runs." : "No automatic updates are configured.");
  return node("div", {class: "table-wrap"}, node("table", {},
    node("thead", {}, node("tr", {}, ...[
      "Device / screen", "Widget / task", "Schedule / interval", "Next update",
      "Last automatic run", "Status", "Details",
    ].map(text => node("th", {scope: "col"}, text)))),
    node("tbody", {}, ...restorationRows), ...groups));
}
function jobAction(job) {
  if (job.operation === "control") return `Device control / ${job.action.replaceAll("_", " ")}`;
  if (job.operation === "device_schedule") return `Night schedule / ${job.action.replace("night_", "")}`;
  return {lighting: "Manual lighting", lighting_restore: "Startup restoration", send: "Screen send", strava: "Strava refresh"}[job.operation] || job.operation;
}
function jobsTable() {
  const jobs = state.jobs.filter(job => !activityFailures || job.status === "error");
  if (!jobs.length) return hint(activityFailures ? "No failures in recent activity." : "No recent activity.");
  const groups = new Map();
  for (const job of jobs) {
    const label = job.device_id
      ? state.config.devices.find(device => device.id === job.device_id)?.name || "Removed device" : "Controller";
    const key = job.device_id || "controller";
    if (!groups.has(key)) groups.set(key, {label, jobs: []});
    groups.get(key).jobs.push(job);
  }
  return node("div", {class: "table-wrap"}, node("table", {},
    node("thead", {}, node("tr", {}, ...["Time", "Action", "Screen", "Status", "Details"].map(text => node("th", {}, text)))),
    ...[...groups.values()].map(group => node("tbody", {},
      node("tr", {class: "activity-group"}, node("th", {colspan: "5", scope: "rowgroup"}, group.label)),
      ...group.jobs.map(job => node("tr", {},
        node("td", {}, timeLabel(job.created)), node("td", {}, jobAction(job)),
        node("td", {}, job.operation === "send" && job.panel != null ? String(job.panel + 1) : "—"),
        node("td", {}, job.status === "done" ? node("span", {class: "pill"}, "Done (controller)") : ragBadge(job)),
        node("td", {}, runDetails(job))))))));
}
function activityPage(main) {
  const filter = {failures: activityFailures};
  main.append(field(filter, "failures", "Failures only (red results)", "checkbox", null, () => {
    activityFailures = filter.failures;
    document.querySelector("#scheduled-updates").replaceChildren(scheduledUpdatesTable());
    document.querySelector("#job-list").replaceChildren(jobsTable());
  }), card("Scheduled updates",
    hint("RGB startup restoration is listed first, then night and screen schedules grouped by device. Green means acknowledged, not verified physical effect. Amber includes disabled, pending, interrupted and demo-only states. Red means the latest automatic run failed. Manual sends do not count as automatic runs. Times include countdowns and absolute timestamps."),
    node("div", {id: "scheduled-updates"}, scheduledUpdatesTable())),
    card("Recent activity",
      hint("Updates every three seconds. Failed device writes are not retried automatically. Acknowledged means the command was accepted—not that download or display completed."),
      node("div", {id: "job-list"}, jobsTable())));
}
function settingsPage(main) {
  main.append(card("Scheduling", field(config, "timezone", "IANA timezone (for clocks and daily schedules)"),
    hint("One background worker owns device I/O. Schedules survive restarts. On recovery, one latest update is queued per overdue screen; old manual jobs are marked interrupted, not replayed."),
    hint("Failed automatic pushes are reported and tried again only at the next scheduled interval. Generated media is retained for 24 hours; uploaded media persists.")),
    card("Deployment", hint(`Running version: ${state.version}`),
      hint(`Device media origin: ${state.media_base_url || "Not configured"}`),
      hint("Keep the media port reachable by the device over HTTP without redirects. Restrict administration to your LAN or an authenticated HTTPS reverse proxy."),
      hint("Settings and tokens are stored unencrypted in the private SQLite data directory. Back up the data and media directories together while the services are stopped."),
      button("Sign out", async () => {
        if ((dirty || lightingDrafts.size) && !confirm("Sign out and discard unsaved changes?")) return;
        await request("auth/logout", "POST"); login();
      })));
}
setInterval(async () => {
  if (!state || polling) return;
  polling = true;
  try {
    const latest = await request("state");
    if (!state) return;
    applyStatus(latest);
  } catch (error) {
    statusStale = true;
    updateGlobalDeviceStatus();
    notify("Status refresh failed: " + error.message, true);
  }
  finally { polling = false; }
}, 3000);
try {
  const savedTheme = localStorage.getItem("timesgate-theme");
  if (savedTheme !== null) {
    if (Object.hasOwn(themes, savedTheme)) document.documentElement.dataset.theme = savedTheme;
    else notify("Saved theme is not supported; using Graphite.", true);
  }
} catch (error) {
  notify("Your theme preference could not be loaded: " + error.message, true);
}
await start();
