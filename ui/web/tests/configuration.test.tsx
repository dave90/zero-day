import { describe, it, expect } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { ModeCards, preset } from "../src/features/configuration";
import type { Config } from "../src/api/types";
const config: Config = {
  execution_mode: "c",
  backend: "codex",
  swarm_size: 1,
  ralph: false,
  ralph_resume: false,
};
describe("mode configuration", () => {
  it("normalizes all mutually exclusive execution paths", () => {
    const swarm = preset(config, "swarm");
    expect(swarm.swarm_size).toBe(2);
    expect(preset(swarm, "ralph")).toMatchObject({
      swarm_size: 1,
      ralph: true,
      execution_mode: "c",
    });
    expect(preset(config, "mcp")).toMatchObject({
      backend: "claude",
      execution_mode: "b",
      ralph: false,
      swarm_size: 1,
    });
    expect(preset(config, "single").execution_mode).toBe("a");
  });
  it("offers five labeled, keyboard accessible mode presets", () => {
    let updated = config;
    render(
      <ModeCards
        config={config}
        onChange={(c) => {
          updated = c;
        }}
      />,
    );
    expect(screen.getAllByRole("button")).toHaveLength(5);
    fireEvent.click(screen.getByRole("button", { name: /MCP delegated/ }));
    expect(updated.backend).toBe("claude");
    cleanup();
  });
});
