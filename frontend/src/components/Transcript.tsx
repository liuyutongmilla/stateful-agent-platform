import { useEffect, useRef } from "react";

import type { Item, SessionView } from "../types";

const SUGGESTIONS: { text: string; needs: string }[] = [
  { text: "Run a health check", needs: "list_services" },
  { text: "Restart search-api", needs: "restart_service" },
  { text: "List users", needs: "list_users" },
  { text: "Show denied attempts in the audit log", needs: "query_audit_log" },
  { text: "What can I do?", needs: "whoami" },
];

const RESULT_LABEL = { ok: "Done", denied: "Denied", rejected: "Declined", error: "Failed" } as const;

function pretty(json: string | undefined): string {
  if (!json) return "";
  try {
    return JSON.stringify(JSON.parse(json), null, 2);
  } catch {
    return json;
  }
}

function argsLine(args: Record<string, unknown>): string {
  const entries = Object.entries(args);
  return entries.length ? entries.map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ") : "no arguments";
}

function ToolRow({ item }: { item: Extract<Item, { kind: "tool" }> }) {
  let detail = "";
  if (item.status === "denied" || item.status === "error") {
    try {
      detail = JSON.parse(item.content ?? "{}").detail ?? "";
    } catch {
      /* keep empty */
    }
  }
  return (
    <details className={`tool tool-${item.status ?? "pending"}`}>
      <summary>
        <span className="tool-name">{item.name}</span>
        <span className="tool-args">{argsLine(item.args)}</span>
        <span className="tool-status">{item.status ? RESULT_LABEL[item.status] : "Running…"}</span>
      </summary>
      {detail && <p className="tool-detail">{detail}</p>}
      {item.content && <pre>{pretty(item.content)}</pre>}
    </details>
  );
}

export function Transcript({ view, onPick, canAct }: { view: SessionView; onPick: (text: string) => void; canAct: Set<string> }) {
  const end = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (pinned.current) end.current?.scrollIntoView({ block: "end" });
  }, [view.items, view.pendingApproval]);

  if (view.loaded && view.items.length === 0) {
    return (
      <div className="transcript empty">
        <h2>Ask the agent to do something</h2>
        <p>It plans, calls admin tools on your behalf, and checks every call against your permissions.</p>
        <div className="suggestions">
          {SUGGESTIONS.map((s) => (
            <button key={s.text} onClick={() => onPick(s.text)}>
              {s.text}
              {!canAct.has(s.needs) && <small>your role can't</small>}
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div
      className="transcript"
      ref={box}
      onScroll={() => {
        const el = box.current!;
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}
    >
      <div className="column" aria-live="polite">
        {view.plan.length > 0 && (
          <section className="plan" aria-label="Plan">
            <h3>Plan</h3>
            <ol>
              {view.plan.map((step, i) => (
                <li key={i}>{step}</li>
              ))}
            </ol>
          </section>
        )}
        {view.items.map((item) => {
          switch (item.kind) {
            case "user":
              return (
                <p key={item.id} className="msg msg-user">
                  {item.text}
                </p>
              );
            case "assistant":
              return (
                <p key={item.id} className={item.streaming ? "msg msg-agent streaming" : "msg msg-agent"}>
                  {item.text}
                </p>
              );
            case "tool":
              return <ToolRow key={item.id} item={item} />;
            case "notice":
              return (
                <p key={item.id} className={`notice notice-${item.tone}`}>
                  {item.text}
                </p>
              );
          }
        })}
        <div ref={end} />
      </div>
    </div>
  );
}
