# LinkedIn comment replies — v0.3.0 post

Drafted 2026-09-26. **Not posted yet.** Track T1 is now done, so reply 2's claim is backed by
`reports/checker_bug_ab.json` and `docs/CHECKER_AB_RESULTS.md`.

Every number about this project below is traceable to a commit or to a file in `reports/`.
Every number about another project is traceable to that project's own paper or repository,
and vendor marketing is labelled as such.

---

## Comment 1 — memory as state management and belief revision

> "Agent memory is fundamentally less of a retrieval problem and more of a state-management
> and belief-revision problem... the next frontier is memory admission control: deciding what
> becomes durable state, how confidence and provenance are represented, when a new observation
> supersedes an old belief, and when the system should simply say 'I don't know.'"

### Short reply (post as-is)

You have named the gap more precisely than my own post did, so let me be exact about where
this engine actually stands.

What is implemented: supersession by natural key — `user:subject:predicate`, last writer wins,
old row deactivated — plus TTL expiry and recency decay on retrieval
(`similarity × exp(-ln2 · age / half_life)`).

What is not, and the code is honest about it: the extractor already returns a confidence score
per fact, and the write path throws it away. There is no confidence column, no admission
threshold, no provenance field, and no abstention path — retrieval always returns its top k.
So today every extracted fact is admitted. That is admission control by default, which is to
say none.

Which matches where the field is. Mem0 puts an LLM in the write path to choose
ADD / UPDATE / DELETE / NOOP. Zep's Graphiti gives every edge `valid_at` and `invalid_at` and
invalidates a contradicted edge instead of deleting it. A recent ICLR 2026 workshop paper
(A-MAC) gates writes on five signals — future utility, factual confidence, semantic novelty,
temporal recency, content-type prior — and reports content type as the strongest one.

And your last clause is the one benchmarks already punish: LongMemEval tests knowledge updates
and abstention as two of its five abilities, and long-context models drop roughly 30% on it.

Since you raised it I ran LongMemEval's knowledge-update and abstention sets, 20 instances each,
486 utterances. The numbers are bad and worth more than the good ones: the current value reached
the store in **3 of 20** knowledge-update instances, the supersession path fired **0 times**,
and for the 20 unanswerable questions the engine returned memories **100% of the time**, mean
4.75, because there is no confidence floor to stop it.

The useful part is *why*. Supersession is keyed on `user_id:subject:predicate`, so two statements
only collide when the extractor picks the same predicate for both — on free text it does not.
And in 17 of 20 instances the answer-bearing fact never reached the store at all, so revision was
never exercised. Which reorders my own roadmap: admission control is not the next thing after
extraction, it is the same problem. Deciding what to admit is worthless while the extractor
decides silently and differently each run.

Numbers, method and limits: `docs/BENCHMARK_LONGMEMEVAL.md`. They are judge-free proxies, not
the paper's LLM-judged scores, so they are not comparable to anyone's published LongMemEval
number, mine included.

### Sources for this reply

- Mem0's four write operations and its own reported numbers: arXiv 2504.19413. LOCOMO
  LLM-as-judge scores single-hop 67.13, multi-hop 51.15, open-domain 72.93, temporal 55.51;
  about 7k tokens per conversation (14k for the graph variant); p95 total latency 1.44s against
  17.117s for full context. Vendor-authored.
- Zep / Graphiti bi-temporal edges and LLM-driven edge invalidation: arXiv 2501.13956, "Zep: A
  Temporal Knowledge Graph Architecture for Agent Memory". Reports DMR 94.8% against MemGPT's
  93.4%, and on LongMemEval accuracy improvements up to 18.5% with 90% lower latency.
  Vendor-authored.
- Letta / MemGPT self-editing memory blocks and sleep-time compute: arXiv 2504.13171.
- A-MAC, "Adaptive Memory Admission Control for LLM Agents", ICLR 2026 MemAgents workshop
  (OpenReview `mmdqUrEY24`): five admission signals; LOCOMO F1 0.583 with 31% lower latency.
- LongMemEval: arXiv 2410.10813 (ICLR 2025). Five abilities — information extraction,
  multi-session reasoning, temporal reasoning, knowledge updates, abstention; 500 curated
  questions; roughly 30% accuracy drop for long-context models.
- LOCOMO: ACL 2024 long paper 747. 50 dialogues, 300–600 turns each.
- MemoryArena, arXiv 2602.16313: memory evaluated inside multi-session agent loops; systems
  that score well on LOCOMO do markedly worse.
