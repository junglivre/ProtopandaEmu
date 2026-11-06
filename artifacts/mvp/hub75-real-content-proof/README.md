# HUB75 real animated content, not boot-time black frame

ISSUES.md ("From M4") previously documented that no HUB75 frame beyond the
boot-time `clearScreen()` all-black state had ever been captured, because the
pinned upstream firmware's Lua task errored out of `onSetup` on a VFS race
before reaching its own render path.

The emu-as-is boot (user's real `~/Projects/Proto/Proto-panda` `alt` fork +
user's real `~/ThunarSDCard` data, see `.rf/backend-fix3.md` "Emu as-is
variant") reaches `completed setup` with zero Lua errors and renders real
expressions continuously. `frame-seq1606.png` is `FRAME_DUMP seq=1606`
(128x64 RGB888, 24576 bytes, 7.6% non-zero bytes) decoded straight from the
QEMU `[protopandaemu.hub75]` trace of session
`.work/protopandaemu-sessions/19d6a0e6-dcc6-43f7-8937-5524cce1a97b` and
visually shows a real mirrored eyes/eyebrows mascot expression, not noise or
a cleared panel.

This closes the "only all-black HUB75 frame" gap for the as-is emu path.
It does not supersede Rock 3/M4's formal per-half-wiring golden-frame proof
(`scripts/prove-m4.py`), which is still the Cycle 2 acceptance gate.
