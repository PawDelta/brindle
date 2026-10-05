## Fixed in 0.11.5
- **`add_dirs` expands a leading `~`.** `~/cache` means your home directory's `cache`, not `<repo>/~/cache`. That holds in a profile in `~/.brindle/agents` too. (#4)
- **A missing `add_dirs` directory is reported once per launch, where someone sees it.** Claude Code silently ignores an `--add-dir` that doesn't exist. brindle used to print that two or three times per launch, to a stderr nobody reads when a supervisor starts the worker. Now `handoff` and `assign` add it to their reply, a queued task's start message carries it, a terminal launch prints it once, and a resume checks again. (#4, #13)

## Added in 0.11.5
- **`brindle doctor` checks `add_dirs`** and warns about any directory that doesn't exist. (#13)
