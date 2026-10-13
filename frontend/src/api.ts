import type { DemoUser, SessionMeta, ToolInfo } from "./types";

async function request<T>(path: string, token?: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export const api = {
  demoUsers: () => request<DemoUser[]>("/api/demo-users"),
  login: (user: string) => request<{ token: string }>("/api/login", undefined, { method: "POST", body: JSON.stringify({ user }) }),
  sessions: (token: string) => request<SessionMeta[]>("/api/sessions", token),
  createSession: (token: string) => request<SessionMeta>("/api/sessions", token, { method: "POST" }),
  tools: (token: string) => request<ToolInfo[]>("/api/tools", token),
  me: (token: string) => request<{ roles: string[]; allowed_tools: string[] }>("/api/me", token),
};
