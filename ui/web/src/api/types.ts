export type Config = Record<
  string,
  string | number | boolean | string[] | null
>;
export interface ConfigField {
  key: string;
  flag: string;
  label: string;
  type: string;
  default: Config[string];
  choices: string[];
  help: string;
}
export interface Schema {
  version: number;
  fields: ConfigField[];
  defaults: Config;
}
export interface Message {
  id: string;
  role: string;
  actor: string;
  text: string;
  complete: boolean;
  timestamp: number;
  run_id: string;
}
export interface Interaction {
  id: string;
  kind: string;
  actor_id: string;
  tool?: string;
  args?: Record<string, unknown>;
  risk_level?: string;
  execution_location?: string;
  reason?: string;
  session_grant_allowed?: boolean;
  question?: string;
  options?: string[];
  free_form?: boolean;
  blocking?: boolean;
  revision?: string;
  plan?: Record<string, unknown>;
}
export interface Actor {
  id: string;
  parent_id?: string;
  peer_id?: string;
  generation?: number;
  memory_path?: string;
  todo_path?: string;
  context_tokens?: number;
  context_window?: number;
  subkind?: string;
  target?: string;
  idea?: string;
  turn?: number;
  status_text?: string;
  state: string;
  status?: string;
}
export interface Tool {
  call_id: string;
  tool: string;
  actor_id: string;
  state: string;
  ok?: boolean;
  output?: string;
  args?: Record<string, unknown>;
  risk_level?: string;
}
export interface SessionState {
  seq: number;
  lifecycle: string;
  activity: string;
  messages: Message[];
  actors: Record<string, Actor>;
  tools: Record<string, Tool>;
  interactions: Record<string, Interaction>;
  effective: Record<string, unknown>;
  phase: string;
  error: string;
  findings: Record<string, unknown>;
  run_id: string;
  status?: Record<string, number | string>;
}
export interface Session {
  kind?: string;
  id: string;
  engagement_id: string;
  config: Config;
  created_at: number;
  run_id: string;
  context_revision: number;
  context: string;
  history_path: string | null;
  snapshot: SessionState;
}
export interface Attack {
  id: string;
  name: string;
  target: string;
  archived: boolean;
  created_at: number;
  context_revision: number;
  session_id: string | null;
  config: Config;
  draft: string;
  view: string;
  state?: SessionState | null;
}
export interface AttackTemplate {
  id: string;
  name: string;
  target: string;
  config: Config;
  context: string | null;
  created_at: number;
}
export interface Event {
  schema_version: number;
  id: string;
  seq: number;
  session_id: string;
  engagement_id: string;
  run_id: string;
  timestamp: number;
  kind: string;
  actor_id: string;
  message_id: string;
  payload: Record<string, any>;
}
export interface Artifact {
  id: string;
  path: string;
  size: number;
  modified_at: number;
  sealed: boolean;
  session_id: string | null;
  content?: string;
  truncated?: boolean;
}
export interface Finding {
  id: string;
  verdict: string;
  submitted_by: string;
  created_at: number;
  duplicate_of: string | null;
  artifact_id: string;
  finding: {
    title: string;
    summary: string;
    endpoint: string;
    method: string;
    cwe: string;
    severity_hint: string;
    evidence: string;
    notes: string;
  };
  validation: {
    verdict: string;
    rationale: string;
    reproducer: string;
    cvss40_score: number;
    cvss40_vector: string;
    additional_evidence: string;
    cwe: string;
  } | null;
}
export interface Health {
  status: string;
  csrf_token: string;
  scripted: boolean;
  backends: Record<string, boolean>;
  docker: boolean;
  template: string;
}

export interface ModelCatalog {
  backend: "codex";
  models: { id: string; name: string; description: string }[];
  source: "codex_app_server" | "codex_cache" | "unavailable";
  configured_default: string | null;
  retrieved_at: number;
  cached_at: string | null;
  warning: string;
}
