import { describe, it, expect } from "vitest";
import { emptyState, reduceEvent } from "../src/api/reducer";
import type { Event } from "../src/api/types";
function event(
  seq: number,
  kind: string,
  payload: Record<string, unknown> = {},
  mid = "m1",
  actor = "orchestrator",
): Event {
  return {
    seq,
    kind,
    payload,
    message_id: mid,
    actor_id: actor,
    schema_version: 1,
    id: "e" + seq,
    session_id: "s1",
    engagement_id: "a1",
    run_id: "r1",
    timestamp: 1,
  };
}
describe("durable event projection", () => {
  it("reconstructs interleaved actors and ignores duplicate events", () => {
    let s = emptyState();
    s = reduceEvent(s, event(1, "agent_text_delta", { text: "First " }, "m1"));
    s = reduceEvent(
      s,
      event(2, "agent_text_delta", { text: "Child" }, "m2", "validator"),
    );
    s = reduceEvent(s, event(3, "agent_text_delta", { text: "answer" }, "m1"));
    s = reduceEvent(s, event(4, "agent_text_end", {}, "m1"));
    expect(s.messages.map((m) => m.text)).toEqual(["First answer", "Child"]);
    expect(s.messages[0].complete).toBe(true);
    expect(
      reduceEvent(s, event(3, "agent_text_delta", { text: "duplicate" })),
    ).toBe(s);
  });
  it("keeps simultaneous decisions distinct and expires them on process loss", () => {
    let s = emptyState();
    s = reduceEvent(s, event(1, "approval_prompt", { call_id: "a" }));
    s = reduceEvent(s, event(2, "ask_user", { ask_id: "b" }));
    s = reduceEvent(s, event(3, "interaction_resolved", { request_id: "a" }));
    expect(Object.keys(s.interactions)).toEqual(["b"]);
    s = reduceEvent(s, event(4, "runtime_state", { lifecycle: "interrupted" }));
    expect(s.interactions).toEqual({});
  });
  it("accepts late tool results and does not turn a cancellation into completion", () => {
    let s = reduceEvent(
      emptyState(),
      event(1, "tool_running", { call_id: "t", tool: "http_replay" }),
    );
    s = reduceEvent(s, event(2, "turn_cancelled"));
    expect(s.tools.t.state).toBe("tool_running");
    s = reduceEvent(
      s,
      event(3, "tool_observation", {
        call_id: "t",
        ok: true,
        output: "result",
      }),
    );
    expect(s.tools.t.output).toBe("result");
  });
  it("reconciles events after a snapshot and rejects unknown versions", () => {
    const s = { ...emptyState(), seq: 100 };
    expect(
      reduceEvent(s, event(99, "runtime_state", { lifecycle: "closed" })),
    ).toBe(s);
    expect(
      reduceEvent(s, { ...event(101, "status"), schema_version: 2 }).error,
    ).toContain("Unsupported event version");
  });
});

it("preserves the active run when an older validator completes, and expires live actor/tool state on close", () => {
  let state = reduceEvent(emptyState(), {
    ...event(1, "user_text", { text: "Next goal" }),
    run_id: "new-goal",
  });
  state = reduceEvent(state, {
    ...event(2, "actor_snapshot", {
      actors: [{ id: "validator", state: "running" }],
    }),
    run_id: "old-goal",
  });
  state = reduceEvent(state, {
    ...event(3, "tool_running", { tool: "http_replay", call_id: "late" }),
    run_id: "old-goal",
  });
  expect(state.run_id).toBe("new-goal");
  state = reduceEvent(
    state,
    event(4, "runtime_state", { lifecycle: "closed" }),
  );
  expect(state.actors.validator.state).toBe("closed");
  expect(state.tools.late.state).toBe("tool_interrupted");
});
