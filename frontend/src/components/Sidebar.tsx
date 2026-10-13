import type { DemoUser, SessionMeta, Status } from "../types";

const STATUS_LABEL: Record<Status, string> = {
  idle: "",
  running: "Running",
  awaiting_approval: "Needs approval",
  interrupted: "Stopped",
  error: "Failed",
};

interface Props {
  user: DemoUser;
  users: DemoUser[];
  roles: string[];
  sessions: SessionMeta[];
  activeId: string;
  statusOf: (id: string) => Status;
  onSelect: (id: string) => void;
  onNew: () => void;
  onSwitchUser: (id: string) => void;
}

export function Sidebar({ user, users, roles, sessions, activeId, statusOf, onSelect, onNew, onSwitchUser }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <h1>Agent console</h1>
        <button className="primary" onClick={onNew}>
          New session
        </button>
      </div>
      <nav className="sessions" aria-label="Sessions">
        {sessions.map((s) => {
          const status = statusOf(s.id);
          return (
            <button key={s.id} className={s.id === activeId ? "session current" : "session"} onClick={() => onSelect(s.id)}>
              <span className="session-title">{s.title}</span>
              {STATUS_LABEL[status] && <span className={`chip chip-${status}`}>{STATUS_LABEL[status]}</span>}
            </button>
          );
        })}
      </nav>
      <div className="whoami">
        <label htmlFor="user">Signed in as</label>
        <select id="user" value={user.id} onChange={(e) => onSwitchUser(e.target.value)}>
          {users.map((u) => (
            <option key={u.id} value={u.id}>
              {u.name} ({u.role}, {u.org})
            </option>
          ))}
        </select>
        <p>
          The agent acts with {user.name.split(" ")[0]}'s permissions{roles.length ? `: ${roles.join(", ")}` : ""}. Switch user to see
          what changes.
        </p>
      </div>
    </aside>
  );
}