- TOKI, arXiv 2606.06240: production contradiction-resolution heuristics typed as bitemporal
  write-time operators. MemTX, arXiv 2607.23929: transactional belief commit for agent memory.
- Not used as evidence, noted as market signal only: ByteRover's 92.2% LOCOMO claim and
  mem0.ai's "State of AI Agent Memory 2026" are vendor blog posts.

### Verified state of this repository, for anyone checking the reply

| Claim in the reply | Where to verify |
| --- | --- |
| Extractor emits confidence | `agentic_memory/providers.py:106`, `:134`; `agentic_memory/models.py:15` |
| Write path discards it | `agentic_memory/store.py:115` (`upsert_fact`); `memory_keys` schema at `store.py:72` has no confidence column |
| Supersession is last-writer-wins on a natural key | `store.py:121` builds `user:subject:predicate`; `store.py:95` `deactivate_by_key`; `store.py:130` `INSERT OR REPLACE` |
| No valid-time, no queryable version history in `memory.db` | schema carries only `updated_at`, `expires_at`, `is_active`; `natural_key` is the primary key, so a superseded row is overwritten. The trail survives only in `audit_log.db`, and LanceDB retains the old vector with `is_active = False` |
| No provenance field | `FactRecord` has subject, predicate, object_value, confidence, scope, expires_at — no source |
| No abstention | `engine.py:112` `retrieve_memories` ranks and returns top k with no confidence floor |

---

## Comment 2 — the 6% versus 86% gap, and the macro application

> "86% after finding the checker bug versus 6% before is a huge gap. Curious what the checker
> was actually miscounting — and I would like to see this applied on a macro level. For example,
> project milestones can shift based on a long-term decision tree and having this at arm's
> length allows better projections and planning."

### Short reply (post as-is)

Fair question, and the honest answer is that "the checker bug" was seven faults, not one, and
none of them were in the engine. The pass rate went 6% → 12% → 82% → 86% across three commits.

What the checker was miscounting:

1. Ingest was fire-and-forget — HTTP 202, no ids — so the harness invented ids like
   `sim:<scenario>:<index>` while the engine stores `mem_*`. Nothing could ever match.
2. The engine stores a triple, `user works_in Dubai`, never the sentence `I work in Dubai`.
   Text overlap scored 0.500 against a 0.6 threshold, so correct storage read as a miss.
3. The profile cache TTL defaulted to 3600s while runs lasted 60–360s, so checks compared
   against a profile cached before the run started.
4. The extractor returns no fact for roughly 60% of utterances. Nothing reaches the store, and
   presence checks were failing on an absence the run never caused. Those are skips now, named
   as `not_extracted`.
5. User isolation was judged by the scenario script rather than the database. Scripts reuse
   phrasings, so "recipe planning side project" was reported as leaking from user_5 to user_3
   while the store showed user_3 as its only owner and user_5 as never having held it. That one
   fix alone took failed checks from 30 to 3 and cross-validation errors from 59 to 8.
6. Every run reused `run_number=1`, and the audit database retains all runs, so validation
   replayed dozens of earlier runs at once and manufactured cross-user findings — 30 isolation
   failures against 0 on an unused number.
7. Two verdicts were literals in the report writer: `"user_isolation": "PASS"` unconditionally,
   and audit completeness decided by `events_played > 0` rather than by any audit check.

So 6% measured the harness and 86% measures the engine. Neither number was ever a statement
about retrieval quality. The current measured state is 42–44 of 50 scenarios per run with
66–69 of 111 utterances yielding no fact at all — that gap is real and open.

Rather than ask you to take that on trust, I put each fault back behind a flag and replayed the
same 50 scenarios against the same running engine, nine times. The engine was not restarted or
reconfigured once. Scenarios passed: **43/50 on the current checker, 0/50 with the faults
back**. Alone, inventing fact ids costs 43 scenarios, deciding ownership from the script costs
43, treating extractor silence as failure costs 35, and replaying `run_number=1` costs 31. The
baseline ran first and last and moved by zero scenarios, so that spread is not noise. Numbers
and method: `docs/CHECKER_AB_RESULTS.md`.

The sharpest one is the report writer. On the all-faults arm the old writer would have
published criterion H as **PASS** with `isolation_pass_rate: 1.0` while the checks underneath it
found 0 passed and 50 failed. A test that can lie to you is bad; a report that can override
the test is worse.

