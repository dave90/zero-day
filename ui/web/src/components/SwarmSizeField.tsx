import { useEffect, useState } from "react";

export function SwarmSizeField({
  value,
  onChange,
}: {
  value: number;
  onChange: (size: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  return (
    <div className="initial-swarm-size">
      <label className="initial-setting-label" htmlFor="new-swarm-size">
        Swarm size
      </label>
      <input
        id="new-swarm-size"
        type="number"
        min={2}
        max={10_000_000}
        step={1}
        required
        value={draft}
        aria-describedby="new-swarm-size-help"
        onChange={(e) => {
          const raw = e.target.value;
          setDraft(raw);
          const size = Number(raw);
          // Preserve the selected style while an empty/invalid draft is edited.
          // Native form validation blocks creation until the size is valid.
          if (
            raw !== "" &&
            Number.isInteger(size) &&
            size >= 2 &&
            size <= 10_000_000
          ) {
            onChange(size);
          }
        }}
      />
      <p className="field-help" id="new-swarm-size-help">
        Number of collaborating top-level orchestrators. Each peer has its own
        agent budgets and subagent limits.
      </p>
    </div>
  );
}
