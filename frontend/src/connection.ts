// One WebSocket for every open session.
//
// - per-session sequence numbers: duplicates are dropped, a gap triggers a resubscribe
// - reconnects with backoff and asks the server to replay from the last seq it saw
// - commands carry an id and are resent until acknowledged, so a command typed
//   during an outage is applied exactly once when the connection returns

import { applyEvent, emptySession, fromSnapshot } from "./reducer";
import type { EventFrame, SessionView } from "./types";

export type LinkState = "connecting" | "online" | "reconnecting";

type Listener = () => void;

const BACKOFF_MS = [250, 500, 1000, 2000, 4000];

export class Connection {
  link: LinkState = "connecting";
  lastError: string | null = null;
  latencyMs: number | null = null;

  private ws: WebSocket | null = null;
  private epoch: string | null = null;
  private attempts = 0;
  private closed = false;
  private sessions = new Map<string, SessionView>();
  private lastSeq = new Map<string, number>();
  private wanted = new Set<string>();
  private unacked = new Map<string, string>();
  private listeners = new Set<Listener>();
  private version = 0;
  private pingTimer: number | undefined;
  private retryTimer: number | undefined;

  constructor(private token: string) {
    this.open();
  }

  // ---- store interface (useSyncExternalStore) ----
  subscribeStore = (fn: Listener) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };
  getVersion = () => this.version;
  session(id: string): SessionView {
    return this.sessions.get(id) ?? emptySession();
  }
  private changed() {
    this.version++;
    this.listeners.forEach((fn) => fn());
  }

  // ---- lifecycle ----
  private open() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/ws`);
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ op: "auth", token: this.token }));
    ws.onmessage = (m) => this.receive(JSON.parse(m.data));
    ws.onclose = (ev) => {
      if (this.ws !== ws || this.closed) return;
      this.ws = null;
      window.clearInterval(this.pingTimer);
      if (ev.code === 1008) {
        this.lastError = "Your sign-in is no longer valid. Sign in again.";
        this.link = "reconnecting";
        this.changed();
        return;
      }
      this.link = "reconnecting";
      this.changed();
      const wait = BACKOFF_MS[Math.min(this.attempts++, BACKOFF_MS.length - 1)] * (0.75 + Math.random() / 2);
      this.retryTimer = window.setTimeout(() => this.open(), wait);
    };
  }

  close() {
    this.closed = true;
    window.clearInterval(this.pingTimer);
    window.clearTimeout(this.retryTimer);
    this.ws?.close();
  }

  /** For demonstrating recovery: cut the socket as a network failure would. */
  simulateDrop() {
    this.ws?.close();
  }

  private raw(frame: object) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(frame));
  }

  // ---- inbound ----
  private receive(msg: any) {
    if (msg.op === "ready") {
      this.link = "online";
      this.attempts = 0;
      this.lastError = null;
      for (const id of this.wanted) this.sendSubscribe(id);
      for (const frame of this.unacked.values()) this.ws?.send(frame);
      this.pingTimer = window.setInterval(() => this.raw({ op: "ping", t: performance.now() }), 5000);
      this.raw({ op: "ping", t: performance.now() });
    } else if (msg.op === "subscribed") {
      this.epoch = msg.epoch;
      if (msg.mode === "snapshot") {
        this.sessions.set(msg.ch, fromSnapshot(msg.snapshot));
        this.lastSeq.set(msg.ch, msg.seq);
      }
    } else if (msg.op === "ack") {
      this.unacked.delete(msg.id);
      return;
    } else if (msg.op === "error") {
      if (msg.id) this.unacked.delete(msg.id);
      this.lastError = msg.message;
    } else if (msg.op === "pong") {
      this.latencyMs = Math.round(performance.now() - msg.t);
    } else if (msg.ch) {
      this.event(msg as EventFrame);
      return;
    }
    this.changed();
  }

  private event(e: EventFrame) {
    const last = this.lastSeq.get(e.ch) ?? 0;
    if (e.seq <= last) return; // duplicate from a replay
    if (e.seq !== last + 1) {
      this.sendSubscribe(e.ch); // missed something: ask for it again
      return;
    }
    this.lastSeq.set(e.ch, e.seq);
    this.sessions.set(e.ch, applyEvent(this.session(e.ch), e));
    this.changed();
  }

  // ---- outbound ----
  private sendSubscribe(id: string) {
    this.raw({ op: "subscribe", ch: id, last_seq: this.lastSeq.get(id) ?? null, epoch: this.epoch });
  }

  watch(id: string) {
    if (this.wanted.has(id)) return;
    this.wanted.add(id);
    this.sendSubscribe(id);
  }

  private command(op: string, fields: object) {
    const id = crypto.randomUUID();
    const frame = JSON.stringify({ op, id, ...fields });
    this.unacked.set(id, frame);
    this.lastError = null;
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(frame);
    this.changed();
  }

  send(ch: string, text: string) {
    this.command("send", { ch, text });
  }
  approve(ch: string, approved: boolean) {
    this.command("approve", { ch, approved });
  }
  resume(ch: string) {
    this.command("resume", { ch });
  }
  cancel(ch: string) {
    this.command("cancel", { ch });
  }
  get queued() {
    return this.unacked.size;
  }
}
