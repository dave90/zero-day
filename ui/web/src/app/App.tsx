import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Plus,
  Search,
  LayoutGrid,
  ChevronRight,
  Globe,
  SlidersHorizontal,
  PanelRightClose,
  PanelRightOpen,
  ArrowUpRight,
  Layers,
  Users,
  ShieldCheck,
  Activity,
  FileText,
  Power,
  Command,
  Check,
  ChevronDown,
  Archive,
  Terminal,
  Folder,
  MoreHorizontal,
  Copy,
} from "lucide-react";
import { api, health, action, resource, download } from "../api/client";
import { navigation, forgetAttack } from "../api/view-state";
import { useSession } from "../api/hooks";
import { runtimeStatus } from "../api/runtime-status";
import type {
  Attack,
  AttackTemplate,
  Config,
  Schema,
  Session,
  Artifact,
  Message,
} from "../api/types";
import {
  Badge,
  Dialog,
  Empty,
  ErrorBox,
  Loading,
  active,
  modeName,
} from "../components/common";
import {
  NewAttack,
  AllAttacks,
  AttackActions,
  TemplateManager,
} from "../features/attacks";
import { ConfigSheet } from "../features/configuration";
import { Conversation } from "../features/conversation";
import {
  Findings,
  Agents,
  Workspace,
  ActivityView,
} from "../features/evidence";
const TABS = [
  ["conversation", "Conversation"],
  ["findings", "Findings"],
  ["agents", "Agents"],
  ["workspace", "Workspace"],
  ["activity", "Activity"],
];
function pref(name: string, defaultValue: number) {
  const n = Number(localStorage.getItem(name));
  return n || defaultValue;
}
function ResizeHandle({
  side,
  onChange,
  value,
}: {
  side: string;
  onChange: (n: number) => void;
  value: number;
}) {
  const left = side === "sidebar",
    min = left ? 210 : 280,
    max = left ? 300 : 400;
  return (
    <div
      className={"resize-handle " + side}
      role="separator"
      tabIndex={0}
      aria-label={`Resize ${side}`}
      aria-orientation="vertical"
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      onKeyDown={(e) => {
        if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
          e.preventDefault();
          onChange(
            Math.min(
              max,
              Math.max(
                min,
                value + (e.key === "ArrowRight" ? 1 : -1) * (left ? 10 : -10),
              ),
            ),
          );
        }
      }}
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture(e.pointerId);
        const start = e.clientX,
          initial = value;
        const el = e.currentTarget;
        const move = (ev: PointerEvent) =>
          onChange(
            Math.min(
              max,
              Math.max(min, initial + (ev.clientX - start) * (left ? 1 : -1)),
            ),
          );
        const end = () => {
          el.removeEventListener("pointermove", move);
          el.removeEventListener("pointerup", end);
        };
        el.addEventListener("pointermove", move);
        el.addEventListener("pointerup", end);
      }}
    />
  );
}
export default function App() {
  const navigate = useNavigate(),
    { id, view = "conversation" } = useParams(),
    [params, setParams] = useSearchParams(),
    client = useQueryClient();
  const { data: attacks = [], error: listError } = useQuery({
    queryKey: ["attacks"],
    queryFn: () => api<Attack[]>("/engagements"),
    refetchInterval: 3000,
  });
  const { data: healthData, error: healthError } = useQuery({
    queryKey: ["health"],
    queryFn: health,
    retry: 2,
  });
  const { data: schema } = useQuery({
    queryKey: ["schema"],
    queryFn: () => api<Schema>("/config-schema"),
  });
  const attackQuery = useQuery({
    queryKey: ["attack", id],
    queryFn: () => api<Attack>(`/engagements/${id}`),
    enabled: !!id,
  });
  const attack = attackQuery.data;
  const sid = params.get("session") || attack?.session_id;
  const { data: conversation = [] } = useQuery({
    queryKey: ["conversation", id, sid],
    queryFn: () =>
      api<{ session_id: string; messages: Message[] }[]>(
        resource(id!, "conversation"),
      ),
    enabled: !!id,
  });
  const history = conversation
    .filter((s) => s.session_id !== sid)
    .flatMap((s) => s.messages);
  const sessionQuery = useSession(sid),
    session = sessionQuery.data,
    state = session?.snapshot;
  const { data: artifacts = [] } = useQuery({
    queryKey: ["artifacts", id, sid],
    queryFn: () =>
      api<Artifact[]>(resource(id!, "artifacts", sid || undefined)),
    enabled: !!id,
  });
  const [newOpen, setNewOpen] = useState(false),
    [templatesOpen, setTemplatesOpen] = useState(false),
    [initialTemplate, setInitialTemplate] = useState<AttackTemplate>(),
    [managedAttack, setManagedAttack] = useState<Attack | null>(null),
    [configOpen, setConfigOpen] = useState(false),
    [endOpen, setEndOpen] = useState(false),
    [search, setSearch] = useState(""),
    [error, setError] = useState(""),
    [creationError, setCreationError] = useState<{
      id: string;
      message: string;
    } | null>(null),
    [closing, setClosing] = useState(false),
    [inspector, setInspector] = useState(
      localStorage.getItem("zero.inspector") !== "closed",
    ),
    [sideWidth, setSideWidth] = useState(pref("zero.sidebar.width", 238)),
    [inspectWidth, setInspectWidth] = useState(
      pref("zero.inspector.width", 310),
    );
  const drafts = useRef<Record<string, string>>({}),
    deleted = useRef(new Set<string>()),
    [draftVersion, setDraftVersion] = useState(0),
    searchRef = useRef<HTMLInputElement>(null);
  // Persist an unsent draft even when navigation happens before the debounce fires.
  useEffect(
    () => () => {
      if (id && !deleted.current.has(id) && drafts.current[id] !== undefined) {
        api(`/engagements/${id}`, "PATCH", { draft: drafts.current[id] }).catch(
          () => {},
        );
      }
    },
    [id],
  );
  useEffect(() => {
    if (id && !deleted.current.has(id)) navigation.set(id, params.toString());
  }, [id, params]);
  const draft = id ? (drafts.current[id] ?? attack?.draft ?? "") : "";
  const draftConfig = {
    ...(schema?.defaults || {}),
    ...(attack?.config || {}),
  };
  const live = active(state?.lifecycle),
    progress = runtimeStatus(state),
    pending = Object.values(state?.interactions || {}),
    historical =
      !!params.get("session") && params.get("session") !== attack?.session_id;
  useEffect(() => {
    const keydown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        setNewOpen(true);
        setTemplatesOpen(false);
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  }, []);
  useEffect(() => {
    localStorage.setItem("zero.sidebar.width", String(sideWidth));
  }, [sideWidth]);
  useEffect(() => {
    localStorage.setItem("zero.inspector.width", String(inspectWidth));
  }, [inspectWidth]);
  useEffect(() => {
    localStorage.setItem("zero.inspector", inspector ? "open" : "closed");
  }, [inspector]);
  useEffect(() => {
    if (!id || drafts.current[id] === undefined) return;
    const eid = id,
      text = drafts.current[id];
    const timer = setTimeout(
      () =>
        !deleted.current.has(eid) &&
        api(`/engagements/${eid}`, "PATCH", { draft: text }).catch((e) =>
          setError(e.message),
        ),
      400,
    );
    return () => clearTimeout(timer);
  }, [id, draftVersion]);
  useEffect(() => {
    setError("");
  }, [id]);
  function setDraft(text: string) {
    if (!id) return;
    drafts.current[id] = text;
    setDraftVersion((v) => v + 1);
  }
  function openAttack(aid: string) {
    const a = attacks.find((a) => a.id === aid);
    navigate(
      `/attacks/${aid}/${a?.view || "conversation"}${navigation.get(aid) ? "?" + navigation.get(aid) : ""}`,
    );
  }
  function go(tab: string) {
    if (!id) return;
    navigate(`/attacks/${id}/${tab}${historical ? "?session=" + sid : ""}`);
    api(`/engagements/${id}`, "PATCH", { view: tab }).catch((e) =>
      setError(e.message),
    );
  }
  function parameter(k: string, v: string | null) {
    setParams((old) => {
      const p = new URLSearchParams(old);
      if (v) p.set(k, v);
      else p.delete(k);
      return p;
    });
  }
  async function archive(a: Attack) {
    await api(`/engagements/${a.id}`, "PATCH", { archived: !a.archived });
    client.invalidateQueries({ queryKey: ["attack", a.id] });
    client.invalidateQueries({ queryKey: ["attacks"] });
  }
  const report = artifacts.find((a) => a.path === ".pentest/report.md");
  const shown = attacks.filter(
    (a) =>
      !a.archived &&
      (a.name + " " + a.target).toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <div
      className="desktop-shell"
      style={
        {
          "--sidebar-width": `${sideWidth}px`,
          "--inspector-width": `${inspectWidth}px`,
        } as CSSProperties
      }
    >
      <aside className="attack-sidebar">
        <button
          className="brand"
          onClick={() => navigate("/")}
          aria-label="Zero Studio home"
        >
          <span className="brand-mark">✳</span>
          <span>
            zero<span className="brand-dot">.</span>
          </span>
          <span className="brand-edition">STUDIO</span>
        </button>
        <button
          className="new-attack-button"
          aria-label="New attack"
          onClick={() => setNewOpen(true)}
        >
          <Plus size={17} />
          <span>New attack</span>
          <kbd>⌘ N</kbd>
        </button>
        <label className="sidebar-search">
          <Search size={15} />
          <input
            ref={searchRef}
            aria-label="Search attacks"
            placeholder="Search attacks"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && shown[0]) openAttack(shown[0].id);
            }}
          />
          <kbd>⌘ K</kbd>
        </label>
        <button
          className={
            "all-attacks-link " + (!id && !templatesOpen ? "selected" : "")
          }
          onClick={() => navigate("/")}
        >
          <LayoutGrid size={16} />
          <span>All attacks</span>
          <span className="count">
            {attacks.filter((a) => !a.archived).length}
          </span>
        </button>
        <button
          className={"all-attacks-link " + (templatesOpen ? "selected" : "")}
          onClick={() => setTemplatesOpen(true)}
        >
          <Copy size={16} />
          <span>Templates</span>
        </button>
        <div className="sidebar-section-title">
          <span>YOUR ATTACKS</span>
          <button
            className="icon-button"
            aria-label="New attack conversation"
            onClick={() => setNewOpen(true)}
          >
            <Plus size={14} />
          </button>
        </div>
        <nav className="attack-list" aria-label="Attack conversations">
          {shown.map((a) => {
            const count = Object.keys(a.state?.interactions || {}).length;
            return (
              <button
                className={"sidebar-attack " + (id === a.id ? "selected" : "")}
                key={a.id}
                onClick={() => openAttack(a.id)}
              >
                <span
                  className={
                    "attack-status-dot " +
                    (count
                      ? "attention"
                      : a.state?.lifecycle === "active"
                        ? "running"
                        : "")
                  }
                />
                <div>
                  <strong>{a.name}</strong>
                  <small>
                    {a.target.replace(/^https?:\/\//, "").replace(/\/$/, "")}
                  </small>
                  <span className="sidebar-attack-mode">
                    {modeName(a.config)}
                    <span>
                      ·{" "}
                      {count
                        ? `${count} decision${count === 1 ? "" : "s"}`
                        : a.state?.lifecycle || "Draft"}
                    </span>
                  </span>
                </div>
                {count > 0 && <span className="pending-count">{count}</span>}
              </button>
            );
          })}
          {!shown.length && (
            <p className="sidebar-empty">
              {search
                ? "No matching attacks."
                : "Your investigations will live here."}
            </p>
          )}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-status">
            <span
              className={
                "service-dot " + (healthError || listError ? "offline" : "")
              }
            />
            <div>
              <strong>
                {healthError || listError
                  ? "Service disconnected"
                  : "Local workspace"}
              </strong>
              <small>
                {attacks.filter((a) => active(a.state?.lifecycle)).length} open
                sessions · on this computer
              </small>
            </div>
          </div>
          <div className="sidebar-profile">
            <div className="profile-avatar">Z</div>
            <div>
              <strong>Zero Studio</strong>
              <small>
                {healthData?.scripted
                  ? "Offline test adapter"
                  : "Private by default"}
              </small>
            </div>
            <span className="version-label">v0.1</span>
          </div>
        </div>
        <ResizeHandle
          side="sidebar"
          value={sideWidth}
          onChange={setSideWidth}
        />
      </aside>
      <main className="main-column">
        <div className="topbar">
          <div className="breadcrumb">
            <button onClick={() => navigate("/")}>
              <Folder size={13} />
              Workspace
            </button>
            <ChevronRight size={12} />
            <span>{attack?.name || "All attacks"}</span>
          </div>
          <div className="topbar-right">
            <span className="local-pill">
              <span className="service-dot" />
              {healthData?.scripted
                ? "Scripted test environment"
                : "Local session"}
            </span>
            <button
              className="icon-button"
              aria-label={inspector ? "Collapse inspector" : "Open inspector"}
              onClick={() => setInspector(!inspector)}
            >
              {inspector ? (
                <PanelRightClose size={17} />
              ) : (
                <PanelRightOpen size={17} />
              )}
            </button>
          </div>
        </div>
        {healthData?.scripted && (
          <div className="test-environment">
            Offline test adapter · all model responses and findings in this
            workspace are synthetic fixtures.
          </div>
        )}
        <div
          className="sr-only"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {pending.length
            ? `${pending.length} operator decisions waiting`
            : state?.lifecycle === "idle"
              ? "Ready for your next question"
              : state?.lifecycle === "active"
                ? "Assessment running"
                : state?.lifecycle === "closed"
                  ? "Session ended"
                  : ""}
        </div>
        {!!state?.effective.warning && (
          <div className="test-environment">
            Sandbox startup failed · host execution is active ·{" "}
            {String(state.effective.warning)}
          </div>
        )}
        {session?.kind === "replay" && (
          <div className="test-environment">
            Recorded audit replay · recorded model responses · tool actions may
            execute again under the approval gate.
          </div>
        )}
        <ErrorBox
          error={
            listError ||
            healthError ||
            error ||
            (creationError && creationError.id === id
              ? creationError.message
              : "")
          }
        />
        {!id ? (
          <AllAttacks
            attacks={attacks}
            onNew={() => setNewOpen(true)}
            onOpen={openAttack}
            onArchive={(a) => archive(a).catch((e) => setError(e.message))}
            onManage={setManagedAttack}
            onTemplates={() => setTemplatesOpen(true)}
          />
        ) : attackQuery.isPending ? (
          <Loading />
        ) : attackQuery.error ? (
          <ErrorBox error={attackQuery.error} />
        ) : (
          attack && (
            <>
              <header className="engagement-header">
                <div className="header-title-row">
                  <div>
                    <div className="eyebrow">ATTACK WORKSPACE</div>
                    <h1>{attack.name}</h1>
                  </div>
                  <div className="header-actions">
                    <button
                      className="icon-button"
                      aria-label={`Manage ${attack.name}`}
                      onClick={() => setManagedAttack(attack)}
                    >
                      <MoreHorizontal size={18} />
                    </button>
                    <button
                      className="button"
                      onClick={() => setConfigOpen(true)}
                    >
                      <SlidersHorizontal size={14} />
                      <span>Configure</span>
                    </button>
                    {report && (
                      <a
                        href={download(attack.id, report.id, sid || undefined)}
                        className="button"
                        download
                      >
                        <FileText size={14} />
                        <span>Report</span>
                      </a>
                    )}
                    {live && !historical && (
                      <button
                        className="icon-button end-session"
                        aria-label="End runtime session"
                        onClick={() => setEndOpen(true)}
                      >
                        <Power size={16} />
                      </button>
                    )}
                  </div>
                </div>
                <div className="engagement-meta">
                  <span className="target-label">
                    <Globe size={13} />
                    {attack.target}
                  </span>
                  <span className="meta-divider" />
                  <span>
                    <Layers size={13} />
                    {modeName(session?.config || draftConfig)}
                  </span>
                  <Badge
                    tone={
                      pending.length
                        ? "amber"
                        : state?.lifecycle === "active"
                          ? "olive"
                          : ""
                    }
                  >
                    {pending.length
                      ? `${pending.length} awaiting decision`
                      : progress.label}
                  </Badge>
                </div>
                <nav className="view-tabs" aria-label="Engagement views">
                  {TABS.map(([tab, label]) => (
                    <button
                      key={tab}
                      className={view === tab ? "selected" : ""}
                      onClick={() => go(tab)}
                    >
                      {label}
                      {tab === "findings" &&
                        Object.keys(state?.findings || {}).length > 0 && (
                          <span>{Object.keys(state!.findings).length}</span>
                        )}
                      {tab === "agents" &&
                        Object.values(state?.actors || {}).filter(
                          (a) => a.state === "running",
                        ).length > 0 && <span className="tab-dot" />}
                    </button>
                  ))}
                </nav>
              </header>
              {historical && (
                <div className="historical-banner">
                  <ClockIcon />
                  Viewing session {sid?.slice(0, 12)} ·{" "}
                  {session?.history_path
                    ? "sealed artifacts"
                    : "session history"}
                  <button
                    className="text-button"
                    onClick={() => parameter("session", null)}
                  >
                    Return to current session <ArrowUpRight size={13} />
                  </button>
                </div>
              )}
              <div
                className={
                  "content-shell " + (inspector ? "with-inspector" : "")
                }
              >
                <section className="primary-content">
                  {sessionQuery.error && (
                    <ErrorBox error={sessionQuery.error} />
                  )}{" "}
                  {view === "conversation" ? (
                    <Conversation
                      key={attack.id}
                      attack={attack}
                      session={session}
                      connected={sessionQuery.connected}
                      lastEventAt={sessionQuery.lastEventAt}
                      history={historical ? [] : history}
                      config={draftConfig}
                      draft={draft}
                      onDraft={setDraft}
                      onConfigure={() => setConfigOpen(true)}
                      onOpenFindings={() => go("findings")}
                      onStarted={(newSid) => {
                        setCreationError((current) =>
                          current?.id === id ? null : current,
                        );
                        client.setQueryData<Attack>(["attack", id], (old) =>
                          old ? { ...old, session_id: newSid } : old,
                        );
                      }}
                      onError={setError}
                    />
                  ) : view === "findings" ? (
                    <Findings
                      key={attack.id}
                      eid={attack.id}
                      sid={sid || undefined}
                      selected={params.get("finding")}
                      onSelect={(v) => parameter("finding", v)}
                    />
                  ) : view === "agents" ? (
                    <Agents
                      key={attack.id}
                      eid={attack.id}
                      sid={sid || undefined}
                      session={session}
                      selected={params.get("actor")}
                      onSelect={(v) => parameter("actor", v)}
                    />
                  ) : view === "workspace" ? (
                    <Workspace
                      key={attack.id}
                      attack={attack}
                      sid={sid || undefined}
                      section={params.get("section") || "Scope context"}
                      onSection={(s) => parameter("section", s)}
                      onSession={(s) => parameter("session", s)}
                    />
                  ) : view === "activity" ? (
                    <ActivityView eid={attack.id} sid={sid || undefined} />
                  ) : (
                    <Empty title="View not found">
                      <button
                        className="button"
                        onClick={() => go("conversation")}
                      >
                        Open conversation
                      </button>
                    </Empty>
                  )}
                </section>
                {inspector && (
                  <aside className="agent-inspector">
                    <ResizeHandle
                      side="inspector"
                      value={inspectWidth}
                      onChange={setInspectWidth}
                    />
                    <div className="inspector-title">
                      <span className="eyebrow">LIVE INSPECTOR</span>
                      <button
                        className="icon-button"
                        aria-label="Close inspector"
                        onClick={() => setInspector(false)}
                      >
                        <PanelRightClose size={15} />
                      </button>
                    </div>
                    <div
                      className={
                        "runtime-card " + (pending.length ? "attention" : "")
                      }
                    >
                      <div className="row">
                        <span
                          className={
                            progress.running ? "working-dot" : "quiet-dot"
                          }
                        />
                        <strong>{progress.label}</strong>
                      </div>
                      <p>{progress.detail}</p>
                      {state && (
                        <div className="runtime-card-bottom">
                          <span>
                            {sessionQuery.connected
                              ? "Connected"
                              : "Reconnecting"}
                          </span>
                          <span>
                            {String(
                              state.effective.execution_location ||
                                "Not started",
                            )}
                          </span>
                        </div>
                      )}
                    </div>
                    <div className="inspector-section">
                      <div className="row spread">
                        <h3>Working team</h3>
                        <button
                          className="text-button"
                          onClick={() => go("agents")}
                        >
                          View all
                          <ArrowUpRight size={12} />
                        </button>
                      </div>
                      {session ? (
                        <>
                          <button
                            className="inspector-agent"
                            onClick={() => {
                              go("agents");
                              setTimeout(
                                () =>
                                  parameter(
                                    "actor",
                                    session?.config.execution_mode === "a"
                                      ? "agent"
                                      : "orchestrator",
                                  ),
                                0,
                              );
                            }}
                          >
                            <span className="agent-icon">
                              <Layers size={16} />
                            </span>
                            <div>
                              <strong>
                                {modeName(session.config) === "Single agent"
                                  ? "Pentester"
                                  : "Orchestrator"}
                              </strong>
                              <small>
                                {state?.lifecycle} · {state?.status?.turn || 0}{" "}
                                turns
                              </small>
                            </div>
                          </button>
                          {Object.values(state?.actors || {})
                            .filter(
                              (a) => !["agent", "orchestrator"].includes(a.id),
                            )
                            .slice(-5)
                            .map((a) => (
                              <button
                                className="inspector-agent"
                                key={a.id}
                                onClick={() =>
                                  navigate(
                                    `/attacks/${id}/agents?actor=${encodeURIComponent(a.id)}`,
                                  )
                                }
                              >
                                <span
                                  className={
                                    "agent-icon " +
                                    (a.subkind === "validator"
                                      ? "validator"
                                      : "")
                                  }
                                >
                                  <Users size={15} />
                                </span>
                                <div>
                                  <strong>
                                    {a.subkind} <span>{a.id}</span>
                                  </strong>
                                  <small>{a.target || a.state}</small>
                                </div>
                                <span
                                  className={
                                    "mini-dot " +
                                    (a.state === "running" ? "running" : "")
                                  }
                                />
                              </button>
                            ))}
                        </>
                      ) : (
                        <p className="inspector-empty">
                          Agents appear when the assessment starts.
                        </p>
                      )}
                    </div>
                    <div className="inspector-section">
                      <h3>Validation</h3>
                      <div className="validation-counts">
                        {["confirmed", "pending", "rejected"].map((v) => (
                          <div key={v}>
                            <strong>
                              {
                                Object.values(state?.findings || {}).filter(
                                  (f) =>
                                    (f as { verdict: string }).verdict === v,
                                ).length
                              }
                            </strong>
                            <span>{v}</span>
                          </div>
                        ))}
                      </div>
                      <p className="inspector-caption">
                        <ShieldCheck size={13} />
                        Every confirmation needs an independent validator.
                      </p>
                    </div>
                    <div className="inspector-section">
                      <h3>Execution</h3>
                      <dl className="execution-list">
                        <div>
                          <dt>Backend</dt>
                          <dd>
                            {String(
                              state?.effective.backend ||
                                draftConfig.backend ||
                                "codex",
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt>Model</dt>
                          <dd className="mono">
                            {String(state?.effective.model || "Not resolved")}
                          </dd>
                        </div>
                        <div>
                          <dt>Tools</dt>
                          <dd>
                            {String(
                              state?.effective.execution_location ||
                                (draftConfig.container
                                  ? "Docker requested"
                                  : "Host requested"),
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt>Context</dt>
                          <dd>
                            {Number(state?.status?.context_tokens) > 0
                              ? `${state?.status?.context_tokens} / ${state?.status?.context_window}`
                              : "Unavailable"}
                          </dd>
                        </div>
                        <div>
                          <dt>Rate limit</dt>
                          <dd>
                            {String(
                              session?.config.rate_limit ||
                                draftConfig.rate_limit,
                            )}
                          </dd>
                        </div>
                      </dl>
                      <button
                        className="text-button"
                        onClick={() => setConfigOpen(true)}
                      >
                        Inspect configuration
                        <ArrowUpRight size={12} />
                      </button>
                    </div>
                    <div className="inspector-section">
                      <div className="row spread">
                        <h3>Recent tools</h3>
                        <button
                          className="text-button"
                          onClick={() => go("activity")}
                        >
                          Activity
                          <ArrowUpRight size={12} />
                        </button>
                      </div>
                      {Object.values(state?.tools || {})
                        .slice(-4)
                        .reverse()
                        .map((t) => (
                          <div className="inspector-tool" key={t.call_id}>
                            <Terminal size={13} />
                            <code>{t.tool}</code>
                            <span>
                              {t.state === "tool_running"
                                ? "Running"
                                : t.state === "tool_proposal"
                                  ? "Proposed"
                                  : t.ok
                                    ? "Done"
                                    : "Failed"}
                            </span>
                          </div>
                        ))}
                      {!Object.keys(state?.tools || {}).length && (
                        <p className="inspector-empty">
                          Execution events will appear here.
                        </p>
                      )}
                    </div>
                    <div className="inspector-bottom">
                      <LockIcon />
                      <span>
                        Agent loops run on the host.
                        <br />
                        Eligible tools run in the sandbox.
                      </span>
                    </div>
                  </aside>
                )}
              </div>
            </>
          )
        )}
      </main>
      {templatesOpen && (
        <TemplateManager
          onClose={() => setTemplatesOpen(false)}
          onUse={(template) => {
            setTemplatesOpen(false);
            setInitialTemplate(template);
            setNewOpen(true);
          }}
        />
      )}
      {newOpen && schema && (
        <NewAttack
          schema={schema}
          initialTemplate={initialTemplate}
          onClose={() => {
            setNewOpen(false);
            setInitialTemplate(undefined);
          }}
          onCreated={(a, issue) => {
            drafts.current[a.id] = a.draft;
            client.setQueryData(["attack", a.id], a);
            client.invalidateQueries({ queryKey: ["attack", a.id] });
            setCreationError(issue ? { id: a.id, message: issue } : null);
            setNewOpen(false);
            setInitialTemplate(undefined);
            client.invalidateQueries({ queryKey: ["attacks"] });
            navigate(`/attacks/${a.id}/conversation`);
          }}
        />
      )}
      {managedAttack && (
        <AttackActions
          key={managedAttack.id}
          attack={managedAttack}
          running={active(
            attacks.find((a) => a.id === managedAttack.id)?.state?.lifecycle,
          )}
          onClose={() => setManagedAttack(null)}
          onArchive={() => archive(managedAttack)}
          onTemplate={(template, create) => {
            client.invalidateQueries({ queryKey: ["templates"] });
            setManagedAttack(null);
            if (create) {
              setInitialTemplate(template);
              setNewOpen(true);
            }
          }}
          onDeleted={(sessions) => {
            const eid = managedAttack.id;
            deleted.current.add(eid);
            delete drafts.current[eid];
            forgetAttack(eid, sessions);
            setManagedAttack(null);
            if (id === eid) {
              setConfigOpen(false);
              setEndOpen(false);
              navigate("/");
            }
            client.setQueryData<Attack[]>(["attacks"], (old) =>
              old?.filter((a) => a.id !== eid),
            );
            client.removeQueries({
              predicate: (q) =>
                q.queryKey.includes(eid) ||
                sessions.some((sid) => q.queryKey.includes(sid)),
            });
            client.invalidateQueries({ queryKey: ["attacks"] });
          }}
        />
      )}
      {configOpen && attack && (
        <ConfigSheet
          initial={draftConfig}
          running={live}
          onClose={() => setConfigOpen(false)}
          onSave={async (cfg) => {
            await api(`/engagements/${attack.id}`, "PATCH", { config: cfg });
            client.invalidateQueries({ queryKey: ["attack", attack.id] });
            client.invalidateQueries({ queryKey: ["attacks"] });
          }}
        />
      )}
      {endOpen && session && (
        <Dialog
          title="End this runtime session?"
          onClose={() => !closing && setEndOpen(false)}
        >
          <div className="dialog-body">
            <p>
              Zero will cancel this session’s agents, close model and tool
              sessions, and stop its sandbox and background jobs. Conversation
              and evidence will remain available.
            </p>
            <p className="notice">
              {
                Object.values(state?.actors || {}).filter(
                  (a) => a.state === "running",
                ).length
              }{" "}
              specialists active · {pending.length} pending decisions will
              expire.
            </p>
          </div>
          <div className="dialog-footer">
            <button
              className="button"
              disabled={closing}
              onClick={() => setEndOpen(false)}
            >
              Keep session
            </button>
            <div className="spacer" />
            <button
              className="button primary"
              disabled={closing}
              onClick={async () => {
                setClosing(true);
                try {
                  await action(session.id, "close");
                  setEndOpen(false);
                  client.invalidateQueries({
                    queryKey: ["session", session.id],
                  });
                  client.invalidateQueries({ queryKey: ["artifacts"] });
                } catch (e) {
                  setError((e as Error).message);
                } finally {
                  setClosing(false);
                }
              }}
            >
              {closing ? "Closing and cleaning up…" : "End session"}
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}
function LockIcon() {
  return <ShieldCheck size={14} />;
}
function ClockIcon() {
  return <Activity size={14} />;
}
