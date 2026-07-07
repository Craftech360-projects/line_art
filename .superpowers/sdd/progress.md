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

---

# Production Readiness (line_art) — SDD progress

Plan: docs/superpowers/plans/2026-07-07-production-readiness.md
Branch: feat/prod-readiness
Branch base (merge-base for final review): 620e594

Task A1: complete (commits 620e594..2f28a05, review clean — SPEC ✅, QUALITY approved)
  Minor (defer to final review):
  - _speaches hardcodes http://localhost:8001 fallback when base URL empty (magic default).
  - _groq/_speaches near-duplicate multipart shape — consider extraction after A2/A3 land.
  - transcribe_with logs full transcript at INFO (kids' speech in logs; relates to Part B hygiene).
Task A2: complete (commits 2f28a05..aa67372, review clean after 1 fix loop — SPEC ✅, QUALITY approved)
  Fix: deepgram empty-channels IndexError guarded (Important, fixed in aa67372, test added, 8/8).
Task A3: complete (commits aa67372..96fb3ff, review clean — SPEC ✅, QUALITY approved, no findings)
  Pre-merge flag (from plan): re-verify Sarvam endpoint paths/field names against live vendor docs.
Task A4: complete (commits 96fb3ff..f6d3407, review clean — SPEC ✅, QUALITY approved; full suite 76 passed)
  Minor (defer to final review):
  - _parse returning None on a 200 (malformed body) degrades to cache silently with no warning log.
  - broad except Exception is deliberate (degrade-to-cache) and logged — reviewed as acceptable.
Task A5: complete (commits f6d3407..d5e1331, review clean — SPEC ✅, QUALITY approved; full suite 76 passed)
  Accepted deviation: transcribe() strips text (plan's verbatim code contradicted its own test; adapters already strip).
  Minor (defer to final review):
  - main.py:42-45 startup log still references now-orphaned config.STT_BACKEND (main.py was protected in A5 scope).
  Note: implementer stalled on first dispatch (returned without working); resumed via SendMessage, completed on retry.
Task A6: complete (commits d5e1331..cd963d6, review clean — SPEC ✅, QUALITY approved)
  Reviewer's "broken ADR link" finding DISMISSED by controller: reviewer checked the wrong repo
  (picoclaw); docs/adr/0002-stt-provider-selection-via-manager-api.md exists in line_art (620e594).
PART A COMPLETE.
Task B1: complete (commits cd963d6..46dce62, review clean — SPEC ✅, QUALITY approved; 77 passed)
Task B2: complete (commits 46dce62..64675c5, review clean — SPEC ✅, QUALITY approved; 79 passed)
Task B3: complete (commits 64675c5..198e530, review clean — SPEC ✅, QUALITY approved; 79 passed, no-DSN check ok)
  Pre-merge flag: reuse cheeko-backend's Sentry project/DSN if one exists (grep manager-api) rather than creating new.
Task B4: complete (commits 198e530..1768b04, review clean — SPEC ✅, QUALITY approved)
  Pending (post-implementation): manual staging verification of deploy gate + rollback on the pilot box.
Task B5: complete (commits 1768b04..b1546e8, review clean — SPEC ✅, QUALITY approved; 81 passed)
  Minor (defer to final review): local `from app import config as _cfg` import (plan-mandated form); plain != vs hmac.compare_digest (low sev at this trust level).
ALL 11 TASKS COMPLETE (A1-A6, B1-B5). Commits: 620e594(docs)..b1546e8.
Live verification done by controller: manager-api /toy/livekit/providers/active returns sarvam/saaras:v3; chain resolves [sarvam, groq]. .env wired.
Final whole-branch review (opus): READY-WITH-MINORS -> 3 fix-now items FIXED in 59a194b
  (transcript log INFO->length-only+DEBUG; honest lifespan STT log + STT_BACKEND removed; hmac.compare_digest WS secret).
  Accepted (recorded, not fixed): speaches localhost default; adapter multipart duplication (YAGNI);
  _parse None silent cache-degrade; local _cfg import; manager fetch 10s serial before STT (70s worst case, fits 90s window);
  chain dedup is name-only (bad manager key for last-resort provider has no net — intended per spec).
Controller fix: pre-existing flaky test test_command_prefix_is_stripped_from_subject pinned (commit follows 59a194b). Suite stable 81/81 across repeated runs.
BRANCH feat/prod-readiness MERGE-READY: 620e594(docs)..HEAD.
