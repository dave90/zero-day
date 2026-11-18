import type { SessionState } from "./types";

export function runtimeStatus(state?: SessionState) {
  const lifecycle = state?.lifecycle;
  const children = Object.values(state?.actors || {}).filter(
    (a) => a.parent_id && ["active", "running", "queued"].includes(a.state),
  );
  const tools = Object.values(state?.tools || {}).filter(
    (t) => t.state === "tool_running",
  );
  const counts = ["pentester", "validator", "peer"].flatMap((kind) => {
    const n = children.filter((a) => a.subkind === kind).length;
    return n ? [`${n} ${kind}${n === 1 ? "" : "s"}`] : [];
  });
  const work = [
    counts.length ? `${counts.join(" · ")} active` : "",
    tools.length
      ? `In progress: ${[...new Set(tools.map((t) => t.tool))].join(", ")}`
      : "",
  ]
    .filter(Boolean)
    .join(". ");
  if (!state)
    return {
      label: "Not started",
      detail: "Send the first goal when you’re ready.",
      running: false,
    };
  if (lifecycle === "closed")
    return {
      label: "Session ended",
      detail: "The runtime is closed. Conversation and evidence are saved.",
      running: false,
    };
  if (lifecycle === "failed")
    return {
      label: "Run failed",
      detail:
        state.error ||
        "The runtime stopped with an error. Review Activity for details.",
      running: false,
    };
  if (lifecycle === "interrupted")
    return {
      label: "Run interrupted",
      detail: "The runtime disconnected before reporting completion.",
      running: false,
    };
  if (lifecycle === "stopping")
    return {
      label: "Ending session",
      detail: "Closing this attack’s agents, tools, and sandbox.",
      running: true,
    };
  if (Object.keys(state.interactions).length)
    return {
      label: "Waiting for your decision",
      detail: work || "Review the pending request in the conversation.",
      running: false,
    };
  if (lifecycle === "idle") {
    // Finishing a goal does not close its reusable runtime. Background validation
    // can outlive the main answer, so don't present all work as complete yet.
    return children.length || tools.length
      ? {
          label: "Background work running",
          detail: `The main agent is idle. ${work}`,
          running: true,
        }
      : {
          label: "Goal finished",
          detail: "Ready for your next goal. The runtime remains open.",
          running: false,
        };
  }
  if (lifecycle === "starting")
    return {
      label: "Starting assessment",
      detail: "Preparing the runtime and sandbox.",
      running: true,
    };
  return {
    label: "Assessment running",
    detail:
      work ||
      (state.phase
        ? `Current phase: ${state.phase}`
        : state.activity === "thinking"
          ? "Zero is thinking. This goal has not finished."
          : "This goal has not finished."),
    running: true,
  };
}
