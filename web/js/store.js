// Client-side store. It is fed only by server events (a snapshot, then
// incremental events), so a page reload reconstructs exactly the same view.

const CONSOLE_LIMIT = 2000;

export function createStore() {
  const state = {
    link: "offline",
    connection: { state: "disconnected" },
    caps: null,
    status: null,
    modal: null,
    offsets: null,
    alarm: null,
    grblSettings: null,
    warnings: [],
    build: null,
    appSettings: null,
    heightmap: null,
    probe: null,
    console: [],
  };
  const listeners = new Set();

  function emit(kind, payload) {
    for (const fn of listeners) fn(kind, state, payload);
  }

  function pushConsole(entry) {
    state.console.push(entry);
    if (state.console.length > CONSOLE_LIMIT) state.console.splice(0, state.console.length - CONSOLE_LIMIT);
  }

  function apply(type, data) {
    switch (type) {
      case "snapshot": {
        const r = data.retained || {};
        state.connection = r.connection || { state: "disconnected" };
        state.caps = r.caps || null;
        state.status = r.status || null;
        state.modal = r.modal || null;
        state.offsets = r.offsets || null;
        state.alarm = r.alarm || null;
        state.grblSettings = r["grbl.settings"] || null;
        state.warnings = r.warnings || [];
        state.build = r.build || null;
        state.appSettings = r["app.settings"] || null;
        state.heightmap = r.heightmap || null;
        state.probe = r.probe || null;
        state.console = (data.console || []).slice(-CONSOLE_LIMIT);
        emit("snapshot");
        return;
      }
      case "console":
        pushConsole(data);
        emit("console", data);
        return;
      case "connection":
      case "caps":
      case "status":
      case "modal":
      case "offsets":
      case "alarm":
      case "warnings":
      case "build":
      case "heightmap":
      case "probe":
        state[type] = data;
        emit(type, data);
        return;
      case "grbl.settings":
        state.grblSettings = data;
        emit(type, data);
        return;
      case "app.settings":
        state.appSettings = data;
        emit(type, data);
        return;
      default:
        emit(type, data); // e.g. machine.state, controller.reset
    }
  }

  return {
    state,
    apply,
    setLink(link) {
      state.link = link;
      emit("link", link);
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}
