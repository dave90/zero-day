import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Search,
  ShieldCheck,
  ArrowUpRight,
  Download,
  FileText,
  ChevronRight,
  ChevronDown,
  RefreshCw,
  Lock,
  Users,
  Activity,
  Clock,
  Terminal,
  Layers,
  Copy,
  Check,
  Circle,
  CircleDot,
} from "lucide-react";
import { uploadFile, api, download, resource, action } from "../api/client";
import type { Artifact, Attack, Finding, Actor, Session } from "../api/types";
import { useViewState } from "../api/view-state";
import { findingSeverity, SEVERITIES } from "../api/severity";
import { PlanOutline } from "../components/PlanOutline";
import {
  Badge,
  Dialog,
  Empty,
  ErrorBox,
  Loading,
  bytes,
  displayTime,
  modeName,
} from "../components/common";

function CopyFindingButton({
  eid,
  artifactId,
  sid,
}: {
  eid: string;
  artifactId: string;
  sid?: string;
}) {
  const [copying, setCopying] = useState(false),
    [copied, setCopied] = useState(false),
    [error, setError] = useState("");
  async function copy() {
    setCopying(true);
    setCopied(false);
    setError("");
    try {
      // Use the original export, rather than the bounded artifact preview, so
      // evidence and reproduction steps are copied in full, including history.
      const response = await fetch(download(eid, artifactId, sid), {
        credentials: "same-origin",
      });
      if (!response.ok)
        throw new Error("Could not load this finding. Refresh and try again.");
      const text = await response.text();
      if (!navigator.clipboard?.writeText)
        throw new Error("Clipboard access is unavailable in this browser.");
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch (e) {
      setError(
        e instanceof DOMException && e.name === "NotAllowedError"
          ? "Clipboard access was denied. Allow clipboard access for this site and try again."
          : (e as Error).message,
      );
    } finally {
      setCopying(false);
    }
  }
  return (
    <>
      <ErrorBox error={error} />
      {copied && (
        <span className="field-help" role="status">
          Finding copied to clipboard.
        </span>
      )}
      <button
        className="button"
        onClick={copy}
        disabled={copying}
        aria-label="Copy finding to clipboard"
      >
        {copied ? <Check size={14} /> : <Copy size={14} />}
        {copying ? "Copying…" : copied ? "Copied" : "Copy to clipboard"}
      </button>
    </>
  );
}

export function ArtifactViewer({
  eid,
  item,
  sid,
  onClose,
}: {
  eid: string;
  item: Artifact;
  sid?: string;
  onClose: () => void;
}) {
  const { data, error, isPending } = useQuery({
    queryKey: ["artifact", eid, item.id, sid],
    queryFn: () => api<Artifact>(resource(eid, `artifacts/${item.id}`, sid)),
  });
  let plan: Record<string, unknown> | null = null;
  if (item.path.endsWith("/prd.json") && data?.content) {
    try {
      plan = JSON.parse(data.content);
    } catch {
      /* Raw preview remains available. */
    }
  }
  return (
    <Dialog
      title={item.path.split("/").at(-1) || "Artifact"}
      onClose={onClose}
      wide
    >
      <div className="artifact-bar">
        <code>{item.path}</code>
        <Badge>{item.sealed ? "Sealed history" : "Live artifact"}</Badge>
      </div>
      <div className="artifact-body">
        <ErrorBox error={error} />
        {isPending ? (
          <Loading />
        ) : plan ? (
          <PlanOutline plan={plan} />
        ) : (
          <pre>{data?.content}</pre>
        )}
        {data?.truncated && (
          <p className="notice amber">
            Preview truncated at 1 MB. Download the original for the complete
            content.
          </p>
        )}
      </div>
      <div className="dialog-footer">
        <span className="field-help">
          Read-only · {bytes(item.size)} ·{" "}
          {new Date(item.modified_at * 1000).toLocaleString()}
        </span>
        <div className="spacer" />
        <a
          className="button primary"
          href={download(eid, item.id, sid)}
          download
        >
          <Download size={14} /> Download
        </a>
      </div>
    </Dialog>
  );
}
export function Findings({
  eid,
  sid,
  selected,
  onSelect,
}: {
  eid: string;
  sid?: string;
  selected: string | null;
  onSelect: (id: string | null) => void;
}) {
  const {
    data: findings = [],
    isPending,
    error,
  } = useQuery({
    queryKey: ["findings", eid, sid],
    queryFn: () => api<Finding[]>(resource(eid, "findings", sid)),
    refetchInterval: 5000,
  });
  const { data: artifacts = [] } = useQuery({
    queryKey: ["artifacts", eid, sid],
    queryFn: () => api<Artifact[]>(resource(eid, "artifacts", sid)),
  });
  const [filter, setFilter] = useViewState(eid + ".findings.filter", "all"),
    [severityFilter, setSeverityFilter] = useViewState(
      eid + ".findings.severity",
      "all",
    ),
    [search, setSearch] = useViewState(eid + ".findings.search", "");
  const item = findings.find((f) => f.id === selected);
  const report = artifacts.find((a) => a.path === ".pentest/report.md");
  const cumulative = artifacts.find(
    (a) => a.path === ".ralph_logs/ralph_report.md",
  );
  const itemSeverity = item ? findingSeverity(item) : null;
  const searched = findings.filter(
    (f) =>
      (filter === "all" || f.verdict === filter) &&
      JSON.stringify([
        f.finding.title,
        f.finding.endpoint,
        f.finding.cwe,
        f.finding.severity_hint,
        findingSeverity(f).label,
        f.validation?.cwe,
      ])
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  const filtered = searched
    .filter(
      (f) =>
        severityFilter === "all" || findingSeverity(f).band === severityFilter,
    )
    .sort((a, b) => b.created_at - a.created_at);
  return (
    <div className="panel-page">
      <div className="page-heading">
        <div>
          <div className="eyebrow">EVIDENCE, INDEPENDENTLY REVIEWED</div>
          <h2>Findings</h2>
          <p>
            Candidates stay pending until an independent validator reaches a
            verdict.
          </p>
        </div>
        <div className="row">
          {cumulative && (
            <a
              className="button"
              download
              href={download(eid, cumulative.id, sid)}
            >
              Ralph report
            </a>
          )}
          {report && (
            <a className="button" download href={download(eid, report.id, sid)}>
              <Download size={14} />
              Export report
            </a>
          )}
        </div>
      </div>
      <div className="stats-strip">
        {["confirmed", "pending", "rejected"].map((v) => (
          <button
            key={v}
            className={filter === v ? "selected" : ""}
            aria-pressed={filter === v}
            title={filter === v ? "Show all verdicts" : `Filter ${v} findings`}
            onClick={() => setFilter(filter === v ? "all" : v)}
          >
            <span className={"stat-icon " + v}>
              <ShieldCheck size={19} />
            </span>
            <div>
              <strong>{findings.filter((f) => f.verdict === v).length}</strong>
              <span>{v}</span>
            </div>
          </button>
        ))}
      </div>
      <div className="filter-toolbar findings-filters">
        <div className="severity-filter">
          <label htmlFor="finding-severity">Severity</label>
          <select
            id="finding-severity"
            value={severityFilter}
            onChange={(e) => setSeverityFilter(e.target.value)}
          >
            <option value="all">All severities ({searched.length})</option>
            {SEVERITIES.map(([value, label]) => (
              <option key={value} value={value}>
                {label} (
                {
                  searched.filter((f) => findingSeverity(f).band === value)
                    .length
                }
                )
              </option>
            ))}
          </select>
        </div>
        <label className="search-field">
          <Search size={14} />
          <input
            placeholder="Search title, endpoint, or CWE"
            aria-label="Search findings"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
      </div>
      <ErrorBox error={error} />
      {isPending ? (
        <Loading />
      ) : !filtered.length ? (
        <Empty
          title={
            findings.length
              ? "No matching findings"
              : "Evidence takes investigation"
          }
          icon="shield"
        >
          <p>
            {findings.length
              ? "Try another filter or search."
              : "Finding records will appear here as candidates are submitted and independently reviewed."}
          </p>
        </Empty>
      ) : (
        <div className="finding-list">
          {filtered.map((f) => (
            <button
              className="finding-row"
              key={f.id}
              onClick={() => onSelect(f.id)}
            >
              <div className="finding-row-title">
                <Badge
                  tone={
                    f.verdict === "confirmed"
                      ? "olive"
                      : f.verdict === "pending"
                        ? "amber"
                        : ""
                  }
                >
                  {f.verdict}
                </Badge>
                <h3>{f.finding.title}</h3>
                <SeverityBadge finding={f} />
                <span className="mono">
                  {f.validation?.cwe || f.finding.cwe}
                </span>
              </div>
              <div className="finding-row-meta">
                <code>
                  {f.finding.method} {f.finding.endpoint}
                </code>
                <span>
                  {findingSeverity(f).score !== null
                    ? `CVSS 4.0 · ${findingSeverity(f).score!.toFixed(1)}`
                    : "Unscored"}
                </span>
                <ArrowUpRight size={16} />
              </div>
            </button>
          ))}
        </div>
      )}
      {item && (
        <Dialog title={item.finding.title} onClose={() => onSelect(null)} wide>
          <div className="finding-detail">
            <div className="row">
              <Badge tone={item.verdict === "confirmed" ? "olive" : "amber"}>
                {item.verdict}
              </Badge>
              <SeverityBadge finding={item} />
              <Badge>{item.validation?.cwe || item.finding.cwe}</Badge>
              <code>{item.id}</code>
            </div>
            <div className="notice">
              {item.verdict === "confirmed"
                ? "Independently confirmed by a validator."
                : item.verdict === "rejected"
                  ? "The validator did not establish a vulnerability."
                  : "Candidate record. Independent validation has not confirmed this finding."}
            </div>
            <div className="detail-grid">
              <div>
                <small>Endpoint</small>
                <code>
                  {item.finding.method} {item.finding.endpoint}
                </code>
              </div>
              <div>
                <small>Submitted by</small>
                <span>{item.submitted_by}</span>
              </div>
              <div>
                <small>Severity</small>
                <span>
                  {itemSeverity!.label}
                  {itemSeverity!.estimated ? " · agent estimate" : ""}
                </span>
                <p className="field-help">{itemSeverity!.description}</p>
              </div>
              <div>
                <small>CVSS 4.0</small>
                <span>
                  {itemSeverity!.score !== null
                    ? itemSeverity!.score.toFixed(1)
                    : "Unscored"}
                </span>
              </div>
              <div>
                <small>Created</small>
                <span>{new Date(item.created_at * 1000).toLocaleString()}</span>
              </div>
            </div>
            {item.duplicate_of && (
              <p className="notice">Duplicate of {item.duplicate_of}</p>
            )}
            <h3>Summary</h3>
            <p>{item.finding.summary}</p>
            <h3>Submitted evidence</h3>
            <pre>{item.finding.evidence}</pre>
            {item.validation && (
              <>
                <h3>Validator rationale</h3>
                <p>{item.validation.rationale}</p>
                <h3>Independent reproducer</h3>
                <pre>{item.validation.reproducer}</pre>
                {item.validation.cvss40_vector && (
                  <>
                    <h3>CVSS vector</h3>
                    <pre>{item.validation.cvss40_vector}</pre>
                  </>
                )}
                {item.validation.additional_evidence && (
                  <>
                    <h3>Additional evidence</h3>
                    <pre>{item.validation.additional_evidence}</pre>
                  </>
                )}
              </>
            )}
            {item.finding.notes && (
              <>
                <h3>Notes</h3>
                <p>{item.finding.notes}</p>
              </>
            )}
            <p className="field-help">
              Authoritative finding record · {sid || "current engagement"}
            </p>
          </div>
          <div className="dialog-footer">
            <div className="spacer" />
            <CopyFindingButton
              key={`${eid}:${sid || "live"}:${item.artifact_id}`}
              eid={eid}
              artifactId={item.artifact_id}
              sid={sid}
            />
            <a
              className="button primary"
              download
              href={download(eid, item.artifact_id, sid)}
            >
              <Download size={14} />
              Export finding
            </a>
          </div>
        </Dialog>
      )}
    </div>
  );
}
function SeverityBadge({ finding }: { finding: Finding }) {
  const severity = findingSeverity(finding);
  return (
    <span
      className={`severity-badge ${severity.band}`}
      title={severity.description}
    >
      <span aria-hidden="true" className="badge-dot" />
      {severity.label}
      {severity.estimated && (
        <span className="severity-estimate"> · estimate</span>
      )}
    </span>
  );
}
export function Agents({
  session,
  eid,
  sid,
  selected,
  onSelect,
}: {
  session?: Session;
  eid: string;
  sid?: string;
  selected: string | null;
  onSelect: (id: string | null) => void;
}) {
  const [tab, setTab] = useState("overview");
  const [collapsedPeers, setCollapsedPeers] = useViewState<
    Record<string, boolean>
  >(eid + ".agents.collapsedPeers", {});
  const {
    data: artifacts = [],
    isPending: artifactsLoading,
    error: artifactsError,
  } = useQuery({
    queryKey: ["artifacts", eid, sid],
    queryFn: () => api<Artifact[]>(resource(eid, "artifacts", sid)),
    refetchInterval: 5000,
  });
  const rootId =
    session?.config.execution_mode === "a" ? "agent" : "orchestrator";
  const root: Actor = {
    id: rootId,
    subkind:
      session?.config.execution_mode === "a" ? "pentest" : "orchestrator",
    state: session?.snapshot.lifecycle || "draft",
    idea: (session?.config.goal as string) || "Awaiting a goal",
    turn: Number(session?.snapshot.status?.turn) || 0,
  };
  const actors = session
    ? [
        {
          ...root,
          ...session.snapshot.actors[rootId],
          state: session.snapshot.lifecycle,
        },
        ...Object.values(session.snapshot.actors).filter(
          (a) => a.id !== rootId,
        ),
      ]
    : [];
  const actor = actors.find((a) => a.id === selected);
  const toolList = Object.values(session?.snapshot.tools || {});
  const peerIds = new Set(
    actors.filter((a) => a.subkind === "peer").map((a) => a.id),
  );
  const underPeer = (a: Actor) =>
    a.id !== rootId && a.subkind !== "peer" && peerIds.has(a.parent_id || "");
  function agentRow(a: Actor, child = false, summary?: string) {
    return (
      <button
        className={`agent-row ${child ? "child-agent" : ""}`}
        key={a.id}
        onClick={() => {
          onSelect(a.id);
          setTab("overview");
        }}
      >
        {child && <span className="tree-branch" />}
        <span
          className={
            "agent-icon " + (a.subkind === "validator" ? "validator" : "")
          }
        >
          <Users size={19} />
        </span>
        <div className="agent-row-text">
          <strong>
            {a.subkind || "Agent"} <span>{a.id}</span>
          </strong>
          <p>{a.idea || a.target || "Runtime actor"}</p>
          {summary && <span className="peer-agent-summary">{summary}</span>}
        </div>
        <span className="mono turn-count">{a.turn ?? "—"} turns</span>
        <Badge tone={a.state === "running" ? "olive" : ""}>
          {a.status || a.state}
        </Badge>
        <ChevronRight size={16} />
      </button>
    );
  }
  return (
    <div className="panel-page">
      <div className="page-heading">
        <div>
          <div className="eyebrow">FOCUSED WORK. VISIBLE OWNERSHIP.</div>
          <h2>Agents</h2>
          <p>
            Inspect each actor’s objective, working memory, tasks, and execution
            trail.
          </p>
        </div>
        <Badge>
          {actors.filter((a) => a.state === "running").length} specialists
          running
        </Badge>
      </div>
      {!actors.length ? (
        <Empty title="Your team assembles here">
          <p>
            Start an assessment to see its actual agents and independently
            validating specialists.
          </p>
        </Empty>
      ) : (
        <div className="agent-list">
          {actors
            .filter((a) => !underPeer(a))
            .map((a) => {
              if (a.subkind !== "peer") return agentRow(a, a.id !== rootId);
              const children = actors.filter(
                (child) => underPeer(child) && child.parent_id === a.id,
              );
              const collapseKey = `${sid || session?.id || "draft"}:${a.id}`;
              const expanded =
                children.length > 0 && !collapsedPeers[collapseKey];
              const groupId = `peer-agents-${a.id}`;
              const running = children.filter((child) =>
                ["running", "active", "queued"].includes(child.state),
              ).length;
              const summary =
                ["pentester", "validator"]
                  .map((kind) => {
                    const n = children.filter(
                      (child) => child.subkind === kind,
                    ).length;
                    return `${n} ${kind}${n === 1 ? "" : "s"}`;
                  })
                  .join(" · ") + ` · ${running} running`;
              return (
                <section
                  key={a.id}
                  className={`peer-agent-group ${expanded ? "expanded" : ""}`}
                  aria-label={`Peer ${a.id}`}
                >
                  <div className="peer-agent-heading">
                    <button
                      className="icon-button peer-agent-toggle"
                      aria-label={`${expanded ? "Collapse" : "Expand"} agents under ${a.id}`}
                      aria-expanded={expanded}
                      aria-controls={groupId}
                      disabled={!children.length}
                      onClick={() =>
                        setCollapsedPeers((old) => ({
                          ...old,
                          [collapseKey]: expanded,
                        }))
                      }
                    >
                      {expanded ? (
                        <ChevronDown size={16} />
                      ) : (
                        <ChevronRight size={16} />
                      )}
                    </button>
                    {agentRow(a, false, summary)}
                  </div>
                  <div
                    id={groupId}
                    className="peer-agent-children"
                    role="group"
                    aria-label={`Agents under ${a.id}`}
                    hidden={!expanded}
                  >
                    {children.map((child) => agentRow(child, true))}
                  </div>
                </section>
              );
            })}
        </div>
      )}
      {actor && (
        <Dialog
          title={`${actor.subkind || "Agent"} · ${actor.id}`}
          onClose={() => onSelect(null)}
          wide
        >
          <div className="agent-detail">
            <div className="row spread">
              <p>{actor.idea || actor.target}</p>
              <Badge>{actor.state}</Badge>
            </div>
            <div className="detail-grid">
              <div>
                <small>Investigation steps</small>
                <span>
                  {actor.turn ?? "Unavailable"} /{" "}
                  {String(
                    actor.id === rootId
                      ? session?.config.max_turns
                      : actor.subkind === "validator"
                        ? session?.config.validator_max_turns
                        : session?.config.pentester_max_turns,
                  )}
                </span>
              </div>
              <div>
                <small>Context usage</small>
                <span>
                  {actor.id === rootId &&
                  Number(session?.snapshot.status?.context_tokens) > 0
                    ? `${session?.snapshot.status?.context_tokens} / ${session?.snapshot.status?.context_window} tokens`
                    : "Unavailable"}
                </span>
              </div>
              <div>
                <small>Model</small>
                <span>
                  {String(session?.snapshot.effective.model || "Not resolved")}
                </span>
              </div>
              <div>
                <small>Tool execution</small>
                <span>
                  {String(
                    session?.snapshot.effective.execution_location ||
                      "Not started",
                  )}
                </span>
              </div>
            </div>
            <div className="segmented agent-tabs">
              {["overview", "memory", "todos", "tools"].map((t) => (
                <button
                  key={t}
                  className={tab === t ? "selected" : ""}
                  onClick={() => setTab(t)}
                >
                  {t}
                </button>
              ))}
            </div>
            {tab === "overview" ? (
              <>
                <h3>Working objective</h3>
                <p>{actor.idea || "No objective recorded"}</p>
                <div className="notice">
                  {actor.subkind === "validator"
                    ? "Only an independent validator can confirm or reject findings."
                    : "Candidate findings are handed to an independent validator."}
                </div>
              </>
            ) : tab === "tools" ? (
              <div>
                {toolList
                  .filter((t) => t.actor_id === actor.id)
                  .map((t) => (
                    <details className="tool-row" key={t.call_id}>
                      <summary>
                        <Terminal size={14} />
                        {t.tool}
                        <Badge>
                          {t.ok === undefined
                            ? "In flight"
                            : t.ok
                              ? "Completed"
                              : "Failed"}
                        </Badge>
                      </summary>
                      <pre>{t.output || JSON.stringify(t.args, null, 2)}</pre>
                    </details>
                  ))}
                {!toolList.some((t) => t.actor_id === actor.id) && (
                  <p className="field-help">
                    No tool records with this actor ID. Full attributed events
                    remain in Activity.
                  </p>
                )}
              </div>
            ) : (
              <>
                <p className="notice">
                  <Lock size={14} /> Agent-owned, read-only {tab}.{" "}
                  {sid
                    ? "Selected session artifacts."
                    : "Live engagement artifacts."}
                </p>
                {artifactsLoading ? (
                  <Loading />
                ) : artifactsError ? (
                  <ErrorBox error={artifactsError} />
                ) : (
                  <AgentArtifactContent
                    key={`${actor.id}:${tab}`}
                    eid={eid}
                    sid={sid}
                    kind={tab === "memory" ? "memory" : "todos"}
                    item={artifacts.find(
                      (a) =>
                        a.path ===
                        (tab === "memory"
                          ? actor.memory_path ||
                            `.pentest/memory/${actor.id}.md`
                          : actor.todo_path ||
                            `.pentest/todo/${actor.id}.json`),
                    )}
                  />
                )}
              </>
            )}
          </div>
        </Dialog>
      )}
    </div>
  );
}

function AgentArtifactContent({
  eid,
  sid,
  item,
  kind,
}: {
  eid: string;
  sid?: string;
  item?: Artifact;
  kind: "memory" | "todos";
}) {
  const { data, error, isPending } = useQuery({
    queryKey: ["artifact", eid, item?.id, sid],
    queryFn: () => api<Artifact>(resource(eid, `artifacts/${item!.id}`, sid)),
    enabled: !!item,
    refetchInterval: item && !item.sealed ? 5000 : false,
  });
  if (!item)
    return (
      <Empty title={`No ${kind} recorded`}>
        <p>This actor has not saved this artifact.</p>
      </Empty>
    );
  let tasks:
    | { step: string; status: "pending" | "in_progress" | "done" }[]
    | null = null;
  if (kind === "todos" && data?.content && !data.truncated) {
    try {
      const parsed = JSON.parse(data.content);
      if (
        Array.isArray(parsed) &&
        parsed.every(
          (task) =>
            task &&
            typeof task.step === "string" &&
            ["pending", "in_progress", "done"].includes(task.status),
        )
      )
        tasks = parsed;
    } catch {
      /* Keep unexpected or partial files readable as their original text. */
    }
  }
  return (
    <section
      className="agent-artifact"
      aria-label={kind === "memory" ? "Agent memory" : "Agent todos"}
    >
      <div className="agent-artifact-meta">
        <code>{item.path}</code>
        <Badge>{item.sealed ? "Sealed history" : "Live artifact"}</Badge>
      </div>
      <ErrorBox error={error} />
      {isPending ? (
        <Loading />
      ) : tasks ? (
        tasks.length ? (
          <ul className="agent-todos">
            {tasks.map((task, index) => (
              <li key={index} className={task.status}>
                <span className="todo-state" aria-hidden="true">
                  {task.status === "done" ? (
                    <Check size={16} />
                  ) : task.status === "in_progress" ? (
                    <CircleDot size={16} />
                  ) : (
                    <Circle size={16} />
                  )}
                </span>
                <span className="todo-step">{task.step}</span>
                <span className="todo-status">
                  {task.status.replaceAll("_", " ")}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="field-help">No tasks recorded yet.</p>
        )
      ) : data?.content ? (
        kind === "memory" ? (
          <div className="markdown">
            <Markdown skipHtml>{data.content}</Markdown>
          </div>
        ) : (
          <pre>{data.content}</pre>
        )
      ) : (
        !error && <p className="field-help">This artifact is empty.</p>
      )}
      {data?.truncated && (
        <p className="notice amber">Preview truncated at 1 MB.</p>
      )}
    </section>
  );
}
const workspaceSections = [
  "Scope context",
  "Context files",
  "Agent memory",
  "Agent todos",
  "Custom tools",
  "Ralph plan & evidence",
  "Swarm coordination",
  "Runs & snapshots",
];
export function Workspace({
  attack,
  sid,
  section,
  onSection,
  onSession,
}: {
  attack: Attack;
  sid?: string;
  section: string;
  onSection: (s: string) => void;
  onSession: (s: string) => void;
}) {
  const navigate = useNavigate();
  const client = useQueryClient(),
    [viewed, setViewed] = useState<Artifact | null>(null),
    [error, setError] = useState(""),
    [content, setContent] = useState(""),
    [revision, setRevision] = useState(0),
    [saved, setSaved] = useState(false),
    [operation, setOperation] = useState<"reset" | Artifact | null>(null),
    [operating, setOperating] = useState(false);
  const { data: context } = useQuery({
    queryKey: ["context", attack.id],
    queryFn: () =>
      api<{ content: string; revision: number }>(
        `/engagements/${attack.id}/context`,
      ),
  });
  const { data: artifacts = [], error: artifactError } = useQuery({
    queryKey: ["artifacts", attack.id, sid],
    queryFn: () => api<Artifact[]>(resource(attack.id, "artifacts", sid)),
    refetchInterval: 5000,
  });
  const { data: runs = [] } = useQuery({
    queryKey: ["runs", attack.id],
    queryFn: () => api<Session[]>(resource(attack.id, "runs")),
    refetchInterval: 10000,
  });
  useEffect(() => {
    if (context) {
      setContent(context.content);
      setRevision(context.revision);
    }
  }, [context]);
  const idx = Math.max(0, workspaceSections.indexOf(section));
  const roots = [
    "",
    "",
    ".pentest/memory/",
    ".pentest/todo/",
    "custom_tools/",
    ".ralph",
    "",
    ".ralph_logs/",
  ];
  const files = artifacts.filter((a) =>
    idx === 7
      ? a.path.startsWith(".ralph_logs/") ||
        a.path.startsWith(".recordings/") ||
        a.path.startsWith("goals/")
      : idx === 1
        ? a.path.startsWith(
            String(attack.config.files_root || "zero_files") + "/",
          )
        : idx === 4
          ? a.path.includes("/custom_tools/")
          : a.path.startsWith(roots[idx]),
  );
  async function upload(list: FileList | null) {
    if (!list) return;
    try {
      for (const f of Array.from(list)) {
        await uploadFile(attack.id, f);
      }
      client.invalidateQueries({ queryKey: ["artifacts"] });
    } catch (e) {
      setError((e as Error).message);
    }
  }
  return (
    <div className="workspace-layout">
      <nav className="workspace-nav" aria-label="Workspace sections">
        <div className="eyebrow">ENGAGEMENT FILES</div>
        {workspaceSections.map((s, i) => (
          <button
            className={idx === i ? "selected" : ""}
            key={s}
            onClick={() => onSection(s)}
          >
            {i === 0 ? (
              <FileText size={15} />
            ) : i === 6 ? (
              <Users size={15} />
            ) : i === 7 ? (
              <Clock size={15} />
            ) : (
              <Layers size={15} />
            )}
            <span>{s}</span>
          </button>
        ))}
      </nav>
      <div className="workspace-content">
        <div className="page-heading">
          <div>
            <h2>{workspaceSections[idx]}</h2>
            <p>
              {idx === 0
                ? "The context your agents receive, exactly as written."
                : idx === 7
                  ? "Session configuration and sealed historical artifacts."
                  : "Actual engagement artifacts, sourced from the shared runtime."}
            </p>
          </div>
          {idx === 1 && (
            <label className="button file-label">
              <Download size={15} />
              Import files
              <input
                type="file"
                multiple
                onChange={(e) => upload(e.target.files)}
              />
            </label>
          )}
        </div>
        <ErrorBox error={error || artifactError} />
        {idx === 0 ? (
          <>
            <div className="artifact-bar">
              <code>pentest.md</code>
              <span className="field-help">
                Revision {revision} · changes apply next session
              </span>
            </div>
            <textarea
              className="scope-editor"
              aria-label="Scope context"
              value={content}
              onChange={(e) => {
                setContent(e.target.value);
                setSaved(false);
              }}
              rows={20}
            />
            <div className="row spread">
              <span className="field-help">
                {saved
                  ? "Saved. The current session keeps its original context."
                  : "Freeform context. Scope is advisory; credentials are sent verbatim to the model."}
              </span>
              <button
                className="button primary"
                onClick={async () => {
                  try {
                    const r = await api<{ revision: number }>(
                      `/engagements/${attack.id}/context`,
                      "PUT",
                      { content, revision },
                    );
                    setRevision(r.revision);
                    setSaved(true);
                    client.invalidateQueries({
                      queryKey: ["context", attack.id],
                    });
                  } catch (e) {
                    setError((e as Error).message);
                  }
                }}
              >
                Save context
              </button>
            </div>
          </>
        ) : idx === 6 ? (
          <SwarmView eid={attack.id} sid={sid} sessionId={attack.session_id} />
        ) : (
          <>
            {idx === 7 &&
              runs.map((run) => (
                <button
                  className="run-card"
                  key={run.id}
                  onClick={() => onSession(run.id)}
                >
                  <span className="run-icon">
                    <Clock size={18} />
                  </span>
                  <div>
                    <strong>
                      {modeName(run.config)} ·{" "}
                      {new Date(run.created_at * 1000).toLocaleString()}
                    </strong>
                    <small className="mono">
                      {run.id.slice(0, 12)} · {run.config.backend} ·{" "}
                      {String(run.config.model || "Account default")}
                    </small>
                  </div>
                  <Badge>{run.snapshot.lifecycle}</Badge>
                  <span className="field-help">
                    {run.history_path ? "Sealed" : "Live"}
                  </span>
                  <ArrowUpRight size={15} />
                </button>
              ))}
            {idx === 7 && (
              <div className="recording-operations">
                <div className="row spread">
                  <label className="button file-label">
                    Import recorded audit
                    <input
                      type="file"
                      accept=".jsonl,.ndjson"
                      onChange={async (e) => {
                        const f = e.target.files?.[0];
                        if (!f) return;
                        try {
                          if (f.size > 5_000_000)
                            throw new Error("Recorded audit exceeds 5 MB");
                          await api(
                            `/engagements/${attack.id}/recordings`,
                            "POST",
                            { content: await f.text() },
                          );
                          client.invalidateQueries({ queryKey: ["artifacts"] });
                        } catch (error) {
                          setError((error as Error).message);
                        }
                      }}
                    />
                  </label>
                  <button
                    className="button subtle"
                    onClick={() => setOperation("reset")}
                  >
                    Reset .pentest
                  </button>
                </div>
                <p className="field-help">
                  Import is read-only. Replaying is a separate action that may
                  execute recorded tool calls.
                </p>
                {artifacts
                  .filter((a) => a.path.startsWith(".recordings/"))
                  .map((a) => (
                    <div key={a.id} className="row spread recording-row">
                      <button
                        className="text-button"
                        onClick={() => setViewed(a)}
                      >
                        <FileText size={14} />
                        {a.path.split("/").pop()}
                      </button>
                      <button
                        className="button"
                        onClick={() => setOperation(a)}
                      >
                        Replay audit
                      </button>
                    </div>
                  ))}
              </div>
            )}
            {idx === 1 && (
              <div className="notice">
                Import text/source files here. For a Burp directory, import its
                folder and set Burp dir to its engagement-relative path. Context
                files stay under{" "}
                {String(attack.config.files_root || "zero_files")}.
              </div>
            )}
            {idx === 1 && (
              <label className="text-button file-label folder-import">
                Import a folder
                <input
                  type="file"
                  multiple
                  {...{ webkitdirectory: "", directory: "" }}
                  onChange={(e) => upload(e.target.files)}
                />
              </label>
            )}
            {idx === 2 || idx === 3 ? (
              <p className="notice">
                <Lock size={14} /> Agent-owned files are read-only. Each
                filename identifies its actor.
              </p>
            ) : null}
            {files.map((a) => (
              <button
                className="file-row"
                key={a.id}
                onClick={() => setViewed(a)}
              >
                <FileText size={17} />
                <span>
                  <code>{a.path}</code>
                  <small>
                    {a.sealed ? "Sealed historical artifact" : "Live artifact"}{" "}
                    · {new Date(a.modified_at * 1000).toLocaleString()}
                  </small>
                </span>
                <span>{bytes(a.size)}</span>
                <ChevronRight size={15} />
              </button>
            ))}
            {!files.length && !(idx === 7 && runs.length) && (
              <Empty
                title={
                  idx === 5
                    ? "No Ralph plan yet"
                    : idx === 4
                      ? "No custom tools yet"
                      : "No artifacts in this view"
                }
              >
                <p>
                  {idx === 5
                    ? "A real plan and task dossiers appear when Ralph planning runs."
                    : "Files appear here when imported or written by the runtime."}
                </p>
              </Empty>
            )}
          </>
        )}
        {operation && (
          <Dialog
            title={
              operation === "reset"
                ? "Reset runtime artifacts?"
                : "Replay this recorded audit?"
            }
            onClose={() => !operating && setOperation(null)}
          >
            <div className="dialog-body">
              {operation === "reset" ? (
                <>
                  <p>
                    This removes only the current engagement’s{" "}
                    <code>.pentest/</code> directory. End active work first.
                  </p>
                  <div className="notice">
                    Conversation, sealed history, pentest.md, context files,
                    recordings, .ralph/, and .ralph_logs/ are preserved.
                  </div>
                </>
              ) : (
                <>
                  <p>
                    Zero will replay recorded model envelopes through the shared
                    runtime. Tool calls may execute again. Replay uses reviewed
                    approvals and no automatic risk-class grants.
                  </p>
                  <div className="notice amber">
                    A new replay session is separate from live model execution.
                    End the current session first. Recorded results are not a
                    new live model assessment.
                  </div>
                </>
              )}
            </div>
            <div className="dialog-footer">
              <button
                className="button"
                disabled={operating}
                onClick={() => setOperation(null)}
              >
                Cancel
              </button>
              <div className="spacer" />
              <button
                className="button primary"
                disabled={operating}
                onClick={async () => {
                  setOperating(true);
                  try {
                    if (operation === "reset") {
                      await api(`/engagements/${attack.id}/reset`, "POST", {
                        scope: ".pentest",
                      });
                    } else {
                      const result = await api<{ session_id: string }>(
                        `/engagements/${attack.id}/replay`,
                        "POST",
                        {
                          artifact_id: operation.id,
                          config: attack.config,
                          idempotency_key: crypto.randomUUID(),
                        },
                      );
                      client.setQueryData<Attack>(
                        ["attack", attack.id],
                        (old) =>
                          old ? { ...old, session_id: result.session_id } : old,
                      );
                      navigate(`/attacks/${attack.id}/conversation`);
                    }
                    client.invalidateQueries({ queryKey: ["artifacts"] });
                    client.invalidateQueries({ queryKey: ["runs"] });
                    client.invalidateQueries({ queryKey: ["attacks"] });
                    setOperation(null);
                  } catch (error) {
                    setError((error as Error).message);
                    setOperation(null);
                  } finally {
                    setOperating(false);
                  }
                }}
              >
                {operating
                  ? "Working…"
                  : operation === "reset"
                    ? "Reset .pentest"
                    : "Start replay session"}
              </button>
            </div>
          </Dialog>
        )}
        {viewed && (
          <ArtifactViewer
            eid={attack.id}
            item={viewed}
            sid={sid}
            onClose={() => setViewed(null)}
          />
        )}
      </div>
    </div>
  );
}
export function SwarmView({
  eid,
  sid,
  sessionId,
}: {
  eid: string;
  sid?: string;
  sessionId: string | null;
}) {
  const { data = {} } = useQuery({
    queryKey: ["swarm", eid, sid],
    queryFn: () =>
      api<Record<string, Record<string, unknown>[]>>(
        resource(eid, "swarm", sid),
      ),
    refetchInterval: 3000,
  });
  const [error, setError] = useState("");
  return (
    <div>
      <ErrorBox error={error} />
      {sessionId && !sid && (
        <button
          className="button"
          onClick={async () => {
            try {
              await action(sessionId, "respond-now");
            } catch (e) {
              setError((e as Error).message);
            }
          }}
        >
          <RefreshCw size={14} />
          Respond now
        </button>
      )}
      {Object.keys(data).length ? (
        Object.entries(data).map(([name, rows]) => (
          <section className="swarm-section" key={name}>
            <h3>
              {name.replaceAll("_", " ")}{" "}
              <span className="muted">{rows.length}</span>
            </h3>
            {!rows.length ? (
              <p className="field-help">No records</p>
            ) : (
              rows.map((row, i) => (
                <details className="tool-row" key={i}>
                  <summary>
                    {String(
                      row.title ||
                        row.name ||
                        row.body ||
                        row.actor_id ||
                        row.worker_id ||
                        row.kind ||
                        "Record",
                    )}
                    <Badge>{String(row.state || "recorded")}</Badge>
                  </summary>
                  <pre>{JSON.stringify(row, null, 2)}</pre>
                </details>
              ))
            )}
          </section>
        ))
      ) : (
        <Empty title="No Swarm coordination state">
          <p>
            Ownership, work claims, messages, groups, and handoffs appear when a
            Swarm session runs.
          </p>
        </Empty>
      )}
    </div>
  );
}
export function ActivityView({ eid, sid }: { eid: string; sid?: string }) {
  const [kind, setKind] = useViewState(eid + ".logs.kind", "audit"),
    [q, setQ] = useViewState(eid + ".logs.query", ""),
    [actor, setActor] = useViewState(eid + ".logs.actor", ""),
    [offset, setOffset] = useState(0),
    [follow, setFollow] = useState(true);
  const { data, error, isPending, refetch } = useQuery({
    queryKey: ["logs", eid, sid, kind, q, actor, offset],
    queryFn: () =>
      api<{
        items: {
          line: string;
          source: string;
          record: Record<string, unknown> | null;
          truncated: boolean;
        }[];
        next_offset: number | null;
        available: boolean;
      }>(
        resource(eid, "logs", sid) +
          (sid ? "&" : "?") +
          new URLSearchParams({ kind, q, actor, offset: String(offset) }),
      ),
    refetchInterval: follow ? 3000 : false,
  });
  const client = useQueryClient();
  useEffect(() => {
    setOffset(0);
  }, [kind, q, actor]);
  return (
    <div className="panel-page activity-page">
      <div className="page-heading">
        <div>
          <div className="eyebrow">THE WORK, AS IT HAPPENED</div>
          <h2>Activity</h2>
          <p>
            Inspect the audit trail, saved reasoning, and exact model inputs and
            outputs.
          </p>
        </div>
        <button className="button" onClick={() => refetch()}>
          <RefreshCw size={14} />
          Refresh
        </button>
      </div>
      <div className="filter-toolbar">
        <div className="segmented">
          {["audit", "reasoning", "model", "swarm"].map((k) => (
            <button
              key={k}
              className={k === kind ? "selected" : ""}
              onClick={() => setKind(k)}
            >
              {k === "model" ? "Model I/O" : k}
            </button>
          ))}
        </div>
        <label className="auto-refresh">
          <input
            type="checkbox"
            checked={follow}
            onChange={(e) => setFollow(e.target.checked)}
          />
          Auto refresh
        </label>
      </div>
      {kind === "swarm" ? (
        <SwarmView eid={eid} sid={sid} sessionId={null} />
      ) : (
        <>
          <div className="log-filters">
            <label className="search-field">
              <Search size={14} />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search events or content"
                aria-label="Search logs"
              />
            </label>
            <input
              value={actor}
              onChange={(e) => setActor(e.target.value)}
              placeholder="Filter actor"
              aria-label="Filter logs by actor"
            />
          </div>
          <ErrorBox error={error} />
          {isPending ? (
            <Loading />
          ) : !data?.items.length ? (
            <Empty
              title={
                data?.available ? "No matching log entries" : "No log available"
              }
              icon="shield"
            >
              <p>
                {kind === "model"
                  ? "Model logging may be disabled, or no model call has been recorded."
                  : "Entries appear when the runtime records activity."}
              </p>
            </Empty>
          ) : (
            <div className="log-view">
              {data.items.map((line, i) => (
                <details key={offset + i} className="log-row">
                  <summary>
                    <span className="log-index">{offset + i + 1}</span>
                    <Activity size={13} />
                    <span>
                      {String(
                        line.record?.event ||
                          line.record?.type ||
                          line.line.slice(0, 180),
                      )}
                    </span>
                    <span className="log-source">{line.source}</span>
                  </summary>
                  <pre>
                    {line.record
                      ? JSON.stringify(line.record, null, 2)
                      : line.line}
                  </pre>
                  {line.truncated && <small>Line truncated at 64 KB</small>}
                </details>
              ))}
            </div>
          )}
          <div className="pagination">
            <span className="field-help">
              {offset + 1}–{offset + (data?.items.length || 0)}
            </span>
            <button
              className="button"
              disabled={!offset}
              onClick={() => setOffset(Math.max(0, offset - 100))}
            >
              Previous
            </button>
            <button
              className="button"
              disabled={data?.next_offset == null}
              onClick={() => setOffset(data?.next_offset || 0)}
            >
              Next
            </button>
          </div>
        </>
      )}
    </div>
  );
}
