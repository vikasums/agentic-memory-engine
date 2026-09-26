# Benchmarks

External benchmarks, so a comparison against another memory system rests on a
public dataset rather than on this project's own harness. Nothing here writes to
the production store: every run targets a second engine process with its own
`memory.db` and LanceDB directory.

The datasets are downloaded, not authored, and are gitignored.

## Fetch the data

```bash
mkdir -p benchmarks/data
curl -sL -o benchmarks/data/longmemeval_oracle.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json
curl -sL -o benchmarks/data/locomo10.json \
  https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
```

| File | Size | Contents | Source |
| --- | --- | --- | --- |
| `longmemeval_oracle.json` | 15 MB | 500 instances, evidence sessions only | LongMemEval, arXiv 2410.10813 (ICLR 2025) |
| `locomo10.json` | 2.8 MB | 10 very long conversations with annotated QA | LoCoMo, ACL 2024 long paper 747 |

## Start the benchmark engine

A separate process on port 8010 with its own store, so a benchmark pass cannot
touch `memory.db` in the repository root:

```bash
MEMORY_DB_PATH=./benchmarks/data/bench_memory.db \
LANCEDB_PATH=./benchmarks/data/bench_lancedb \
venv/bin/python -m uvicorn agentic_memory.main_api:app --host 127.0.0.1 --port 8010
```

The engine extracts with `qwen2.5:14b-instruct` through Ollama by default, so
ingestion is the slow part: roughly one to four seconds per utterance on a local
machine.

## Run LongMemEval

```bash
venv/bin/python benchmarks/longmemeval/run_benchmark.py \
  --engine http://localhost:8010 \
  --memory-db ./benchmarks/data/bench_memory.db \
  --limit 5
```

`--limit` is per bucket: five answerable instances and five `_abs` (abstention)
instances. Drop it for the full selection. `--question-types` defaults to
`knowledge-update`, which is the ability the v0.3.0 post makes a claim about.

### What the numbers mean, and what they do not

LongMemEval's published metric scores a **generated answer** with an LLM judge.
This engine generates nothing — it stores facts and ranks them — so this harness
reports judge-free quantities instead, and they are **not comparable to the
scores in the paper or to any vendor's published LongMemEval number**:

| Metric | Definition |
| --- | --- |
| `answer_supported` | Some memory in the top *k* carries at least 60% of the gold answer's content tokens |
| `stale_returned` | Some memory in the top *k* carries at least 60% of the *superseded* statement's content tokens — the failure the post claims does not happen |
| `answer_in_store` | The current value is in `memory.db` at all, whether or not retrieval ranked it |
| `belief_state` | What the store did with the update, per instance (see below) |
| `returned_something_rate` | Share of abstention instances where the engine returned any memory at all. The engine has no confidence floor, so this is expected to be 1.0, and that is the gap being measured |

`belief_state` is the metric that separates two failures a single rate would
confuse:

| Value | Meaning |
| --- | --- |
| `superseded` | Current value active, old one deactivated. Belief revision happened. |
| `both_active` | Both values active. The engine accumulated instead of replacing. |
| `old_value_never_stored` | Current value active, old one absent entirely — the extractor never produced it, so this instance does not test revision at all. |
| `current_value_missing` | The current value never reached the store. |

A gold answer's parenthetical alternative is scored as its own variant, because
the engine stores whichever form the extractor produced: `"25 minutes and 50
seconds (or 25:50)"` is satisfied by a stored `user has_personal_best_time
25:50`. Counting that as a miss would be the harness's bug rather than the
engine's, which is the same class of mistake the v0.3.0 checker made.

Token coverage is a proxy for an answer being present. It is stated as a proxy
everywhere it appears, including in the result files.
