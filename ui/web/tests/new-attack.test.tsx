import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import fields from "../../server/zero_web/config-schema.json";
import type {
  AttackTemplate,
  Config,
  ConfigField,
  Schema,
} from "../src/api/types";
import { NewAttack } from "../src/features/attacks";

const apiMock = vi.hoisted(() => vi.fn());
vi.mock("../src/api/client", () => ({ api: apiMock, uploadFile: vi.fn() }));
const schema: Schema = {
  version: 1,
  fields: fields as ConfigField[],
  defaults: Object.fromEntries(
    fields.map((field) => [field.key, field.default]),
  ),
};
const catalog = {
  models: [
    {
      id: "fixture-model",
      name: "Fixture model",
      description: "Offline model",
    },
  ],
  source: "codex_cache",
  configured_default: "fixture-model",
  warning: "",
};
const template: AttackTemplate = {
  id: "saved-template",
  name: "Saved setup",
  target: "https://template.example",
  context: "# Template context",
  created_at: 1,
  config: {
    ...schema.defaults,
    backend: "claude",
    model: "saved-claude-model",
    goal: "Template goal",
    max_turns: 42,
    pentester_max_turns: 32,
    validator_max_turns: 22,
    max_subagents: 12,
    auto_approve: ["active"],
    scope_host: ["template.example"],
  },
};

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function () {
    this.open = false;
  };
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
function setup(initialTemplate?: AttackTemplate) {
  apiMock.mockImplementation(
    async (path: string, method?: string, body?: unknown) => {
      if (path === "/templates") return [template];
      if (path.startsWith("/models?")) return catalog;
      if (path === "/engagements" && method === "POST")
        return { id: "new-attack", ...(body as object) };
      throw new Error("Unexpected API request: " + path);
    },
  );
  const created = vi.fn();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <NewAttack
        schema={schema}
        initialTemplate={initialTemplate}
        onClose={vi.fn()}
        onCreated={created}
      />
    </QueryClientProvider>,
  );
  return created;
}
function fillIdentity() {
  fireEvent.change(screen.getByLabelText("Attack name"), {
    target: { value: "Configured at creation" },
  });
  fireEvent.change(screen.getByLabelText("Target / start URL"), {
    target: { value: "https://app.example" },
  });
}
function postedConfig(): Config | undefined {
  return apiMock.mock.calls.find(
    ([path, method]) => path === "/engagements" && method === "POST",
  )?.[2]?.config;
}

