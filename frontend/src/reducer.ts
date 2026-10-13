// Turns the server's event stream into what the screen shows.
//
// Every handler is idempotent by id: after a reconnect the server may replay
// frames that overlap a snapshot, and applying one twice must change nothing.

import type { EventFrame, Item, SessionView, ToolStatus } from "./types";

export const emptySession = (): SessionView => ({
  loaded: false,
  status: "idle",
  stage: "",
  iteration: 0,
  maxIterations: 10,
  plan: [],
  allowedTools: [],
  roles: [],
  items: [],
  pendingApproval: null,
  resumable: false,
  review: null,
});

function upsert(items: Item[], item: Item): Item[] {
  const i = items.findIndex((x) => x.id === item.id && x.kind === item.kind);
  if (i < 0) return [...items, item];
  const next = items.slice();
  next[i] = { ...items[i], ...item } as Item;
  return next;
}

export function fromSnapshot(snap: any): SessionView {
  const results = new Map<string, { status: ToolStatus; content: string }>();
  for (const m of snap.messages) if (m.role === "tool") results.set(m.tool_call_id, { status: m.status, content: m.content });
  const items: Item[] = [];
  for (const m of snap.messages) {
    if (m.role === "user") items.push({ kind: "user", id: m.id, text: m.content });
    if (m.role === "assistant") {
      if (m.content) items.push({ kind: "assistant", id: m.id, text: m.content, streaming: false });
      for (const c of m.tool_calls ?? [])
        items.push({ kind: "tool", id: c.id, name: c.name, args: c.args, destructive: false, ...results.get(c.id) });
    }
  }
  return {
    ...emptySession(),
    loaded: true,
    status: snap.status,
    // a finished run shows a clear track; an unfinished one shows where it stopped
    stage: snap.stage === "done" ? "" : snap.stage,
    iteration: snap.stage === "done" ? 0 : snap.iterations,
    maxIterations: snap.max_iterations,
    plan: snap.stage === "done" ? [] : snap.plan,
    allowedTools: snap.allowed_tools,
    items,
    pendingApproval: snap.pending_approval,
    resumable: snap.resumable,
  };
}

export function applyEvent(s: SessionView, e: EventFrame): SessionView {
  const d = e.data;
  switch (e.type) {
    case "user_message":
      return {
        ...s,
        stage: "",
        iteration: 0,
        plan: [],
        review: null,
        items: upsert(s.items, { kind: "user", id: d.id, text: d.text }),
      };
    case "status":
      return { ...s, status: d.status, resumable: Boolean(d.resumable) };
    case "stage":
      return { ...s, stage: d.stage, iteration: d.iteration ?? s.iteration, maxIterations: d.max_iterations ?? s.maxIterations };
    case "tools":
      return { ...s, allowedTools: d.allowed, roles: d.roles };
    case "plan":
      return { ...s, plan: d.steps };
    case "message_start":
      return { ...s, items: upsert(s.items, { kind: "assistant", id: d.id, text: "", streaming: true }) };
    case "token": {
      const current = s.items.find((x) => x.kind === "assistant" && x.id === d.id);
      if (current && current.kind === "assistant" && !current.streaming) return s; // already complete
      const text = (current && current.kind === "assistant" ? current.text : "") + d.text;
      return { ...s, items: upsert(s.items, { kind: "assistant", id: d.id, text, streaming: true }) };
    }
    case "message_end":
      // A turn that only called tools has no text; drop its empty bubble.
      return d.text
        ? { ...s, items: upsert(s.items, { kind: "assistant", id: d.id, text: d.text, streaming: false }) }
        : { ...s, items: s.items.filter((x) => !(x.kind === "assistant" && x.id === d.id)) };
    case "tool_call":
      return { ...s, items: upsert(s.items, { kind: "tool", id: d.id, name: d.name, args: d.args, destructive: d.destructive }) };
    case "tool_result": {
      const call = s.items.find((x) => x.kind === "tool" && x.id === d.id);
      const base = call && call.kind === "tool" ? call : { kind: "tool" as const, id: d.id, name: d.name, args: {}, destructive: false };
      return { ...s, items: upsert(s.items, { ...base, status: d.status, content: d.content }) };
    }
    case "approval_required":
      return { ...s, pendingApproval: d };
    case "approval_resolved":
      return { ...s, pendingApproval: null };
    case "limit":
      return {
        ...s,
        items: upsert(s.items, {
          kind: "notice",
          id: `limit:${e.seq}`,
          tone: "warn",
          text: `Stopped: the agent used all ${d.max_iterations} reasoning iterations for this request.`,
        }),
      };
    case "review":
      return { ...s, review: d };
    case "resumed":
      // Whatever was mid-stream when the run stopped is about to be produced again.
      return {
        ...s,
        items: upsert(
          s.items.filter((x) => !(x.kind === "assistant" && x.streaming)),
          { kind: "notice", id: `resumed:${e.seq}`, tone: "info", text: `Resumed from the last checkpoint (${d.from_stage} stage).` },
        ),
      };
    case "error":
      return { ...s, items: upsert(s.items, { kind: "notice", id: `error:${e.seq}`, tone: "warn", text: `The run failed: ${d.message}` }) };
    default:
      return s;
  }
}
