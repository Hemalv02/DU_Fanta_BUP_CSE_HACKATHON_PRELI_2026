# Marking & Requirement Audit — BUP CSE Fest 2026 Preliminary

Line-by-line coverage of the Participant Guide & Evaluation Rubric (100 pts)
and the Problem Statement requirements, with the implementation location and
the verification evidence for each item.

Verified state at last run: **10/10 public cases + 18/18 adversarial edge
cases pass through the live LLM** (`gpt-5.6-sol`), all costs exactly at the
LP optimum (quality ratio 1.0000); **42/42 section-by-section contract
conformance checks** (input validation, output schema, physics replay,
judge consistency — `scripts/contract_conformance.py`); 85 pytest tests,
`mypy --strict` clean, `ruff` clean; live p95 latency 3.5 s (bracket: ≤5 s);
Docker image 378 MB (multi-stage, non-root, no secrets, deps layer cached).

---

## 1. LLM Directive Interpretation — 25 pts

| Sub-item | Implementation | Evidence |
|---|---|---|
| Relevance / no_op (5) | LLM classifies; guardrails force `applies=false` + `null` for no_op; per-note rule-based fallback | EDGE-01..03, 13, 17 distractors pass live |
| directive_type (5) | enum-locked strict JSON schema at generation; `_VALID_TYPES` guardrail | 28/28 live cases correct type |
| Affected hours (5) | window rules in prompt (start-inclusive/end-exclusive, midnight wrap); guardrail normalizes unique/ascending 0–23 | EDGE-06/07/15 midnight wraps pass |
| Numeric values + required shape (5) | field-shape validation per type; factor∈[0,1] with percent-repair; reserve≤capacity; capacity-percentage conversion (battery capacity supplied to model) | EDGE-04 (factor 0.0), EDGE-09 (60% of 250→150), EDGE-14 (reserve=capacity) pass |
| Paraphrase robustness (5) | few-shot tricky examples + window/fraction rules in system prompt; no public-phrase hard-coding on the LLM path | Section 11.4 wordings + EDGE-05/11/12 fraction/complement phrasings pass |

## 2. Directive Application & Constraint Correctness — 25 pts

| Sub-item | Implementation | Evidence |
|---|---|---|
| Ground-truth directive application (10) | directives become per-hour LP bounds (Section 5.3) before solving; judge replay in tests/scripts re-checks against the interpretation, not our belief | EDGE-18 (3 stacked directives) replay passes |
| Energy balance / effective solar (5) | equality constraint in LP; replay re-derives effective solar after solar_reduction | `_judge_replay` in tests + `scripts/run_public_samples.py` |
| Battery transitions/bounds/rate limits (5) | LP bounds + transition equalities; replay re-checks each hour | `app/services/energy_service.py::replay_validate` |
| Action consistency / neutrality / non-negative (5) | post-processing labels exactly one action, cancels simultaneous charge/discharge, recomputes energy; end-of-day neutrality equality | all 28 cases end at initial energy within 0.01 |

## 3. Optimization Quality — 10 pts

Formula: `min(1, organizer_optimal / team_cost)`.
The LP (scipy HiGHS) solves the exact problem statement objective; measured
**quality ratio = 1.0000 on every one of the 28 live cases** (reference
optimum for public cases from the pack; for edge cases computed by running
the same LP with the ground-truth directives).

## 4. API Contract & Schema — 10 pts

| Sub-item | Implementation | Evidence |
|---|---|---|
| Endpoints/status (2) | `GET /health` → `200 {"status":"ok"}`; `POST /optimize-energy` → 200 | `tests/test_health.py` |
| Request validation (2) | strict Pydantic request model; malformed/structural → 400 (FastAPI 422 remapped) | `tests/test_request_validation.py` (10 cases) |
| Interpretation schema/order/types (3) | one entry per note in note_index order, `applies` semantics enforced by model validator | guardrail tests + live runs |
| hourly_plan/top-level + scenario_id echo (3) | response model validates 24 ordered hours, totals, summary; scenario_id echoed | `tests/test_public_samples.py` |

## 5. Performance & Reliability — 10 pts

