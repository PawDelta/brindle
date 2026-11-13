import MARK from "../assets/brindle-mark.svg?raw";

/* The brindle lockup: the braid mark beside the lowercase "brindle" wordmark,
   as on the pawdelta.com site. The mark (assets/brindle-mark.svg, a copy of
   the site's) is inlined so styles.css can recolour its threads per theme
   (.logo .lm-*). */
export const LOCKUP = `${MARK.trim()}<span class="logo-word">brindle</span>`;

/* With onHome the lockup is a button that goes Home (the sidebar's logo, and the
   logo over a full-window visual where the sidebar is hidden). */
/* `mark`: the braid alone, no wordmark (the first run's slim rail). */
export default function Logo({ className = "", onHome, mark = false }: { className?: string; onHome?: () => void; mark?: boolean }) {
  const html = mark ? MARK.trim() : LOCKUP;
  if (onHome) {
    return <button type="button" className={`logo rounded-md outline-none focus-visible:ring-2 focus-visible:ring-branch ${className}`.trim()} aria-label="Home" onClick={onHome} dangerouslySetInnerHTML={{ __html: html }} />;
  }
  return <span className={`logo ${className}`.trim()} role="img" aria-label="brindle" dangerouslySetInnerHTML={{ __html: html }} />;
}
