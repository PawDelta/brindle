# Clean Mac test

The last check before a release. Run it on a fresh macOS user account or a
VM. The machine must have no Homebrew, no tmux and no brindle. Follow the
steps in order and fill in the Results table at the end.

Issue #212, step 4. Use the latest release candidate, which is published
under `rc/<version>/` on the downloads site.

## Before you start

- Make a new macOS user account, or start a clean VM.
- Check that `brew`, `tmux` and `brindle` are not on the PATH:
  `which brew tmux brindle` should print nothing.
- Have a Claude account ready, and a folder or a Git repo to open.
- Write down the macOS version and the arch (`uname -m` prints `arm64` or
  `x86_64`).

## 1. Download the DMG

1. Find the newest release candidate. In Safari, open the App release
   workflow runs:

       https://github.com/PawDelta/brindle/actions/workflows/app-release.yml

   Pick the newest run for an `app-v<X.Y.Z>-rc<N>` tag that shows a green
   check. Open it and read the `Publish to pawdelta.com/brindle/downloads/desktop/`
   step summary. It names the published version and the `rc/` folder.
   The version is the tag without the `app-v` prefix, for example
   `0.2.0-rc.1`. Write it down. You need it for the Results table.

   If you have the GitHub CLI, this lists the same runs. It is optional:

       gh run list --repo PawDelta/brindle --workflow "App release" --limit 10
2. Download the DMG for your arch. The files are at
   `https://pawdelta.com/brindle/downloads/desktop/rc/<version>/`:
   - Apple Silicon:
     `https://pawdelta.com/brindle/downloads/desktop/rc/<version>/brindle_<version>_aarch64.dmg`
   - Intel:
     `https://pawdelta.com/brindle/downloads/desktop/rc/<version>/brindle_<version>_x86_64.dmg`

   Open the link in Safari, or use `curl -O` with the link. Do not browse
   folders. The folder listing is not public.

## 2. Check the DMG with Gatekeeper

Run this on the DMG in Terminal:

    spctl -a -vvv -t open --context context:primary-signature ~/Downloads/brindle_<version>_<arch>.dmg

It should print `accepted` and `source=Notarized Developer ID`. A plain
`spctl` call prints nothing when it passes, so keep `-vvv`. Then open the
DMG. Drag the app to Applications.

Check the app itself:

    codesign --verify --deep --strict --verbose=2 /Applications/brindle.app

It should end with `valid on disk` and `satisfies its Designated Requirement`.

Then open the app from Applications. It should open with no warning. If
macOS blocks it, record the exact message in the Results table and stop.

## 3. Onboarding

1. Sign in with your account.
2. Connect Claude Code when the app asks for it. Follow the prompts. Do not
   open Terminal to set anything up.
3. Open a folder, or clone a repo from inside the app.

Each part should work without typing a command. If you have to type a
command, record it as a fail.

## 4. Start a session

1. Start a new session in the folder or repo from step 3.
2. Wait for the agent to start streaming output.
3. While it streams:
   - Scroll the output up and down. It should move smoothly.
   - Click in the output and in the input box. The click should land where you
     point.
   - Type a short sentence in the input box. Every key should show up, in
     order, with no lag and no lost keys.
4. Record what you typed and what you saw. This is the typing check from
   #215.
5. Copy and paste (#217), in the session window and again in the Terminal
   panel (the Terminal button at the bottom):
   - Drag over some text, press ⌘C, and paste it into another app. It should
     be the text you highlighted.
   - Copy a line from another app and press ⌘V in the pane. It should appear
     once.

## Results

Fill in one row per step. Use `PASS`, `FAIL` or `not run`. Put any error text
or odd behaviour in the notes.

| Step | Pass/fail | Notes | Build version | macOS version | Arch |
|---|---|---|---|---|---|
| 1. Download the DMG from `rc/` | not run | | | | |
| 2a. `spctl` accepts the DMG | not run | | | | |
| 2b. `codesign --verify` passes on the app | not run | | | | |
| 2c. App opens with no warning | not run | | | | |
| 3a. Sign in | not run | | | | |
| 3b. Connect Claude Code | not run | | | | |
| 3c. Open a folder or clone a repo | not run | | | | |
| 4a. Start a session | not run | | | | |
| 4b. Scroll while the agent streams | not run | | | | |
| 4c. Click while the agent streams | not run | | | | |
| 4d. Type while the agent streams (#215) | not run | | | | |
| 4e. Drag, ⌘C, ⌘V in the session window (#217) | not run | | | | |
| 4f. Drag, ⌘C, ⌘V in the Terminal panel (#217) | not run | | | | |
