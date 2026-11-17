import { test, expect, type Page } from "../../web/test-support";

for (const backend of ["codex", "claude"]) {
  test(`new attacks apply ${backend} model, budgets and approvals to their first runtime`, async ({
    page,
  }) => {
    await page.route("**/api/v1/models?**", (route) =>
      route.fulfill({
        json: {
          backend: "codex",
          source: "codex_cache",
          configured_default: null,
          warning: "Offline model fixture",
          retrieved_at: 1,
          cached_at: 1,
          models: [
            {
              id: "creation-model",
              name: "Creation model",
              description: "Offline fixture",
            },
          ],
        },
      }),
    );
    await page.goto("/");
    const schema = await (
      await page.request.get("/api/v1/config-schema")
    ).json();
    const csrf = (await (await page.request.get("/api/v1/health")).json())
      .csrf_token;
    // The template preserves the hidden container preference while creation edits
    // the visible settings. Runtime verification remains offline and host-only.
    const source = await (
      await page.request.post("/api/v1/engagements", {
        headers: { "X-Zero-CSRF": csrf },
        data: {
          name: "Offline runtime setup",
          target: "https://fixture.example",
          config: { ...schema.defaults, container: false },
        },
      })
    ).json();
    const template = await (
      await page.request.post("/api/v1/templates", {
        headers: { "X-Zero-CSRF": csrf },
        data: { source_id: source.id, name: "Offline runtime setup" },
      })
    ).json();
    await page
      .getByRole("button", { name: "New attack", exact: true })
      .first()
      .click();
    await page.getByLabel("Start from").selectOption(template.id);
    const name = `${backend} creation settings ` + Date.now();
    await page.getByLabel("Attack name", { exact: true }).fill(name);
    await page.getByLabel("Target / start URL").fill("https://fixture.example");
    await page.getByLabel("First goal").fill("Inspect the offline fixture");
    for (const label of [
      "Max turns",
      "Pentester max turns",
      "Validator max turns",
    ]) {
      await expect(page.getByLabel(label, { exact: true })).toHaveValue("50");
    }
    await expect(page.getByLabel("Max subagents", { exact: true })).toHaveValue(
      "100",
    );
    await page
      .getByLabel("Model", { exact: true })
      .selectOption("creation-model");
    await page.getByLabel("Backend", { exact: true }).selectOption("claude");
    await page
      .getByLabel("Model", { exact: true })
      .fill("claude-creation-choice");
    await page.getByLabel("Backend", { exact: true }).selectOption("codex");
    await expect(page.getByLabel("Model", { exact: true })).toHaveValue(
      "creation-model",
    );
    if (backend === "claude") {
      await page.getByLabel("Backend", { exact: true }).selectOption("claude");
      await expect(page.getByLabel("Model", { exact: true })).toHaveValue(
        "claude-creation-choice",
      );
      await page.getByLabel("Yolo", { exact: true }).check();
      await page.getByLabel("Allow host exec", { exact: true }).check();
    }
    for (const [label, value] of [
      ["Max turns", "17"],
      ["Pentester max turns", "23"],
      ["Validator max turns", "29"],
      ["Max subagents", "73"],
    ]) {
      await page.getByLabel(label, { exact: true }).fill(value);
    }
    await page.getByLabel("safe", { exact: true }).check();
    await page.getByLabel("active", { exact: true }).check();
    await page.getByLabel("exploit", { exact: true }).check();
    await expect(
      page.getByRole("button", { name: "Only create attack", exact: true }),
    ).toBeDisabled();
    await page.getByLabel("I understand", { exact: true }).check();
    await page.screenshot({
      path: `test-results/studio-initial-run-settings-${backend}.png`,
    });
    await page
      .getByRole("button", { name: "Only create attack", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
    const eid = page.url().split("/attacks/")[1].split("/")[0];
    const attack = await (
      await page.request.get(`/api/v1/engagements/${eid}`)
    ).json();
    expect(attack.session_id).toBeNull();
    const expected = {
      backend,
      model: backend === "codex" ? "creation-model" : "claude-creation-choice",
      max_turns: 17,
      pentester_max_turns: 23,
      validator_max_turns: 29,
      max_subagents: 73,
      auto_approve: ["safe", "active", "exploit"],
      i_understand: true,
      yolo: backend === "claude",
      allow_host_exec: backend === "claude",
      container: false,
    };
    expect(attack.config).toMatchObject(expected);
    await page
      .getByRole("button", { name: "Start assessment", exact: true })
      .click();
    await expect
      .poll(
        async () =>
          (await (await page.request.get(`/api/v1/engagements/${eid}`)).json())
            .session_id,
      )
      .toBeTruthy();
    const sid = (
      await (await page.request.get(`/api/v1/engagements/${eid}`)).json()
    ).session_id;
    await expect
      .poll(async () => {
        const session = await (
          await page.request.get(`/api/v1/sessions/${sid}/snapshot`)
        ).json();
        return Object.values(session.snapshot.tools).some(
          (tool: { tool?: string; ok?: boolean }) =>
            tool.tool === "offline_fixture" && tool.ok === true,
        );
      })
      .toBe(true);
    const running = await (
      await page.request.get(`/api/v1/sessions/${sid}/snapshot`)
    ).json();
    expect(running.config).toMatchObject(expected);
    expect(
      Object.values(running.snapshot.interactions).some(
        (item: { kind?: string }) => item.kind === "approval_prompt",
      ),
    ).toBe(false);
    await page
      .getByRole("button", { name: "End runtime session", exact: true })
      .click();
    await page
      .getByRole("button", { name: "End session", exact: true })
      .click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.getByRole("button", { name: "Configure", exact: true }).click();
    await expect(page.getByLabel("Backend", { exact: true })).toHaveValue(
      backend,
    );
    await page.getByRole("button", { name: /Agents & budgets/ }).click();
    for (const [label, value] of [
      ["Max turns", "17"],
      ["Pentester max turns", "23"],
      ["Validator max turns", "29"],
      ["Max subagents", "73"],
    ]) {
      await expect(page.getByLabel(label, { exact: true })).toHaveValue(value);
    }
    await page.getByRole("button", { name: /Model & sessions/ }).click();
    await expect(page.getByLabel("Model", { exact: true })).toHaveValue(
      expected.model,
    );
    await page.getByRole("button", { name: /06 Approvals/ }).click();
    for (const risk of ["safe", "active", "exploit"]) {
      await expect(page.getByLabel(risk, { exact: true })).toBeChecked();
    }
    await expect(
      page.getByLabel("I understand", { exact: true }),
    ).toBeChecked();
    await expect(page.getByLabel("Yolo", { exact: true })).toBeChecked({
      checked: backend === "claude",
    });
    await expect(
      page.getByLabel("Allow host exec", { exact: true }),
    ).toBeChecked({ checked: backend === "claude" });
    // Remove this test's fixture so the template-management test starts empty.
    expect(
      (
        await page.request.delete(`/api/v1/templates/${template.id}`, {
          headers: { "X-Zero-CSRF": csrf },
        })
      ).ok(),
    ).toBe(true);
  });
}

test("new Swarm attacks save the selected size and start that many peers", async ({
  page,
}) => {
  await page.route("**/api/v1/models?**", (route) =>
    route.fulfill({
      json: {
        backend: "codex",
        models: [],
        source: "codex_cache",
        configured_default: null,
        warning: "Offline fixture",
        retrieved_at: 1,
        cached_at: 1,
      },
    }),
  );
  await page.goto("/");
  const schema = await (await page.request.get("/api/v1/config-schema")).json();
  const csrf = (await (await page.request.get("/api/v1/health")).json())
    .csrf_token;
  const source = await (
    await page.request.post("/api/v1/engagements", {
      headers: { "X-Zero-CSRF": csrf },
      data: {
        name: "Offline Swarm setup",
        target: "https://fixture.example",
        config: {
          ...schema.defaults,
          container: false,
          auto_approve: ["safe", "active", "exploit"],
          i_understand: true,
        },
      },
    })
  ).json();
  const template = await (
    await page.request.post("/api/v1/templates", {
      headers: { "X-Zero-CSRF": csrf },
      data: { source_id: source.id, name: "Offline Swarm setup" },
    })
  ).json();
  await page
    .getByRole("button", { name: "New attack", exact: true })
    .first()
    .click();
  const dialog = page.getByRole("dialog", { name: "A new assessment" });
  await dialog.getByLabel("Start from").selectOption(template.id);
  const name = "Three-peer creation " + Date.now();
  await dialog.getByLabel("Attack name", { exact: true }).fill(name);
  await dialog
    .getByLabel("First goal")
    .fill("Inspect the offline Swarm fixture");
  await expect(dialog.getByLabel("Swarm size", { exact: true })).toHaveCount(0);
  const swarm = dialog.getByRole("button", { name: /^Swarm / });
  await swarm.click();
  const size = dialog.getByLabel("Swarm size", { exact: true });
  await expect(size).toHaveValue("2");
  for (const invalid of ["", "1", "2.5"]) {
    await size.fill(invalid);
    await dialog
      .getByRole("button", { name: "Only create attack", exact: true })
      .click();
    await expect(dialog).toBeVisible();
    await expect(swarm).toHaveAttribute("aria-pressed", "true");
    await expect(size).toBeVisible();
  }
  await size.fill("3");
  await page.screenshot({ path: "test-results/studio-new-swarm-size.png" });
  await dialog
    .getByRole("button", { name: "Only create attack", exact: true })
    .click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  const eid = page.url().split("/attacks/")[1].split("/")[0];
  const attack = await (
    await page.request.get(`/api/v1/engagements/${eid}`)
  ).json();
  expect(attack.config).toMatchObject({
    execution_mode: "c",
    swarm_size: 3,
    ralph: false,
  });
  await page
    .getByRole("button", { name: "Start assessment", exact: true })
    .click();
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/v1/engagements/${eid}`)).json())
          .session_id,
    )
    .toBeTruthy();
  const sid = (
    await (await page.request.get(`/api/v1/engagements/${eid}`)).json()
  ).session_id;
  await expect
    .poll(async () => {
      const runtime = await (
        await page.request.get(`/api/v1/sessions/${sid}/snapshot`)
      ).json();
      return Object.values(runtime.snapshot.actors).filter(
        (actor: { subkind?: string }) => actor.subkind === "peer",
      ).length;
    })
    .toBe(3);
  const runtime = await (
    await page.request.get(`/api/v1/sessions/${sid}/snapshot`)
  ).json();
  expect(runtime.config.swarm_size).toBe(3);
  await page
    .getByRole("button", { name: "End runtime session", exact: true })
    .click();
  await page.getByRole("button", { name: "End session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await expect(page.getByLabel("Swarm size", { exact: true })).toHaveValue("3");
  expect(
    (
      await page.request.delete(`/api/v1/templates/${template.id}`, {
        headers: { "X-Zero-CSRF": csrf },
      })
    ).ok(),
  ).toBe(true);
});

test("findings show severity and combine severity, verdict, and search filters", async ({
  page,
}) => {
  await page.goto("/");
  await createAttack(page, "Severity fixture " + Date.now());
  const eid = page.url().split("/attacks/")[1].split("/")[0];
  const vector =
    "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N";
  const record = (
    id: string,
    title: string,
    verdict: string,
    hint: string,
    score: number | null,
  ) => ({
    id,
    verdict,
    submitted_by: "fixture",
    created_at: 1,
    duplicate_of: null,
    artifact_id: id,
    finding: {
      title,
      summary: "Synthetic severity fixture",
      endpoint: `/fixture/${id}`,
      method: "GET",
      cwe: "CWE-79",
      severity_hint: hint,
      evidence: "Synthetic evidence",
      notes: "",
    },
    validation:
      score === null
        ? null
        : {
            verdict,
            rationale: "Fixture validator",
            reproducer: "Read fixture",
            cvss40_score: score,
            cvss40_vector:
              score === 0
                ? "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:N/VI:N/VA:N/SC:N/SI:N/SA:N"
                : vector,
            cwe: "CWE-79",
            additional_evidence: "",
          },
  });
  await page.route(`**/api/v1/engagements/${eid}/findings*`, (route) =>
    route.fulfill({
      json: [
        record("critical", "Critical fixture", "confirmed", "low", 9.3),
        record("high", "High fixture", "confirmed", "critical", 7.1),
        record("medium", "Stored snippet fixture", "pending", "medium", null),
        record("low", "Low fixture", "rejected", "medium", 3.1),
        record("zero", "Zero impact fixture", "rejected", "high", 0),
        record("unknown", "Unknown fixture", "pending", "P1-ish", null),
      ],
    }),
  );
  const findingsTab = page
    .locator(".view-tabs")
    .getByRole("button", { name: /^Findings/ });
  await findingsTab.click();
  await expect(page.locator(".finding-row")).toHaveCount(6);
  await expect(page.locator(".severity-badge.critical")).toHaveText("Critical");
  await expect(page.locator(".severity-badge.medium")).toHaveText(
    /Medium.*estimate/,
  );
  await expect(page.locator(".severity-badge.unrated")).toHaveText("Unrated");
  const severity = page.getByLabel("Severity", { exact: true });
  await expect(page.locator(".findings-filters .segmented")).toHaveCount(0);
  const verdict = page.locator(".stats-strip");
  await severity.selectOption("critical");
  await expect(page.locator(".finding-row")).toHaveCount(1);
  await expect(page.locator(".finding-row")).toContainText("CVSS 4.0 · 9.3");
  await verdict.getByRole("button", { name: /rejected$/ }).click();
  await expect(
    page.getByRole("heading", { name: "No matching findings" }),
  ).toBeVisible();
  await expect(severity).toHaveValue("critical");
  await severity.selectOption("none");
  await expect(page.locator(".finding-row")).toContainText(
    "Zero impact fixture",
  );
  await expect(page.locator(".finding-row")).toContainText("CVSS 4.0 · 0.0");
  await page.locator(".finding-row").click();
  await expect(page.getByRole("dialog").locator(".severity-badge")).toHaveText(
    "None",
  );
  await page.getByRole("button", { name: "Close dialog" }).click();
  await verdict.getByRole("button", { pressed: true }).click();
  await severity.selectOption("medium");
  await verdict.getByRole("button", { name: /pending$/ }).click();
  await page.getByLabel("Search findings").fill("missing");
  await expect(
    page.getByRole("heading", { name: "No matching findings" }),
  ).toBeVisible();
  await page.getByLabel("Search findings").fill("Stored snippet");
  await expect(page.locator(".finding-row")).toHaveCount(1);
  await page.getByRole("button", { name: "Agents", exact: true }).click();
  await findingsTab.click();
  await expect(severity).toHaveValue("medium");
  await expect(page.getByLabel("Search findings")).toHaveValue(
    "Stored snippet",
  );
  await page.locator(".finding-row").click();
  await expect(page.getByRole("dialog")).toContainText(
    "Medium · agent estimate",
  );
  await expect(page.getByRole("dialog")).toContainText("Unscored");
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.getByLabel("Search findings").fill("");
  await verdict.getByRole("button", { pressed: true }).click();
  await severity.selectOption("all");
  await expect(page.locator(".finding-row")).toHaveCount(6);
  await page.screenshot({ path: "test-results/studio-finding-severity.png" });
});

test("templates reuse setup in fresh attacks and survive source deletion", async ({
  page,
}) => {
  await page.goto("/");
  const source = "Template source " + Date.now();
  await createAttack(page, source);
  const sourceId = page.url().split("/attacks/")[1].split("/")[0];
  const csrf = (await (await page.request.get("/api/v1/health")).json())
    .csrf_token;
  const schema = await (await page.request.get("/api/v1/config-schema")).json();
  const config = {
    ...schema.defaults,
    model: "future-template-model",
    max_turns: 42,
    goal: "Original saved goal",
    ralph: true,
    ralph_resume: true,
  };
  await page.request.patch(`/api/v1/engagements/${sourceId}`, {
    headers: { "X-Zero-CSRF": csrf },
    data: { config },
  });
  await page.request.post(`/api/v1/engagements/${sourceId}/files`, {
    headers: { "X-Zero-CSRF": csrf },
    data: { name: "source.txt", content: "Do not copy evidence" },
  });
  await page
    .getByRole("button", { name: `Manage ${source}`, exact: true })
    .click();
  await page.getByRole("button", { name: /Create template/ }).click();
  await page.getByLabel("Template name").fill("Reusable setup");
  await page.getByLabel("Include engagement context (pentest.md)").check();
  await page.getByRole("button", { name: "Save & create attack" }).click();
  await expect(page.getByLabel("Start from")).toHaveValue(/.+/);
  await expect(page.getByLabel("First goal")).toHaveValue(
    "Original saved goal",
  );
  await expect(page.getByLabel("Target / start URL")).toHaveValue(
    "https://fixture.example",
  );
  const copy = "Fresh copy " + Date.now();
  await page.getByLabel("Attack name").fill(copy);
  await page.getByLabel("Target / start URL").fill("https://another.example");
  await page
    .getByRole("button", { name: "Only create attack", exact: true })
    .click();
  await expect(page.getByRole("heading", { name: copy })).toBeVisible();
  const copyId = page.url().split("/attacks/")[1].split("/")[0];
  const fresh = await (
    await page.request.get(`/api/v1/engagements/${copyId}`)
  ).json();
  expect(fresh.session_id).toBeNull();
  expect(fresh.config).toMatchObject({
    model: "future-template-model",
    max_turns: 42,
    ralph: true,
    ralph_resume: false,
  });
  expect(
    await (
      await page.request.get(`/api/v1/engagements/${copyId}/conversation`)
    ).json(),
  ).toEqual([]);
  const artifacts = await (
    await page.request.get(`/api/v1/engagements/${copyId}/artifacts`)
  ).json();
  expect(
    artifacts.some((a: { path: string }) => a.path.endsWith("source.txt")),
  ).toBe(false);
  await page
    .getByRole("button", { name: "All attacks", exact: false })
    .first()
    .click();
  await page
    .getByRole("button", { name: `Manage ${source}`, exact: true })
    .click();
  await page.getByRole("button", { name: /Delete attack/ }).click();
  await expect(
    page.getByRole("button", { name: "Delete permanently" }),
  ).toBeDisabled();
  await page.getByLabel("Type the attack name to delete").fill(source);
  await page.screenshot({ path: "test-results/studio-delete-attack.png" });
  await page.getByRole("button", { name: "Delete permanently" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: `Manage ${source}`, exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: `Manage ${copy}`, exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "New attack", exact: true })
    .first()
    .click();
  await page.getByLabel("Start from").selectOption({ label: "Reusable setup" });
  await expect(page.getByLabel("First goal")).toHaveValue(
    "Original saved goal",
  );
  await page.screenshot({ path: "test-results/studio-template.png" });
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.reload();
  expect(
    (await page.request.get(`/api/v1/engagements/${sourceId}`)).status(),
  ).toBe(404);
  await page.request.post("/api/v1/templates", {
    headers: { "X-Zero-CSRF": csrf },
    data: { source_id: copyId, name: "Keep setup" },
  });
  await page
    .locator(".overview-actions")
    .getByRole("button", { name: "Templates", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await page
    .getByRole("button", {
      name: "Delete template Reusable setup",
      exact: true,
    })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Attacks already created from this template keep their configuration",
  );
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(
    page.getByRole("article", { name: "Reusable setup", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", {
      name: "Delete template Reusable setup",
      exact: true,
    })
    .click();
  await page.screenshot({ path: "test-results/studio-delete-template.png" });
  await page
    .getByRole("button", { name: "Delete template", exact: true })
    .click();
  await expect(
    page.getByRole("article", { name: "Reusable setup", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("article", { name: "Keep setup", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("status").filter({ hasText: "Deleted template" }),
  ).toBeVisible();
  const preserved = await (
    await page.request.get(`/api/v1/engagements/${copyId}`)
  ).json();
  expect(preserved.config).toEqual(fresh.config);
  await page.screenshot({ path: "test-results/studio-templates.png" });
  await page.getByRole("button", { name: "Done", exact: true }).click();
  await page
    .locator(".attack-sidebar")
    .getByRole("button", { name: "Templates", exact: true })
    .click();
  await page
    .getByRole("article", { name: "Keep setup", exact: true })
    .getByRole("button", { name: "Create attack", exact: true })
    .click();
  await page.getByLabel("Attack name").fill("Preserved template draft");
  await page.getByLabel("First goal").fill("Preserve my unsent goal");
  await page
    .getByRole("button", { name: "Manage templates", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await page
    .getByRole("button", { name: "Delete template Keep setup", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Delete template", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "No saved templates", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Back to assessment", exact: true })
    .click();
  await expect(page.getByLabel("Start from")).toHaveValue("");
  await expect(page.getByLabel("Attack name")).toHaveValue(
    "Preserved template draft",
  );
  await expect(page.getByLabel("First goal")).toHaveValue(
    "Preserve my unsent goal",
  );
  await page
    .getByRole("button", { name: "Only create attack", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Preserved template draft",
      exact: true,
    }),
  ).toBeVisible();
  expect(await (await page.request.get("/api/v1/templates")).json()).toEqual(
    [],
  );
  expect(
    (await page.request.get(`/api/v1/engagements/${copyId}`)).status(),
  ).toBe(200);
});

test("deleting an open attack requires confirmation and closes its runtime", async ({
  page,
}) => {
  await page.goto("/");
  const name = "Delete runtime " + Date.now();
  await createAttack(page, name);
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /Single agent/ })
    .click();
  await page.getByRole("button", { name: "05 Sandbox & network" }).click();
  await page.getByLabel("Container", { exact: true }).uncheck();
  await page.getByRole("button", { name: "Save configuration" }).click();
  await page
    .getByRole("button", { name: "Start assessment", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Tool approval" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: `Manage ${name}`, exact: true })
    .click();
  await page.getByRole("button", { name: /Delete attack/ }).click();
  await page.getByLabel("Type the attack name to delete").fill(name);
  await expect(
    page.getByRole("button", { name: "Delete permanently" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page.getByRole("button", { name: "Close dialog" }).click();
  await expect(
    page.getByRole("region", { name: "Tool approval" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: `Manage ${name}`, exact: true })
    .click();
  await page.getByRole("button", { name: /Delete attack/ }).click();
  await page.getByLabel("Type the attack name to delete").fill(name);
  await page.getByLabel("End any runtime belonging to this attack").check();
  await page.getByRole("button", { name: "Delete permanently" }).click();
  await expect(
    page.getByRole("heading", { name: "Every attack starts with a question." }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: new RegExp(name) }),
  ).toHaveCount(0);
});
async function createAttack(page: Page, name: string) {
  await page
    .getByRole("button", { name: "New attack", exact: true })
    .first()
    .click();
  await page.getByLabel("Attack name").fill(name);
  await page.getByLabel("Target / start URL").fill("https://fixture.example");
  await page
    .getByLabel("First goal")
    .fill("Inspect the synthetic access control fixture");
  await page
    .getByRole("button", { name: "Only create attack", exact: true })
    .click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
}
test("isolated conversation, configuration, approval, validation, history", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  const name = "Browser fixture " + Date.now();
  await createAttack(page, name);
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /Single agent/ })
    .click();
  await page.getByRole("button", { name: "05 Sandbox & network" }).click();
  await page.getByLabel("Container", { exact: true }).uncheck();
  await page.screenshot({ path: "test-results/studio-configuration.png" });
  await page.getByRole("button", { name: "Save configuration" }).click();
  await page
    .getByRole("button", { name: "Start assessment", exact: true })
    .click();
  const approval = page.getByRole("region", { name: "Tool approval" });
  await expect(approval).toBeVisible();
  await expect(approval).toContainText("offline_fixture");
  await expect(
    page.getByRole("region", { name: "Assessment progress" }),
  ).toContainText("Waiting for your decision");
  await page.screenshot({ path: "test-results/studio-approval.png" });
  await page.reload();
  await expect(approval).toBeVisible();
  const second = await page.context().newPage();
  await second.goto(page.url());
  await expect(
    second.getByRole("region", { name: "Tool approval" }),
  ).toBeVisible();
  await approval
    .getByRole("button", { name: "Allow once", exact: true })
    .click();
  await expect(
    second.getByRole("region", { name: "Tool approval" }),
  ).toHaveCount(0);
  await second.close();
  await page
    .getByRole("region", { name: "Operator question" })
    .getByRole("button", { name: "Orders", exact: true })
    .click();
  const progress = page.getByRole("region", { name: "Assessment progress" });
  await expect(progress.getByRole("status")).toHaveText("Goal finished");
  await expect(progress.locator(".working-dot")).toHaveCount(0);
  await page.reload();
  await expect(progress.getByRole("status")).toHaveText("Goal finished");
  await page.screenshot({ path: "test-results/studio-goal-finished.png" });
  await page
    .locator(".view-tabs")
    .getByRole("button", { name: /^Findings/ })
    .click();
  await expect(
    page.getByRole("heading", { name: "Synthetic access control fixture" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: /confirmed Synthetic access control fixture/ })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Independent scripted validator fixture",
  );
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  const findingDialog = page.getByRole("dialog");
  const exportUrl = await findingDialog
    .getByRole("link", { name: "Export finding" })
    .getAttribute("href");
  const exportedFinding = await (await page.request.get(exportUrl!)).text();
  await findingDialog
    .getByRole("button", { name: "Copy finding to clipboard" })
    .click();
  await expect(findingDialog.getByRole("status")).toHaveText(
    "Finding copied to clipboard.",
  );
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    exportedFinding,
  );
  await page.screenshot({ path: "test-results/studio-finding-detail.png" });
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "Agents", exact: true }).click();
  await expect(page.locator(".agent-row")).toHaveCount(1);
  await page.locator(".agent-row").first().click();
  await page.getByRole("button", { name: "memory", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText(
    ".pentest/memory/agent.md",
  );
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await expect(
    page.getByRole("region", { name: "Agent memory" }),
  ).toContainText("Isolated actor");
  await page.getByRole("button", { name: "todos", exact: true }).click();
  const todos = page.getByRole("region", { name: "Agent todos" });
  await expect(todos).toContainText("Inspect the synthetic fixture");
  await expect(todos).toContainText("Review independent validation");
  await expect(todos).toContainText("in progress");
  await expect(todos.locator("li")).toHaveCount(3);
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await page.screenshot({ path: "test-results/studio-agent-todos.png" });
  await page.getByRole("button", { name: "memory", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Agent memory" }),
  ).toContainText("Isolated actor");
  await page.screenshot({ path: "test-results/studio-agent-memory.png" });
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "End runtime session" }).click();
  await page.getByRole("button", { name: "End session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Workspace", exact: true })
    .last()
    .click();
  await page
    .getByRole("button", { name: "Runs & snapshots", exact: true })
    .click();
  await expect(page.locator(".run-card").first()).toContainText("closed");
  await page.locator(".run-card").first().click();
  await expect(page.locator(".run-card").first()).toContainText("Sealed");
  expect(errors).toEqual([]);
});
test("switching attacks preserves drafts; Studio fits desktop widths", async ({
  page,
}) => {
  await page.goto("/");
  const a = "Draft Alpha " + Date.now(),
    b = "Draft Beta " + Date.now();
  await createAttack(page, a);
  await page
    .getByRole("textbox", { name: "Assessment goal or message" })
    .fill("Keep this first draft");
  await page.waitForTimeout(550);
  await createAttack(page, b);
  await page
    .getByRole("textbox", { name: "Assessment goal or message" })
    .fill("Keep this second draft");
  await page.waitForTimeout(550);
  await page
    .getByRole("button", { name: new RegExp(a) })
    .first()
    .click();
  await expect(
    page.getByRole("textbox", { name: "Assessment goal or message" }),
  ).toHaveValue("Keep this first draft");
  await page.reload();
  await expect(
    page.getByRole("textbox", { name: "Assessment goal or message" }),
  ).toHaveValue("Keep this first draft");
  for (const [width, height] of [
    [1100, 800],
    [1280, 800],
    [1440, 900],
    [1728, 1117],
    [1920, 1080],
  ]) {
    await page.setViewportSize({ width, height });
    await expect(
      page.getByRole("button", { name: "Start assessment", exact: true }),
    ).toBeInViewport();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `test-results/studio-${width}.png`,
      fullPage: true,
    });
  }
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Configure", exact: true }),
  ).toBeFocused();
});

test("Ralph has revision-specific checkpoints and structured historical plans", async ({
  page,
}) => {
  await page.goto("/");
  await createAttack(page, "Ralph fixture " + Date.now());
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  const config = page.getByRole("dialog");
  await config.getByRole("button", { name: /^Ralph Plan/ }).click();
  await config.getByRole("button", { name: "05 Sandbox & network" }).click();
  await config.getByLabel("Container", { exact: true }).uncheck();
  await config.getByRole("button", { name: "Save configuration" }).click();
  await page
    .getByRole("button", { name: "Start assessment", exact: true })
    .click();
  const checkpoint = page.getByRole("region", { name: "Plan checkpoint" });
  await expect(checkpoint).toBeVisible();
  await page.screenshot({ path: "test-results/studio-ralph-checkpoint.png" });
  await checkpoint
    .getByLabel("Plan revision feedback")
    .fill("Focus on independently reviewed evidence");
  await checkpoint
    .getByRole("button", { name: "Request revision", exact: true })
    .click();
  await expect(checkpoint).toContainText("draft 3");
  await checkpoint
    .getByRole("button", { name: "Approve plan", exact: true })
    .click();
  await expect(checkpoint).toHaveCount(0);
  await expect(page.locator(".runtime-card")).toContainText("Session ended");
  await page
    .getByRole("button", { name: "Workspace", exact: true })
    .last()
    .click();
  await page
    .getByRole("button", { name: "Ralph plan & evidence", exact: true })
    .click();
  await page
    .locator(".file-row")
    .filter({ hasText: ".ralph/prd.json" })
    .click();
  await expect(page.getByRole("dialog")).toContainText("ACCEPTANCE CRITERIA");
  await expect(page.getByRole("dialog")).toContainText("completed");
  await page.screenshot({ path: "test-results/studio-ralph-plan.png" });
});

test("Swarm exposes real peers, ownership work, and messages", async ({
  page,
}) => {
  await page.goto("/");
  await createAttack(page, "Swarm fixture " + Date.now());
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  const config = page.getByRole("dialog");
  await config.getByRole("button", { name: /^Swarm Peers/ }).click();
  await config.getByRole("button", { name: "05 Sandbox & network" }).click();
  await config.getByLabel("Container", { exact: true }).uncheck();
  await config.getByRole("button", { name: "Save configuration" }).click();
  await page
    .getByRole("button", { name: "Start assessment", exact: true })
    .click();
  await expect(page.locator(".runtime-card")).toContainText("Goal finished");
  await page
    .getByRole("button", { name: "Workspace", exact: true })
    .last()
    .click();
  await page
    .getByRole("button", { name: "Swarm coordination", exact: true })
    .click();
  await expect(page.locator(".swarm-section")).toContainText([
    "workers",
    "work items",
    "messages",
    "groups",
    "evidence",
    "events",
  ]);
  await page.screenshot({ path: "test-results/studio-swarm-ledger.png" });
  await page.getByRole("button", { name: "Agents", exact: true }).click();
  await expect(
    page.locator(".agent-row-text > strong").filter({ hasText: /^peer / }),
  ).toHaveCount(2);
  await expect(
    page
      .locator(".agent-row-text > strong")
      .filter({ hasText: /^validator / })
      .first(),
  ).toBeVisible();
  const eid = page.url().split("/attacks/")[1].split("/")[0];
  const attack = await (
    await page.request.get(`/api/v1/engagements/${eid}`)
  ).json();
  const session = await (
    await page.request.get(`/api/v1/sessions/${attack.session_id}/snapshot`)
  ).json();
  const actors = Object.values(session.snapshot.actors) as {
    id: string;
    subkind: string;
    parent_id: string;
  }[];
  const peers = actors.filter((actor) => actor.subkind === "peer");
  expect(peers).toHaveLength(2);
  for (const peer of peers) {
    const owned = actors.filter((actor) => actor.parent_id === peer.id);
    expect(owned.some((actor) => actor.subkind === "pentester")).toBe(true);
    expect(owned.some((actor) => actor.subkind === "validator")).toBe(true);
    const group = page.getByRole("group", {
      name: `Agents under ${peer.id}`,
      exact: true,
    });
    await expect(group.locator(".agent-row")).toHaveCount(owned.length);
    expect(
      (
        await group.locator(".agent-row-text > strong > span").allTextContents()
      ).sort(),
    ).toEqual(owned.map((actor) => actor.id).sort());
  }
  await page.screenshot({ path: "test-results/studio-swarm-agents.png" });
  const firstGroup = page.getByRole("group", {
    name: `Agents under ${peers[0].id}`,
    exact: true,
  });
  await page
    .getByRole("button", {
      name: `Collapse agents under ${peers[0].id}`,
      exact: true,
    })
    .click();
  await expect(firstGroup).toBeHidden();
  await expect(
    page.getByRole("group", {
      name: `Agents under ${peers[1].id}`,
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page
    .getByRole("region", { name: `Peer ${peers[0].id}`, exact: true })
    .locator(".peer-agent-heading .agent-row")
    .click();
  await expect(page.getByRole("dialog")).toContainText(peers[0].id);
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page
    .getByRole("button", { name: "Workspace", exact: true })
    .last()
    .click();
  await page.getByRole("button", { name: "Agents", exact: true }).click();
  await expect(firstGroup).toBeHidden();
  const expand = page.getByRole("button", {
    name: `Expand agents under ${peers[0].id}`,
    exact: true,
  });
  await expect(expand).toHaveAttribute("aria-expanded", "false");
  await expand.focus();
  await page.keyboard.press("Enter");
  await expect(firstGroup).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: `Collapse agents under ${peers[0].id}`,
      exact: true,
    }),
  ).toHaveAttribute("aria-expanded", "true");
  const firstChild = actors.find((actor) => actor.parent_id === peers[0].id)!;
  await firstGroup
    .locator(".agent-row")
    .filter({ has: page.getByText(firstChild.id, { exact: true }) })
    .click();
  await expect(page.getByRole("dialog")).toContainText(firstChild.id);
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "End runtime session" }).click();
  await page.getByRole("button", { name: "End session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("recorded audit import/replay/reset stays separate from live model execution", async ({
  page,
}) => {
  await page.goto("/");
  await createAttack(page, "Recorded audit " + Date.now());
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  const config = page.getByRole("dialog");
  await config.getByRole("button", { name: "05 Sandbox & network" }).click();
  await config.getByLabel("Container", { exact: true }).uncheck();
  await config.getByRole("button", { name: "Save configuration" }).click();
  await page
    .getByRole("textbox", { name: "Assessment goal or message" })
    .fill("/history");
  await page
    .getByRole("textbox", { name: "Assessment goal or message" })
    .press("Enter");
  await expect(
    page.getByRole("heading", { name: "Runs & snapshots", exact: true }),
  ).toBeVisible();
  const recording =
    JSON.stringify({
      kind: "agent_step",
      raw: JSON.stringify({
        type: "final",
        answer: "Recorded UI envelope answer",
      }),
    }) + "\n";
  await page.locator('input[accept=".jsonl,.ndjson"]').setInputFiles({
    name: "audit.jsonl",
    mimeType: "application/x-ndjson",
    buffer: Buffer.from(recording),
  });
  await page.getByRole("button", { name: "Replay audit", exact: true }).click();
  await page
    .getByRole("button", { name: "Start replay session", exact: true })
    .click();
  await expect(
    page
      .locator(".test-environment")
      .filter({ hasText: "Recorded audit replay" }),
  ).toBeVisible();
  await expect(
    page.getByText("Recorded UI envelope answer", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "End runtime session" }).click();
  await page.getByRole("button", { name: "End session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Workspace", exact: true })
    .last()
    .click();
  await page
    .getByRole("button", { name: "Runs & snapshots", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Reset .pentest", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText("sealed history");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Reset .pentest", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator(".run-card").first()).toContainText("Sealed");
});

test("discovers, searches, refreshes, and saves a model for only one Zero conversation", async ({
  page,
}) => {
  let refreshed = false;
  await page.route("**/api/v1/models?**", async (route) => {
    if (route.request().url().includes("refresh=1")) refreshed = true;
    await route.fulfill({
      json: {
        backend: "codex",
        source: "codex_app_server",
        configured_default: "fixture-coding-model",
        warning: "",
        retrieved_at: 1,
        cached_at: null,
        models: [
          {
            id: "fixture-coding-model",
            name: "Coding model",
            description: "Fixture account default",
          },
          {
            id: "fixture-security-model",
            name: "Security specialist",
            description: "Fixture model for this Zero session",
          },
          ...(refreshed
            ? [
                {
                  id: "fixture-new-catalog-model",
                  name: "New catalog model",
                  description: "Newly discovered option",
                },
              ]
            : []),
        ],
      },
    });
  });
  await page.goto("/");
  const name = "Model selection " + Date.now();
  await createAttack(page, name);
  const selectedId = page.url().split("/attacks/")[1].split("/")[0];
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  const config = page.getByRole("dialog");
  await config.getByRole("button", { name: "04 Model & sessions" }).click();
  await expect(
    config.getByRole("option", { name: /Security specialist/ }),
  ).toBeAttached();
  await config
    .getByRole("searchbox", { name: "Search Codex models" })
    .fill("security");
  await config
    .getByRole("combobox", { name: "Model", exact: true })
    .selectOption("fixture-security-model");
  await expect(config).toContainText(
    "Your other Codex sessions and global settings stay unchanged",
  );
  await config.getByRole("searchbox", { name: "Search Codex models" }).fill("");
  await config.getByRole("button", { name: "Refresh Codex models" }).click();
  await expect(
    config.getByRole("option", { name: /New catalog model/ }),
  ).toBeAttached();
  await expect(
    config.getByRole("combobox", { name: "Model", exact: true }),
  ).toHaveValue("fixture-security-model");
  await page.screenshot({ path: "test-results/studio-model-selection.png" });
  await config.getByRole("button", { name: "Save configuration" }).click();
  await createAttack(page, "Other model conversation " + Date.now());
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await config.getByRole("button", { name: "04 Model & sessions" }).click();
  await expect(
    config.getByRole("combobox", { name: "Model", exact: true }),
  ).toHaveValue("");
  await config.getByRole("button", { name: "Close dialog" }).click();
  await page.goto("/attacks/" + selectedId + "/conversation");
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await config.getByRole("button", { name: "04 Model & sessions" }).click();
  await expect(
    config.getByRole("combobox", { name: "Model", exact: true }),
  ).toHaveValue("fixture-security-model");
});

test("fills blank pentest.md from the editable template and applies it to the first runtime", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page
    .getByRole("button", { name: "New attack", exact: true })
    .first()
    .click();
  const context = page.getByLabel("Engagement context pentest.md", {
    exact: true,
  });
  await expect(context).toHaveValue("");
  const name = "Generated engagement " + Date.now();
  await page.getByLabel("Attack name", { exact: true }).fill(name);
  await page
    .getByLabel("Target / start URL")
    .fill("https://fixture.example/assessment/");
  await page
    .getByLabel("First goal", { exact: true })
    .fill("Review cross-account access on the synthetic application.");
  await page
    .getByRole("button", { name: "Fill from template", exact: true })
    .click();
  const builder = page.getByRole("group", { name: "Build pentest.md" });
  await expect(builder).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Only create attack", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Additional in-scope URLs").fill("not a URL");
  await builder
    .getByRole("button", { name: "Generate pentest.md", exact: true })
    .click();
  await expect(builder.getByRole("alert")).toContainText(
    "Additional in-scope URLs, line 1",
  );
  await expect(context).toHaveValue("");
  await page
    .getByLabel("Additional in-scope URLs")
    .fill("https://api.fixture.example/v1/\nhttps://api.fixture.example/v1/");
  await page
    .getByLabel("Out-of-scope URLs")
    .fill("https://fixture.example/assessment/billing/");
  const credentials =
    "Reader: user=fixture-reader; password=synthetic-only-$&-{{goal}}\nAdmin: use the second synthetic account";
  await page.getByLabel("Test credentials").fill(credentials);
  await expect(builder.getByRole("alert")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Manage templates", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Back to assessment", exact: true })
    .click();
  await expect(page.getByLabel("Test credentials")).toHaveValue(credentials);
  await expect(page.getByLabel("Additional in-scope URLs")).toHaveValue(
    "https://api.fixture.example/v1/\nhttps://api.fixture.example/v1/",
  );
  await page.screenshot({
    path: "test-results/studio-pentest-template-builder.png",
  });
  await builder
    .getByRole("button", { name: "Generate pentest.md", exact: true })
    .click();
  await expect(builder).toHaveCount(0);
  await expect(context).toBeFocused();
  const generated = await context.inputValue();
  expect(generated).toContain("Start URL: https://fixture.example/assessment/");
  expect(generated).toContain("- https://api.fixture.example/v1/");
  expect(
    generated.match(/^- https:\/\/api.fixture.example\/v1\/$/gm),
  ).toHaveLength(1);
  expect(generated).toContain("- https://fixture.example/assessment/billing/");
  expect(generated).toContain("password=synthetic-only-$&-{{goal}}");
  expect(generated).toContain(
    "Review cross-account access on the synthetic application.",
  );
  expect(generated).toContain("## Evidence and reporting");
  const edited =
    generated + "\n## Operator note\nUse only synthetic records.\n";
  await context.fill(edited);
  await page
    .getByRole("button", { name: "Fill from template", exact: true })
    .click();
  await expect(page.getByLabel("Test credentials")).toHaveValue(credentials);
  await expect(
    builder.getByRole("button", { name: "Replace pentest.md" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Cancel template fill" }).click();
  await expect(context).toHaveValue(edited);
  await expect(
    page.getByRole("button", { name: "Fill from template", exact: true }),
  ).toBeFocused();
  await page
    .getByRole("button", { name: "Only create attack", exact: true })
    .click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  const eid = page.url().split("/attacks/")[1].split("/")[0];
  const csrf = (await (await page.request.get("/api/v1/health")).json())
    .csrf_token;
  const saved = await (
    await page.request.get(`/api/v1/engagements/${eid}/context`)
  ).json();
  expect(saved.content).toBe(edited);
  const inventory = await (
    await page.request.get(`/api/v1/engagements/${eid}/artifacts`)
  ).json();
  const brief = inventory.find(
    (file: { path: string }) => file.path === "pentest.md",
  );
  const artifact = await (
    await page.request.get(`/api/v1/engagements/${eid}/artifacts/${brief.id}`)
  ).json();
  expect(artifact.content).toBe(edited);
  const attack = await (
    await page.request.get(`/api/v1/engagements/${eid}`)
  ).json();
  expect(attack.session_id).toBeNull();
  expect(
    (
      await page.request.patch(`/api/v1/engagements/${eid}`, {
        headers: { "X-Zero-CSRF": csrf },
        data: {
          config: { ...attack.config, execution_mode: "a", container: false },
        },
      })
    ).ok(),
  ).toBe(true);
  await page.reload();
  await page
    .getByRole("button", { name: "Start assessment", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Tool approval" }),
  ).toBeVisible();
  const running = await (
    await page.request.get(`/api/v1/engagements/${eid}`)
  ).json();
  const session = await (
    await page.request.get(`/api/v1/sessions/${running.session_id}/snapshot`)
  ).json();
  expect(session.context).toBe(edited);
  await page
    .getByRole("button", { name: "End runtime session", exact: true })
    .click();
  await page.getByRole("button", { name: "End session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("blank creation and scope-file import remain available alongside template fill", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByRole("button", { name: "New attack", exact: true })
    .first()
    .click();
  const editor = page.getByLabel("Engagement context pentest.md", {
    exact: true,
  });
  await expect(editor).toHaveValue("");
  await page
    .getByRole("button", { name: "Fill from template", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Generate pentest.md", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("Target / start URL");
  await expect(editor).toHaveValue("");
  await page.getByRole("button", { name: "Cancel template fill" }).click();
  const imported =
    "# Imported operator scope\n\nOnly the synthetic fixture application.\n";
  await page.getByLabel("Import file").setInputFiles({
    name: "pentest.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(imported),
  });
  await expect(editor).toHaveValue(imported);
  await page
    .getByRole("button", { name: "Fill from template", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Replace pentest.md" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Cancel template fill" }).click();
  await expect(editor).toHaveValue(imported);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page
    .getByRole("button", { name: "New attack", exact: true })
    .first()
    .click();
  await expect(editor).toHaveValue("");
  const name = "Blank engagement " + Date.now();
  await page.getByLabel("Attack name", { exact: true }).fill(name);
  await page.getByLabel("Target / start URL").fill("https://fixture.example/");
  await page
    .getByRole("button", { name: "Only create attack", exact: true })
    .click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  const eid = page.url().split("/attacks/")[1].split("/")[0];
  const saved = await (
    await page.request.get(`/api/v1/engagements/${eid}/context`)
  ).json();
  expect(saved.content).toBe("");
});

async function creationRunFixture(page: Page, backend = "codex") {
  const csrf = (await (await page.request.get("/api/v1/health")).json())
    .csrf_token;
  const schema = await (await page.request.get("/api/v1/config-schema")).json();
  const config = {
    ...schema.defaults,
    backend,
    model: `offline-create-run-${backend}`,
    container: false,
    execution_mode: "a",
    swarm_size: 1,
    goal: null,
    max_turns: 31,
    pentester_max_turns: 27,
    validator_max_turns: 21,
    max_subagents: 64,
  };
  const source = await (
    await page.request.post("/api/v1/engagements", {
      headers: { "X-Zero-CSRF": csrf },
      data: {
        name: "Create-run offline setup",
        target: "https://fixture.example/",
        config,
      },
    })
  ).json();
  const template = await (
    await page.request.post("/api/v1/templates", {
      headers: { "X-Zero-CSRF": csrf },
      data: { source_id: source.id, name: "Create-run offline setup" },
    })
  ).json();
  return { csrf, config, template };
}

for (const backend of ["codex", "claude"]) {
  test(`Create attack and run starts ${backend} with the default goal, saved settings and imported context`, async ({
    page,
  }) => {
    const fixture = await creationRunFixture(page, backend);
    const sessionRequests: string[] = [];
    page.on("request", (request) => {
      if (
        request.method() === "POST" &&
        /\/engagements\/[^/]+\/sessions$/.test(request.url())
      )
        sessionRequests.push(request.url());
    });
    await page.goto("/");
    await page
      .getByRole("button", { name: "New attack", exact: true })
      .first()
      .click();
    await page.getByLabel("Start from").selectOption(fixture.template.id);
    const name = `Create and run ${backend} ` + Date.now();
    const target = `https://fixture.example/assessment/${backend}/`;
    await page.getByLabel("Attack name", { exact: true }).fill(name);
    await page.getByLabel("Target / start URL").fill(target);
    const goal = await page
      .getByLabel("First goal", { exact: true })
      .inputValue();
    expect(goal).toContain(target);
    expect(goal).toContain("independently validate");
    await page
      .getByRole("button", { name: "Fill from template", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Create attack and run", exact: true }),
    ).toBeDisabled();
    await page
      .getByRole("button", { name: "Generate pentest.md", exact: true })
      .click();
    const context = await page
      .getByLabel("Engagement context pentest.md", { exact: true })
      .inputValue();
    expect(context).toContain(goal);
    await page.locator('.drop-zone input[type="file"]').setInputFiles({
      name: "request-context.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("Synthetic uploaded request context"),
    });
    await page.screenshot({
      path: `test-results/studio-create-and-run-${backend}.png`,
    });
    await page
      .getByRole("button", { name: "Create attack and run", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Tool approval" }),
    ).toBeVisible();
    const eid = page.url().split("/attacks/")[1].split("/")[0];
    const attack = await (
      await page.request.get(`/api/v1/engagements/${eid}`)
    ).json();
    expect(attack.session_id).toBeTruthy();
    const session = await (
      await page.request.get(`/api/v1/sessions/${attack.session_id}/snapshot`)
    ).json();
    expect(session.config).toMatchObject({ ...fixture.config, goal });
    expect(session.context).toBe(context);
    expect(sessionRequests).toHaveLength(1);
    const artifacts = await (
      await page.request.get(`/api/v1/engagements/${eid}/artifacts`)
    ).json();
    const uploaded = artifacts.find(
      (file: { path: string }) =>
        file.path === "zero_files/request-context.txt",
    );
    expect(uploaded).toBeTruthy();
    const content = await (
      await page.request.get(
        `/api/v1/engagements/${eid}/artifacts/${uploaded.id}`,
      )
    ).json();
    expect(content.content).toBe("Synthetic uploaded request context");
    await expect(page.getByLabel("Assessment goal or message")).toHaveValue("");
    await expect
      .poll(
        async () =>
          (await (await page.request.get(`/api/v1/engagements/${eid}`)).json())
            .draft,
      )
      .toBe("");
    await page.reload();
    await expect(
      page.getByRole("region", { name: "Tool approval" }),
    ).toBeVisible();
    await expect(page.getByLabel("Assessment goal or message")).toHaveValue("");
    expect(sessionRequests).toHaveLength(1);
    await page
      .getByRole("button", { name: "End runtime session", exact: true })
      .click();
    await page
      .getByRole("button", { name: "End session", exact: true })
      .click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.request.delete(`/api/v1/templates/${fixture.template.id}`, {
      headers: { "X-Zero-CSRF": fixture.csrf },
    });
  });
}

test("create-and-run startup failure preserves one attack and allows retry in its conversation", async ({
  page,
}) => {
  const fixture = await creationRunFixture(page);
  await page.route("**/api/v1/engagements/*/sessions", (route) =>
    route.fulfill({
      status: 422,
      json: { error: "Synthetic sandbox readiness failure" },
    }),
  );
  await page.goto("/");
  await page
    .getByRole("button", { name: "New attack", exact: true })
    .first()
    .click();
  await page.getByLabel("Start from").selectOption(fixture.template.id);
  const name = "Recover created attack " + Date.now();
  await page.getByLabel("Attack name", { exact: true }).fill(name);
  const goal = await page
    .getByLabel("First goal", { exact: true })
    .inputValue();
  await page
    .getByRole("button", { name: "Create attack and run", exact: true })
    .click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText(
    "Attack created, but starting the assessment failed",
  );
  await expect(page.getByRole("alert")).toContainText(
    "Synthetic sandbox readiness failure",
  );
  await expect(page.getByLabel("Assessment goal or message")).toHaveValue(goal);
  const eid = page.url().split("/attacks/")[1].split("/")[0];
  const attacks = await (await page.request.get("/api/v1/engagements")).json();
  expect(
    attacks.filter((attack: { name: string }) => attack.name === name),
  ).toHaveLength(1);
  expect(
    attacks.find((attack: { id: string }) => attack.id === eid).session_id,
  ).toBeNull();
  await page.unroute("**/api/v1/engagements/*/sessions");
  await page
    .getByRole("button", { name: "Start assessment", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Tool approval" }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "Synthetic sandbox readiness failure" }),
  ).toHaveCount(0);
  expect(page.url()).toContain(`/attacks/${eid}/`);
  await page
    .getByRole("button", { name: "End runtime session", exact: true })
    .click();
  await page.getByRole("button", { name: "End session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.request.delete(`/api/v1/templates/${fixture.template.id}`, {
    headers: { "X-Zero-CSRF": fixture.csrf },
  });
});