On the macro version: that is a truth maintenance system, and the literature is exact about it.
De Kleer's ATMS (1986) holds several consistent belief sets at once, one per assumption set —
your decision tree, kept at arm's length — and dependency-directed backtracking retracts only
the conclusions that depended on the assumption that failed. Map it onto planning and a
milestone is a fact with a validity window, an assumption is provenance, a decision is a
superseding write, and the audit log is the dependency trail. The pieces I would need are
`valid_from` / `valid_to`, a `depends_on` edge, and persisted confidence — and three of those
four are already half-present.

One caveat I would not skip: on real project data, Batselier and Vanhoucke found reference class
forecasting beat both earned value management and Monte Carlo simulation on accuracy, stability
and timeliness. So the projection layer should take the outside view from comparable past work,
not only simulate forward from this project's own optimism. I would rather show you that running
against a real history than describe it, so it is on the list.

### Sources for this reply

- Fault 1–3: commit `2132dc8`, "fix(simulation): validate against what the engine actually
  stored". Reports checks passed 292 → 305 of 411 and states the 6% figure.
- Fault 4–5: commit `b779619`, "fix(validator): stop reporting engine silence and shared
  phrasings as failures". Reports 30 failed checks → 3, cross-validation errors 59 → 8,
  scenarios 6/50 → 41/50 on a one-minute run.
- Fault 6–7: commit `8a80d91`, "fix(validation): derive the run reports from the runs". Names
  the hardcoded `"user_isolation": "PASS"` and `isolation_pass_rate: 1.0`, the
  `events_played > 0` audit criterion, and the 30-versus-0 isolation failures from run-number
  reuse.
- Current numbers: `reports/run_{1..4}_*.json` and `reports/summary.json`, rendered into
  `VALIDATION_RESULTS.md`. 411 checks per run; scenarios 43/42/44/43 of 50; passed
  290/293/291/299; failed 3/4/2/3; skipped 118/114/118/109; cross-validation disagreements
  10/10/9/10; extraction blanks 69/69/69/66 of 111 utterances.
- ATMS: de Kleer, "An Assumption-based TMS", 1986; Reiter and de Kleer, "Foundations of
  Assumption-based Truth Maintenance Systems". Dependency-directed backtracking: Stallman and
  Sussman. The ATMS-to-AGM correspondence is published, so the belief-revision semantics are
  not hand-waving.
- Reference class forecasting versus EVM and Monte Carlo on real project data: Batselier and
  Vanhoucke, "Practical Application and Empirical Evaluation of Reference Class Forecasting for
  Project Management", Project Management Journal 47(5), 2016, 36–51; follow-up in
  International Journal of Project Management 35 (2017), 28–43.
- Bayesian-Monte Carlo schedule updating, for the digital-twin framing: arXiv 2605.17608.
- Jira has no native Monte Carlo forecasting; probabilistic delivery forecasting is supplied by
  Marketplace apps. Relevant because "at arm's length" is exactly the gap those apps fill.

---

## Proof plan

Four tracks. Each ends in a committed artifact, because a reply that cites a file someone can
open is worth more than a reply that cites a memory.

### T1 — reproduce the miscount, publish both numbers — **done**

Each fault is a flag on `LegacyCheckerConfig` (`src/simulation/legacy_checker.py`), threaded
through `ValidatorService` at exactly the points the fixes touched, so one fault can be enabled
alone. `scripts/run_checker_ab.py` plays the same 50 scenarios (seed 42) once per arm against
the same running engine; `scripts/generate_ab_table.py` renders the report.

Artifacts: `reports/checker_bug_ab.json`, `docs/CHECKER_AB_RESULTS.md`. 19 tests in
`tests/test_legacy_checker.py` pin each fault's behaviour against the fixed behaviour.

Measured, 60s per arm, engine untouched throughout:

| Arm | Scenarios passed | Checks failed |
| --- | --- | --- |
| baseline | 43/50 | 1 |
| `synthetic_fact_ids` | 0/50 | 82 |
| `compare_utterance_text` | 41/50 | 10 |
| `silence_is_failure` | 8/50 | 39 |
| `script_decides_ownership` | 0/50 | 51 |
| `reuse_run_number` | 12/50 | 32 |
| `literal_verdicts` | 42/50 | 2 |
| every harness fault at once | 0/50 | 103 |
| baseline, repeated last | 43/50 | 3 |

