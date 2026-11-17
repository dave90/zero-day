import { useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Plus,
  ArrowUpRight,
  Globe,
  Archive,
  Upload,
  Layers,
  Search,
  ArrowRight,
  MoreHorizontal,
  Copy,
  Trash2,
  FileText,
} from "lucide-react";
import { uploadFile, api, key } from "../api/client";
import type { Attack, AttackTemplate, Config, Schema } from "../api/types";
import {
  Dialog,
  ErrorBox,
  Badge,
  Empty,
  Loading,
  modeName,
} from "../components/common";
import { ModeCards } from "./configuration";
import { SwarmSizeField } from "../components/SwarmSizeField";
import { defaultFirstGoal } from "../templates/first-goal";
import {
  PentestContextBuilder,
  type PentestContextDetails,
} from "../components/PentestContextBuilder";
import {
  InitialRunSettings,
  approvalError,
} from "../components/InitialRunSettings";
function goalForTemplate(template?: AttackTemplate): string {
  const goal = String(template?.config.goal || "");
  return goal.trim() ? goal : defaultFirstGoal(template?.target);
}

function automaticGoalForTemplate(template?: AttackTemplate): boolean {
  const goal = String(template?.config.goal || "");
  return !goal.trim() || goal === defaultFirstGoal(template?.target);
}

