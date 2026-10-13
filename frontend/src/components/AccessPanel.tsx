import type { Connection } from "../connection";
import type { DemoUser, ToolInfo } from "../types";

interface Props {
  tools: ToolInfo[];
  allowed: Set<string>;
  roles: string[];
  user: DemoUser;
  conn: Connection;
}

const LINK_LABEL = { connecting: "Connecting…", online: "Live", reconnecting: "Reconnecting…" } as const;

export function AccessPanel({ tools, allowed, roles, user, conn }: Props) {
  const usable = tools.filter((t) => allowed.has(t.name));
  const blocked = tools.filter((t) => !allowed.has(t.name));
  return (
    <aside className="access">
      <section>
        <h2>What the agent can do for {user.name.split(" ")[0]}</h2>
        <p className="muted">
          {usable.length} of {tools.length} tools, from the role{roles.length === 1 ? "" : "s"} {roles.join(", ") || "none"}. Each call is
          checked again when it runs.
        </p>
        <ul className="toollist">
          {usable.map((t) => (
            <li key={t.name} title={t.description}>
              <span>{t.name}</span>
              {t.destructive && <em>asks first</em>}
            </li>
          ))}
        </ul>
        {blocked.length > 0 && (
          <details>
            <summary>{blocked.length} tools this role can't use</summary>
            <ul className="toollist blocked">
              {blocked.map((t) => (
                <li key={t.name} title={`Requires ${t.permission}`}>
                  <span>{t.name}</span>
                  <em>{t.permission.split("#")[1]}</em>
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>
      <section className="link">
        <p className={`linkstate linkstate-${conn.link}`}>
          {LINK_LABEL[conn.link]}
          {conn.link === "online" && conn.latencyMs !== null ? ` (${conn.latencyMs} ms round trip)` : ""}
          {conn.queued > 0 ? ` ${conn.queued} waiting to send` : ""}
        </p>
        <button className="ghost" onClick={() => conn.simulateDrop()} disabled={conn.link !== "online"}>
          Drop the connection
        </button>
        <p className="muted">Runs continue on the server. The console reconnects and replays what it missed.</p>
      </section>
    </aside>
  );
}
