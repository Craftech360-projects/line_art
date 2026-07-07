# AI Imagine (line_art) — SDD progress

Plan: docs/superpowers/plans/2026-06-30-ai-imagine-line-art.md
Branch: feat/ai-imagine
Branch base (merge-base for final review): 3da66dc

Task 1: complete (commits 3da66dc..6fd642e, review clean — SPEC ✅, QUALITY approved)
  Minor (defer to final review):
  - test_to_device_jpeg_center_crops_to_4_3 asserts only final size, not that a crop (vs squish) occurred.
  - to_device_jpeg silently returns >200KB if all quality steps fail (physically ~impossible at 320x240; comment acknowledges).
  - (report-only naming nit: loop called "progressive fallback" though code uses progressive=False — no code defect.)

Task 2: complete (commits 6fd642e..dc85e0b, review clean — SPEC ✅, QUALITY approved, no findings)

Task 3: complete (commits dc85e0b..d733ed8, review clean — SPEC ✅, QUALITY approved; full suite 47/47)
  Minor (defer to final review):
  - import base64 placed before import json (isort would prefer this anyway; no effect).
  ⚠️ resolved by controller: dm.line_art_progress/line_art_error DO accept stage= (verified in device_messages.py).

ALL TASKS COMPLETE. Feature commits: 3da66dc(docs)..d733ed8.

Final whole-branch review: READY-WITH-MINORS (no Critical/Important). Non-blocking minors for a follow-up hardening pass:
- device_protocol.py hardcodes 320,240 in dm.image(...) instead of image_gen.DEVICE_W/DEVICE_H (DRY nit; values contractually fixed).
- to_device_jpeg returns >200KB if all 6 quality steps fail (physically unreachable at 320x240).
- crop tests cover only "too wide"; no "too tall" case; crop-vs-squish not asserted.
- import base64 ordering (cosmetic).
- opus_frames has no max-frame guard (pre-existing on printer path; now reachable by gateway — flag for hardening).

line_art subsystem = MERGE-READY on branch feat/ai-imagine. Remaining subsystems NOT started: mqtt-gateway, manager-api.