`literal_verdicts` shows almost no delta by construction — its effect is not a failed check but a
published verdict that ignores the checks, which the report shows separately.

The seventh fault, `assume_long_profile_cache`, needed the engine itself restarted with
`PROFILE_CACHE_TTL_SECONDS=3600`, so it was measured in its own pair
(`reports/checker_ttl_fault.json`, `docs/CHECKER_TTL_FAULT.md`) — and it came back **negative**.
With the engine at 3600s, the arm scored 42/50 against 41/50 for the same checker told the TTL
was 30s: Δ failed checks −1. Exactly one outcome failed there and not in its baseline,
`cache_miss_recorded`. The reason is visible in the run notes: the realised gap before the
`cache_002` read was 27.6s, shorter than either TTL, so the runner marked that miss expectation
unsatisfiable and skipped it in both arms rather than failing it.

So of the seven, six are demonstrated and one is not. The TTL was a real pre-fix problem — the
commit message describes profiles cached before the run started — but against today's harness it
no longer manufactures failures, because the TTL solver that skips unsatisfiable cache
expectations absorbs it. I would rather publish that than leave the impression all seven were
reproduced.

### T2 — admission control, measured rather than asserted — **reordered by T3's result**

T3 changed the order here. Persisting confidence and gating on it cannot be measured while the
extractor drops the fact in 17 of 20 external instances, so the first step is now extraction:
separate "no fact in this utterance" from "the extractor failed", make a second pass on silence,
and make the same input produce the same output. Admission control lands on top of that.

Persist `confidence` and `source` on a fact. Add an admission floor. Gate writes on the A-MAC
signal set — future utility, factual confidence, semantic novelty, temporal recency, content-type
prior — and, separately, split "this utterance contains no fact" from "the extractor failed",
which is the honest decomposition of today's 66–69 blanks per run.

Artifact: precision, recall and F1 of admitted facts against the scenario catalogue's own labels
across 111 utterances, as a curve over the threshold, plus tokens and latency per user.
Proof standard: we own the ground truth for those 111 utterances, so precision and recall are
computable without a new labelling effort.

### T3 — external benchmark, so comparison becomes legitimate — **done for LongMemEval**

`benchmarks/longmemeval/run_benchmark.py` ingests an instance's user turns into a second engine
process with its own store, asks the instance's question, and scores judge-free: answer coverage
in the top k, stale-value coverage, whether the value reached the store at all, and a
`belief_state` per instance that separates "the engine failed to supersede" from "the extractor
never produced the old fact". 27 tests in `tests/test_longmemeval_harness.py` cover the scoring.

Artifacts: `benchmarks/longmemeval/results.json`, `docs/BENCHMARK_LONGMEMEVAL.md`,
`benchmarks/README.md`. Measured on 20 knowledge-update and 20 abstention instances, 486
utterances, 1183s:

| Measure | Result |
| --- | --- |
| Current value present in the store | 3/20 |
| Current value in the top 5 | 3/20 |
| Superseded value returned (stale read) | 0/20 |
| `belief_state` = `superseded` | 0/20 |
| `belief_state` = `current_value_missing` | 17/20 |
| Unanswerable questions answered with memories | 20/20, mean 4.75 |

Limits, stated rather than buried: these are token-coverage proxies at a 0.6 bar, not the
paper's LLM-judged scores, so they compare to nothing published; the oracle split was used, so
retrieval-at-scale is untested; two of the three successes matched on a single content token and
are weak; and the extractor is non-deterministic, so a rerun will not reproduce the figures
exactly — an earlier pass over the same instance stored nine facts including the answer, this one
stored five without it.

**LOCOMO is not done.** Its QA is two-speaker and largely multi-hop, which needs a generator and
a judge to score fairly, and a bad mapping would produce a number that looks like a comparison
without being one. It is worth doing after the extractor work, not before.

### T4 — the macro prototype

Add `valid_from` / `valid_to` and a `depends_on` edge. Load a real history — this repository's
own commit log and its fourteen-task ledger, which is authentic data we already hold. Retract one
assumption and show which milestone dates move, which do not, and why, with the dependency trail
read out of the audit log. Forecast with throughput Monte Carlo over the real commit timestamps,
and state the reference-class caveat in the same view.

Artifact: one dashboard page and a written report.
Proof standard: the retraction is applied to data nobody curated for the demo, and the unchanged
milestones are shown alongside the changed ones.
