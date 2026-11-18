import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ModelSelect } from "../src/components/ModelSelect";
import { api } from "../src/api/client";
import type { ModelCatalog } from "../src/api/types";
vi.mock("../src/api/client", () => ({ api: vi.fn() }));
const catalog: ModelCatalog = {
  backend: "codex",
  models: [
    { id: "fixture-one", name: "First model", description: "General model" },
    {
      id: "fixture-future",
      name: "Future specialist",
      description: "A newly discovered model",
    },
  ],
  source: "codex_app_server",
  configured_default: "fixture-one",
  retrieved_at: 1,
  cached_at: null,
  warning: "",
};
function setup(value = "") {
  const change = vi.fn();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <label htmlFor="config-model">Model</label>
      <ModelSelect value={value} onChange={change} />
    </QueryClientProvider>,
  );
  return { change, client };
}
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
describe("discovered Codex model selection", () => {
  it("shows catalog labels and searches without entering a model ID", async () => {
    vi.mocked(api).mockResolvedValue(catalog);
    const { change } = setup();
    await screen.findByRole("option", { name: /Future specialist/ });
    expect(
      screen.getByRole("option", { name: /Codex default/ }),
    ).toHaveTextContent("First model");
    fireEvent.change(
      screen.getByRole("searchbox", { name: "Search Codex models" }),
      { target: { value: "specialist" } },
    );
    expect(
      screen.queryByRole("option", { name: /First model ·/ }),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "Model" }), {
      target: { value: "fixture-future" },
    });
    expect(change).toHaveBeenCalledWith("fixture-future");
    expect(api).toHaveBeenCalledWith("/models?backend=codex");
  });
  it("refreshes catalog options when models change", async () => {
    vi.mocked(api)
      .mockResolvedValueOnce(catalog)
      .mockResolvedValueOnce({
        ...catalog,
        models: [
          { id: "just-added", name: "Just added model", description: "" },
        ],
      });
    setup();
    await screen.findByRole("option", { name: /Future specialist/ });
    fireEvent.click(
      screen.getByRole("button", { name: "Refresh Codex models" }),
    );
    await screen.findByRole("option", { name: /Just added model/ });
    expect(api).toHaveBeenLastCalledWith("/models?backend=codex&refresh=1");
  });
  it("retains a saved model that disappears from the catalog instead of switching it", async () => {
    vi.mocked(api).mockResolvedValue(catalog);
    const { change } = setup("saved-but-absent");
    await screen.findByText(/saved model is not in this catalog/);
    expect(screen.getByRole("combobox")).toHaveValue("saved-but-absent");
    expect(change).not.toHaveBeenCalled();
  });
  it("explains session isolation and preserves explicit account-default selection", async () => {
    vi.mocked(api).mockResolvedValue(catalog);
    const { change } = setup("fixture-future");
    await screen.findByText(
      /Your other Codex sessions and global settings stay unchanged/,
    );
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "" } });
    expect(change).toHaveBeenCalledWith("");
  });
  it("shows cache and discovery-failure states truthfully", async () => {
    vi.mocked(api).mockResolvedValue({
      ...catalog,
      source: "codex_cache",
      warning: "Showing cached model hints; access is checked during the run.",
    });
    setup();
    await screen.findByText(/Showing cached model hints/);
    expect(screen.getByText(/Cached Codex model hints/)).toBeVisible();
  });
  it("normalizes the shared Claude default for the Codex picker without changing stored config", async () => {
    vi.mocked(api).mockResolvedValue(catalog);
    const { change } = setup("claude-existing-default");
    await screen.findByRole("option", { name: /Future specialist/ });
    expect(screen.getByRole("combobox")).toHaveValue("");
    expect(change).not.toHaveBeenCalled();
  });
  it("offers refresh after an API error without discarding an explicit selection", async () => {
    vi.mocked(api)
      .mockRejectedValueOnce(new Error("Service unavailable"))
      .mockResolvedValueOnce(catalog);
    const { change } = setup("fixture-future");
    await screen.findByText("Service unavailable");
    fireEvent.click(
      screen.getByRole("button", { name: "Refresh Codex models" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("combobox")).toHaveValue("fixture-future"),
    );
    expect(change).not.toHaveBeenCalled();
  });
});
