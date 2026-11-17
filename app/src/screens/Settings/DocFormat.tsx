/* Settings › Doc format: the shapes a planning doc may take, how docs are
   named, and what an empty required section stops (GET/PUT /doc-format,
   brindle.doc_templates). A template is an ordered list of sections, each
   with a kind, whether it is required, and the line the supervisor is told
   about what belongs there; one template is the default a new doc starts
   from. Docs already written keep the template they were written with.
   Edits are a draft until Save, which replaces what the org level sets.
   An engine without the route shows nothing. */

import { useEffect, useRef, useState } from "react";
import type { DaemonClient, DocFormat as Format, DocFormatBlock, DocFormatView, DocSectionKind, DocTemplate, DocTemplateSection } from "../../api/client";
import { plain } from "../../api/wording";
import { alert, card, field, h2, label, main, notice, quiet } from "../../components/ui";

export type DocFormatClient = Pick<DaemonClient, "docFormat" | "setDocFormat">;

const message = (e: unknown) => plain(e instanceof Error ? e.message : String(e));

const KINDS: { kind: DocSectionKind; name: string }[] = [
  { kind: "text", name: "Text" },
  { kind: "diagram", name: "Diagram" },
  { kind: "table", name: "Table" },
];

const BLOCKS: { block: DocFormatBlock; name: string; help: string }[] = [
  { block: "sign_off", name: "Sign-off", help: "Approve, a reviewer's sign-off and the author's Freeze wait until it is filled." },
  { block: "share", name: "Sharing", help: "The doc can't be shared to the Library until it is filled." },
];

/* The first template someone makes: the sections most designs need. */
const STARTER: DocTemplateSection[] = [
  { title: "Summary", kind: "text", required: true, hint: "Two short paragraphs: what changes for the user, and the one constraint that shapes it." },
  { title: "Goals and non-goals", kind: "text", required: true, hint: "Bulleted. At least one non-goal." },
  { title: "Architecture", kind: "diagram", required: false, hint: "One figure with a caption. Skip it for changes inside one module." },
  { title: "Rollout", kind: "text", required: true, hint: "Numbered steps, and how to turn it off." },
  { title: "Open questions", kind: "text", required: true, hint: 'One per line, or "None".' },
];

const newId = (taken: DocTemplate[]) => {
  let n = taken.length + 1;
  while (taken.some((t) => t.id === `t${n}`)) n += 1;
  return `t${n}`;
};

