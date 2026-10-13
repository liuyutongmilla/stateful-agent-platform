export type Status = "idle" | "running" | "awaiting_approval" | "interrupted" | "error";
export type ToolStatus = "ok" | "denied" | "rejected" | "error";

export interface ToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
}

export type Item =
  | { kind: "user"; id: string; text: string }
  | { kind: "assistant"; id: string; text: string; streaming: boolean }
  | { kind: "tool"; id: string; name: string; args: Record<string, unknown>; destructive: boolean; status?: ToolStatus; content?: string }
  | { kind: "notice"; id: string; tone: "info" | "warn"; text: string };

export interface Review {
  tool_calls: number;
  ok: number;
  denied: number;
  rejected: number;
  errors: number;
  iterations: number;
  max_iterations: number;
  limit_reached: boolean;
}

export interface SessionView {
  loaded: boolean;
  status: Status;
  stage: string;
  iteration: number;
  maxIterations: number;
  plan: string[];
  allowedTools: string[];
  roles: string[];
  items: Item[];
  pendingApproval: { calls: ToolCall[] } | null;
  resumable: boolean;
  review: Review | null;
}

export interface SessionMeta {
  id: string;
  title: string;
  status: Status;
  created_at: number;
}

export interface DemoUser {
  id: string;
  name: string;
  org: string;
  role: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  permission: string;
  destructive: boolean;
  read_only: boolean;
}

export interface EventFrame {
  ch: string;
  seq: number;
  ts: number;
  type: string;
  data: any;
}

export const STAGES = ["intake", "plan", "execute", "review", "finalize"] as const;
