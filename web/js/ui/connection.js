// Connection bar: port/baud selection, connect/disconnect, server link state.

import { api } from "../api.js";
import { $, showBanner } from "./dom.js";

const LAST_PORT = "gcs-last-port";
const LAST_BAUD = "gcs-last-baud";

export function initConnection(store, log) {
  const portSel = $("#conn-port");
  const baudSel = $("#conn-baud");
  const toggle = $("#conn-toggle");
  const info = $("#conn-info");
  const link = $("#link");
  const linkText = $("#link-text");

  const savedBaud = localStorage.getItem(LAST_BAUD);
  if (savedBaud) baudSel.value = savedBaud;

  async function refreshPorts() {
    try {
      const { ports } = await api.ports();
      const current = portSel.value || store.state.connection.port || localStorage.getItem(LAST_PORT);
      portSel.replaceChildren(
        ...ports.map((p) => {
          const o = document.createElement("option");
          o.value = p.device;
          o.textContent = p.description && p.description !== "n/a" ? `${p.device} — ${p.description}` : p.device;
          return o;
        }),
      );
      if (!ports.length) {
        const o = document.createElement("option");
        o.value = "";
        o.textContent = "no serial ports found";
        portSel.append(o);
      }
      if (current && ports.some((p) => p.device === current)) portSel.value = current;
    } catch (e) {
      log(`cannot list ports: ${e.message}`);
    }
  }

  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    try {
      if (store.state.connection.state === "disconnected") {
        const port = portSel.value;
        if (!port) throw new Error("choose a port first");
        localStorage.setItem(LAST_PORT, port);
        localStorage.setItem(LAST_BAUD, baudSel.value);
        await api.connect(port, Number(baudSel.value));
      } else {
        await api.disconnect();
      }
    } catch (e) {
      log(e.message);
    } finally {
      toggle.disabled = false;
    }
  });

  $("#conn-refresh").addEventListener("click", refreshPorts);

  function render() {
    const c = store.state.connection || { state: "disconnected" };
    const disconnected = c.state === "disconnected";
    toggle.textContent = disconnected ? "Connect" : "Disconnect";
    toggle.classList.toggle("primary", disconnected);
    portSel.disabled = !disconnected;
    baudSel.disabled = !disconnected;
    if (!disconnected && c.port) {
      if (![...portSel.options].some((o) => o.value === c.port)) {
        const o = document.createElement("option");
        o.value = c.port;
        o.textContent = c.port;
        portSel.append(o);
      }
      portSel.value = c.port;
    }
    const parts = [];
    if (c.state !== "disconnected") parts.push(c.state);
    if (c.firmware) parts.push(`GRBL ${c.firmware}`);
    else if (store.state.caps) parts.push(`GRBL ${store.state.caps.family}.x`);
    if (c.session === "attached") parts.push("re-attached to running session");
    if (c.error) parts.push(c.error);
    info.textContent = parts.join(" · ");
  }

  function renderLink() {
    const l = store.state.link;
    link.className = "link " + (l === "live" ? "live" : l === "connecting" ? "connecting" : "");
    linkText.textContent = { live: "server live", connecting: "reconnecting…", offline: "server offline", unauthorized: "token rejected" }[l] || l;
    if (l === "unauthorized") showBanner("The server was restarted with a new access token — reload this page.", "error");
    else if (l === "offline") showBanner("Lost contact with the gcsender server. Retrying…", "error");
    else if (l === "live") showBanner(null);
  }

  store.subscribe((kind) => {
    if (kind === "snapshot" || kind === "connection" || kind === "caps") render();
    if (kind === "link") renderLink();
  });

  refreshPorts();
  render();
  renderLink();
}
