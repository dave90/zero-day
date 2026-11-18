import { describe, expect, it } from "vitest";
import { findingSeverity } from "../src/api/severity";
import type { Finding } from "../src/api/types";

const vector =
  "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N";
function finding(score: unknown, hint = "high", cvssVector = vector): Finding {
  return {
    finding: { severity_hint: hint },
    validation: { cvss40_score: score, cvss40_vector: cvssVector },
    verdict: "confirmed",
  } as Finding;
}

describe("finding severity", () => {
  it.each([
    [0, "none"],
    [0.1, "low"],
    [3.9, "low"],
    [4, "medium"],
    [6.9, "medium"],
    [7, "high"],
    [8.9, "high"],
    [9, "critical"],
    [10, "critical"],
  ])("maps CVSS %s to %s", (score, band) => {
    expect(findingSeverity(finding(score))).toMatchObject({
      score,
      band,
      estimated: false,
    });
  });
  it("uses a recognized estimate when the validator has no vector, and keeps CVSS unscored", () => {
    expect(findingSeverity(finding(0, " HIGH ", ""))).toMatchObject({
      score: null,
      band: "high",
      estimated: true,
    });
    expect(findingSeverity(finding(5.3, "low", "  "))).toMatchObject({
      score: null,
      band: "low",
      estimated: true,
    });
  });
  it.each([undefined, null, -1, 11, NaN, Infinity, "9.8"])(
    "does not manufacture a score from invalid/missing value %s",
    (score) => {
      expect(findingSeverity(finding(score, ""))).toMatchObject({
        score: null,
        band: "unrated",
        estimated: false,
      });
    },
  );
  it("keeps severity independent of a rejected verdict and overrides the agent hint with validator CVSS", () => {
    expect(
      findingSeverity({ ...finding(9.1, "low"), verdict: "rejected" }),
    ).toMatchObject({ score: 9.1, band: "critical", estimated: false });
  });
  it("handles pending hints and unrecognized freeform hints without inventing a CVSS rating", () => {
    expect(
      findingSeverity({
        ...finding(null, "Medium"),
        validation: null,
        verdict: "pending",
      }),
    ).toMatchObject({ band: "medium", estimated: true, score: null });
    expect(findingSeverity(finding(null, "P1-ish"))).toMatchObject({
      band: "unrated",
      estimated: false,
      score: null,
    });
  });
  it("does not label a different CVSS version as a scored CVSS 4.0 finding", () => {
    expect(
      findingSeverity(finding(9.8, "high", "CVSS:3.1/AV:N/AC:L")),
    ).toMatchObject({ score: null, band: "high", estimated: true });
  });
});
