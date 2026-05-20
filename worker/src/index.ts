/// <reference types="@cloudflare/workers-types" />

/**
 * monomi — Cloudflare Worker.
 *
 * Routes:
 *   POST /ingest      — Pi collector pushes JSON here (Bearer auth)
 *   GET  /api/stats   — latest snapshot + recent history for the page
 *   GET  /            — the status page
 *   GET  /app.js      — client logic
 *   GET  /style.css   — visual theme
 *
 * State lives in a single Durable Object so reads see the freshest write
 * without KV's eventual-consistency lag.
 */

import { INDEX_HTML } from "./assets/html";
import { APP_JS } from "./assets/js";
import { STYLE_CSS } from "./assets/css";

export interface Env {
  STATS: DurableObjectNamespace;
  INGEST_TOKEN: string;
}

const HISTORY_CAP = 360;   // ~6 minutes at 1s cadence

interface Snapshot {
  host?: string;
  ts?: number;
  [k: string]: unknown;
}

interface Payload {
  latest: Snapshot | null;
  history: HistoryShape;
}

interface HistoryShape {
  ts: number[];
  cpu_percent:    (number | null)[];
  cpu_0_percent:  (number | null)[];
  cpu_1_percent:  (number | null)[];
  cpu_2_percent:  (number | null)[];
  cpu_3_percent:  (number | null)[];
  cpu_temperature:(number | null)[];
  memory_percent: (number | null)[];
  swap_percent:   (number | null)[];
  network_download_speed: (number | null)[];
  network_upload_speed:   (number | null)[];
  eth0_down:      (number | null)[];
  eth0_up:        (number | null)[];
  wg0_down:       (number | null)[];
  wg0_up:         (number | null)[];
  disk_read:      (number | null)[];
  disk_write:     (number | null)[];
}

function emptyHistory(): HistoryShape {
  return {
    ts: [],
    cpu_percent: [], cpu_0_percent: [], cpu_1_percent: [], cpu_2_percent: [], cpu_3_percent: [],
    cpu_temperature: [],
    memory_percent: [], swap_percent: [],
    network_download_speed: [], network_upload_speed: [],
    eth0_down: [], eth0_up: [],
    wg0_down: [], wg0_up: [],
    disk_read: [], disk_write: [],
  };
}

/** Single global stats room — latest snapshot + recent history. */
export class StatsRoom implements DurableObject {
  state: DurableObjectState;
  env: Env;
  latest: Snapshot | null = null;
  history: HistoryShape = emptyHistory();

