/* Engine messages, as the screens show them. The engine writes for a
   terminal and often ends a sentence with the command to type, in
   backticks ("...: run `brindle account upgrade`") or bare after a colon
   ("...: uv tool install --upgrade brindle"). The app has buttons for those
   actions, so the typed command is dropped and the sentence kept. */

/* Programs the engine tells the person to run; a trailing ": <program> ..." is a command. */
const CLIS = ["brindle", "uv", "npm", "brew", "claude", "codex", "agy", "opencode", "pip", "apt", "sudo"];

export function plain(text: string): string {
  let out = text
    // "run `x`", "(or run `x`)", ": `x`", "with `x`" and a bare `x`.
    .replace(/\s*\((?:or\s+)?(?:run|with|see|start)\s+`[^`]*`[^)]*\)/g, "")
    .replace(/[:;,]?\s*(?:or\s+)?(?:run|with|see|start)\s+`[^`]*`/g, "")
    .replace(/[:;,]?\s*`[^`]*`/g, "")
    // A command after a colon at the end: ": uv tool install --upgrade brindle".
    .replace(new RegExp(`:\\s*(?:${CLIS.join("|")})\\b[^.;]*$`), "")
    .replace(/\s+([.,;:])/g, "$1")
    .replace(/\s{2,}/g, " ")
    .replace(/[:;,\s]+$/, "")
    .trim();
  if (out && !/[.!?]$/.test(out)) out += ".";
  return out;
}
