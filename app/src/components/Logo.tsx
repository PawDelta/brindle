import MARK from "../assets/brindle-mark.svg?raw";

/* The brindle lockup: the braid mark beside the lowercase "brindle" wordmark,
   as on the pawdelta.com site. The mark (assets/brindle-mark.svg, a copy of
   the site's) is inlined so styles.css can recolour its threads per theme
   (.logo .lm-*). */
export const LOCKUP = `${MARK.trim()}<span class="logo-word">brindle</span>`;

export default function Logo({ className = "" }: { className?: string }) {
  return <span className={`logo ${className}`.trim()} role="img" aria-label="brindle" dangerouslySetInnerHTML={{ __html: LOCKUP }} />;
}
