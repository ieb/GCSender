// Login for LAN access. The page served to the server's own computer carries
// the API token and never shows this. Elsewhere the user logs in; the server
// sets an HttpOnly session cookie, which scripts on the page cannot read.

import { session, token, ApiError } from "../api.js";
import { $ } from "./dom.js";

function dialog() {
  const dlg = $("#dlg-login");
  dlg.addEventListener("cancel", (e) => e.preventDefault()); // Esc must not skip the login
  return dlg;
}

async function showLogin({ expired = false, loginAvailable = true } = {}) {
  const dlg = dialog();
  const form = $("#login-form");
  const err = $("#login-error");
  const user = $("#login-user");
  const pass = $("#login-pass");
  const submit = $("#login-submit");
  $("#login-note").textContent = expired
    ? "Your session has ended. Log in again to continue."
    : "Log in to use GCSender on this computer.";
  const noUsers = $("#login-no-users");
  noUsers.hidden = loginAvailable;
  for (const el of [user, pass, submit]) el.disabled = !loginAvailable;
  err.hidden = true;
  if (!dlg.open) dlg.showModal();
  (user.value ? pass : user).focus();
  return new Promise((resolve) => {
    form.onsubmit = async (e) => {
      e.preventDefault();
      submit.disabled = true;
      err.hidden = true;
      try {
        const r = await session.login(user.value.trim(), pass.value);
        pass.value = "";
        dlg.close();
        resolve(r);
      } catch (ex) {
        err.textContent = ex instanceof ApiError && ex.status === 0 ? "The server is unreachable." : ex.message;
        err.hidden = false;
        pass.select();
      } finally {
        submit.disabled = false;
      }
    };
  });
}

function showUser(info) {
  if (info.via !== "session") return;
  const who = $("#session-user");
  who.textContent = `👤 ${info.user}`;
  who.hidden = false;
  const out = $("#logout");
  out.hidden = false;
  out.addEventListener("click", async () => {
    try { await session.logout(); } catch { /* the session may be gone already */ }
    location.reload();
  });
}

/** Resolve once this page may use the API (token, or a login session). */
export async function ensureSession() {
  if (token) return { via: "token" };
  let info;
  for (;;) {
    try {
      info = await session.info();
      break;
    } catch {
      $("#login-note").textContent = "Waiting for the server…";
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
  if (!info.authenticated) {
    await showLogin({ loginAvailable: info.loginAvailable });
    info = await session.info();
  }
  showUser(info);
  let asking = false;
  window.addEventListener("gcs-login-required", async () => {
    if (asking) return;
    asking = true;
    await showLogin({ expired: true });
    location.reload(); // simplest way to resubscribe everything with the new session
  });
  return info;
}