describe("new attack run configuration", () => {
  it("shows Swarm size only for Swarm and submits the chosen size", async () => {
    const created = setup();
    fillIdentity();
    expect(screen.queryByLabelText("Swarm size")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Swarm / }));
    expect(screen.getByLabelText("Swarm size")).toHaveValue(2);
    fireEvent.change(screen.getByLabelText("Swarm size"), {
      target: { value: "4" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create attack" }));
    await waitFor(() => expect(created).toHaveBeenCalledOnce());
    expect(postedConfig()).toMatchObject({
      execution_mode: "c",
      swarm_size: 4,
      ralph: false,
    });
  });

  it("keeps Swarm selected while an invalid size is edited and blocks creation", async () => {
    const created = setup();
    fillIdentity();
    fireEvent.click(screen.getByRole("button", { name: /^Swarm / }));
    for (const value of ["", "1", "2.5"]) {
      fireEvent.change(screen.getByLabelText("Swarm size"), {
        target: { value },
      });
      expect(screen.getByLabelText("Swarm size")).toBeInvalid();
      expect(screen.getByRole("button", { name: /^Swarm / })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      fireEvent.click(screen.getByRole("button", { name: "Create attack" }));
      expect(created).not.toHaveBeenCalled();
      expect(postedConfig()).toBeUndefined();
    }
    fireEvent.change(screen.getByLabelText("Swarm size"), {
      target: { value: "3" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create attack" }));
    await waitFor(() => expect(created).toHaveBeenCalledOnce());
    expect(postedConfig()?.swarm_size).toBe(3);
  });

  it("hides the field and resets peer count when switching away from Swarm", async () => {
    const created = setup();
    fillIdentity();
    fireEvent.click(screen.getByRole("button", { name: /^Swarm / }));
    fireEvent.change(screen.getByLabelText("Swarm size"), {
      target: { value: "6" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Single agent / }));
    expect(screen.queryByLabelText("Swarm size")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create attack" }));
    await waitFor(() => expect(created).toHaveBeenCalledOnce());
    expect(postedConfig()).toMatchObject({
      execution_mode: "a",
      swarm_size: 1,
    });
  });

  it("prefills and edits the peer count from a saved Swarm template", async () => {
    const created = setup({
      ...template,
      config: { ...template.config, swarm_size: 6 },
    });
    expect(screen.getByLabelText("Swarm size")).toHaveValue(6);
    fireEvent.change(screen.getByLabelText("Swarm size"), {
      target: { value: "5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create attack" }));
    await waitFor(() => expect(created).toHaveBeenCalledOnce());
    expect(postedConfig()?.swarm_size).toBe(5);
  });

  it("offers the shared defaults with manual approvals before creation", async () => {
    const created = setup();
    await screen.findByRole("option", { name: /^Fixture model ·/ });
    expect(screen.getByLabelText("Backend")).toHaveValue("codex");
    for (const label of [
      "Max turns",
      "Pentester max turns",
      "Validator max turns",
    ]) {
      expect(screen.getByLabelText(label)).toHaveValue(50);
    }
    expect(screen.getByLabelText("Max subagents")).toHaveValue(100);
    for (const checkbox of screen.getAllByRole("checkbox"))
      expect(checkbox).not.toBeChecked();
    fillIdentity();
    fireEvent.click(screen.getByRole("button", { name: "Create attack" }));
    await waitFor(() => expect(created).toHaveBeenCalledOnce());
    expect(postedConfig()).toMatchObject({
      max_turns: 50,
      pentester_max_turns: 50,
      validator_max_turns: 50,
      max_subagents: 100,
      auto_approve: [],
      yolo: false,
      i_understand: false,
      allow_host_exec: false,
    });
  });

  it("submits the chosen backend, model, numeric budgets and approvals together", async () => {
    const created = setup();
    fillIdentity();
    fireEvent.change(screen.getByLabelText("Backend"), {
      target: { value: "claude" },
    });
    fireEvent.change(screen.getByLabelText("Model"), {
      target: { value: "chosen-claude-model" },
    });
    for (const [label, value] of [
      ["Max turns", "17"],
      ["Pentester max turns", "23"],
      ["Validator max turns", "29"],
      ["Max subagents", "73"],
    ]) {
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
    }
    fireEvent.click(screen.getByLabelText("exploit"));
    expect(
      screen.getByRole("button", { name: "Create attack" }),
    ).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      'Select "I understand"',
    );
    expect(postedConfig()).toBeUndefined();
    fireEvent.click(screen.getByLabelText("I understand"));
    fireEvent.click(screen.getByLabelText("Yolo"));
    fireEvent.click(screen.getByLabelText("Allow host exec"));
    fireEvent.click(screen.getByRole("button", { name: "Create attack" }));
    await waitFor(() => expect(created).toHaveBeenCalledOnce());
    expect(postedConfig()).toMatchObject({
      backend: "claude",
      model: "chosen-claude-model",
      max_turns: 17,
      pentester_max_turns: 23,
      validator_max_turns: 29,
      max_subagents: 73,
      auto_approve: ["exploit"],
      i_understand: true,
      yolo: true,
      allow_host_exec: true,
    });
  });

  it("retains separate explicit models when switching backends", async () => {
    setup();
    await screen.findByRole("option", { name: /^Fixture model ·/ });
    fireEvent.change(screen.getByLabelText("Model"), {
      target: { value: "fixture-model" },
    });
    fireEvent.change(screen.getByLabelText("Backend"), {
      target: { value: "claude" },
    });
    fireEvent.change(screen.getByLabelText("Model"), {
      target: { value: "claude-choice" },
    });
    fireEvent.change(screen.getByLabelText("Backend"), {
      target: { value: "codex" },
    });
    expect(screen.getByLabelText("Model")).toHaveValue("fixture-model");
    fireEvent.change(screen.getByLabelText("Backend"), {
      target: { value: "claude" },
    });
    expect(screen.getByLabelText("Model")).toHaveValue("claude-choice");
  });

  it("prefills templates, resets blank defaults, and preserves unrelated settings", async () => {
    const created = setup(template);
    expect(screen.getByLabelText("Backend")).toHaveValue("claude");
    expect(screen.getByLabelText("Model")).toHaveValue("saved-claude-model");
    expect(screen.getByLabelText("Max turns")).toHaveValue(42);
    expect(screen.getByLabelText("active")).toBeChecked();
    fireEvent.change(screen.getByLabelText("Start from"), {
      target: { value: "" },
    });
    expect(screen.getByLabelText("Max turns")).toHaveValue(50);
    expect(screen.getByLabelText("Backend")).toHaveValue("codex");
    expect(screen.getByLabelText("active")).not.toBeChecked();
    await screen.findByRole("option", { name: "Saved setup" });
    fireEvent.change(screen.getByLabelText("Start from"), {
      target: { value: template.id },
    });
    fireEvent.change(screen.getByLabelText("Max turns"), {
      target: { value: "37" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create attack" }));
    await waitFor(() => expect(created).toHaveBeenCalledOnce());
    expect(postedConfig()).toMatchObject({ ...template.config, max_turns: 37 });
  });
});
