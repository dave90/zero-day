import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Copy,
  Check,
  SlidersHorizontal,
  Layers,
  Users,
  Repeat,
  User,
} from "lucide-react";
import { api } from "../api/client";
import type { Config, Schema, ConfigField } from "../api/types";
import { ModelSelect } from "../components/ModelSelect";
import { Dialog, ErrorBox, Loading } from "../components/common";
const GROUPS: Record<string, string[]> = {
  Execution: ["goal", "execution_mode", "swarm_size", "ralph", "backend"],
  "Scope & context": ["scope_host", "files_root", "burp_dir"],
  "Agents & budgets": [
    "max_turns",
    "pentester_max_turns",
    "validator_max_turns",
    "max_subagents",
    "max_total_subagents",
    "subagent_deadline",
    "llm_timeout",
    "validator_claude_max_turns",
  ],
  "Model & sessions": ["model", "session_resume", "context_window"],
  "Sandbox & network": [
    "container",
    "container_image",
    "container_cpus",
    "container_memory",
    "host_network",
    "remote_all_tools",
    "rate_limit",
  ],
  Approvals: ["auto_approve", "yolo", "i_understand", "allow_host_exec"],
  "Ralph workflow": [
    "ralph_iterations",
    "ralph_resume",
    "ralph_approve_prd",
    "ralph_discussion_rounds",
    "ralph_epics",
    "analyzer_validator_max_turns",
  ],
  "Display & logs": ["thought_display", "llm_log"],
};
export function preset(c: Config, mode: string): Config {
  return {
    ...c,
    execution_mode: mode === "single" ? "a" : "c",
    swarm_size: mode === "swarm" ? Math.max(2, Number(c.swarm_size) || 2) : 1,
    ralph: mode === "ralph",
    ralph_resume: mode === "ralph" ? c.ralph_resume : false,
  };
}
export function ModeCards({
  config,
  onChange,
}: {
  config: Config;
  onChange: (c: Config) => void;
}) {
  const selected = config.ralph
    ? "ralph"
    : Number(config.swarm_size) > 1
      ? "swarm"
      : config.execution_mode === "a"
        ? "single"
        : config.execution_mode === "c"
          ? "orchestrated"
          : "";
  return (
    <div className="mode-grid">
      {[
        {
          id: "orchestrated",
          title: "Orchestrated",
          text: "One lead. Focused specialists.",
          Icon: Layers,
        },
        {
          id: "swarm",
          title: "Swarm",
          text: "Peers with shared evidence.",
          Icon: Users,
        },
        {
          id: "ralph",
          title: "Ralph",
          text: "Plan, assess, review, repeat.",
          Icon: Repeat,
        },
        {
          id: "single",
          title: "Single agent",
          text: "One investigator + validators.",
          Icon: User,
        },
      ].map(({ id, title, text, Icon }) => (
        <button
          key={id}
          type="button"
          className={`mode-card ${selected === id ? "selected" : ""}`}
          onClick={() => onChange(preset(config, id))}
          aria-pressed={selected === id}
        >
          <Icon size={19} />
          <strong>{title}</strong>
          <small>{text}</small>
          {selected === id && <Check className="mode-check" size={14} />}
        </button>
      ))}
    </div>
  );
}
export function ConfigSheet({
  initial,
  onClose,
  onSave,
  running = false,
}: {
  initial: Config;
  onClose: () => void;
  onSave: (cfg: Config) => Promise<void>;
  running?: boolean;
}) {
  const { data: schema } = useQuery({
    queryKey: ["schema"],
    queryFn: () => api<Schema>("/config-schema"),
  });
  const [config, setConfig] = useState<Config>(initial),
    [group, setGroup] = useState("Execution"),
    [error, setError] = useState(""),
    [command, setCommand] = useState(""),
    [saving, setSaving] = useState(false),
    [copied, setCopied] = useState(false);
  useEffect(() => {
    if (schema) setConfig((c) => ({ ...schema.defaults, ...c }));
  }, [schema]);
  useEffect(() => {
    let live = true;
    const t = setTimeout(
      () =>
        api<{ command: string }>("/config/validate", "POST", config)
          .then((r) => {
            if (live) {
              setCommand(r.command);
              setError("");
            }
          })
          .catch((e) => {
            if (live) setError(e.message);
          }),
      200,
    );
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [config]);
  const backendModels = useRef<Record<string, Config[string]>>({});
  function applyConfig(next: Config) {
    setConfig((previous) => {
      if (next.backend !== previous.backend) {
        backendModels.current[String(previous.backend)] = previous.model;
        return {
          ...next,
          model:
            backendModels.current[String(next.backend)] ??
            (next.backend === "claude" ? (schema?.defaults.model ?? "") : ""),
        };
      }
      return next;
    });
  }
  const update = (f: ConfigField, value: Config[string]) =>
    applyConfig({ ...config, [f.key]: value });
  async function save() {
    setSaving(true);
    try {
      await api("/config/validate", "POST", config);
      await onSave(config);
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }
  return (
    <Dialog title="Run configuration" onClose={onClose} wide>
      <div className="config-subtitle">
        {running
          ? "These settings apply to your next session. The active session keeps its configuration."
          : "Set the working style, boundaries, and budget for this assessment."}
      </div>
      {!schema ? (
        <Loading />
      ) : (
        <div className="config-layout">
          <nav className="config-nav" aria-label="Configuration sections">
            {Object.keys(GROUPS).map((g, i) => (
              <button
                key={g}
                className={group === g ? "selected" : ""}
                onClick={() => setGroup(g)}
              >
                <span>0{i + 1}</span>
                {g}
              </button>
            ))}
          </nav>
          <div className="config-fields">
            <div className="section-heading">
              <SlidersHorizontal size={17} />
              <h3>{group}</h3>
            </div>
            {group === "Execution" && (
              <ModeCards config={config} onChange={applyConfig} />
            )}
            {group === "Scope & context" && (
              <p className="notice">
                Scope is advisory. Scope hosts affect only web_fetch
                blocklisting; they are not a global allowlist. Paths refer to
                this engagement's local directories.
              </p>
            )}
            {group === "Approvals" && (
              <p className="notice amber">
                Safe + active may be automatic. Exploit needs acknowledgement.
                Destructive actions require a decision; host execution has its
                own policy.
              </p>
            )}
            {group === "Agents & budgets" && (
              <p className="notice">
                Child caps apply per Swarm peer. Zero total children means
                unlimited. Subagent deadlines are checked between turns; model
                timeout bounds each model call.
              </p>
            )}
            {group === "Model & sessions" && (
              <p className="notice">
                Choose a model for this Zero session and its agents. Active
                sessions keep their model; saved changes apply to the next
                session. Claude session-resume controls are backend specific.
              </p>
            )}
            {GROUPS[group].map((k) => {
              const f = schema.fields.find((f) => f.key === k)!;
              const value = config[k];
              return (
                <div className="config-field" key={k}>
                  <div className="field-label">
                    <label htmlFor={"config-" + k}>{f.label}</label>
                    <code>{f.flag}</code>
                  </div>
                  {k === "model" && config.backend === "codex" ? (
                    <ModelSelect
                      value={String(value ?? "")}
                      onChange={(value) => update(f, value)}
                    />
                  ) : f.type === "boolean" ? (
                    <label className="toggle-row">
                      <input
                        id={"config-" + k}
                        type="checkbox"
                        checked={!!value}
                        onChange={(e) => update(f, e.target.checked)}
                      />
                      <span>{value ? "Enabled" : "Disabled"}</span>
                    </label>
                  ) : f.type === "select" ? (
                    <select
                      id={"config-" + k}
                      value={String(value ?? "")}
                      onChange={(e) => update(f, e.target.value)}
                    >
                      {f.choices.map((v) => (
                        <option key={v} value={v}>
                          {v}
                        </option>
                      ))}
                    </select>
                  ) : f.type === "multi-select" ? (
                    <div className="risk-options">
                      {f.choices.map((v) => (
                        <label key={v}>
                          <input
                            type="checkbox"
                            checked={Array.isArray(value) && value.includes(v)}
                            onChange={(e) =>
                              update(
                                f,
                                e.target.checked
                                  ? [...(value as string[]), v]
                                  : (value as string[]).filter((x) => x !== v),
                              )
                            }
                          />
                          {v}
                        </label>
                      ))}
                    </div>
                  ) : (
                    <input
                      id={"config-" + k}
                      type={f.type === "number" ? "number" : "text"}
                      step={k === "container_cpus" ? "any" : 1}
                      value={
                        Array.isArray(value)
                          ? value.join(", ")
                          : String(value ?? "")
                      }
                      placeholder={
                        k === "model"
                          ? "Account default"
                          : f.default === null
                            ? "Automatic / unset"
                            : ""
                      }
                      onChange={(e) =>
                        update(
                          f,
                          f.type === "number"
                            ? e.target.value === ""
                              ? null
                              : Number(e.target.value)
                            : f.type === "list"
                              ? e.target.value
                                  .split(",")
                                  .map((x) => x.trim())
                                  .filter(Boolean)
                              : e.target.value,
                        )
                      }
                    />
                  )}
                  <p className="field-help">
                    {k === "model" && config.backend === "codex"
                      ? "Model discovery lists catalog options without starting a model turn. Codex checks model access when the run starts."
                      : k === "scope_host"
                        ? "Repeatable hosts, separated by commas. This preserves core web_fetch blocklist semantics."
                        : f.help}
                  </p>
                </div>
              );
            })}
            <div className="command-section">
              <div className="row spread">
                <h3>Equivalent CLI command</h3>
                <button
                  className="text-button"
                  onClick={async () => {
                    await navigator.clipboard.writeText(command);
                    setCopied(true);
                  }}
                >
                  {copied ? <Check size={14} /> : <Copy size={14} />}{" "}
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
              <pre>
                {command ||
                  "Resolve configuration errors to generate a command."}
              </pre>
            </div>
          </div>
        </div>
      )}
      <div className="dialog-footer">
        <ErrorBox error={error} />
        <button
          className="button subtle"
          onClick={() => {
            if (schema) {
              backendModels.current = {};
              setConfig(schema.defaults);
            }
          }}
        >
          Reset defaults
        </button>
        <div className="spacer" />
        <button className="button" onClick={onClose}>
          Cancel
        </button>
        <button
          className="button primary"
          disabled={saving || !!error}
          onClick={save}
        >
          {saving ? "Saving…" : "Save configuration"}
        </button>
      </div>
    </Dialog>
  );
}
