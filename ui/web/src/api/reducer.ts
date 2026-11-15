import type { Event, SessionState } from "./types";
export const emptyState = (): SessionState => ({
  seq: 0,
  lifecycle: "starting",
  activity: "idle",
  messages: [],
  actors: {},
  tools: {},
  interactions: {},
  effective: {},
  phase: "",
  error: "",
  findings: {},
  run_id: "",
});
export function reduceEvent(
  previous: SessionState,
  event: Event,
): SessionState {
  if (event.schema_version !== 1)
    return {
      ...previous,
      error:
        "Unsupported event version. Reload Studio after updating the service.",
    };
  if (event.seq <= previous.seq) return previous;
  const s = structuredClone(previous),
    p = event.payload,
    k = event.kind;
  s.seq = event.seq;
  if (k === "user_text" || !s.run_id) s.run_id = event.run_id;
  if (k === "runtime_state") {
    if (p.lifecycle) s.lifecycle = p.lifecycle;
    if (p.activity) s.activity = p.activity;
    Object.entries(p).forEach(([key, v]) => {
      if (v !== "" && v !== null) s.effective[key] = v;
    });
  } else if (k === "user_text" || k === "agent_text_delta") {
    let msg = s.messages.find((m) => m.id === event.message_id);
    if (!msg) {
      msg = {
        id: event.message_id,
        role: k === "user_text" ? "user" : "assistant",
        actor: event.actor_id,
        text: "",
        complete: k === "user_text",
        timestamp: event.timestamp,
        run_id: event.run_id,
      };
      s.messages.push(msg);
    }
    msg.text += p.text || "";
  } else if (k === "agent_text_end") {
    s.messages
      .filter((m) => m.id === event.message_id)
      .forEach((m) => {
        m.complete = true;
      });
  } else if (["approval_prompt", "ask_user", "plan_checkpoint"].includes(k)) {
    const id = p.call_id || p.ask_id || p.request_id;
    s.interactions[id] = { ...p, id, kind: k, actor_id: event.actor_id };
  } else if (k === "interaction_resolved") delete s.interactions[p.request_id];
  else if (["tool_proposal", "tool_running", "tool_observation"].includes(k))
    s.tools[p.call_id] = {
      ...s.tools[p.call_id],
      ...p,
      actor_id: event.actor_id,
      state: k,
    };
  else if (k === "actor_snapshot") {
    for (const actor of p.actors) {
      s.actors[actor.id] = { ...s.actors[actor.id], ...actor };
    }
  } else if (k.startsWith("subagent_")) {
    const id = p.subagent_id || event.actor_id;
    s.actors[id] = {
      ...s.actors[id],
      ...p,
      id,
      state: k === "subagent_finished" ? "done" : "running",
    };
  } else if (k === "status") s.status = p;
  else if (k === "ralph_iteration" || k === "swarm_phase")
    s.phase = p.phase || "";
  else if (k === "finding" && p.finding_id) s.findings[p.finding_id] = p;
  else if (k === "runtime_error") s.error = p.error;
  if (["closed", "failed", "interrupted"].includes(s.lifecycle)) {
    s.interactions = {};
    Object.values(s.actors).forEach((a) => {
      if (["active", "running", "queued"].includes(a.state))
        a.state = s.lifecycle;
    });
    Object.values(s.tools).forEach((t) => {
      if (t.state === "tool_running") t.state = "tool_interrupted";
    });
    s.messages.forEach((m) => {
      m.complete = true;
    });
  }
  return s;
}
