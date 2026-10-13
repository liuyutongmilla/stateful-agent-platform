import { useState } from "react";

import type { Connection } from "../connection";
import type { SessionView } from "../types";

interface Props {
  conn: Connection;
  sessionId: string;
  view: SessionView;
  onSend: (text: string) => void;
}

export function Composer({ conn, sessionId, view, onSend }: Props) {
  const [text, setText] = useState("");
  const running = view.status === "running";
  const approval = view.status === "awaiting_approval" ? view.pendingApproval : null;
  const canResume = view.resumable && (view.status === "interrupted" || view.status === "error");
  const blocked = running || approval !== null || canResume;

  const submit = () => {
    const value = text.trim();
    if (!value || blocked) return;
    onSend(value);
    setText("");
  };

  return (
    <footer className="composer">
      <div className="column">
        {conn.lastError && <p className="notice notice-warn">{conn.lastError}</p>}

        {approval && (
          <div className="approval" role="alertdialog" aria-label="Approval needed">
            <div>
              <strong>Approve {approval.calls.length === 1 ? "this action" : "these actions"}?</strong>
              <ul>
                {approval.calls.map((c) => (
                  <li key={c.id}>
                    <code>{c.name}</code> {Object.entries(c.args).map(([k, v]) => `${k}: ${String(v)}`).join(", ")}
                  </li>
                ))}
              </ul>
            </div>
            <div className="approval-actions">
              <button onClick={() => conn.approve(sessionId, false)}>Decline</button>
              <button className="danger" onClick={() => conn.approve(sessionId, true)}>
                Approve
              </button>
            </div>
          </div>
        )}

        {canResume && (
          <div className="approval">
            <div>
              <strong>This run stopped before it finished.</strong>
              <p>Completed steps are saved. Resuming continues from the last checkpoint.</p>
            </div>
            <div className="approval-actions">
              <button className="primary" onClick={() => conn.resume(sessionId)}>
                Resume run
              </button>
            </div>
          </div>
        )}

        <div className="inputrow">
          <textarea
            value={text}
            rows={1}
            placeholder={blocked ? (running ? "The agent is working…" : "Answer the prompt above to continue") : "Ask the agent to do something"}
            aria-label="Message"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
          />
          {running ? (
            <button onClick={() => conn.cancel(sessionId)}>Stop</button>
          ) : (
            <button className="primary" disabled={!text.trim() || blocked} onClick={submit}>
              Send
            </button>
          )}
        </div>
      </div>
    </footer>
  );
}