export function DocFormat({ client }: { client: DocFormatClient }) {
  const [view, setView] = useState<DocFormatView | null>(null);
  const [draft, setDraft] = useState<Format | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /* The draft as last rendered: a save compares it with what it sent. */
  const latest = useRef<Format | null>(null);
  latest.current = draft;

  const load = (v: DocFormatView) => {
    setView(v);
    setDraft(v.resolved);
    setOpen((was) => (v.resolved.templates.some((t) => t.id === was) ? was : (v.resolved.templates.find((t) => t.default) ?? v.resolved.templates[0])?.id ?? null));
  };

  useEffect(() => {
    let live = true;
    client.docFormat().then(
      (v) => live && load(v),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [client]);

  if (!view || !draft) return null;

  const change = (next: Format) => {
    setDraft(next);
    setSaved(false);
  };
  const template = draft.templates.find((t) => t.id === open) ?? null;
  const setTemplate = (edit: (t: DocTemplate) => DocTemplate) =>
    change({ ...draft, templates: draft.templates.map((t) => (t.id === open ? edit(t) : t)) });
  const setSection = (i: number, edit: Partial<DocTemplateSection>) =>
    setTemplate((t) => ({ ...t, sections: t.sections.map((s, j) => (j === i ? { ...s, ...edit } : s)) }));
  const move = (i: number, by: number) =>
    setTemplate((t) => {
      const sections = [...t.sections];
      [sections[i], sections[i + by]] = [sections[i + by], sections[i]];
      return { ...t, sections };
    });

  const add = () => {
    const first = draft.templates.length === 0;
    const made: DocTemplate = {
      id: newId(draft.templates),
      name: first ? "Design doc" : "New template",
      default: first,
      sections: first ? STARTER : [{ title: "Summary", kind: "text", required: true, hint: null }],
    };
    change({ ...draft, templates: [...draft.templates, made] });
    setOpen(made.id);
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    const sent = draft;
    try {
      const naming = { title_hint: sent.naming.title_hint?.trim() || null, reference: sent.naming.reference?.trim() || null };
      const templates = sent.templates.map((t) => ({ ...t, sections: t.sections.map((s) => ({ ...s, hint: s.hint?.trim() || null })) }));
      const got = await client.setDocFormat({ templates, naming, blocks: sent.blocks }, view.version);
      // What was typed while it saved stays as the draft; only an untouched draft takes the saved format.
      if (latest.current === sent) load(got);
      else setView(got);
      setSaved(true);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  };

  const dirty = JSON.stringify(draft) !== JSON.stringify(view.resolved);

  return (
    <section id="doc-format" aria-labelledby="doc-format-title" className="flex scroll-mt-s2 flex-col gap-s2" data-testid="doc-format">
      <header className="flex flex-wrap items-baseline gap-x-s2">
        <h2 id="doc-format-title" className={h2}>
          Doc format
        </h2>
        <span className="text-xs text-ink-3">How designs are structured and named. Supervisors follow the template when they write a design; people can add sections but not remove required ones.</span>
      </header>

      <div className="grid gap-s3 md:grid-cols-[minmax(0,14rem)_minmax(0,1fr)]">
        <div className="flex flex-col gap-s2">
          <div className="flex flex-col gap-1">
            <div className="flex items-center">
              <span className={label}>Templates</span>
              <span className="flex-1" />
              <button type="button" className={`${quiet} text-xs`} onClick={add}>
                + New
              </button>
            </div>
            {draft.templates.length === 0 && <p className="text-xs text-ink-3">None yet: supervisors choose the sections themselves.</p>}
            <div role="listbox" aria-label="Templates" className="flex flex-col gap-px">
              {draft.templates.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  role="option"
                  aria-selected={t.id === open}
                  className={`flex flex-col rounded px-2 py-1 text-left text-sm hover:bg-surface-2 ${t.id === open ? "bg-surface-2 font-medium" : ""}`}
                  onClick={() => setOpen(t.id)}
                >
                  {t.name}
                  <span className="text-[11px] font-normal text-ink-3">
                    {t.default ? "default · " : ""}
                    {view.used[t.id] ?? 0} {view.used[t.id] === 1 ? "doc" : "docs"}
                  </span>
                </button>
              ))}
            </div>
          </div>

          <div className="flex flex-col gap-1">
            <span className={label}>Naming</span>
            <label className="flex flex-col gap-0.5 text-xs">
              Title hint
              <input className={field} value={draft.naming.title_hint ?? ""} placeholder="What it does, in plain words" onChange={(e) => change({ ...draft, naming: { ...draft.naming, title_hint: e.target.value } })} />
            </label>
            <label className="flex flex-col gap-0.5 text-xs">
              Reference
              <input className={`${field} font-mono`} value={draft.naming.reference ?? ""} placeholder="DES-{number}" onChange={(e) => change({ ...draft, naming: { ...draft.naming, reference: e.target.value } })} />
            </label>
            <p className="text-[11px] text-ink-3" data-testid="next-reference">
              {view.next_reference
                ? `Next: ${view.next_reference}. Shown beside the title.`
                : view.source === "org" && view.resolved.naming.reference
                  ? "Your organization numbers them in order. Shown beside the title."
                  : "Use {number} where the doc's number goes."}
            </p>
          </div>
        </div>

        <div className="flex min-w-0 flex-col gap-s2">
          {template && (
            <div className={`${card} flex flex-col gap-1 pt-s1`}>
              <div className="flex flex-wrap items-center gap-s1">
                <input aria-label="Template name" className={`${field} max-w-[16rem] font-medium`} value={template.name} onChange={(e) => setTemplate((t) => ({ ...t, name: e.target.value }))} />
                <label className="flex items-center gap-1 text-xs">
                  <input
                    type="checkbox"
                    checked={template.default}
                    onChange={(e) => change({ ...draft, templates: draft.templates.map((t) => ({ ...t, default: t.id === open ? e.target.checked : e.target.checked ? false : t.default })) })}
                  />
                  New docs start from it
                </label>
                <span className="flex-1" />
                <button
                  type="button"
                  className={`${quiet} text-xs`}
                  onClick={() => {
                    const rest = draft.templates.filter((t) => t.id !== open);
                    change({ ...draft, templates: rest });
                    setOpen(rest[0]?.id ?? null);
                  }}
                >
                  Remove template
                </button>
              </div>
              <div className="grid grid-cols-[auto_minmax(0,10rem)_auto_auto_minmax(0,1fr)_auto] items-center gap-x-s1 gap-y-1 text-xs">
                <span />
                <span className={label}>Section</span>
                <span className={label}>Kind</span>
                <span className={label}>Required</span>
                <span className={label}>What the supervisor is told</span>
                <span />
                {template.sections.map((s, i) => (
                  <div key={i} role="group" aria-label={`Section ${i + 1}`} className="contents">
                    <span className="flex flex-col">
                      <button type="button" aria-label={`Move ${s.title || "section"} up`} disabled={i === 0} className="px-1 text-ink-3 disabled:opacity-30" onClick={() => move(i, -1)}>
                        ↑
                      </button>
                      <button type="button" aria-label={`Move ${s.title || "section"} down`} disabled={i === template.sections.length - 1} className="px-1 text-ink-3 disabled:opacity-30" onClick={() => move(i, 1)}>
                        ↓
                      </button>
                    </span>
                    <input aria-label="Section title" className={field} value={s.title} onChange={(e) => setSection(i, { title: e.target.value })} />
                    <select aria-label={`Kind of ${s.title || "section"}`} className={field} value={s.kind} onChange={(e) => setSection(i, { kind: e.target.value as DocSectionKind })}>
                      {KINDS.map((k) => (
                        <option key={k.kind} value={k.kind}>
                          {k.name}
                        </option>
                      ))}
                    </select>
                    <input type="checkbox" aria-label={`${s.title || "Section"} is required`} checked={s.required} onChange={(e) => setSection(i, { required: e.target.checked })} />
                    <input aria-label={`Hint for ${s.title || "section"}`} className={field} value={s.hint ?? ""} onChange={(e) => setSection(i, { hint: e.target.value })} />
                    <button
                      type="button"
                      aria-label={`Remove ${s.title || "section"}`}
                      disabled={template.sections.length === 1}
                      className="px-1 text-ink-3 disabled:opacity-30"
                      onClick={() => setTemplate((t) => ({ ...t, sections: t.sections.filter((_, j) => j !== i) }))}
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
              <button
                type="button"
                className={`${quiet} self-start text-xs`}
                onClick={() => setTemplate((t) => ({ ...t, sections: [...t.sections, { title: "", kind: "text", required: false, hint: null }] }))}
              >
                + Add a section
              </button>
              <p className="text-[11px] text-ink-3">Decisions always come last, each with its options and a recommendation. Docs already written keep the template they were written with.</p>
            </div>
          )}

          <div className={`${card} flex flex-col gap-1 pt-s1`}>
            <span className={label}>When a required section is empty</span>
            <p className="text-xs text-ink-3">Writing and review go on: the section shows as an empty heading with its hint.</p>
            {BLOCKS.map((b) => (
              <label key={b.block} className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={draft.blocks.includes(b.block)}
                  onChange={(e) => change({ ...draft, blocks: e.target.checked ? [...draft.blocks, b.block] : draft.blocks.filter((x) => x !== b.block) })}
                />
                <span>
                  {b.name} is blocked
                  <span className="block text-xs text-ink-3">{b.help}</span>
                </span>
              </label>
            ))}
          </div>

          <div className="flex items-center gap-s1">
            <span className="flex-1" />
            <button type="button" className={quiet} disabled={!dirty || busy} onClick={() => change(view.resolved)}>
              Discard
            </button>
            <button type="button" className={main} disabled={!dirty || busy || !view.editable} onClick={() => void save()}>
              Save
            </button>
          </div>
          {view.source === "org" && (
            <p className="text-xs text-ink-3" data-testid="doc-format-org">
              Your organization keeps this format in the Library for every member; its admins change it.
            </p>
          )}
          {saved && !dirty && (
            <p role="status" className={notice}>
              Saved.
            </p>
          )}
          {error && (
            <p role="alert" className={alert}>
              {error}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
