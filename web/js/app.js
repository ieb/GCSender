// SPA bootstrap: store + event stream + panels.

import { createStore } from "./store.js";
import { EventStream } from "./events.js";
import { initConnection } from "./ui/connection.js";
import { initMachine } from "./ui/machine.js";
import { initConsole } from "./ui/console.js";

const store = createStore();
const log = initConsole(store);
initConnection(store, log);
initMachine(store, log);

const stream = new EventStream({
  onEvent: (type, data) => store.apply(type, data),
  onLink: (link) => store.setLink(link),
});
stream.start();

// Exposed for debugging from the browser console only.
window.gcs = { store, stream };
