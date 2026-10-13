import { STAGES, type SessionView } from "../types";

const LABEL: Record<string, string> = { intake: "Intake", plan: "Plan", execute: "Execute", review: "Review", finalize: "Finalize" };

/** The run, drawn as it actually executes: five stages, with the execute stage showing its iteration budget. */
export function RunTrack({ view }: { view: SessionView }) {
  const at = STAGES.indexOf(view.stage as (typeof STAGES)[number]);
  const finished = view.status === "idle" && view.review !== null;
  const stopped = view.status === "interrupted" || view.status === "error";
  const paused = view.status === "awaiting_approval";

  const stateOf = (i: number) => {
    if (finished || i < at) return "done";
    if (i === at) return stopped ? "stopped" : paused ? "paused" : "active";
    return "todo";
  };

  const caption = (() => {
    if (finished)
      return view.review!.limit_reached
        ? `Stopped at the limit of ${view.maxIterations} iterations`
        : `Finished in ${view.review!.iterations} of ${view.maxIterations} iterations`;
    if (at < 0) return `Up to ${view.maxIterations} iterations per request`;
    if (stopped) return `Stopped during ${LABEL[view.stage].toLowerCase()}. Progress is saved`;
    if (paused) return "Waiting for your approval";
    if (view.stage === "execute") return `Iteration ${view.iteration} of ${view.maxIterations}`;
    return LABEL[view.stage];
  })();

  return (
    <div className="track" role="group" aria-label={`Run progress: ${caption}`}>
      <ol className="track-stages">
        {STAGES.map((stage, i) => (
          <li key={stage} className={`stage stage-${stateOf(i)} ${stage === "execute" ? "stage-execute" : ""}`}>
            <span className="stage-name">{LABEL[stage]}</span>
            {stage === "execute" ? (
              <span className="ticks" aria-hidden="true">
                {Array.from({ length: view.maxIterations }, (_, n) => {
                  const used = n < view.iteration;
                  const current = used && n === view.iteration - 1 && stateOf(i) !== "done" && at === i;
                  return <i key={n} className={current ? "tick tick-current" : used ? "tick tick-used" : "tick"} />;
                })}
              </span>
            ) : (
              <span className="bar" aria-hidden="true" />
            )}
          </li>
        ))}
      </ol>
      <p className="track-caption">{caption}</p>
    </div>
  );
}
