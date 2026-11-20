# brindle:// links

The desktop app registers the `brindle://` URL scheme on macOS, Windows and
Linux, so a link outside the app can open something in it. Today there is one
kind: a shared plan. Its web page on the plans host
(`https://plans.pawdelta.com/...`) offers **Open in brindle**, which follows
the matching `brindle://plan/...` link.

## Plan links

| Plan | Web link | App link |
| --- | --- | --- |
| A Pro account's own | `https://plans.pawdelta.com/b/{token}` | `brindle://plan/b/{token}` |
| One an org holds | `https://plans.pawdelta.com/{org}/{plan}/{token}` | `brindle://plan/{org}/{plan}/{token}` |
| One whose link is kept to a team | `https://plans.pawdelta.com/{org}/{team}/{plan}/{token}` | `brindle://plan/{org}/{team}/{plan}/{token}` |

The app link is `brindle://plan/` followed by the web link's path, unchanged.

- **`{token}`** is the share link's token: `dsl_` and 43 URL-safe characters
  (`A-Z a-z 0-9 _ -`). It alone opens the plan.
- **`{org}`, `{team}`, `{plan}`** are slugs for people to read: letters,
  digits, `-` and `_`, at most 100 characters each. The app never sends them
  anywhere, and a wrong or old slug still opens the plan.
- Nothing else is accepted: no query, no fragment, no escaped character, no
  empty, `.` or `..` segment, and no more than four segments after `plan/`. One
  trailing `/` is ignored. Links over 512 characters are dropped by the app's
  shell before the page sees them.

## What the app does with one

1. The shell (`app/src-tauri/src/deep_links.rs`) receives the link: macOS
   hands it to the running app; on Windows and Linux a second launch passes it
   to the running one and exits. The shell keeps `brindle://` links only, in a
   short queue, and tells the page some are waiting. It acts on none of them.
2. The page (`app/src/lib/deeplink.ts`) takes them and checks each one against
   the format above. A link that doesn't match opens a calm "Couldn't open this
   link" page and does nothing else: nothing in it is run, fetched or followed.
3. A plan link opens the plan's reader. The daemon reads the plan through the
   link (`POST /v1/share-links/design` with the token in the body, so it never
   lands in a request path or log), from the Library's
   `GET /share-links/{token}/design`, as the account signed in. The Library
   runs the link's own checks (revoked or replaced, the org's link policy, an
   org or team scope), the same as for the web page, so the app opens nothing
   the browser wouldn't. The plan is read only in the app, as on the web, and
   nothing of it is kept on the machine.
4. Signed out, the app shows its sign-in first; the link waits in the shell
   and opens once the person has signed in. If the sign-in has expired when
   the plan is read, the reader asks the person to sign in and then opens it.

The Library's side of share links (tokens, policy, scopes, the web page) is
`docs/share-links-api.md` in pawdelta-web.
