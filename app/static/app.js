(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const PAGE_META = {
    overview: ["Overview", "Server state and recent activity"],
    console: ["Console", "Live keyboard, video and mouse"],
    power: ["Power", "Power state and next boot"],
    logs: ["Event log", "System event log"],
    settings: ["Settings", "Connection and maintenance"],
  };

  const state = {
    route: "overview",
    status: null,
    sensors: [],
    history: [],
    captured: false,
    mods: 0,
    keys: new Set(),
    buttons: 0,
    lastMouseSend: 0,
    statusFailures: 0,
    polling: {},
  };

  // ------------------------------------------------------------------ utils

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) {
      const message = data && data.error ? data.error : `HTTP ${response.status}`;
      throw new Error(message);
    }
    return data;
  }

  function toast(message, kind = "") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = message;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), 4600);
  }

  function confirmAction(title, text, okLabel = "Confirm", danger = true) {
    return new Promise((resolve) => {
      const modal = $("#modal");
      $("#modal-title").textContent = title;
      $("#modal-text").textContent = text;
      const ok = $("#modal-ok");
      ok.textContent = okLabel;
      ok.className = danger ? "btn btn-danger" : "btn btn-primary";
      modal.hidden = false;
      const close = (value) => {
        modal.hidden = true;
        ok.removeEventListener("click", onOk);
        $("#modal-cancel").removeEventListener("click", onCancel);
        resolve(value);
      };
      const onOk = () => close(true);
      const onCancel = () => close(false);
      ok.addEventListener("click", onOk);
      $("#modal-cancel").addEventListener("click", onCancel);
      ok.focus();
    });
  }

  function fmtTemperature(value) {
    if (value == null || Number.isNaN(Number(value))) return "—";
    return `${Math.round(Number(value))}`;
  }

  function shortTime(value) {
    if (!value) return "—";
    return String(value).replace("T", " ").slice(0, 16);
  }

  function severityClass(value) {
    const text = String(value || "").toLowerCase();
    if (/critical|error|fail|non-fatal|fatal/.test(text)) return "bad";
    if (/warn|minor|major/.test(text)) return "warn";
    return "ok";
  }

  function severityLabel(value) {
    const text = String(value || "").trim();
    return text || "Info";
  }

  // ------------------------------------------------------------------ router

  function navigate(route) {
    if (!PAGE_META[route]) route = "overview";
    if (state.route === "console" && route !== "console") releaseCapture();
    state.route = route;
    $$(".nav-item").forEach((item) => {
      item.classList.toggle("is-active", item.dataset.route === route);
    });
    $$(".page").forEach((page) => {
      page.classList.toggle("is-active", page.id === `page-${route}`);
    });
    $("#page-title").textContent = PAGE_META[route][0];
    $("#page-sub").textContent = PAGE_META[route][1];
    if (location.hash !== `#/${route}`) history.replaceState(null, "", `#/${route}`);
    if (route === "logs") loadSel();
    if (route === "settings") loadConfig();
    if (route === "overview" || route === "console") {
      pollStatus();
      pollSensors();
    }
  }

  $$(".nav-item").forEach((item) => {
    item.addEventListener("click", () => navigate(item.dataset.route));
  });
  $$("[data-nav]").forEach((item) => {
    item.addEventListener("click", () => navigate(item.dataset.nav));
  });
  window.addEventListener("hashchange", () => {
    navigate(location.hash.replace(/^#\//, "") || "overview");
  });

  // ------------------------------------------------------------------ status

  function setDot(selector, kind) {
    const el = $(selector);
    if (el) el.className = `dot ${kind}`;
  }

  function setText(selector, text) {
    const el = $(selector);
    if (el) el.textContent = text;
  }

  async function pollStatus() {
    if (state.polling.status) return;
    state.polling.status = true;
    try {
      const status = await api("/api/status");
      state.status = status;
      state.statusFailures = 0;

      const power = String(status.power || "unknown");
      const powerOk = power === "on";
      setText("#power-state", powerOk ? "On" : power);
      setText("#rail-power", powerOk ? "On" : power);
      setText("#power-big", powerOk ? "On" : power);
      setDot("#power-dot", powerOk ? "ok" : power.startsWith("error") ? "bad" : "warn");

      const kvm = status.kvm || {};
      const kvmState = kvm.connected ? "Connected" : (kvm.error ? "Error" : "Connecting");
      setText("#rail-kvm", kvmState);
      setText("#rail-bmc", status.bmc ? status.bmc.firmware : "—");
      setText("#rail-link", kvm.connected ? "BMC online" : (kvm.error ? "BMC error" : "Connecting"));
      setDot("#rail-dot", kvm.connected ? "ok" : (kvm.error ? "bad" : "warn"));

      setText("#server-meta", `${status.host}  ·  BMC ${status.bmc ? status.bmc.firmware : "—"}  ·  ${status.mac}`);
      setText("#console-res", kvm.width ? `${kvm.width}×${kvm.height}` : "—");
      setText("#console-fps", kvm.fps ? `${kvm.fps} fps` : "0 fps");
      setText("#console-age", kvm.last_frame_age == null ? "no frames"
        : kvm.last_frame_age < 1.5 ? "live" : `${Math.round(kvm.last_frame_age)}s ago`);
      setText("#console-session", kvm.connected ? "connected" : (kvm.error || "disconnected"));
      const decoderReady = (kvm.seq > 0) || (status.decoder && status.decoder.running);
      setText("#console-decoder", decoderReady ? "decoder ready" : "decoder idle");
      setText("#side-host", status.host);
      setText("#side-user", status.user);
      setText("#side-stream", kvm.width ? `${kvm.width}×${kvm.height}` : "no signal");
      setText("#side-age", kvm.last_frame_age == null ? "—" : `${kvm.last_frame_age}s`);
      setText("#side-kbd", state.captured ? "captured" : "released");
      setText("#about-ipmi", "IPMI 2.0 over LAN");
      setText("#about-bmc", status.bmc ? status.bmc.firmware : "—");
      setText("#password-state", status.password_set
        ? "Password loaded from environment" : "Password is not set");

      const powerKnown = power === "on" || power === "off";
      $$("[data-power]").forEach((button) => {
        const action = button.dataset.power;
        if (!powerKnown) {
          button.disabled = false;
          return;
        }
        button.disabled = (action === "on" && power === "on")
          || (action !== "on" && power === "off");
      });
    } catch (error) {
      state.statusFailures += 1;
      setDot("#rail-dot", "bad");
      setText("#rail-link", "Panel offline");
      setText("#console-session", "backend unreachable");
      if (state.statusFailures === 3) toast("Backend is not responding", "bad");
    } finally {
      state.polling.status = false;
    }
  }

  // ------------------------------------------------------------------ sensors

  function sensorByName(name) {
    return state.sensors.find((sensor) => sensor.name === name) || null;
  }

  function tempSensor(name) {
    const sensor = sensorByName(name);
    if (!sensor || sensor.value == null) return null;
    return Number(sensor.value);
  }

  async function pollSensors() {
    if (state.polling.sensors) return;
    state.polling.sensors = true;
    try {
      const data = await api("/api/sensors");
      state.sensors = data.sensors || [];
      const cpu1 = tempSensor("CPU1 Temp");
      const cpu2 = tempSensor("CPU2 Temp");
      const mb = tempSensor("MB Temp");
      const fans = state.sensors.filter((sensor) => sensor.type === "Fan" && sensor.value != null);

      $("#stat-cpu1").innerHTML = `${fmtTemperature(cpu1)}<span class="unit">°C</span>`;
      $("#stat-cpu2").innerHTML = `${fmtTemperature(cpu2)}<span class="unit">°C</span>`;
      $("#stat-mb").innerHTML = `${fmtTemperature(mb)}<span class="unit">°C</span>`;
      $("#stat-cpu1-sub").textContent = sensorByName("CPU1 Temp") ? "CPU1 Temp" : "Not present";
      $("#stat-cpu2-sub").textContent = sensorByName("CPU2 Temp") ? "CPU2 Temp" : "Not present";
      $("#stat-mb-sub").textContent = sensorByName("MB Temp") ? "MB Temp" : "Not present";

      if (fans.length) {
        const values = fans.map((fan) => Number(fan.value));
        const average = values.reduce((sum, value) => sum + value, 0) / values.length;
        $("#stat-fans").innerHTML = `${Math.round(average)}<span class="unit">RPM</span>`;
        $("#stat-fans-sub").textContent = `${fans.length} fans · max ${Math.round(Math.max(...values))} RPM`;
        renderFans(fans);
      } else {
        $("#stat-fans").textContent = "—";
        $("#stat-fans-sub").textContent = "No fan sensors";
        $("#fan-list").innerHTML = '<div class="empty">No fan sensors</div>';
      }

      state.history.push({ t: Date.now(), cpu1, cpu2, mb });
      if (state.history.length > 120) state.history.shift();
      drawChart();
    } catch (error) {
      // keep the last values, the status poll reports the connection state
    } finally {
      state.polling.sensors = false;
    }
  }

  function renderFans(fans) {
    const list = $("#fan-list");
    const rows = fans.slice().sort((a, b) => a.name.localeCompare(b.name));
    const max = Math.max(...rows.map((fan) => Number(fan.value)), 1);
    const top = rows.reduce((best, fan) => Number(fan.value) > Number(best.value) ? fan : best, rows[0]);
    list.innerHTML = rows.map((fan) => {
      const width = Math.max(4, Math.round((Number(fan.value) / max) * 100));
      const isTop = fan === top;
      return `<div class="fan-row ${isTop ? "is-top" : ""}">
        <span class="fan-name">${escapeHtml(fan.name)}</span>
        <span class="fan-track"><span class="fan-fill" style="width:${width}%"></span></span>
        <span class="fan-value">${Math.round(Number(fan.value))}</span>
      </div>`;
    }).join("");
  }

  // ------------------------------------------------------------------ chart

  function drawChart() {
    const canvas = $("#thermal-chart");
    if (!canvas || !canvas.clientWidth) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = canvas.clientWidth;
    const height = 224;
    if (canvas.width !== Math.round(width * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const left = 34;
    const right = 10;
    const top = 10;
    const bottom = 22;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const data = state.history.filter((sample) => sample.cpu1 != null || sample.cpu2 != null || sample.mb != null);
    const values = [];
    data.forEach((sample) => {
      ["cpu1", "cpu2", "mb"].forEach((key) => {
        if (sample[key] != null) values.push(sample[key]);
      });
    });
    const maxValue = values.length ? Math.max(...values) : 60;
    const yMax = Math.max(60, Math.ceil((maxValue + 5) / 20) * 20);

    ctx.font = '10px "Cascadia Mono", Consolas, monospace';
    ctx.fillStyle = "#68717a";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 3; i += 1) {
      const value = (yMax / 3) * i;
      const y = top + plotHeight - (value / yMax) * plotHeight;
      ctx.strokeStyle = "rgba(255,255,255,.06)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, y + 0.5);
      ctx.lineTo(width - right, y + 0.5);
      ctx.stroke();
      ctx.fillText(`${Math.round(value)}`, left - 8, y);
    }

    if (data.length < 2) {
      ctx.textAlign = "center";
      ctx.fillStyle = "#68717a";
      ctx.fillText("Waiting for samples", left + plotWidth / 2, top + plotHeight / 2);
      return;
    }

    const plot = (key, color, lineWidth) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = lineWidth;
      ctx.beginPath();
      let started = false;
      data.forEach((sample, index) => {
        const value = sample[key];
        if (value == null) return;
        const x = left + (index / (data.length - 1)) * plotWidth;
        const y = top + plotHeight - (value / yMax) * plotHeight;
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      ctx.stroke();
    };

    plot("mb", "#4f5962", 1.2);
    plot("cpu2", "#88929b", 1.2);
    plot("cpu1", "#dda43f", 1.6);

    const last = data[data.length - 1];
    if (last.cpu1 != null) {
      const x = width - right;
      const y = top + plotHeight - (last.cpu1 / yMax) * plotHeight;
      ctx.fillStyle = "#dda43f";
      ctx.beginPath();
      ctx.arc(x - 1, y, 2, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.textAlign = "right";
    ctx.fillStyle = "#68717a";
    ctx.fillText("now", width - right, height - 6);
    ctx.textAlign = "left";
    ctx.fillText("4 min", left, height - 6);
  }

  window.addEventListener("resize", () => {
    if (state.route === "overview") drawChart();
  });

  // ------------------------------------------------------------------ events

  async function loadSel() {
    try {
      const data = await api("/api/sel");
      state.sel = data.events || [];
      renderEvents(state.sel);
    } catch (error) {
      renderEvents([]);
    }
  }

  function renderEvents(events) {
    const rows = (events || []).map((event) => {
      const kind = severityClass(event.severity);
      return `<tr>
        <td class="col-time"><span class="time">${escapeHtml(shortTime(event.timestamp))}</span></td>
        <td class="col-sev"><span class="sev ${kind}"><span class="dot"></span>${escapeHtml(severityLabel(event.severity))}</span></td>
        <td>${escapeHtml(event.description || "—")}</td>
      </tr>`;
    }).join("");

    const overview = $("#overview-events");
    if (overview) {
      const slice = (events || []).slice(0, 8);
      overview.innerHTML = slice.length ? slice.map((event) => {
        const kind = severityClass(event.severity);
        return `<tr>
          <td class="col-time"><span class="time">${escapeHtml(shortTime(event.timestamp))}</span></td>
          <td class="col-sev"><span class="sev ${kind}"><span class="dot"></span>${escapeHtml(severityLabel(event.severity))}</span></td>
          <td>${escapeHtml(event.description || "—")}</td>
        </tr>`;
      }).join("") : '<tr><td colspan="3" class="empty">No events</td></tr>';
    }

    const body = $("#sel-body");
    if (body) {
      body.innerHTML = rows || '<tr><td colspan="3" class="empty">No events</td></tr>';
    }
  }

  // ------------------------------------------------------------------ console

  const MODMAP = {
    ControlLeft: 1, ShiftLeft: 2, AltLeft: 4, MetaLeft: 8,
    ControlRight: 16, ShiftRight: 32, AltRight: 64, MetaRight: 128,
  };

  const KEYMAP = {
    KeyA: 4, KeyB: 5, KeyC: 6, KeyD: 7, KeyE: 8, KeyF: 9, KeyG: 10, KeyH: 11,
    KeyI: 12, KeyJ: 13, KeyK: 14, KeyL: 15, KeyM: 16, KeyN: 17, KeyO: 18,
    KeyP: 19, KeyQ: 20, KeyR: 21, KeyS: 22, KeyT: 23, KeyU: 24, KeyV: 25,
    KeyW: 26, KeyX: 27, KeyY: 28, KeyZ: 29,
    Digit1: 30, Digit2: 31, Digit3: 32, Digit4: 33, Digit5: 34, Digit6: 35,
    Digit7: 36, Digit8: 37, Digit9: 38, Digit0: 39,
    Enter: 40, Escape: 41, Backspace: 42, Tab: 43, Space: 44, Minus: 45,
    Equal: 46, BracketLeft: 47, BracketRight: 48, Backslash: 49,
    Semicolon: 51, Quote: 52, Backquote: 53, Comma: 54, Period: 55, Slash: 56,
    CapsLock: 57, F1: 58, F2: 59, F3: 60, F4: 61, F5: 62, F6: 63,
    F7: 64, F8: 65, F9: 66, F10: 67, F11: 68, F12: 69,
    PrintScreen: 70, ScrollLock: 71, Pause: 72, Insert: 73, Home: 74,
    PageUp: 75, Delete: 76, End: 77, PageDown: 78,
    ArrowRight: 79, ArrowLeft: 80, ArrowDown: 81, ArrowUp: 82,
    NumLock: 83, NumpadDivide: 84, NumpadMultiply: 85, NumpadSubtract: 86,
    NumpadAdd: 87, NumpadEnter: 88, Numpad1: 89, Numpad2: 90, Numpad3: 91,
    Numpad4: 92, Numpad5: 93, Numpad6: 94, Numpad7: 95, Numpad8: 96,
    Numpad9: 97, Numpad0: 98, NumpadDecimal: 99, IntlBackslash: 100,
    ContextMenu: 101, IntlRo: 135, IntlYen: 137,
  };

  function captureKeyboard() {
    if (state.route !== "console") return;
    state.captured = true;
    const hint = $("#screen-hint");
    if (hint) hint.hidden = true;
    const badge = $("#capture-badge");
    if (badge) badge.hidden = false;
    const wrap = $("#screen-wrap");
    if (wrap) wrap.focus();
    setText("#side-kbd", "captured");
  }

  function releaseCapture() {
    if (!state.captured) return;
    state.captured = false;
    state.mods = 0;
    state.keys.clear();
    sendKeyReport();
    const hint = $("#screen-hint");
    if (hint) hint.hidden = false;
    const badge = $("#capture-badge");
    if (badge) badge.hidden = true;
    setText("#side-kbd", "released");
  }

  function sendKeyReport() {
    api("/api/kvm/key", {
      method: "POST",
      body: JSON.stringify({ mods: state.mods, keys: Array.from(state.keys) }),
    }).catch(() => setText("#console-session", "input failed"));
  }

  document.addEventListener("keydown", (event) => {
    if (state.route !== "console" || !state.captured) return;
    if (event.code === "Escape" && !event.ctrlKey && !event.altKey && !event.metaKey) {
      event.preventDefault();
      releaseCapture();
      return;
    }
    if (event.repeat) {
      event.preventDefault();
      return;
    }
    if (MODMAP[event.code]) {
      state.mods |= MODMAP[event.code];
    } else if (KEYMAP[event.code]) {
      state.keys.add(KEYMAP[event.code]);
    } else {
      return;
    }
    event.preventDefault();
    sendKeyReport();
  });

  document.addEventListener("keyup", (event) => {
    if (state.route !== "console" || !state.captured) return;
    if (MODMAP[event.code]) {
      state.mods &= ~MODMAP[event.code];
    } else if (KEYMAP[event.code]) {
      state.keys.delete(KEYMAP[event.code]);
    } else {
      return;
    }
    event.preventDefault();
    sendKeyReport();
  });

  window.addEventListener("blur", () => {
    if (!state.captured) return;
    state.mods = 0;
    state.keys.clear();
    sendKeyReport();
  });

  function setupConsole() {
    const screen = $("#screen");
    const wrap = $("#screen-wrap");
    if (!screen || !wrap) return;
    wrap.addEventListener("click", (event) => {
      if (event.target.closest(".btn")) return;
      captureKeyboard();
    });

    const sendMouse = (event, buttons) => {
      const rect = screen.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const x = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
      const y = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
      const now = performance.now();
      if (buttons === state.buttons && now - state.lastMouseSend < 32) return;
      state.lastMouseSend = now;
      api("/api/kvm/mouse", {
        method: "POST",
        body: JSON.stringify({ mode: "abs", buttons, x, y }),
      }).catch(() => {});
    };

    screen.addEventListener("mousemove", (event) => sendMouse(event, state.buttons));
    screen.addEventListener("mousedown", (event) => {
      event.preventDefault();
      const bit = event.button === 0 ? 1 : event.button === 2 ? 2 : event.button === 1 ? 4 : 0;
      state.buttons |= bit;
      sendMouse(event, state.buttons);
    });
    screen.addEventListener("mouseup", (event) => {
      event.preventDefault();
      const bit = event.button === 0 ? 1 : event.button === 2 ? 2 : event.button === 1 ? 4 : 0;
      state.buttons &= ~bit;
      sendMouse(event, state.buttons);
    });
    screen.addEventListener("contextmenu", (event) => event.preventDefault());
    screen.addEventListener("wheel", (event) => {
      event.preventDefault();
      api("/api/kvm/mouse", {
        method: "POST",
        body: JSON.stringify({ mode: "rel", dx: 0, dy: 0, wheel: event.deltaY < 0 ? 1 : -1 }),
      }).catch(() => {});
    }, { passive: false });

    $("#btn-cad").addEventListener("click", sendCad);
    $("#btn-cad-2").addEventListener("click", sendCad);
    $("#btn-shot").addEventListener("click", takeScreenshot);
    $("#btn-fullscreen").addEventListener("click", toggleFullscreen);
  }

  function sendCad() {
    api("/api/kvm/cad", { method: "POST" }).then(() => toast("Sent Ctrl Alt Del", "ok"))
      .catch((error) => toast(error.message, "bad"));
  }

  async function takeScreenshot() {
    try {
      const result = await api("/api/kvm/screenshot", { method: "POST" });
      toast("Screenshot saved", "ok");
      const link = document.createElement("a");
      link.href = result.url;
      link.target = "_blank";
      link.rel = "noopener";
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  function toggleFullscreen() {
    const card = $(".console-card");
    if (!card) return;
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      card.requestFullscreen().catch(() => toast("Fullscreen is not available", "bad"));
    }
  }

  // ------------------------------------------------------------------ power

  const POWER_TEXT = {
    off: ["Force off", "This cuts power immediately. Unsaved work on the host is lost."],
    reset: ["Reset", "This reboots the host immediately without a graceful shutdown."],
    cycle: ["Power cycle", "This powers the host off, waits, then powers it on."],
    shutdown: ["Graceful shutdown", "The OS is asked to shut down, then the host powers off."],
  };

  $$("[data-power]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.power;
      if (POWER_TEXT[action]) {
        const [title, text] = POWER_TEXT[action];
        const ok = await confirmAction(title, text, title);
        if (!ok) return;
      }
      button.disabled = true;
      try {
        const result = await api("/api/power", {
          method: "POST",
          body: JSON.stringify({ action }),
        });
        toast(`Power: ${result.power}`, "ok");
        pollStatus();
      } catch (error) {
        toast(error.message, "bad");
      } finally {
        button.disabled = false;
      }
    });
  });

  $("#boot-apply").addEventListener("click", async () => {
    try {
      const device = $("#boot-device").value;
      await api("/api/boot", { method: "POST", body: JSON.stringify({ device }) });
      toast("Boot device set for next reboot", "ok");
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  $("#btn-sel-refresh").addEventListener("click", loadSel);

  // ------------------------------------------------------------------ settings

  async function loadConfig() {
    try {
      const data = await api("/api/config");
      const config = data.config || {};
      $("#cfg-host").value = config.host || "";
      $("#cfg-user").value = config.user || "";
      $("#cfg-mac").value = config.mac || "";
      $("#cfg-quality").value = String(config.quality == null ? 3 : config.quality);
      $("#cfg-password").value = "";
      $("#password-state").textContent = data.password_set
        ? "Password loaded from environment" : "Password is not set";
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      host: $("#cfg-host").value.trim(),
      user: $("#cfg-user").value.trim(),
      mac: $("#cfg-mac").value.trim(),
      quality: Number($("#cfg-quality").value),
    };
    const password = $("#cfg-password").value;
    if (password) payload.password = password;
    try {
      await api("/api/config", { method: "POST", body: JSON.stringify(payload) });
      $("#cfg-password").value = "";
      toast("Saved. Reconnecting the console", "ok");
      pollStatus();
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  $("#btn-kvm-reconnect").addEventListener("click", async () => {
    try {
      await api("/api/reconnect", { method: "POST" });
      toast("Console reconnect requested", "ok");
      pollStatus();
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  $("#btn-bmc-reset").addEventListener("click", async () => {
    const ok = await confirmAction(
      "Cold reset BMC",
      "The BMC restarts and the console goes offline for about 90 seconds. The host keeps running.",
      "Reset BMC",
    );
    if (!ok) return;
    try {
      await api("/api/bmc/reset", { method: "POST" });
      toast("BMC reset. Waiting for it to come back", "ok");
      setTimeout(pollStatus, 5000);
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  // ------------------------------------------------------------------ init

  $("#btn-refresh-top").addEventListener("click", () => {
    pollStatus();
    pollSensors();
    loadSel();
    toast("Refreshed");
  });

  setupConsole();

  const initial = location.hash.replace(/^#\//, "") || "overview";
  navigate(initial);
  pollStatus();
  pollSensors();
  loadSel();

  setInterval(pollStatus, 2000);
  setInterval(pollSensors, 5000);
  setInterval(() => {
    if (state.route === "logs" || state.route === "overview") loadSel();
  }, 10000);
})();
