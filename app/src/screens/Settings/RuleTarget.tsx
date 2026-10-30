/* What a rule covers: its action (a request kind, or a git push), how it
   matches (exactly, a prefix, a pattern, or one of brindle's named matchers)
   and its target. Shared by the rule editor (Settings › Permissions) and the
   organization's rules (Settings › Organization approvals). */

import { PLAIN_MATCHES, kindLabel } from "../../api/permissions";
import { field } from "../../components/ui";

/* The rule editor's actions: a request kind, or a git push (a bash rule with the push matcher). */
export const ACTIONS = ["read", "write", "edit", "bash", "push", "fetch", "mcp", "other"];
const MATCH_LABEL: Record<string, string> = { exact: "Is exactly", prefix: "Starts with", glob: "Matches pattern" };
/* brindle.permissions.BUILTIN_MATCHERS other than push: the kinds each applies to, and the words its target is. */
export const NAMED_MATCHERS: Record<string, { kinds: string[]; match: string }> = {
  tracked: { kinds: ["read"], match: "a file git tracks in the worktree or repo root" },
  worktree: { kinds: ["read", "write", "edit"], match: "a file in the worktree" },
  check: { kinds: ["bash"], match: "exactly one of the repo's `checks` commands" },
  "git-readonly": { kinds: ["bash"], match: "git status / diff / log / show" },
  "git-push": { kinds: ["bash"], match: "any git push" },
  "git-force": { kinds: ["bash"], match: "a git command with a force flag, or a push that forces or deletes a branch" },
  "readonly-pipeline": { kinds: ["bash"], match: "a pipeline of read-only commands (grep, ls, cat, head, sed -n ...)" },
  "rm-destructive": { kinds: ["bash"], match: "a recursive rm of the root, a top-level folder or the home directory" },
};

export interface Target {
  action: string; // a request kind, or "push"
  matchType: string;
  match: string;
}

/* "<kind> <match_type> <match>" (an org control's rule, a profile's permission_denies) as a Target, and back. */
export function parseRule(rule: string): Target {
  const [, kind = "bash", matchType = "prefix", match = ""] = /^(\S+)\s+(\S+)\s+([\s\S]*)$/.exec(rule) ?? [];
  return { action: matchType === "push" ? "push" : kind, matchType, match };
}
export const ruleText = (t: Target) => `${t.action === "push" ? "bash" : t.action} ${t.matchType} ${t.match}`;

const isNamed = (matchType: string) => !PLAIN_MATCHES.includes(matchType) && matchType !== "push";

/* `offerNamed`: the named matchers are choices (else only an existing rule's is shown, read-only). */
export function RuleTarget({ value, onChange, offerNamed = false, labelClass }: { value: Target; onChange(next: Target): void; offerNamed?: boolean; labelClass: string }) {
  const { action, matchType, match } = value;
  const named = isNamed(matchType);
  const choices = Object.keys(NAMED_MATCHERS).filter((m) => (offerNamed && NAMED_MATCHERS[m].kinds.includes(action)) || m === matchType);
  const placeholder = action === "push" ? "* → @default" : action === "bash" ? "npm test" : action === "fetch" ? "https://docs.example.com/*" : action === "mcp" ? "mcp__github__create_issue" : "docs/**";
  const pickAction = (a: string) => {
    if (a === "push") onChange({ action: a, matchType: "push", match: matchType === "push" ? match : "" });
    else if (matchType === "push" || (named && !NAMED_MATCHERS[matchType]?.kinds.includes(a))) onChange({ action: a, matchType: "exact", match: "" });
    else onChange({ ...value, action: a });
  };
  const pickMatch = (m: string) => onChange({ action, matchType: m, match: isNamed(m) ? NAMED_MATCHERS[m].match : named ? "" : match });
  return (
    <>
      <label className={labelClass}>
        Action
        <select className={field} aria-label="Action" value={action} onChange={(e) => pickAction(e.target.value)}>
          {ACTIONS.map((a) => (
            <option key={a} value={a}>
              {kindLabel(a)}
            </option>
          ))}
        </select>
      </label>
      {action !== "push" && (
        <label className={labelClass}>
          Match
          <select className={field} aria-label="Match" value={matchType} onChange={(e) => pickMatch(e.target.value)}>
            {PLAIN_MATCHES.map((m) => (
              <option key={m} value={m}>
                {MATCH_LABEL[m]}
              </option>
            ))}
            {choices.map((m) => (
              <option key={m} value={m}>
                Built-in: {m}
              </option>
            ))}
          </select>
        </label>
      )}
      <label className={`${labelClass} sm:col-span-2`}>
        Target
        <input className={`${field} font-mono`} aria-label="Target" value={match} readOnly={named} placeholder={placeholder} onChange={(e) => onChange({ ...value, match: e.target.value })} />
        <span className="font-normal">
          {named
            ? "A built-in matcher: brindle decides what it covers. Pick another match to write your own target."
            : action === "push"
              ? "source → destination; * for any branch, @default for the repo's default branch."
              : matchType === "glob"
                ? "* matches anything; docs/** matches files in docs and its folders."
                : ""}
        </span>
      </label>
    </>
  );
}
