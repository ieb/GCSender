// Server-sent events with gap-free resume.
//
// The browser's EventSource reconnects by itself and sends Last-Event-ID, so
// the server replays anything missed (or sends a fresh snapshot). If the
// stream is closed for good (e.g. a 401 after a server restart with a new
// token) we retry manually and report the condition.

import { eventsUrl, api, ApiError } from "./api.js";

export class EventStream {
  constructor({ onEvent, onLink }) {
    this.onEvent = onEvent;
    this.onLink = onLink;
    this.lastId = null;
    this.es = null;
    this.retryTimer = null;
  }

  start() {
    this._open();
  }

  _open() {
    clearTimeout(this.retryTimer);
    if (this.es) this.es.close();
    this.onLink("connecting");
    const es = new EventSource(eventsUrl(this.lastId));
    this.es = es;
    es.onopen = () => this.onLink("live");
    es.onmessage = (msg) => {
      if (msg.lastEventId) this.lastId = msg.lastEventId;
      let ev;
      try { ev = JSON.parse(msg.data); } catch { return; }
      this.onEvent(ev.type, ev.data);
    };
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) {
        this.onLink("offline");
        this._diagnoseAndRetry();
      } else {
        this.onLink("connecting"); // the browser is retrying on its own
      }
    };
  }

  async _diagnoseAndRetry() {
    try {
      await api.state();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        this.onLink("unauthorized");
        return; // a reload is needed to pick up the new token
      }
    }
    this.retryTimer = setTimeout(() => this._open(), 2000);
  }
}
