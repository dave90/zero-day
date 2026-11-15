import { test, expect, type Page } from "../../web/test-support";

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
    .getByRole("button", { name: "Create attack", exact: true })
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
    .getByRole("button", { name: "Create attack", exact: true })
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
    .getByRole("button", { name: "Create attack", exact: true })
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
