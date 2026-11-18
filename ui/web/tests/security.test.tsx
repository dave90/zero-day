import { it, expect } from "vitest";
import { render, cleanup } from "@testing-library/react";
import Markdown from "react-markdown";
it("renders target controlled content without executable HTML", () => {
  const { container } = render(
    <Markdown skipHtml>
      {"<script>alert(1)</script>\n\n[click](javascript:alert(1))"}
    </Markdown>,
  );
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("a")?.getAttribute("href")).not.toContain(
    "javascript:",
  );
  cleanup();
});
