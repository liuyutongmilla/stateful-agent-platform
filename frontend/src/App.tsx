import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { api } from "./api";
import { Connection } from "./connection";
import { AccessPanel } from "./components/AccessPanel";
import { Composer } from "./components/Composer";
import { RunTrack } from "./components/RunTrack";
import { Sidebar } from "./components/Sidebar";
import { Transcript } from "./components/Transcript";
import type { DemoUser, SessionMeta, ToolInfo } from "./types";

function remembered(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function remember(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode: fine */
  }
}

export function App() {
  const [users, setUsers] = useState<DemoUser[]>([]);
  const [userId, setUserId] = useState<string>(() => remembered("console.user") ?? "alice");
  const [bootError, setBootError] = useState<string | null>(null);

  useEffect(() => {
    api.demoUsers().then(setUsers).catch(() => setBootError("Can't reach the server. Start the backend on port 8000, then reload."));
  }, []);

  if (bootError) return <div className="boot">{bootError}</div>;
  const user = users.find((u) => u.id === userId) ?? users[0];
  if (!user) return <div className="boot">Loading…</div>;
  return (
    <Workspace
      key={user.id}
      user={user}
      users={users}
      onSwitchUser={(id) => {
        remember("console.user", id);
        setUserId(id);
      }}
    />
  );
}

interface Me {
  roles: string[];
  allowed_tools: string[];
}

function Workspace({ user, users, onSwitchUser }: { user: DemoUser; users: DemoUser[]; onSwitchUser: (id: string) => void }) {
  const [token, setToken] = useState<string | null>(null);
  const [conn, setConn] = useState<Connection | null>(null);
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [me, setMe] = useState<Me | null>(null);
  const [panel, setPanel] = useState<"none" | "sessions" | "access">("none");
  const creating = useRef(false);

  useEffect(() => {
    let connection: Connection | null = null;
    let cancelled = false;
    (async () => {
      const { token } = await api.login(user.id);
      if (cancelled) return;
      setToken(token);
      connection = new Connection(token);
      setConn(connection);
      const [list, catalog, self] = await Promise.all([api.sessions(token), api.tools(token), api.me(token)]);
      if (cancelled) return;
      setTools(catalog);
      setMe(self);
      if (list.length === 0) list.push(await api.createSession(token));
      setSessions(list);
      setActiveId(list[0].id);
    })();
    return () => {
      cancelled = true;
      connection?.close();
    };
  }, [user.id]);

  // Re-render on any connection change.
  useSyncExternalStore(conn?.subscribeStore ?? noopSubscribe, conn?.getVersion ?? zero);

  // Every listed session rides the same socket, so background runs stay live in the sidebar.
  useEffect(() => {
    if (conn) sessions.slice(0, 25).forEach((s) => conn.watch(s.id));
  }, [conn, sessions]);

  const newSession = useCallback(async () => {
    if (!token || creating.current) return;
    creating.current = true;
    try {
      const created = await api.createSession(token);
      setSessions((list) => [created, ...list]);
      setActiveId(created.id);
      setPanel("none");
    } finally {
      creating.current = false;
    }
  }, [token]);

  const view = conn && activeId ? conn.session(activeId) : null;

  // Roles can change mid-conversation (an owner grants one); each run reports the fresh answer.
  const allowed = useMemo(() => new Set(view?.allowedTools.length ? view.allowedTools : (me?.allowed_tools ?? [])), [view?.allowedTools, me]);
  const roles = view?.roles.length ? view.roles : (me?.roles ?? []);

  if (!conn || !activeId || !view) return <div className="boot">Signing in as {user.name}…</div>;

  const send = (text: string) => {
    if (view.items.length === 0) setSessions((list) => list.map((s) => (s.id === activeId ? { ...s, title: text } : s)));
    conn.send(activeId, text);
  };

  return (
    <div className={`shell panel-${panel}`}>
      <Sidebar
        user={user}
        users={users}
        roles={roles}
        sessions={sessions}
        activeId={activeId}
        statusOf={(id) => (conn.session(id).loaded ? conn.session(id).status : (sessions.find((s) => s.id === id)?.status ?? "idle"))}
        onSelect={(id) => {
          setActiveId(id);
          setPanel("none");
        }}
        onNew={newSession}
        onSwitchUser={onSwitchUser}
      />
      <main className="main">
        <header className="topbar">
          <button className="ghost only-narrow" onClick={() => setPanel(panel === "sessions" ? "none" : "sessions")}>
            Sessions
          </button>
          <RunTrack view={view} />
          <button className="ghost only-narrow" onClick={() => setPanel(panel === "access" ? "none" : "access")}>
            Access
          </button>
        </header>
        <Transcript view={view} onPick={send} canAct={allowed} />
        <Composer conn={conn} sessionId={activeId} view={view} onSend={send} />
      </main>
      <AccessPanel tools={tools} allowed={allowed} roles={roles} user={user} conn={conn} />
    </div>
  );
}

const noopSubscribe = () => () => {};
const zero = () => 0;
