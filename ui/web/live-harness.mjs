// Deliberate live-only browser driver. Never imported by the offline E2E suite.
import { chromium } from "playwright";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
const spec = JSON.parse(await readFile(process.argv[2], "utf8"));
await mkdir(spec.output, { recursive: true });
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
});
const page = await context.newPage();
const events = [],
  decisions = [];
let sid, eid, streamAbort, streamTask;
const deadline = Date.now() + spec.deadline_seconds * 1000;
async function get(url) {
  const r = await context.request.get(spec.origin + "/api/v1" + url);
  if (!r.ok()) throw new Error(await r.text());
  return r.json();
}
async function mutate(url, method, body) {
  const h = await get("/health");
  const r = await context.request.fetch(spec.origin + "/api/v1" + url, {
    method,
    headers: { "X-Zero-CSRF": h.csrf_token },
    data: body,
  });
  if (!r.ok()) throw new Error(await r.text());
  return r.json();
}
async function collect() {
  streamAbort = new AbortController();
  const response = await fetch(
    spec.origin + `/api/v1/sessions/${sid}/events?after=0`,
    { signal: streamAbort.signal },
  );
  const reader = response.body.getReader(),
    decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const line = block.split("\n").find((s) => s.startsWith("data: "));
        if (line) events.push(JSON.parse(line.slice(6)));
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") throw e;
  }
}
let denied = false,
  revised = false,
  reloaded = false;
