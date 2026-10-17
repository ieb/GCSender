// Settings → Application: profiles, defaults, export/import; GRBL $$ backup.

import { api, downloadUrl } from "../api.js";
import { $ } from "./dom.js";

export function initProfiles(store) {
  const sel = $("#profile-select");
  const status = $("#app-settings-status");
  const say = (t) => { status.textContent = t; };

  async function refresh(keep) {
    try {
      const { profiles } = await api.profiles();
      const cur = keep ?? sel.value;
      sel.replaceChildren(new Option("—", ""), ...profiles.map((p) => new Option(p.name, p.name)));
      sel.value = profiles.some((p) => p.name === cur) ? cur : "";
    } catch { /* server unreachable */ }
  }

  $("#profile-save").addEventListener("click", async () => {
    const name = prompt("Save the current application settings and user commands as profile:", sel.value || "");
    if (!name) return;
    try {
      await api.profileSave(name.trim());
      await refresh(name.trim());
      say(`Saved profile “${name.trim()}”.`);
    } catch (e) { say(e.message); }
  });
  $("#profile-load").addEventListener("click", async () => {
    if (!sel.value) return;
    const macros = confirm(`Load profile “${sel.value}”.\n\nAlso replace the user commands with the profile's? (Cancel keeps your current user commands.)`);
    try {
      const r = await api.profileLoad(sel.value, macros);
      say(`Loaded “${sel.value}”${r.macros ? " with its user commands" : ""}.`);
    } catch (e) { say(e.message); }
  });
  $("#profile-delete").addEventListener("click", async () => {
    if (!sel.value || !confirm(`Delete profile “${sel.value}”?`)) return;
    try {
      await api.profileDelete(sel.value);
      await refresh("");
      say("Profile deleted.");
    } catch (e) { say(e.message); }
  });
  $("#settings-defaults").addEventListener("click", async () => {
    if (!confirm("Restore every application setting to its default?\n\nUser commands, GRBL $$ values and display preferences are not changed.")) return;
    try {
      await api.settingsReset();
      say("Defaults restored.");
    } catch (e) { say(e.message); }
  });
  $("#settings-export").addEventListener("click", (e) => { e.currentTarget.href = downloadUrl("/api/settings/export"); });
  $("#grbl-settings-backup").addEventListener("click", (e) => {
    if (!store.state.grblSettings) {
      e.preventDefault();
      $("#grbl-settings-status").textContent = "Connect first: the $$ values are read from the controller.";
      return;
    }
    e.currentTarget.href = downloadUrl("/api/grbl/settings/export");
  });
  $("#settings-import").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    try {
      const doc = JSON.parse(await file.text());
      const n = Array.isArray(doc.macros) ? doc.macros.length : 0;
      const macros = n > 0 && confirm(`Import settings from ${file.name}.\n\nAlso replace your user commands with the ${n} in the file? (Cancel imports the settings only.)`);
      const r = await api.settingsImport(doc, macros);
      say(`Imported ${file.name}${r.macros ? " with its user commands" : ""}${r.ignored.length ? ` (ignored: ${r.ignored.join(", ")})` : ""}.`);
    } catch (err) {
      say(err instanceof SyntaxError ? "That file is not JSON." : err.message);
    }
  });

  $("#open-settings").addEventListener("click", () => refresh());
}
