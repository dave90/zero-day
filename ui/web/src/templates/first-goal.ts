import template from "./first-goal.txt?raw";

export function defaultFirstGoal(target = ""): string {
  return template
    .trim()
    .replace(
      /\{\{target_url\}\}/g,
      () => target.trim() || "the target application",
    );
}