  constructor(state: DurableObjectState, env: Env) {
    this.state = state;
    this.env = env;
    this.state.blockConcurrencyWhile(async () => {
      const stored = await this.state.storage.get<{
        latest: Snapshot | null;
        history: Partial<HistoryShape>;
      }>("snapshot");
      if (stored) {
        this.latest = stored.latest;
        // pad missing arrays so an older persisted shape doesn't crash pushHistory
        const fresh = emptyHistory();
        const stamp = stored.history?.ts ?? [];
        const padLen = stamp.length;
        const pad = (a?: (number | null)[]) => a ?? new Array(padLen).fill(null);
        this.history = {
          ts: stamp,
          cpu_percent:    pad(stored.history?.cpu_percent),
          cpu_0_percent:  pad(stored.history?.cpu_0_percent),
          cpu_1_percent:  pad(stored.history?.cpu_1_percent),
          cpu_2_percent:  pad(stored.history?.cpu_2_percent),
          cpu_3_percent:  pad(stored.history?.cpu_3_percent),
          cpu_temperature:pad(stored.history?.cpu_temperature),
          memory_percent: pad(stored.history?.memory_percent),
          swap_percent:   pad(stored.history?.swap_percent),
          network_download_speed: pad(stored.history?.network_download_speed),
          network_upload_speed:   pad(stored.history?.network_upload_speed),
          eth0_down:      pad(stored.history?.eth0_down),
          eth0_up:        pad(stored.history?.eth0_up),
          wg0_down:       pad(stored.history?.wg0_down),
          wg0_up:         pad(stored.history?.wg0_up),
          disk_read:      pad(stored.history?.disk_read),
          disk_write:     pad(stored.history?.disk_write),
        };
      }
    });
  }

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    if (req.method === "POST" && url.pathname === "/ingest") {
      const snap = (await req.json()) as Snapshot;
      this.latest = snap;
      this.pushHistory(snap);
      // Persist every ~20 samples so we don't burn IO at 1Hz ingest.
      if (this.history.ts.length % 20 === 0) {
        await this.state.storage.put("snapshot", {
          latest: this.latest,
          history: this.history,
        });
      }
      return new Response("ok");
    }
    if (req.method === "GET" && url.pathname === "/stats") {
      const body: Payload = { latest: this.latest, history: this.history };
      return Response.json(body, { headers: { "Cache-Control": "no-store" } });
    }
    return new Response("not found", { status: 404 });
  }

  pushHistory(snap: Snapshot): void {
    const pm = (snap.pironman ?? {}) as Record<string, unknown>;
    const mi = (snap.meminfo ?? {}) as Record<string, unknown>;
    const iface = (snap.iface ?? {}) as Record<string, Record<string, unknown>>;
    const num = (v: unknown): number | null =>
      typeof v === "number" && Number.isFinite(v) ? v : null;

    // swap% from meminfo if available
    let swapPct: number | null = null;
    const swT = num(mi.swap_total);
    const swU = num(mi.swap_used);
    if (swT && swT > 0 && swU != null) swapPct = (swU / swT) * 100;

    const eth0 = (iface.eth0 ?? {}) as Record<string, unknown>;
    const wg0  = (iface.wg0  ?? {}) as Record<string, unknown>;
    const dio  = (snap.disk_io ?? {}) as Record<string, unknown>;

    this.history.ts.push(typeof snap.ts === "number" ? snap.ts : Date.now() / 1000);
    this.history.cpu_percent.push(num(pm.cpu_percent));
    this.history.cpu_0_percent.push(num(pm.cpu_0_percent));
    this.history.cpu_1_percent.push(num(pm.cpu_1_percent));
    this.history.cpu_2_percent.push(num(pm.cpu_2_percent));
    this.history.cpu_3_percent.push(num(pm.cpu_3_percent));
    this.history.cpu_temperature.push(num(pm.cpu_temperature));
    this.history.memory_percent.push(num(pm.memory_percent));
    this.history.swap_percent.push(swapPct);
    this.history.network_download_speed.push(num(pm.network_download_speed));
    this.history.network_upload_speed.push(num(pm.network_upload_speed));
    this.history.eth0_down.push(num(eth0.down_Bps));
    this.history.eth0_up.push(num(eth0.up_Bps));
    this.history.wg0_down.push(num(wg0.down_Bps));
    this.history.wg0_up.push(num(wg0.up_Bps));
    this.history.disk_read.push(num(dio.read_Bps));
    this.history.disk_write.push(num(dio.write_Bps));

    for (const k of Object.keys(this.history) as (keyof HistoryShape)[]) {
      const arr = this.history[k] as unknown[];
      while (arr.length > HISTORY_CAP) arr.shift();
    }
  }
}

function roomStub(env: Env): DurableObjectStub {
  return env.STATS.get(env.STATS.idFromName("singleton"));
}

const TEXT_HEADERS = { "Cache-Control": "no-store" };

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    if (req.method === "POST" && url.pathname === "/ingest") {
      const auth = req.headers.get("authorization") ?? "";
      if (auth !== `Bearer ${env.INGEST_TOKEN}`) {
        return new Response("unauthorized", { status: 401 });
      }
      return roomStub(env).fetch(
        new Request("https://do/ingest", { method: "POST", body: await req.text() })
      );
    }
    if (req.method === "GET" && url.pathname === "/api/stats") {
      return roomStub(env).fetch(new Request("https://do/stats"));
    }
    if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html")) {
      return new Response(INDEX_HTML, {
        headers: { "Content-Type": "text/html; charset=utf-8", ...TEXT_HEADERS },
      });
    }
    if (req.method === "GET" && url.pathname === "/app.js") {
      return new Response(APP_JS, {
        headers: { "Content-Type": "application/javascript; charset=utf-8", ...TEXT_HEADERS },
      });
    }
    if (req.method === "GET" && url.pathname === "/style.css") {
      return new Response(STYLE_CSS, {
        headers: { "Content-Type": "text/css; charset=utf-8", ...TEXT_HEADERS },
      });
    }
    return new Response("not found", { status: 404 });
  },
};