export function NewAttack({
  schema,
  initialTemplate,
  onClose,
  onCreated,
}: {
  schema: Schema;
  initialTemplate?: AttackTemplate;
  onClose: () => void;
  onCreated: (a: Attack, creationError?: string) => void;
}) {
  const submitting = useRef(false);
  const scopeEditor = useRef<HTMLTextAreaElement>(null);
  const fillButton = useRef<HTMLButtonElement>(null);
  const { data: templates = [], error: templateError } = useQuery({
    queryKey: ["templates"],
    queryFn: () => api<AttackTemplate[]>("/templates"),
  });
  const [selected, setSelected] = useState(initialTemplate?.id || ""),
    [managingTemplates, setManagingTemplates] = useState(false),
    [buildingContext, setBuildingContext] = useState(false),
    [contextDetails, setContextDetails] = useState<PentestContextDetails>({
      additionalUrls: "",
      excludedUrls: "",
      credentials: "",
    }),
    [name, setName] = useState(
      initialTemplate ? `${initialTemplate.name} copy` : "",
    ),
    [target, setTarget] = useState(initialTemplate?.target || ""),
    [scope, setScope] = useState(initialTemplate?.context ?? ""),
    [goal, setGoal] = useState(goalForTemplate(initialTemplate)),
    [automaticGoal, setAutomaticGoal] = useState(
      automaticGoalForTemplate(initialTemplate),
    ),
    [config, setConfig] = useState<Config>({
      ...schema.defaults,
      ...initialTemplate?.config,
    }),
    [files, setFiles] = useState<File[]>([]),
    [creatingAction, setCreatingAction] = useState<"save" | "run" | null>(null),
    [error, setError] = useState("");
  const busy = creatingAction !== null;
  function selectTemplate(tid: string) {
    applyTemplate(templates.find((t) => t.id === tid));
  }
  function applyTemplate(template?: AttackTemplate) {
    setSelected(template?.id || "");
    setName(template ? `${template.name} copy` : "");
    setTarget(template?.target || "");
    setScope(template?.context ?? "");
    setBuildingContext(false);
    setContextDetails({
      additionalUrls: "",
      excludedUrls: "",
      credentials: "",
    });
    setGoal(goalForTemplate(template));
    setAutomaticGoal(automaticGoalForTemplate(template));
    setConfig({ ...schema.defaults, ...template?.config });
    setFiles([]);
  }
  async function create(e: FormEvent) {
    e.preventDefault();
    if (submitting.current || buildingContext || approvalError(config)) return;
    const submitter = (e.nativeEvent as SubmitEvent).submitter;
    const run =
      submitter instanceof HTMLButtonElement && submitter.value === "run";
    if (run && !goal.trim()) {
      setError("Enter the first assessment goal before running.");
      return;
    }
    submitting.current = true;
    setCreatingAction(run ? "run" : "save");
    setError("");
    let attack: Attack | undefined;
    let creationError: string | undefined;
    let stage = "saving the attack";
    try {
      const oversized = files.find((f) => f.size > 1_000_000);
      if (oversized) throw new Error(`${oversized.name} exceeds 1 MB`);
      attack = await api<Attack>("/engagements", "POST", {
        name,
        target,
        context: scope,
        config: { ...config, goal: goal || null },
        draft: goal,
        ...(selected ? { template_id: selected } : {}),
      });
      stage = "importing context files";
      for (const f of files) await uploadFile(attack.id, f);
      if (run) {
        stage = "starting the assessment";
        const result = await api<{ session_id: string }>(
          `/engagements/${attack.id}/sessions`,
          "POST",
          { config: attack.config, idempotency_key: key() },
        );
        attack = { ...attack, session_id: result.session_id, draft: "" };
      }
    } catch (e) {
      if (attack) {
        // Keep the saved workspace when imports/preflight/startup fail. Opening
        // it lets the operator fix the problem and retry without duplicate attacks.
        creationError = `Attack created, but ${stage} failed: ${(e as Error).message}`;
      } else {
        setError((e as Error).message);
      }
    } finally {
      submitting.current = false;
      setCreatingAction(null);
    }
    if (attack) onCreated(attack, creationError);
  }
  if (managingTemplates)
    return (
      <TemplateManager
        onClose={() => setManagingTemplates(false)}
        onUse={(template) => {
          applyTemplate(template);
          setManagingTemplates(false);
        }}
        onDeleted={(tid) => {
          if (selected === tid) setSelected("");
        }}
        returnLabel="Back to assessment"
      />
    );
  return (
    <Dialog
      title="A new assessment"
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <form onSubmit={create}>
        <div className="dialog-body">
          <p className="intro">
            Start with a target, a clear scope, and a question worth
            investigating.
          </p>
          <label className="template-picker">
            Start from
            <select
              value={selected}
              onChange={(e) => selectTemplate(e.target.value)}
            >
              <option value="">Blank assessment</option>
              {initialTemplate &&
                selected === initialTemplate.id &&
                !templates.some((t) => t.id === initialTemplate.id) && (
                  <option value={initialTemplate.id}>
                    {initialTemplate.name}
                  </option>
                )}
              {templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <button
            className="text-button manage-templates-link"
            type="button"
            onClick={() => setManagingTemplates(true)}
          >
            <Copy size={13} />
            Manage templates
          </button>
          <ErrorBox error={templateError} />
          {selected && (
            <p className="template-note">
              Saved configuration and first goal are copied. Review the target
              and scope below. Messages, findings, files, and runtime state
              start fresh. Reimport any Burp files referenced in configuration.
            </p>
          )}
          <div className="form-grid">
            <label>
              Attack name
              <input
                autoFocus
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Acme API assessment"
                maxLength={200}
              />
            </label>
            <label>
              Target / start URL
              <input
                type="url"
                required
                value={target}
                onChange={(e) => {
                  setTarget(e.target.value);
                  if (automaticGoal) setGoal(defaultFirstGoal(e.target.value));
                }}
                placeholder="https://app.example"
              />
            </label>
          </div>
          <div className="row spread field-heading">
            <label htmlFor="new-scope">
              Engagement context <code>pentest.md</code>
            </label>
            <div className="scope-actions">
              <button
                type="button"
                className="text-button"
                ref={fillButton}
                aria-expanded={buildingContext}
                aria-controls="pentest-template-builder"
                onClick={() => setBuildingContext(!buildingContext)}
              >
                <FileText size={14} /> Fill from template
              </button>
              <label className="text-button file-label">
                <Upload size={14} /> Import file
                <input
                  type="file"
                  accept=".md,.txt"
                  onChange={async (e) => {
                    const f = e.target.files?.[0];
                    if (f) {
                      if (f.size > 1_000_000)
                        setError("Scope file exceeds 1 MB");
                      else {
                        setScope(await f.text());
                        setBuildingContext(false);
                      }
                    }
                  }}
                />
              </label>
            </div>
          </div>
          <PentestContextBuilder
            key={"context-" + (selected || "blank")}
            open={buildingContext}
            target={target}
            goal={goal}
            hasContext={!!scope.trim()}
            details={contextDetails}
            onChange={setContextDetails}
            onClose={() => {
              setBuildingContext(false);
              fillButton.current?.focus();
            }}
            onGenerate={(context) => {
              setScope(context);
              setBuildingContext(false);
              scopeEditor.current?.focus();
            }}
          />
          <textarea
            ref={scopeEditor}
            id="new-scope"
            className="scope-input"
            placeholder="Write engagement context, import pentest.md, or choose Fill from template."
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            rows={7}
          />
          <p className="field-help">
            Saved locally as pentest.md and passed verbatim to this attack’s
            agents. You can edit it before creating the attack. Scope is
            advisory.
          </p>
          <label className="block-label" htmlFor="new-goal">
            First goal
          </label>
          <textarea
            id="new-goal"
            value={goal}
            onChange={(e) => {
              setGoal(e.target.value);
              setAutomaticGoal(false);
            }}
            placeholder="What should Zero investigate?"
            rows={3}
          />
          <span className="block-label">Working style</span>
          <ModeCards config={config} onChange={setConfig} />
          {Number(config.swarm_size) > 1 && (
            <SwarmSizeField
              key={"swarm-" + (selected || "blank")}
              value={Number(config.swarm_size)}
              onChange={(swarm_size) => setConfig({ ...config, swarm_size })}
            />
          )}
          <InitialRunSettings
            key={selected || "blank"}
            config={config}
            schema={schema}
            onChange={setConfig}
          />
          <label
            className="drop-zone"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              setFiles([...files, ...Array.from(e.dataTransfer.files)]);
            }}
          >
            <Upload size={21} />
            <span>Add context files or drop them here</span>
            <small>
              {files.length
                ? files.map((f) => f.name).join(", ")
                : "Text, source, or Burp exports · up to 1 MB per file"}
            </small>
            <input
              multiple
              type="file"
              onChange={(e) =>
                setFiles([...files, ...Array.from(e.target.files || [])])
              }
            />
          </label>
          <ErrorBox error={error} />
        </div>
        <div className="dialog-footer new-attack-footer">
          <span className="field-help">
            {buildingContext
              ? "Generate or cancel template fill to continue."
              : "Run now with these settings, or save a draft to start later."}
          </span>
          <div className="spacer" />
          <button
            type="button"
            className="button"
            disabled={busy}
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="submit"
            value="save"
            className="button"
            disabled={busy || buildingContext || !!approvalError(config)}
          >
            {creatingAction === "save" ? "Creating…" : "Only create attack"}
          </button>
          <button
            type="submit"
            value="run"
            className="button primary"
            disabled={
              busy || buildingContext || !!approvalError(config) || !goal.trim()
            }
          >
            {creatingAction === "run"
              ? "Creating and starting…"
              : "Create attack and run"}
            <ArrowRight size={15} />
          </button>
        </div>
      </form>
    </Dialog>
  );
}
export function AllAttacks({
  attacks,
  onNew,
  onOpen,
  onArchive,
  onManage,
  onTemplates,
}: {
  attacks: Attack[];
  onNew: () => void;
  onOpen: (id: string) => void;
  onArchive: (a: Attack) => void;
  onManage: (a: Attack) => void;
  onTemplates: () => void;
}) {
  const [search, setSearch] = useState(""),
    [archived, setArchived] = useState(false);
  const visible = attacks.filter(
    (a) =>
      a.archived === archived &&
      (a.name + " " + a.target).toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <div className="overview-page">
      <div className="overview-intro">
        <div className="eyebrow">YOUR SECURITY WORKSPACE</div>
        <div className="row spread">
          <h1>Every attack starts with a question.</h1>
          <div className="row overview-actions">
            <button className="button" onClick={onTemplates}>
              <Copy size={15} />
              Templates
            </button>
            <button className="button primary" onClick={onNew}>
              <Plus size={16} /> New attack
            </button>
          </div>
        </div>
        <p>One conversation. Focused agents. Evidence you can inspect.</p>
      </div>
      <div className="overview-toolbar">
        <div className="segmented">
          <button
            className={!archived ? "selected" : ""}
            onClick={() => setArchived(false)}
          >
            All attacks <span>{attacks.filter((a) => !a.archived).length}</span>
          </button>
          <button
            className={archived ? "selected" : ""}
            onClick={() => setArchived(true)}
          >
            Archived
          </button>
        </div>
        <label className="search-field">
          <Search size={15} />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search name or target"
            aria-label="Filter all attacks"
          />
        </label>
      </div>
      {!visible.length ? (
        <Empty
          title={
            search ? "No matching attacks" : "Room for your next investigation"
          }
        >
          <p>
            {archived
              ? "Archived conversations will appear here."
              : "Create an attack to bring your scope, agents, and findings into one workspace."}
          </p>
          {!archived && (
            <button className="button" onClick={onNew}>
              <Plus size={15} /> Create your first attack
            </button>
          )}
        </Empty>
      ) : (
        <div className="attack-grid">
          {visible.map((a) => {
            const counts = Object.values(a.state?.findings || {}) as {
              verdict: string;
            }[];
            return (
              <article className="attack-card" key={a.id}>
                <div className="row spread">
                  <div className="target-icon">
                    <Globe size={20} />
                  </div>
                  <Badge tone={a.state?.lifecycle === "active" ? "olive" : ""}>
                    {a.state?.lifecycle || "Draft"}
                  </Badge>
                </div>
                <button className="card-open" onClick={() => onOpen(a.id)}>
                  <h2>{a.name}</h2>
                  <span>{a.target}</span>
                </button>
                <div className="attack-card-meta">
                  <span>
                    <Layers size={13} />
                    {modeName(a.config)}
                  </span>
                  <span>
                    {counts.filter((f) => f.verdict === "confirmed").length}{" "}
                    confirmed
                  </span>
                </div>
                <div className="row spread attack-card-footer">
                  <button className="text-button" onClick={() => onOpen(a.id)}>
                    Open conversation <ArrowUpRight size={14} />
                  </button>
                  <div className="row">
                    <button
                      className="icon-button"
                      aria-label={
                        a.archived ? "Restore attack" : "Archive attack"
                      }
                      onClick={() => onArchive(a)}
                    >
                      <Archive size={15} />
                    </button>
                    <button
                      className="icon-button"
                      aria-label={`Manage ${a.name}`}
                      onClick={() => onManage(a)}
                    >
                      <MoreHorizontal size={16} />
                    </button>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}
      <div className="overview-footnote">
        <span className="little-cross">✳</span> Your workspace lives on this
        computer. Assessments continue when you switch conversations.
      </div>
    </div>
  );
}

export function TemplateManager({
  onClose,
  onUse,
  onDeleted,
  returnLabel = "Done",
}: {
  onClose: () => void;
  onUse: (template: AttackTemplate) => void;
  onDeleted?: (id: string) => void;
  returnLabel?: string;
}) {
  const client = useQueryClient();
  const {
    data: templates = [],
    isPending,
    error: loadError,
  } = useQuery({
    queryKey: ["templates"],
    queryFn: () => api<AttackTemplate[]>("/templates"),
    refetchInterval: 5000,
  });
  const [deleting, setDeleting] = useState<AttackTemplate | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  async function remove() {
    if (!deleting) return;
    setBusy(true);
    setError("");
    try {
      await api(`/templates/${deleting.id}`, "DELETE");
      client.setQueryData<AttackTemplate[]>(["templates"], (old) =>
        old?.filter((t) => t.id !== deleting.id),
      );
      client.invalidateQueries({ queryKey: ["templates"] });
      onDeleted?.(deleting.id);
      setNotice(`Deleted template “${deleting.name}”.`);
      setDeleting(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      title={deleting ? "Delete this template?" : "Saved templates"}
      onClose={() => !busy && onClose()}
    >
      <div className="dialog-body">
        {deleting ? (
          <>
            <p className="intro">
              Delete <strong>{deleting.name}</strong> from your saved templates?
            </p>
            <p className="template-target">{deleting.target}</p>
            <p className="field-help">
              This removes the saved setup. Attacks already created from this
              template keep their configuration, conversation, and evidence.
            </p>
            <ErrorBox error={error} />
          </>
        ) : (
          <>
            <p className="intro">
              Reuse a saved setup or remove templates you no longer need.
            </p>
            {notice && (
              <p className="notice" role="status">
                {notice}
              </p>
            )}
            <ErrorBox error={loadError} />
            {isPending ? (
              <Loading />
            ) : !loadError && !templates.length ? (
              <Empty title="No saved templates">
                <p>
                  Open an attack’s ⋯ menu and choose Create template to save its
                  setup.
                </p>
              </Empty>
            ) : (
              <div className="template-list">
                {templates.map((template) => (
                  <article
                    className="template-card"
                    key={template.id}
                    aria-label={template.name}
                  >
                    <h3>{template.name}</h3>
                    <p className="template-target">{template.target}</p>
                    <div className="template-card-meta">
                      <span>{modeName(template.config)}</span>
                      <span>
                        {new Date(
                          template.created_at * 1000,
                        ).toLocaleDateString()}
                      </span>
                      {template.context !== null && (
                        <span>Includes context</span>
                      )}
                    </div>
                    <div className="row spread">
                      <button
                        className="button"
                        onClick={() => onUse(template)}
                      >
                        Create attack
                        <ArrowUpRight size={14} />
                      </button>
                      <button
                        className="text-button danger-text"
                        aria-label={`Delete template ${template.name}`}
                        onClick={() => {
                          setDeleting(template);
                          setError("");
                          setNotice("");
                        }}
                      >
                        <Trash2 size={14} />
                        Delete
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </>
        )}
      </div>
      <div className="dialog-footer">
        <div className="spacer" />
        {deleting ? (
          <>
            <button
              className="button"
              autoFocus
              disabled={busy}
              onClick={() => {
                setDeleting(null);
                setError("");
              }}
            >
              Cancel
            </button>
            <button
              className="button danger-button"
              disabled={busy}
              onClick={remove}
            >
              <Trash2 size={14} />
              {busy ? "Deleting…" : "Delete template"}
            </button>
          </>
        ) : (
          <button className="button" onClick={onClose}>
            {returnLabel}
          </button>
        )}
      </div>
    </Dialog>
  );
}

export function AttackActions({
  attack,
  running,
  onClose,
  onArchive,
  onDeleted,
  onTemplate,
}: {
  attack: Attack;
  running: boolean;
  onClose: () => void;
  onArchive: () => Promise<void>;
  onDeleted: (sessions: string[]) => void;
  onTemplate: (t: AttackTemplate, create: boolean) => void;
}) {
  const [step, setStep] = useState<"menu" | "template" | "delete">("menu"),
    [templateName, setTemplateName] = useState(attack.name),
    [includeContext, setIncludeContext] = useState(false),
    [confirmation, setConfirmation] = useState(""),
    [closeSession, setCloseSession] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function perform(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function saveTemplate(create: boolean) {
    await perform(async () => {
      const template = await api<AttackTemplate>("/templates", "POST", {
        source_id: attack.id,
        name: templateName,
        include_context: includeContext,
      });
      onTemplate(template, create);
    });
  }
  return (
    <Dialog
      title={
        step === "delete"
          ? "Delete this attack?"
          : step === "template"
            ? "Create a template"
            : "Manage attack"
      }
      onClose={() => !busy && onClose()}
    >
      {step === "menu" ? (
        <>
          <div className="dialog-body attack-actions">
            <p className="intro">{attack.name}</p>
            <button
              className="attack-action"
              onClick={() => setStep("template")}
            >
              <Copy size={19} />
              <span>
                <strong>Create template</strong>
                <small>
                  Reuse configuration and the first goal in a fresh attack.
                </small>
              </span>
              <ArrowRight size={16} />
            </button>
            <button
              className="attack-action"
              disabled={busy}
              onClick={() =>
                perform(async () => {
                  await onArchive();
                  onClose();
                })
              }
            >
              <Archive size={19} />
              <span>
                <strong>
                  {attack.archived ? "Restore attack" : "Archive attack"}
                </strong>
                <small>
                  Keep the conversation and evidence in your workspace.
                </small>
              </span>
            </button>
            <button
              className="attack-action danger-text"
              onClick={() => setStep("delete")}
            >
              <Trash2 size={19} />
              <span>
                <strong>Delete attack</strong>
                <small>
                  Permanently remove its conversation, files, and history.
                </small>
              </span>
            </button>
            <ErrorBox error={error} />
          </div>
        </>
      ) : step === "template" ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            saveTemplate(true);
          }}
        >
          <div className="dialog-body">
            <p className="intro">
              Save the current configuration and first goal as a reusable
              starting point. New attacks have no messages, findings, uploaded
              files, agents, or run history. Ralph starts a new plan.
            </p>
            <label className="block-label">
              Template name
              <input
                autoFocus
                required
                maxLength={200}
                value={templateName}
                onChange={(e) => setTemplateName(e.target.value)}
              />
            </label>
            <label className="management-check">
              <input
                type="checkbox"
                checked={includeContext}
                onChange={(e) => setIncludeContext(e.target.checked)}
              />
              Include engagement context (pentest.md)
            </label>
            <p className="field-help">
              If included, scope and credentials in that file are copied
              verbatim. Otherwise each attack gets a blank engagement context.
              Files referenced by configuration must be imported again.
            </p>
            <ErrorBox error={error} />
          </div>
          <div className="dialog-footer">
            <button
              type="button"
              className="button"
              disabled={busy}
              onClick={() => setStep("menu")}
            >
              Back
            </button>
            <div className="spacer" />
            <button
              type="button"
              className="button"
              disabled={busy || !templateName.trim()}
              onClick={() => saveTemplate(false)}
            >
              Save template
            </button>
            <button
              className="button primary"
              disabled={busy || !templateName.trim()}
            >
              {busy ? "Saving…" : "Save & create attack"}
              <ArrowRight size={15} />
            </button>
          </div>
        </form>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            perform(async () => {
              const result = await api<{ session_ids: string[] }>(
                `/engagements/${attack.id}`,
                "DELETE",
                {
                  confirm_name: confirmation,
                  close_session: closeSession,
                },
              );
              onDeleted(result.session_ids);
            });
          }}
        >
          <div className="dialog-body">
            <p className="intro">
              This permanently removes <strong>{attack.name}</strong>, including
              messages, findings, uploads, scope, and all saved run history from
              this workspace. Saved templates remain available. This cannot be
              undone.
            </p>
            <label className="management-check">
              <input
                type="checkbox"
                checked={closeSession}
                onChange={(e) => setCloseSession(e.target.checked)}
              />
              End any runtime belonging to this attack
            </label>
            {running && (
              <p className="field-help">
                This attack has an open runtime. Confirm above to close it
                before deleting its data.
              </p>
            )}
            <label className="block-label">
              Type the attack name to delete
              <input
                autoFocus
                required
                autoComplete="off"
                value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)}
                placeholder={attack.name}
              />
            </label>
            <ErrorBox error={error} />
          </div>
          <div className="dialog-footer">
            <button
              type="button"
              className="button"
              disabled={busy}
              onClick={() => setStep("menu")}
            >
              Back
            </button>
            <div className="spacer" />
            <button
              className="button danger-button"
              disabled={
                busy ||
                confirmation !== attack.name ||
                (running && !closeSession)
              }
            >
              {busy ? "Deleting…" : "Delete permanently"}
              <Trash2 size={15} />
            </button>
          </div>
        </form>
      )}
    </Dialog>
  );
}
