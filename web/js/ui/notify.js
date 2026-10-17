// Notifications: when the tab is in the background, a job finishing or needing
// the user (tool change, error hold, interruption) and alarms raise a desktop
// notification (if allowed) and mark the tab title until it is seen.

import { $ } from "./dom.js";

const PREF = "gcs-notify";
const BASE_TITLE = document.title;

export function initNotify(store) {
  const btn = $("#notify-toggle");
  let lastJob = store.state.job?.state ?? null;
  let lastAlarm = store.state.alarm ?? null;
  let pending = 0;

  const supported = "Notification" in window;
  const enabled = () => supported && localStorage.getItem(PREF) === "on" && Notification.permission === "granted";

  function renderButton() {
    btn.hidden = !supported;
    btn.textContent = enabled() ? "🔔" : "🔕";
    btn.title = enabled()
      ? "Desktop notifications are on (click to turn off)"
      : supported && Notification.permission === "denied"
        ? "Notifications are blocked for this site in the browser settings"
        : "Turn on desktop notifications for job events while this tab is in the background";
  }

  btn.addEventListener("click", async () => {
    if (enabled()) {
      localStorage.setItem(PREF, "off");
    } else {
      const p = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
      localStorage.setItem(PREF, p === "granted" ? "on" : "off");
    }
    renderButton();
  });

  function attention(title, body) {
    if (!document.hidden) return;
    pending += 1;
    document.title = `(${pending}) ${title} · ${BASE_TITLE}`;
    if (enabled()) {
      try {
        const n = new Notification(title, { body, tag: "gcsender", renotify: true });
        n.onclick = () => { window.focus(); n.close(); };
      } catch { /* some browsers only allow notifications from a service worker */ }
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      pending = 0;
      document.title = BASE_TITLE;
    }
  });

  store.subscribe((kind) => {
    if (kind === "job" || kind === "snapshot") {
      const j = store.state.job;
      const st = j?.state ?? null;
      const name = j?.job?.name || "job";
      if (st !== lastJob && kind === "job") {
        if (st === "done") attention("Job finished", `${name}: ${j.message || ""}`);
        else if (st === "failed") attention("Job failed", `${name}: ${j.message || ""}`);
        else if (st === "toolchange") attention("Tool change", j.message || `${name} is waiting for a tool change`);
        else if (st === "error") attention("Job error: machine on hold", j.message || "");
        else if (st === "interrupted") attention("Job interrupted", j.message || "");
      }
      lastJob = st;
    }
    if (kind === "alarm") {
      const a = store.state.alarm;
      if (a && !lastAlarm) attention("ALARM", a.title || a.text || "");
      lastAlarm = a;
    }
  });
  renderButton();
}
