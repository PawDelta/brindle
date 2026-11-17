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

/* A new branch: a line forking off another. */
export const BranchIcon = (p: Props) => (
  <Icon {...p}>
    <circle cx="6" cy="5.25" r="2.5" />
    <circle cx="6" cy="18.75" r="2.5" />
    <circle cx="18" cy="8.25" r="2.5" />
    <path d="M6 7.8v8.4" />
    <path d="M18 10.8c0 3.9-5.4 3.6-9.6 5.4" />
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

/* A folder on this Mac. */
export const FolderIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M3 6.5h6.5l2 2H21V19H3z" />
  </Icon>
);

/* A link: a repository's URL. */
export const LinkIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M10 14l4-4" />
    <path d="M11.5 6.5l1.6-1.6a3.9 3.9 0 0 1 5.5 5.5L17 12" />
    <path d="M12.5 17.5l-1.6 1.6a3.9 3.9 0 0 1-5.5-5.5L7 12" />
  </Icon>
);

/* Play: watch something run. */
export const PlayIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M7.5 5v14l10.5-7z" />
  </Icon>
);

/* A tick: the picked item in a menu. */
export const CheckIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M4.5 12.5l4.5 4.5 10.5-10.5" />
  </Icon>
);

/* Add more. */
export const PlusIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M12 4.5v15M4.5 12h15" />
  </Icon>
);

/* Options: two sliders. */
export const SlidersIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M3 7h12M19 7h2M3 17h3M10 17h11" />
    <circle cx="17" cy="7" r="2" />
    <circle cx="8" cy="17" r="2" />
  </Icon>
);

/* The Inbox: a tray. */
export const InboxIcon = (p: Props) => (
  <Icon {...p}>
    <path d="M3 13.5h4.5l1.5 3h6l1.5-3H21" />
    <path d="M5.2 5.2h13.6L21 13.5V19H3v-5.5z" />
  </Icon>
);

/* Only you see it: a padlock. */
export const LockIcon = (p: Props) => (
  <Icon {...p}>
    <rect x="5.5" y="10.5" width="13" height="9.5" rx="1.5" />
    <path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3" />
  </Icon>
);

/* Search: a magnifier. */
export const SearchIcon = (p: Props) => (
  <Icon {...p}>
    <circle cx="10.5" cy="10.5" r="6.5" />
    <path d="M15.5 15.5 21 21" />
  </Icon>
);

/* Settings: a gear. */
export const GearIcon = (p: Props) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="3.2" />
    <path d="M12 2.7v2.4M12 18.9v2.4M2.7 12h2.4M18.9 12h2.4M5.4 5.4l1.7 1.7M16.9 16.9l1.7 1.7M5.4 18.6l1.7-1.7M16.9 7.1l1.7-1.7" />
  </Icon>
);