| Sub-item | Evidence |
|---|---|
| Health readiness ≤ 60 s | container HEALTHCHECK passes in seconds; `/health` is dependency-free |
| p95 ≤ 5 s | measured p95 **3.5 s** over 20 live LLM requests (rule-based path ≈ 10 ms); `LLM_TIMEOUT_SECONDS=12` with `timeout x (retries+1)` auto-clamped inside the 30 s judge limit |
| Stability / failure rate | 28 consecutive valid requests, zero 5xx; sync endpoints run in threadpool |
| Malformed/model-failure handling | malformed JSON → 400 clean body; provider error → guarded fallback → worst case controlled 500 with fixed message; `tests/test_llm_security.py` |
| Secret safety | key only in `.env` (gitignored) / runtime env; logs record exception class names only; `.dockerignore` excludes `.env`; image verified to contain no key file; opt-in JSONL request log (`REQUEST_LOG_FILE`, off by default) stores API payloads only via an off-request-path writer thread |

## 6. Deployment & Docker Fallback — 10 pts

| Sub-item | Evidence |
|---|---|
| Live endpoint reachability (3) | compose service verified externally on port 8000, `restart: unless-stopped` |
| Pullable Docker image reaches /health (4) | `docker build -t gridwise-llm:0.1.0 .` + `docker run --rm -p 8000:8000 --env-file .env gridwise-llm:0.1.0` documented and tested; multi-stage (uv builder → python:3.12-slim), non-root, HEALTHCHECK, deps layer cached |
| Clean startup/reproducibility (2) | one-command startup; `uv.lock` pinned; README quickstart from clean env |
| No judge debugging (1) | no manual steps; errors are controlled JSON |

## 7. Documentation & Local Reproducibility — 10 pts

README covers: 3-pt quickstart (`uv sync`, `.env`, `uvicorn`), env-var table
(model/provider/config), public-sample + edge-pack test commands with
expected result, architecture diagram (LLM → guardrails → optimizer),
Docker fallback commands, dependencies/credits, known limitations, secret
handling. This audit maps every rubric line to its evidence.

---

## Problem Statement — specific requirement checklist

- [x] LLM is in the interpretation path producing optimizer constraints (not just summary text)
- [x] Exactly one `directive_interpretation` per note, note_index order, no missing/duplicate mappings
- [x] Only the 6 supported directive types; unsupported → controlled failure, never invented
- [x] Time windows whole-hour, start-inclusive/end-exclusive, ascending unique 0–23
- [x] `factor` = remaining fraction (80% reduction ⇒ 0.2); percent-repair for misformatted model output
- [x] `applies=true` for all non-no_op; `false` + `null` only for no_op
- [x] Deterministic validation between LLM and optimizer (guardrails module)
- [x] Final replay of the completed schedule against every extracted directive
- [x] Battery rules 9.1–9.6 (state, bounds, rates, solar cap, balance, neutrality)
- [x] Totals recalculated from `hourly_plan` (judge consistency rule 11.3)
- [x] 0.01 kWh/BDT tolerance respected in all comparisons
- [x] 400/422/500 semantics; no stack traces/secrets in responses
- [x] Safe failure: malformed LLM output → per-note deterministic fallback → controlled error
- [x] Provider rejects strict `json_schema` mode → immediate `json_object` retry (fast 400, no latency stacking); markdown-fenced JSON parsed leniently
- [x] Rounding drift (repeating-decimal LP vertices) → precision-retry ladder re-rounds the plan finer instead of failing a valid case

## Remaining submission actions (outside the code)

1. Deploy to a public URL (any platform; keep reachable through judging).
2. DONE: image published to GHCR as `ghcr.io/hemalv02/gridwise-llm:0.1.0`
   (digest `sha256:b8c29384cc5f18a24f017dce2cbadc3b97e1f3b0f1549c44fc5c1889aedd59b8`,
   multi-arch linux/amd64 + linux/arm64, amd64 smoke-tested end to end).
   After the deadline, make the GHCR package public together with the repo
   (package settings, or `gh api --method PATCH /user/packages/container/gridwise-llm -f visibility=public`).
3. Record the ≤3-minute architecture video (tie-break only).
4. Repository private during the event, public after the deadline.
