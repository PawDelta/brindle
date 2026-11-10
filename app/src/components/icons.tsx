/* Line icons for buttons and menus: 16px, a 1.5 stroke in currentColor, so a
   button's tone (and its hover) colours them. Decorative: the label beside
   one names the action. */

import type { ReactNode } from "react";

function Icon({ size = 16, className, children }: { size?: number; className?: string; children: ReactNode }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" className={className}>
      {children}
    </svg>
  );
}

type Props = { size?: number; className?: string };

export const ArrowLeftIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M19 12H5" />
    <path d="m11 6-6 6 6 6" />
  </Icon>
);

export const ChevronDownIcon = (p: Props) => (
  <Icon {...p}>
    <path d="m6 9 6 6 6-6" />
  </Icon>
);

export const ChevronRightIcon = (p: Props) => (
  <Icon {...p}>
    <path d="m9 6 6 6-6 6" />
  </Icon>
);

/* Export: out of the box. */
export const ShareIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M12 15V3" />
    <path d="m7 8 5-5 5 5" />
    <path d="M20 14v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-5" />
  </Icon>
);

export const DownloadIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M12 3v12" />
    <path d="m7 10 5 5 5-5" />
    <path d="M20 17v2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-2" />
  </Icon>
);

/* Google Drive: the three-sided mark, drawn as lines. */
export const DriveIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M8.6 3.5h6.8l6.6 11.5-3.4 5.5H5.4L2 15z" />
    <path d="M8.6 3.5 15.2 15H22" />
    <path d="M5.4 20.5 12 9" />
  </Icon>
);

/* SharePoint: a site of linked circles. */
export const SitesIcon = (p: Props) => (
  <Icon {...p}>
    <circle cx="10" cy="8" r="5" />
    <circle cx="16.5" cy="15.5" r="4" />
    <circle cx="8" cy="18" r="2.5" />
  </Icon>
);

export const FileIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
    <path d="M14 3v5h5" />
  </Icon>
);

/* The Library: a page with two lines of text, and a second page behind it. */
export const LibraryIcon = (p: Props) => (
  <Icon {...p}>
    <rect x="4" y="7" width="12" height="14" rx="2" />
    <path d="M8 3.5h10a2 2 0 0 1 2 2V17" />
    <path d="M7.5 12h5" />
    <path d="M7.5 16h5" />
  </Icon>
);

export const ArchiveIcon = (p: Props) => (
  <Icon {...p}>
    <rect x="3" y="4" width="18" height="5" rx="1" />
    <path d="M5 9v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9" />
    <path d="M10 13h4" />
  </Icon>
);

/* Attach a file. */
export const PaperclipIcon = (p: Props) => (
  <Icon {...p}>
    <path d="m20 11-8.5 8.5a5 5 0 0 1-7-7L13 4a3.3 3.3 0 0 1 4.7 4.7l-8.4 8.4a1.7 1.7 0 0 1-2.4-2.4L14.5 7" />
  </Icon>
);

/* A comment: a speech bubble. */
export const CommentIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M4 5h16v11H9l-5 4z" />
  </Icon>
);

/* Keep: a bookmark, filled once kept. */
export const BookmarkIcon = ({ filled, ...p }: Props & { filled?: boolean }) => (
  <Icon {...p}>
    <path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4.5L5 21V4a1 1 0 0 1 1-1z" fill={filled ? "currentColor" : "none"} />
  </Icon>
);
