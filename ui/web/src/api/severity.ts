import type { Finding } from "./types";

export const SEVERITIES = [
  ["critical", "Critical"],
  ["high", "High"],
  ["medium", "Medium"],
  ["low", "Low"],
  ["none", "None"],
  ["unrated", "Unrated"],
] as const;
export type Severity = (typeof SEVERITIES)[number][0];

export function findingSeverity(finding: Finding) {
  const validation = finding.validation;
  const score = validation?.cvss40_score;
  const vector = validation?.cvss40_vector?.trim();
  const scored =
    typeof score === "number" &&
    Number.isFinite(score) &&
    score >= 0 &&
    score <= 10 &&
    !!vector?.startsWith("CVSS:4.0/");
  // FIRST CVSS v4.0 qualitative bands. A missing vector/score is not a real zero:
  // https://www.first.org/cvss/v4.0/specification-document#Qualitative-Severity-Rating-Scale
  const hint = finding.finding.severity_hint?.trim().toLowerCase();
  const normalizedHint =
    hint === "informational" || hint === "info" ? "none" : hint;
  const band: Severity = scored
    ? score === 0
      ? "none"
      : score < 4
        ? "low"
        : score < 7
          ? "medium"
          : score < 9
            ? "high"
            : "critical"
    : SEVERITIES.some(([key]) => key !== "unrated" && key === normalizedHint)
      ? (normalizedHint as Severity)
      : "unrated";
  const label = SEVERITIES.find(([key]) => key === band)![1];
  const estimated = !scored && band !== "unrated";
  return {
    band,
    label,
    estimated,
    score: scored ? score : null,
    description: scored
      ? `Severity from the validator’s CVSS 4.0 score (${score.toFixed(1)}).`
      : estimated
        ? "Agent estimate. No validator CVSS 4.0 score with a vector is available."
        : "No recognized severity estimate or validator CVSS 4.0 rating is available.",
  };
}
