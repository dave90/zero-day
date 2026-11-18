import { useRef } from "react";
import type { Config, Schema } from "../api/types";
import { ErrorBox } from "./common";
import { ModelSelect } from "./ModelSelect";

export function approvalError(config: Config): string {
  return Array.isArray(config.auto_approve) &&
    config.auto_approve.includes("exploit") &&
    !config.i_understand
    ? 'Select "I understand" before auto-approving exploit actions.'
    : "";
}

export function InitialRunSettings({
  config,
  schema,
  onChange,
}: {
  config: Config;
  schema: Schema;
  onChange: (config: Config) => void;
}) {
  const backendModels = useRef<Record<string, Config[string]>>({});
  const update = (key: string, value: Config[string]) =>
    onChange({ ...config, [key]: value });
  const field = (key: string) => schema.fields.find((f) => f.key === key)!;
  const autoApprove = Array.isArray(config.auto_approve)
    ? config.auto_approve
    : [];

  function changeBackend(backend: string) {
    // Match Configure: keep model choices separate when switching CLI backends.
    backendModels.current[String(config.backend)] = config.model;
    onChange({
      ...config,
      backend,
      model:
        backendModels.current[backend] ??
        (backend === "claude" ? schema.defaults.model : ""),
    });
  }

  return (
    <div className="initial-run-settings">
      <fieldset className="initial-settings-section">
        <legend>Backend &amp; model</legend>
        <label className="initial-setting-label" htmlFor="new-backend">
          Backend
        </label>
        <select
          id="new-backend"
          value={String(config.backend)}
          onChange={(e) => changeBackend(e.target.value)}
        >
          {field("backend").choices.map((backend) => (
            <option key={backend} value={backend}>
              {backend === "codex" ? "Codex" : "Claude"}
            </option>
          ))}
        </select>
        <p className="field-help">
          Uses your authenticated CLI for the main agent, pentesters, and
          validators.
        </p>
        <label className="initial-setting-label" htmlFor="config-model">
          Model
        </label>
        {config.backend === "codex" ? (
          <ModelSelect
            value={String(config.model ?? "")}
            onChange={(model) => update("model", model)}
          />
        ) : (
          <>
            <input
              id="config-model"
              value={String(config.model ?? "")}
              placeholder="Claude account default"
              onChange={(e) => update("model", e.target.value)}
            />
            <p className="field-help">
              Enter a Claude model, or leave empty to follow its account
              default.
            </p>
          </>
        )}
      </fieldset>
      <fieldset className="initial-settings-section">
        <legend>Investigation budgets</legend>
        <div className="form-grid initial-budget-grid">
          {[
            "max_turns",
            "pentester_max_turns",
            "validator_max_turns",
            "max_subagents",
          ].map((key) => (
            <label key={key} htmlFor={"new-" + key}>
              {field(key).label}
              <input
                id={"new-" + key}
                type="number"
                min={1}
                max={10_000_000}
                step={1}
                required
                value={String(config[key] ?? "")}
                onChange={(e) =>
                  update(
                    key,
                    e.target.value === "" ? "" : Number(e.target.value),
                  )
                }
              />
            </label>
          ))}
        </div>
        <p className="field-help">
          Turn limits apply independently to each agent. Max subagents limits
          concurrent children per Swarm peer.
        </p>
      </fieldset>
      <fieldset className="initial-settings-section">
        <legend>Approvals</legend>
        <p className="field-help initial-approval-help">
          Actions that need approval prompt by default. Choose which classes may
          proceed automatically.
        </p>
        <fieldset className="initial-auto-approve">
          <legend>Auto approve</legend>
          <div className="risk-options">
            {field("auto_approve").choices.map((risk) => (
              <label key={risk}>
                <input
                  type="checkbox"
                  checked={autoApprove.includes(risk)}
                  onChange={(e) =>
                    update(
                      "auto_approve",
                      e.target.checked
                        ? [...autoApprove, risk]
                        : autoApprove.filter((value) => value !== risk),
                    )
                  }
                />
                {risk}
              </label>
            ))}
          </div>
        </fieldset>
        {[
          ["yolo", "Yolo", "Automatically approve safe and active actions."],
          [
            "i_understand",
            "I understand",
            "Required for automatic approval of exploit actions.",
          ],
          [
            "allow_host_exec",
            "Allow host exec",
            "Allow configured approvals to apply to code and binaries running on the host.",
          ],
        ].map(([key, label, help]) => (
          <div className="initial-approval-option" key={key}>
            <label className="toggle-row">
              <input
                type="checkbox"
                checked={!!config[key]}
                onChange={(e) => update(key, e.target.checked)}
                aria-describedby={"new-help-" + key}
              />
              {label}
            </label>
            <p className="field-help" id={"new-help-" + key}>
              {help}
            </p>
          </div>
        ))}
        <ErrorBox error={approvalError(config)} />
      </fieldset>
      <p className="field-help">
        Saved with this attack. Configure remains available for advanced
        settings.
      </p>
    </div>
  );
}