async function finishCycle(after = 0) {
  while (Date.now() < deadline) {
    const session = await get(`/sessions/${sid}/snapshot`),
      state = session.snapshot;
    if (["failed", "interrupted"].includes(state.lifecycle))
      throw new Error(state.error || state.lifecycle);
    for (const interaction of Object.values(state.interactions)) {
      const card = page
        .getByRole("region", {
          name:
            interaction.kind === "approval_prompt"
              ? "Tool approval"
              : interaction.kind === "plan_checkpoint"
                ? "Plan checkpoint"
                : "Operator question",
        })
        .filter({ hasText: interaction.id.slice(0, 12) });
      await card.waitFor();
      if (!reloaded) {
        await page.reload();
        reloaded = true;
        await card.waitFor();
      }
      await page.screenshot({
        path: path.join(spec.output, `decision-${decisions.length}.png`),
      });
      if (interaction.kind === "approval_prompt") {
        const deny =
          interaction.risk_level === "destructive" ||
          (spec.profile === "P1" && !denied);
        await card
          .getByRole("button", {
            name: deny ? "Deny" : "Allow once",
            exact: true,
          })
          .click();
        if (deny) denied = true;
        decisions.push({
          id: interaction.id,
          decision: deny ? "deny" : "once",
          actor: interaction.actor_id,
        });
      } else if (interaction.kind === "plan_checkpoint") {
        if (spec.profile === "P1" && !revised) {
          await card
            .getByLabel("Plan revision feedback")
            .fill(
              "Keep the plan bounded to the provided instance and prioritize the guided benign-marker XSS capability check.",
            );
          await card
            .getByRole("button", { name: "Request revision", exact: true })
            .click();
          revised = true;
          decisions.push({
            id: interaction.id,
            revision: interaction.revision,
            decision: "revise",
          });
        } else {
          await card
            .getByRole("button", { name: "Approve plan", exact: true })
            .click();
          decisions.push({
            id: interaction.id,
            revision: interaction.revision,
            decision: "approve",
          });
        }
      } else {
        if (interaction.options?.length) {
          await card
            .getByRole("button", { name: interaction.options[0], exact: true })
            .click();
        } else {
          await card
            .getByLabel("Your answer")
            .fill(
              "Stay within the supplied instance prefix; use the unauthenticated benign-marker capability path and preserve evidence.",
            );
          await card
            .getByRole("button", { name: "Send answer", exact: true })
            .click();
        }
        decisions.push({ id: interaction.id, decision: "answer" });
      }
    }
    if (
      ["idle", "closed"].includes(state.lifecycle) &&
      state.seq > after &&
      events.some(
        (e) =>
          e.seq > after &&
          ["turn_start", "ralph_iteration", "swarm_phase"].includes(e.kind),
      )
    )
      return session;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("Case deadline exceeded");
}
try {
  await page.goto(spec.origin);
  await page
    .getByRole("button", { name: "New attack", exact: true })
    .first()
    .click();
  await page.getByLabel("Attack name").fill(spec.id);
  await page.getByLabel("Target / start URL").fill(spec.target);
  await page
    .getByLabel("Engagement context", { exact: false })
    .fill(spec.context);
  await page.getByLabel("First goal").fill(spec.config.goal);
  await page
    .getByRole("button", { name: "Create attack", exact: true })
    .click();
  await page.getByRole("heading", { name: spec.id, exact: true }).waitFor();
  eid = page.url().split("/attacks/")[1].split("/")[0];
  await mutate(`/engagements/${eid}`, "PATCH", { config: spec.config });
  await page.reload();
  await page
    .getByRole("button", { name: "Start assessment", exact: true })
    .click();
  while (Date.now() < deadline) {
    const attack = await get(`/engagements/${eid}`);
    if (attack.session_id) {
      sid = attack.session_id;
      break;
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  if (!sid) throw new Error("Browser did not start a session");
  streamTask = collect();
  let session = await finishCycle();
  await page.screenshot({ path: path.join(spec.output, "conversation.png") });
  if (
    spec.mode !== "ralph" &&
    spec.profile === "P2" &&
    session.snapshot.lifecycle === "idle"
  ) {
    const previous = session.snapshot.seq;
    await page
      .getByRole("textbox", { name: "Assessment goal or message" })
      .fill(
        "Summarize independently confirmed evidence and the remaining limitations. Do not start additional target testing.",
      );
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    session = await finishCycle(previous);
  }
  await page.getByRole("button", { name: "Findings", exact: true }).click();
  await page.screenshot({ path: path.join(spec.output, "findings.png") });
  const records = await get(`/engagements/${eid}/findings`);
  const confirmed = records.find((f) => f.verdict === "confirmed");
  if (confirmed) {
    await page
      .locator(".finding-row")
      .filter({ hasText: confirmed.finding.title })
      .first()
      .click();
    await page.getByRole("dialog").waitFor();
    await page.screenshot({
      path: path.join(spec.output, "finding-detail.png"),
    });
    await page.getByRole("button", { name: "Close dialog" }).click();
  }
  if (
    spec.profile === "P1" &&
    (!denied || !decisions.some((d) => d.decision === "once"))
  )
    throw new Error("P1 did not exercise both Deny and Allow once");
  if (session.snapshot.lifecycle !== "closed")
    await mutate(`/sessions/${sid}/close`, "POST", {
      idempotency_key: crypto.randomUUID(),
    });
  if (spec.mode === "ralph") {
    const resumed = await mutate(`/engagements/${eid}/sessions`, "POST", {
      config: { ...spec.config, ralph_resume: true, ralph_iterations: 1 },
      idempotency_key: crypto.randomUUID(),
    });
    const original = sid;
    sid = resumed.session_id;
    await page.reload();
    await finishCycle();
    sid = original;
  }
  await new Promise((r) => setTimeout(r, 300));
  streamAbort?.abort();
  await streamTask;
  await writeFile(
    path.join(spec.output, "result.json"),
    JSON.stringify(
      {
        engagement_id: eid,
        session_id: sid,
        decisions,
        events,
        findings: records,
      },
      null,
      2,
    ),
  );
} catch (error) {
  await page
    .screenshot({ path: path.join(spec.output, "failure.png") })
    .catch(() => {});
  await writeFile(
    path.join(spec.output, "failure.json"),
    JSON.stringify(
      {
        error: String(error),
        engagement_id: eid,
        session_id: sid,
        events,
        decisions,
      },
      null,
      2,
    ),
  );
  throw error;
} finally {
  streamAbort?.abort();
  await context.close();
  await browser.close();
}
