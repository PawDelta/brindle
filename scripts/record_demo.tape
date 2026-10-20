# vhs tape for assets/brindle-demo.gif, filled in and run by
# scripts/record_demo.sh (DEMO_RENDERER=vhs, the default). It replays the
# recorded, cleaned-up asciinema cast in vhs's xterm.js terminal, which draws
# Claude Code's redraws correctly; agg's emulator leaves doubled lines.
# 1250x706 px at font size 13 with padding 10 is exactly 150x45 cells, the
# size the cast was recorded at.

Output "@OUT@"
Set Shell bash
Set FontSize 13
Set Width 1250
Set Height 706
Set Padding 10
Set Framerate 15
Set TypingSpeed 0
Set Theme @THEME@

Hide
# --quiet: no "::: Playback ended" in the last frame; tput civis: no cursor.
Type "clear; asciinema --quiet play --speed @SPEED@ --idle-time-limit 2 '@CAST@'; tput civis; sleep 600"
Enter
Sleep 400ms
Show
# The playback, then the finished dashboard held at the end.
Sleep @HOLD@s
