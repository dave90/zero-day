type Task = {
  id?: string;
  title?: string;
  description?: string;
  acceptanceCriteria?: string;
  status?: string;
  priority?: number;
  passes?: number;
  note?: string;
  epic_id?: string;
};
export function PlanOutline({ plan }: { plan: Record<string, unknown> }) {
  const tasks = (Array.isArray(plan.tasks) ? plan.tasks : []) as Task[],
    epics = (Array.isArray(plan.epics) ? plan.epics : []) as Task[];
  return (
    <div className="plan-outline">
      {typeof plan.goal === "string" && (
        <p className="plan-goal">{plan.goal}</p>
      )}
      {epics.length > 0 && (
        <div className="plan-epics">
          {epics.map((e) => (
            <div className="plan-epic" key={e.id}>
              <small>
                {e.id} · priority {e.priority}
              </small>
              <h3>{e.title}</h3>
              <p>{e.description}</p>
              {e.acceptanceCriteria && (
                <p className="field-help">Coverage: {e.acceptanceCriteria}</p>
              )}
            </div>
          ))}
        </div>
      )}
      {tasks.map((t) => (
        <article className="plan-task" key={t.id}>
          <div className="row spread">
            <code>
              {t.id} · P{t.priority ?? "—"}
            </code>
            <span
              className={"badge " + (t.status === "completed" ? "olive" : "")}
            >
              {t.status || "pending"}
            </span>
          </div>
          <h3>{t.title}</h3>
          {t.description && <p>{t.description}</p>}
          <div className="task-criteria">
            <small>ACCEPTANCE CRITERIA</small>
            <p>{t.acceptanceCriteria || "No criteria recorded"}</p>
          </div>
          <div className="row spread field-help">
            <span>{t.epic_id || "Independent task"}</span>
            <span>{t.passes || 0} passes</span>
          </div>
          {t.note && <div className="notice">Reviewer: {t.note}</div>}
        </article>
      ))}
      <p className="field-help">
        Completion is the independent reviewer’s judgement. A completed coverage
        task does not imply a confirmed vulnerability.
      </p>
    </div>
  );
}
