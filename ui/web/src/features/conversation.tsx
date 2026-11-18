import { useEffect, useLayoutEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import { useNavigate } from "react-router-dom";
import {
  ArrowUp,
  ArrowUpRight,
  Square,
  Paperclip,
  SlidersHorizontal,
  ChevronDown,
  ShieldCheck,
  Check,
  X,
  MessageCircle,
  Terminal,
  ArrowRight,
  CornerDownLeft,
  Layers,
  FileText,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { uploadFile, action, api, key } from "../api/client";
import type {
  Attack,
  Config,
  Interaction,
  Session,
  Tool,
  Message,
} from "../api/types";
import { saveViewState, questionDrafts } from "../api/view-state";
import { PlanOutline } from "../components/PlanOutline";
import { RuntimeProgress } from "../components/RuntimeProgress";
import { runtimeStatus } from "../api/runtime-status";
import {
  Badge,
  ErrorBox,
  displayTime,
  modeName,
  active,
} from "../components/common";
export function DecisionCard({
  request,
  sid,
}: {
  request: Interaction;
  sid: string;
}) {
  const [answer, setAnswer] = useState(
      questionDrafts.get(sid + request.id) || "",
    ),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function resolve(choice: string) {
    setBusy(true);
    setError("");
    try {
      const kind =
        request.kind === "approval_prompt"
          ? "approvals"
          : request.kind === "ask_user"
            ? "questions"
            : "plans";
      const body =
        kind === "approvals"
          ? { decision: choice }
          : kind === "questions"
            ? { answer: choice }
            : {
                decision: choice,
                feedback: answer,
                revision: request.revision,
              };
      await action(sid, `${kind}/${request.id}`, body);
      questionDrafts.delete(sid + request.id);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }
  const approval = request.kind === "approval_prompt",
    plan = request.kind === "plan_checkpoint";
  return (
    <section
      className="decision-card"
      aria-label={
        approval
          ? "Tool approval"
          : plan
            ? "Plan checkpoint"
            : "Operator question"
      }
    >
      <div className="decision-top">
        <span className="decision-symbol">
          {approval ? (
            <ShieldCheck size={17} />
          ) : plan ? (
            <FileText size={17} />
          ) : (
            <MessageCircle size={17} />
          )}
        </span>
        <div>
          <strong>
            {approval
              ? "Your approval is needed"
              : plan
                ? "Review the assessment plan"
                : "A question for you"}
          </strong>
          <small>
            {request.actor_id} · {request.id.slice(0, 12)}
          </small>
        </div>
        <Badge tone="amber">
          {approval
            ? "Awaiting decision"
            : plan
              ? "Plan checkpoint"
              : request.blocking
                ? "Blocking"
                : "Non-blocking"}
        </Badge>
      </div>
      <div className="decision-content">
        {approval ? (
          <>
            <div className="row">
              <code className="tool-name">{request.tool}</code>
              <Badge>{request.risk_level}</Badge>
              <Badge>{request.execution_location}</Badge>
            </div>
            {request.reason && <p>{request.reason}</p>}
            <pre>{JSON.stringify(request.args, null, 2)}</pre>
          </>
        ) : plan ? (
          <>
            <p>
              Only this plan revision will be approved. Task completion remains
              a reviewer decision.
            </p>
            <PlanOutline plan={request.plan || {}} />
          </>
        ) : (
          <p>{request.question}</p>
        )}
        {!approval && (plan || request.free_form) && (
          <textarea
            aria-label={plan ? "Plan revision feedback" : "Your answer"}
            value={answer}
            onChange={(e) => {
              setAnswer(e.target.value);
              questionDrafts.set(sid + request.id, e.target.value);
            }}
            placeholder={
              plan ? "Describe the changes you’d like…" : "Type your answer…"
            }
            rows={3}
          />
        )}
        <ErrorBox error={error} />
      </div>
      <div className="decision-actions">
        {approval ? (
          <>
            <button
              className="button"
              disabled={busy}
              onClick={() => resolve("no")}
            >
              <X size={14} />
              Deny
            </button>
            {request.session_grant_allowed && (
              <button
                className="button"
                disabled={busy}
                onClick={() => resolve("always")}
              >
                Allow tool this session
              </button>
            )}
            <button
              className="button primary"
              disabled={busy}
              onClick={() => resolve("yes")}
            >
              <Check size={14} />
              Allow once
            </button>
          </>
        ) : plan ? (
          <>
            <button
              className="button"
              disabled={busy}
              onClick={() => resolve("abort")}
            >
              Abort plan
            </button>
            <button
              className="button"
              disabled={busy || !answer.trim()}
              onClick={() => resolve("revise")}
            >
              Request revision
            </button>
            <button
              className="button primary"
              disabled={busy}
              onClick={() => resolve("approve")}
            >
              Approve plan
            </button>
          </>
        ) : (
          <>
            {request.options?.map((option) => (
              <button
                className="button"
                disabled={busy}
                key={option}
                onClick={() => resolve(option)}
              >
                {option}
              </button>
            ))}
            {request.free_form && (
              <button
                className="button primary"
                disabled={busy || !answer.trim()}
                onClick={() => resolve(answer)}
              >
                Send answer
              </button>
            )}
          </>
        )}
      </div>
    </section>
  );
}
export function ToolActivityGroup({ tools }: { tools: Tool[] }) {
  const [open, setOpen] = useState(false);
  if (!tools.length) return null;
  const running = tools.filter((t) => t.state === "tool_running");
  return (
    <div className="tool-group">
      <button
        className="tool-group-heading"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <Terminal size={15} />
        <span>
          {running.length
            ? `${running.length} tool${running.length === 1 ? "" : "s"} running`
            : `${tools.length} tool activities`}
        </span>
        <span className="secondary">
          {!running.length ? "Inspect execution" : ""}
        </span>
        <ChevronDown size={14} className={open ? "rotated" : ""} />
      </button>
      {open && (
        <div className="tool-group-body">
          {tools.map((t) => (
            <details key={t.call_id} className="tool-row">
              <summary>
                <code>{t.tool}</code>
                <span>{t.actor_id}</span>
                <Badge
                  tone={
                    t.ok === false
                      ? "red"
                      : t.state === "tool_running"
                        ? "olive"
                        : ""
                  }
                >
                  {t.state === "tool_interrupted"
                    ? "Interrupted"
                    : t.state === "tool_running"
                      ? "Executing"
                      : t.state === "tool_proposal"
                        ? "Proposed"
                        : t.ok
                          ? "Completed"
                          : "Failed"}
                </Badge>
              </summary>
              {t.args && <pre>{JSON.stringify(t.args, null, 2)}</pre>}
              {t.output && <pre>{t.output}</pre>}
            </details>
          ))}
        </div>
      )}
    </div>
  );
}
export function Conversation({
  attack,
  session,
  connected = false,
  lastEventAt,
  history = [],
  config,
  draft,
  onDraft,
  onConfigure,
  onOpenFindings,
  onStarted,
  onError,
}: {
  attack: Attack;
  session?: Session;
  connected?: boolean;
  lastEventAt?: number;
  history?: Message[];
  config: Config;
  draft: string;
  onDraft: (s: string) => void;
  onConfigure: () => void;
  onOpenFindings: () => void;
  onStarted: (sid: string) => void;
  onError: (s: string) => void;
}) {
  const navigate = useNavigate();
  const client = useQueryClient(),
    [sending, setSending] = useState(false),
    [note, setNote] = useState("");
  const scroll = useRef<HTMLDivElement>(null),
    follow = useRef(true),
    input = useRef<HTMLTextAreaElement>(null),
    programmatic = useRef(false);
  const state = session?.snapshot,
    pending = Object.values(state?.interactions || {}),
    messages = [...history, ...(state?.messages || [])].filter(
      (m) =>
        [
          "orchestrator",
          "pentest",
          "analyzer",
          "analyzer_validator",
          "swarm_summarizer",
        ].includes(m.actor) || m.role === "user",
    );
  const live = active(state?.lifecycle),
    working = live && state?.lifecycle !== "idle",
    hasOutstandingWork =
      working || runtimeStatus(state).running || pending.length > 0;
  useEffect(() => {
    const el = scroll.current;
    if (el)
      el.scrollTop =
        Number(localStorage.getItem("zero.scroll." + attack.id)) || 0;
    return () => {
      if (el)
        localStorage.setItem("zero.scroll." + attack.id, String(el.scrollTop));
    };
  }, [attack.id]);
  useLayoutEffect(() => {
    if (messages.length > 0 && follow.current && scroll.current) {
      programmatic.current = true;
      scroll.current.scrollTop = scroll.current.scrollHeight;
      const frame = requestAnimationFrame(() => {
        programmatic.current = false;
      });
      return () => cancelAnimationFrame(frame);
    }
  }, [state?.seq, pending.length]);
  async function send() {
    if (!draft.trim() || sending) return;
    const shortcut = draft.trim().toLowerCase();
    const targets: Record<string, string> = {
      "/scope": "workspace?section=Scope%20context",
      "/findings": "findings",
      "/show-rejected": "findings",
      "/agents": "agents",
      "/running": "agents",
      "/work": "workspace?section=Swarm%20coordination",
      "/messages": "workspace?section=Swarm%20coordination",
      "/groups": "workspace?section=Swarm%20coordination",
      "/history": "workspace?section=Runs%20%26%20snapshots",
      "/replay": "workspace?section=Runs%20%26%20snapshots",
    };
    if (targets[shortcut]) {
      if (shortcut === "/show-rejected")
        saveViewState(attack.id + ".findings.filter", "rejected");
      onDraft("");
      navigate(`/attacks/${attack.id}/${targets[shortcut]}`);
      return;
    }
    if (shortcut === "/mode") {
      onDraft("");
      onConfigure();
      return;
    }
    if (shortcut === "/help") {
      onDraft("");
      setNote(
        "Shortcuts: /scope · /findings · /show-rejected · /agents · /work · /messages · /groups · /history · /replay · /respond-now · /mode",
      );
      return;
    }
    if (shortcut === "/respond-now") {
      try {
        if (!session || !live) throw new Error("No active Swarm session");
        await action(session.id, "respond-now");
        onDraft("");
        setNote("Swarm response requested.");
      } catch (e) {
        onError((e as Error).message);
      }
      return;
    }
    setSending(true);
    setNote("");
    try {
      if (live && session) {
        const result = await action<{ outcome: string }>(
          session.id,
          "messages",
          { text: draft },
        );
        setNote(
          result.outcome === "queued"
            ? "Guidance queued for the agent. In-flight tools continue."
            : "Message accepted.",
        );
      } else {
        const r = await api<{ session_id: string }>(
          `/engagements/${attack.id}/sessions`,
          "POST",
          { config: { ...config, goal: draft }, idempotency_key: key() },
        );
        onStarted(r.session_id);
        client.invalidateQueries({ queryKey: ["attacks"] });
        client.invalidateQueries({ queryKey: ["attack", attack.id] });
      }
      onDraft("");
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setSending(false);
    }
  }
  async function attach(files: FileList | null) {
    if (!files) return;
    try {
      for (const f of Array.from(files)) {
        await uploadFile(attack.id, f);
      }
      setNote("Context files saved to this engagement.");
      client.invalidateQueries({ queryKey: ["artifacts"] });
    } catch (e) {
      onError((e as Error).message);
    }
  }
  return (
    <div className="conversation-view">
      {pending.length > 0 && (
        <button
          className="pending-strip"
          onClick={() =>
            document
              .getElementById("pending-decisions")
              ?.scrollIntoView({ behavior: "smooth", block: "center" })
          }
        >
          <span className="attention-dot" />
          {pending.length} operator decision{pending.length === 1 ? "" : "s"}{" "}
          waiting
          <ArrowRight size={14} />
        </button>
      )}
      <div
        className="conversation-scroll"
        ref={scroll}
        onWheel={(e) => {
          if (e.deltaY < 0) follow.current = false;
        }}
        onScroll={() => {
          const el = scroll.current;
          if (el && !programmatic.current)
            follow.current =
              el.scrollHeight - el.scrollTop - el.clientHeight < 100;
        }}
      >
        <div className="conversation-inner">
          {!messages.length ? (
            <div className="conversation-welcome">
              <div className="welcome-mark">✳</div>
              <div className="eyebrow">
                A CLEAR QUESTION. A THOROUGH INVESTIGATION.
              </div>
              <h2>What should we look into?</h2>
              <p>
                Give Zero a goal. It will investigate, coordinate specialist
                agents, and independently validate what it finds.
              </p>
              <div className="suggestions">
                {[
                  "Map the attack surface and identify promising entry points.",
                  "Investigate access control and authorization boundaries.",
                  "Review authentication and session handling.",
                ].map((s, i) => (
                  <button
                    key={s}
                    onClick={() => {
                      onDraft(s);
                      input.current?.focus();
                    }}
                  >
                    <span>0{i + 1}</span>
                    {s}
                    <ArrowUp size={14} />
                  </button>
                ))}
              </div>
              <div className="context-ready">
                <FileText size={14} /> pentest.md · revision{" "}
                {attack.context_revision}
                <span>Ready for the next session</span>
              </div>
            </div>
          ) : (
            messages.map((m) =>
              m.role === "user" ? (
                <div className="user-message" key={m.id}>
                  <div className="message-meta">
                    <span>You</span>
                    <time>{displayTime(m.timestamp)}</time>
                  </div>
                  <div className="user-message-text">{m.text}</div>
                </div>
              ) : (
                <article className="agent-message" key={m.id}>
                  <div className="message-meta">
                    <span className="zero-avatar">✳</span>
                    <strong>
                      {m.actor === "orchestrator" || m.actor === "pentest"
                        ? "Zero"
                        : m.actor.replaceAll("_", " ")}
                    </strong>
                    <time>{displayTime(m.timestamp)}</time>
                    {!m.complete && (
                      <span className="stream-label">Writing</span>
                    )}
                  </div>
                  <div className="markdown">
                    <Markdown skipHtml>{m.text}</Markdown>
                  </div>
                </article>
              ),
            )
          )}
          <ToolActivityGroup tools={Object.values(state?.tools || {})} />
          <div id="pending-decisions">
            {pending.map((p) => (
              <DecisionCard
                request={p}
                sid={session!.id}
                key={session!.id + p.id}
              />
            ))}
          </div>
          {Object.keys(state?.findings || {}).length > 0 && (
            <button className="finding-summary-card" onClick={onOpenFindings}>
              <span className="finding-icon">
                <ShieldCheck size={20} />
              </span>
              <div>
                <strong>
                  {hasOutstandingWork
                    ? "Findings collected so far"
                    : "Evidence ready for review"}
                </strong>
                <p>
                  {Object.keys(state!.findings).length} finding records ·
                  verdicts from independent validation
                </p>
              </div>
              <ArrowUpRight size={17} />
            </button>
          )}
          {state && (
            <RuntimeProgress
              state={state}
              connected={connected}
              lastEventAt={lastEventAt}
            />
          )}
          <ErrorBox error={state?.error} />
        </div>
      </div>
      <div className="composer-wrap">
        <div className="composer">
          <textarea
            ref={input}
            aria-label="Assessment goal or message"
            value={draft}
            onChange={(e) => onDraft(e.target.value)}
            onKeyDown={(e) => {
              if (
                e.key === "Enter" &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing
              ) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={
              live
                ? "Guide the assessment, ask a question…"
                : "Describe your assessment goal…"
            }
            rows={2}
          />
          <div className="composer-toolbar">
            <div className="row">
              <label
                className="icon-button file-label"
                title="Attach context files"
              >
                <Paperclip size={17} />
                <span className="sr-only">Attach context files</span>
                <input
                  type="file"
                  multiple
                  onChange={(e) => attach(e.target.files)}
                />
              </label>
              <button className="composer-config" onClick={onConfigure}>
                <Layers size={14} />
                {modeName(session?.config || config)}
                <ChevronDown size={12} />
              </button>
              <span className="composer-divider" />
              <button className="composer-config" onClick={onConfigure}>
                {String((session?.config || config).backend || "codex")}
                <span className="model-default">
                  {" "}
                  ·{" "}
                  {String(
                    state?.effective.model ||
                      ((session?.config || config).model &&
                      !(
                        String((session?.config || config).model).startsWith(
                          "claude",
                        ) && (session?.config || config).backend === "codex"
                      )
                        ? (session?.config || config).model
                        : "Account default"),
                  )}
                </span>
              </button>
            </div>
            <div className="row">
              <button
                className="icon-button"
                aria-label="Configure run"
                onClick={onConfigure}
              >
                <SlidersHorizontal size={16} />
              </button>
              {working && session && (
                <button
                  className="icon-button stop-button"
                  aria-label="Stop current turn"
                  onClick={async () => {
                    try {
                      const r = await action<{ cancelled: boolean }>(
                        session.id,
                        "cancel-turn",
                      );
                      setNote(
                        r.cancelled
                          ? "Turn cancellation requested. Other work may continue."
                          : "No model turn is currently cancellable. Running tools may continue.",
                      );
                    } catch (e) {
                      onError((e as Error).message);
                    }
                  }}
                >
                  <Square size={13} />
                </button>
              )}
              <button
                className="send-button"
                aria-label={live ? "Send message" : "Start assessment"}
                disabled={
                  !draft.trim() ||
                  sending ||
                  pending.some((p) => p.kind === "plan_checkpoint")
                }
                onClick={send}
              >
                {sending ? (
                  <span className="working-dot" />
                ) : (
                  <ArrowUp size={18} />
                )}
              </button>
            </div>
          </div>
        </div>
        <div className="composer-note">
          <span>
            {note ||
              "Scope is advisory. Review decisions and independently validated evidence."}
          </span>
          <span>
            <CornerDownLeft size={11} /> to send · Shift + Enter for newline
          </span>
        </div>
      </div>
    </div>
  );
}
