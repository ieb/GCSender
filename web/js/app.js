// SPA bootstrap: store + event stream + panels.

import { createStore } from "./store.js";
import { EventStream } from "./events.js";
import { initConnection } from "./ui/connection.js";
import { initMachine } from "./ui/machine.js";
import { initConsole } from "./ui/console.js";
import { initWarnings } from "./ui/warnings.js";
import { initSettings } from "./ui/settings.js";
import { initLogs } from "./ui/logs.js";
import { initJog } from "./ui/jog.js";
import { initWcs, initSpindle } from "./ui/panels.js";
import { initVisualizer } from "./view/visualizer.js";
import { initLeftTabs, initProbe, initHeightmap } from "./ui/heightmap.js";
import { initSim } from "./ui/sim.js";

const store = createStore();
const log = initConsole(store);
initConnection(store, log);
initMachine(store, log);
initWarnings(store, log);
initSettings(store);
initLogs();
initJog(store, log);
initWcs(store, log);
initSpindle(store, log);
const view = initVisualizer(store);
initLeftTabs();
initProbe(store, log);
initHeightmap(store, log, view);
initSim(store, log);

const stream = new EventStream({
  onEvent: (type, data) => store.apply(type, data),
  onLink: (link) => store.setLink(link),
});
stream.start();

// Exposed for debugging from the browser console only.
window.gcs = { store, stream };
