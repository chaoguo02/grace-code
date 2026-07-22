/**
 * Pure utility — extract a human-readable target from tool-call parameters.
 */
const PRIMARY_PARAM: Record<string, string> = {
  Read: "file_path", Write: "file_path", Edit: "file_path",
  Bash: "command", Grep: "pattern", Glob: "pattern",
  WebFetch: "url", WebSearch: "query", Skill: "skill",
};

export function summarizeTarget(name: string, params: Record<string, unknown>): string {
  const key = PRIMARY_PARAM[name];
  if (key && typeof params[key] === "string") {
    return (params[key] as string).slice(0, 60);
  }
  return name;
}
