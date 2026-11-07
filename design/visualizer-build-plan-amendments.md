# Visualizer build plan: owner amendments (2026-11-06)

These override design/visualizer-build-plan.html where they differ.

1. Sidebar (revised after seeing it): no slim rail and no Focus mode. An open visual takes the whole window (sidebar hidden) with a clear Back button (and Esc) to return. Export controls must use the Tinted button scheme and look polished.
2. No visuals count in the session-remove confirmation. Visuals are kept only through an explicit Save/Keep inside the session; nothing else asks about them.
3. Planning docs appear only when the person explicitly asks for one (not automatically for large requests). The visuals user setting controls the "Make a visual?" offer popup: on = brindle may offer when a decision is hard to read (rules + rate limit as planned); off = no offers. Visuals are made only when the person accepts an offer or asks for one.
4. SharePoint export as .html now; Word later. (As planned.)
5. Drive/SharePoint export are Pro; Download for everyone. (As planned.)
6. No "Never offer" setting. (As planned.)
7. Workers may publish visuals into the supervisor's session; only supervisors offer. (As planned.)
8. Do not accept the iframe freeze risk: render visuals in a separate child webview (Tauri multiwebview) on the brindle-visual:// scheme, with the same deny-by-default navigation/network hooks and CSP, so a hung visual cannot freeze the app's main webview.
