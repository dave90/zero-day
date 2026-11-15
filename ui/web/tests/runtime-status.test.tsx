import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { emptyState, reduceEvent } from "../src/api/reducer";
import { runtimeStatus } from "../src/api/runtime-status";
import { RuntimeProgress } from "../src/components/RuntimeProgress";
import type { Event, SessionState } from "../src/api/types";

afterEach(cleanup);
const running = (): SessionState => ({
  ...emptyState(),
  lifecycle: "active",
  activity: "thinking",
  actors: {
    orchestrator: {
      id: "orchestrator",
      state: "active",
      subkind: "orchestrator",
    },
    p: {
      id: "p",
      parent_id: "orchestrator",
      state: "running",
      subkind: "pentester",
    },
    v: {
      id: "v",
      parent_id: "orchestrator",
      state: "running",
      subkind: "validator",
    },
    done: {
      id: "done",
      parent_id: "orchestrator",
      state: "done",
      subkind: "pentester",
    },
  },
  tools: {
    t: { call_id: "t", tool: "sqlmap", actor_id: "p", state: "tool_running" },
  },
});

describe("assessment progress", () => {
  it("shows actual child work and tools instead of only thinking", () => {
    const status = runtimeStatus(running());
    expect(status.label).toBe("Assessment running");
    expect(status.detail).toContain("1 pentester · 1 validator active");
    expect(status.detail).toContain("In progress: sqlmap");
  });

  it("a completed message does not mark a still-active goal finished", () => {
    const event = {
      schema_version: 1,
      seq: 1,
      kind: "agent_text_end",
      payload: {},
      message_id: "m",
      run_id: "r",
    } as Event;
    const state = reduceEvent(running(), event);
    expect(runtimeStatus(state).running).toBe(true);
    expect(runtimeStatus(state).label).toBe("Assessment running");
  });

  it("makes idle completion explicit while ignoring the root's stale active actor snapshot", () => {
    const state = {
      ...emptyState(),
      lifecycle: "idle",
      activity: "thinking",
      actors: { orchestrator: { id: "orchestrator", state: "active" } },
    };
    render(
      <RuntimeProgress
        state={state}
        connected
        lastEventAt={Date.now() / 1000}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Goal finished");
    expect(screen.getByText(/runtime remains open/)).toBeVisible();
    expect(document.querySelector(".working-dot")).toBeNull();
  });

  it("keeps background validation visible after the main goal goes idle", () => {
    const state = { ...running(), lifecycle: "idle" };
    expect(runtimeStatus(state).label).toBe("Background work running");
    expect(runtimeStatus(state).running).toBe(true);
  });

  it("prioritizes a background agent's pending approval even while the main agent is idle", () => {
    const state = { ...running(), lifecycle: "idle" };
    state.interactions.approval = {
      id: "approval",
      kind: "approval_prompt",
      actor_id: "v",
    };
    expect(runtimeStatus(state).label).toBe("Waiting for your decision");
    expect(runtimeStatus(state).detail).toContain("validator");
    expect(runtimeStatus(state).running).toBe(false);
  });

  it("explains a quiet active run without falsely declaring it complete or stalled", () => {
    render(
      <RuntimeProgress
        state={running()}
        connected
        lastEventAt={Date.now() / 1000 - 310}
      />,
    );
    expect(screen.getByText("Last activity 5m ago")).toBeVisible();
    expect(
      screen.getByText(/runtime has not reported completion/),
    ).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Assessment running");
  });

  it("distinguishes lost live updates from completion and expires animation on terminal states", () => {
    const { rerender } = render(
      <RuntimeProgress state={running()} connected={false} />,
    );
    expect(screen.getByText(/Live updates disconnected/)).toBeVisible();
    for (const lifecycle of ["closed", "interrupted", "failed"]) {
      rerender(
        <RuntimeProgress
          state={{ ...running(), lifecycle }}
          connected={false}
        />,
      );
      expect(document.querySelector(".working-dot")).toBeNull();
      expect(screen.getByRole("status")).not.toHaveTextContent("Goal finished");
    }
  });
});
