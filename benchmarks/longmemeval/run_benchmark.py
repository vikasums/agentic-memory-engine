#!/usr/bin/env python3
"""Track T3: run this engine against LongMemEval, judge-free.

LongMemEval (arXiv 2410.10813, ICLR 2025) scores five memory abilities. Two of
them are the ones the v0.3.0 post makes claims about, so those are the two this
harness measures:

* **knowledge-update** — a later session supersedes an earlier value. The engine
  claims to replace a fact rather than accumulate both, and 78 of the oracle
  file's instances are exactly that shape: two sessions, each carrying one
  answer-bearing user turn, the second one holding the current value.
* **abstention** — the question cannot be answered from the history. The engine
  has no abstention path at all today, so the number here is expected to be bad;
  it is measured so the gap has a size.

**This is not the published LongMemEval metric.** The paper scores a generated
answer with an LLM judge. This engine generates nothing — it stores and ranks
facts — so the harness reports judge-free quantities instead:

``answer_supported``
    Does any memory in the top *k* cover the gold answer's content tokens?
``stale_returned``
    Does any memory in the top *k* cover the *superseded* value's distinguishing
    tokens? For a knowledge-update instance that is the failure the post claims
    not to happen.
``belief_state``
    What ``memory.db`` did with the update, read at the store rather than
    through retrieval: ``superseded`` (current value active, old one
    deactivated), ``both_active`` (accumulated instead of replaced),
    ``old_value_never_stored`` (the extractor never produced the old fact, so
    nothing was there to supersede), or ``current_value_missing``.
``returned_count`` / ``top_score``
    For abstention instances: what the engine hands back when the honest answer
    is "I don't know".

Token coverage is a proxy for an answer being present, not an equivalent of the
paper's judge, and every reported number is labelled with the metric that
produced it. Run the engine used here on its own database (see ``--help``) so a
benchmark pass never writes into a production store.

Usage:
    python3 benchmarks/longmemeval/run_benchmark.py \\
        --engine http://localhost:8010 \\
        --memory-db ./benchmarks/data/bench_memory.db \\
        --limit 5
"""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from simulation.engine_client import EngineClient  # noqa: E402

DATA = ROOT / "benchmarks" / "data" / "longmemeval_oracle.json"
DEFAULT_OUT = ROOT / "benchmarks" / "longmemeval" / "results.json"

#: Words carrying no discriminating content for the coverage proxy.
STOPWORDS = {
    "a", "about", "after", "all", "also", "am", "an", "and", "any", "are", "as",
    "at", "be", "been", "before", "but", "by", "can", "did", "do", "does", "for",
    "from", "had", "has", "have", "he", "her", "him", "his", "how", "i", "if",
    "in", "is", "it", "its", "me", "my", "no", "not", "of", "on", "one", "or",
    "our", "out", "she", "so", "some", "still", "such", "than", "that", "the",
    "their", "them", "then", "there", "they", "this", "to", "up", "us", "was",
    "we", "were", "what", "when", "which", "who", "will", "with", "would", "you",
    "your",
}

#: Fraction of an answer's content tokens a memory must carry to count as
#: covering it. Deliberately the same 0.6 bar the simulation harness uses.
COVERAGE_THRESHOLD = 0.6


def tokens(text: Optional[str]) -> Set[str]:
    if not text:
        return set()
    raw = re.findall(r"[a-z0-9]+", str(text).lower())
    return {t for t in raw if t not in STOPWORDS and len(t) > 1}


def answer_variants(answer: Optional[str]) -> List[str]:
    """A gold answer and the alternate forms it offers.

    LongMemEval answers routinely carry an equivalent form in parentheses —
    ``"25 minutes and 50 seconds (or 25:50)"`` — and the engine stores whichever
    one the extractor produced. Scoring only the full string would count the
    stored ``25:50`` as a miss, which would be the harness's bug, not the
    engine's.
    """
    if not answer:
        return []
    text = str(answer)
    variants = [text]
    for match in re.findall(r"\(([^)]*)\)", text):
        cleaned = re.sub(r"^\s*(or|i\.e\.|e\.g\.)\s*", "", match, flags=re.I).strip()
        if cleaned:
            variants.append(cleaned)
    outside = re.sub(r"\([^)]*\)", " ", text).strip()
    if outside and outside not in variants:
        variants.append(outside)
    return variants


def coverage(target: Optional[str], candidate: Optional[str]) -> float:
    """Share of ``target``'s content tokens present in ``candidate``."""
    wanted = tokens(target)
    if not wanted:
        return 0.0
    return len(wanted & tokens(candidate)) / len(wanted)


def best_variant_coverage(
    answer: Optional[str], texts: Sequence[str]
) -> Tuple[float, Optional[str], Optional[str]]:
    """Best coverage over every gold variant: ``(score, matched text, variant)``."""
    best, best_text, best_variant = 0.0, None, None
    for variant in answer_variants(answer):
        score, text = best_coverage(variant, texts)
        if score > best:
            best, best_text, best_variant = score, text, variant
    return best, best_text, best_variant


def best_coverage(target: Optional[str], texts: Sequence[str]) -> Tuple[float, Optional[str]]:
    best, best_text = 0.0, None
    for text in texts:
        score = coverage(target, text)
        if score > best:
            best, best_text = score, text
    return best, best_text


def answer_bearing_turns(instance: Dict[str, Any]) -> List[Tuple[int, str]]:
    """``(session_index, content)`` for every turn flagged ``has_answer``."""
    found = []
    for session_index, session in enumerate(instance["haystack_sessions"]):
        for turn in session:
            if turn.get("has_answer"):
                found.append((session_index, turn.get("content", "")))
    return found


def user_turns(instance: Dict[str, Any]) -> List[Tuple[int, str]]:
    """Every user utterance, in session order — what a memory engine would see."""
    out = []
    for session_index, session in enumerate(instance["haystack_sessions"]):
        for turn in session:
            if turn.get("role") == "user" and turn.get("content"):
                out.append((session_index, turn["content"]))
    return out


def superseded_statement(instance: Dict[str, Any]) -> Optional[str]:
    """The answer-bearing turn from an earlier session than the last one.

    For a knowledge-update instance this is the value the engine is supposed to
    have replaced, so a memory covering it in the top k is a stale read.
    """
    bearing = answer_bearing_turns(instance)
    if len(bearing) < 2:
        return None
    latest_session = max(index for index, _ in bearing)
    earlier = [text for index, text in bearing if index < latest_session]
    return earlier[-1] if earlier else None


def store_state(memory_db: str, user_id: str) -> Dict[str, Any]:
    """What ``memory.db`` holds for this user, active and deactivated."""
    try:
        conn = sqlite3.connect(f"file:{memory_db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"available": False, "reason": str(exc)}
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT natural_key, subject, predicate, object_value, is_active "
            "FROM memory_keys WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        return {"available": False, "reason": str(exc)}
    finally:
        conn.close()

    active = [dict(r) for r in rows if r["is_active"]]
    inactive = [dict(r) for r in rows if not r["is_active"]]
    return {
        "available": True,
        "active": len(active),
        "inactive": len(inactive),
        "active_texts": [
            f"{r['subject']} {r['predicate']} {r['object_value']}".strip()
            for r in active
        ],
        "inactive_texts": [
            f"{r['subject']} {r['predicate']} {r['object_value']}".strip()
            for r in inactive
        ],
    }


def belief_state(
    answer_in_store: bool,
    stale_present: bool,
    stale_active: bool,
    stale_deactivated: bool,
) -> str:
    """Classify what the store did with a superseding update.

    A knowledge-update instance states a value, then restates it differently in a
    later session. Four outcomes are distinguishable at the store, and only one
    of them is the behaviour the v0.3.0 post claims:

    ``superseded``
        The current value is active and the old one is deactivated. Belief
        revision happened.
    ``both_active``
        Both values are active. The engine accumulated instead of replacing,
        which is the failure the post says does not occur.
    ``old_value_never_stored``
        The current value is active and the old one is nowhere, active or
        inactive. Nothing was superseded because the extractor never produced
        the old fact, so this instance does not test revision at all.
    ``current_value_missing``
        The current value is not in the store, so there is nothing to have
        revised to.
    """
    if not stale_present:
        return "no_superseded_statement_annotated"
    if not answer_in_store:
        return "current_value_missing"
    if stale_deactivated:
        return "superseded"
    if stale_active:
        return "both_active"
    return "old_value_never_stored"


async def run_instance(
    client: EngineClient,
    instance: Dict[str, Any],
    memory_db: str,
    top_k: int,
) -> Dict[str, Any]:
    """Ingest one instance's history, ask its question, measure what came back."""
    question_id = instance["question_id"]
    user_id = f"lme_{question_id}"
    turns = user_turns(instance)

    ingest_started = time.time()
    ingested, ingest_errors = 0, []
    for _, content in turns:
        try:
            await client.ingest(user_id=user_id, text=content)
            ingested += 1
        except Exception as exc:  # pragma: no cover - live engine
            ingest_errors.append(str(exc)[:200])
    ingest_seconds = round(time.time() - ingest_started, 3)

    retrieval = await client.retrieve(
        user_id=user_id, query=instance["question"], top_k=top_k
    )
    memories = getattr(retrieval, "memories", None) or []
    texts = [getattr(m, "text", "") or "" for m in memories]
    scores = [getattr(m, "score", None) for m in memories]

    gold = instance["answer"]
    stale = superseded_statement(instance)
    answer_cov, answer_text, answer_variant = best_variant_coverage(gold, texts)
    stale_cov, stale_text = best_coverage(stale, texts) if stale else (0.0, None)

    store = store_state(memory_db, user_id)
    store_texts = store.get("active_texts") or []
    inactive_texts = store.get("inactive_texts") or []
    answer_store_cov, _, _ = best_variant_coverage(gold, store_texts)
    stale_store_cov, _ = best_coverage(stale, store_texts) if stale else (0.0, None)
    stale_inactive_cov, _ = (
        best_coverage(stale, inactive_texts) if stale else (0.0, None)
    )

    answer_in_store = answer_store_cov >= COVERAGE_THRESHOLD
    stale_active = bool(stale) and stale_store_cov >= COVERAGE_THRESHOLD
    stale_deactivated = bool(stale) and stale_inactive_cov >= COVERAGE_THRESHOLD

    return {
        "question_id": question_id,
        "question_type": instance["question_type"],
        "abstention": question_id.endswith("_abs"),
        "user_id": user_id,
        "question": instance["question"],
        "gold_answer": gold,
        "superseded_statement": stale,
        "utterances_ingested": ingested,
        "utterances_total": len(turns),
        "ingest_errors": ingest_errors,
        "ingest_seconds": ingest_seconds,
        "returned_count": len(memories),
        "top_score": scores[0] if scores else None,
        "scores": scores,
        "retrieved_texts": texts,
        "answer_coverage": round(answer_cov, 3),
        "answer_supported": answer_cov >= COVERAGE_THRESHOLD,
        "answer_supporting_text": answer_text if answer_cov >= COVERAGE_THRESHOLD else None,
        "answer_variant_matched": answer_variant,
        "stale_coverage": round(stale_cov, 3),
        "stale_returned": bool(stale) and stale_cov >= COVERAGE_THRESHOLD,
        "stale_text": stale_text if stale and stale_cov >= COVERAGE_THRESHOLD else None,
        "answer_in_store": answer_in_store,
        "answer_store_coverage": round(answer_store_cov, 3),
        "stale_active_in_store": stale_active,
        "stale_deactivated_in_store": stale_deactivated,
        "belief_state": belief_state(
            answer_in_store=answer_in_store,
            stale_present=bool(stale),
            stale_active=stale_active,
            stale_deactivated=stale_deactivated,
        ),
        "store": store,
    }


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    def _group(predicate) -> List[Dict[str, Any]]:
        return [r for r in rows if predicate(r)]

    def _rate(subset: List[Dict[str, Any]], key: str) -> Optional[float]:
        if not subset:
            return None
        return round(sum(1 for r in subset if r.get(key)) / len(subset), 3)

    ku = _group(lambda r: r["question_type"] == "knowledge-update" and not r["abstention"])
    abstention = _group(lambda r: r["abstention"])

    ku_store = [r for r in ku if r["store"].get("available")]
    return {
        "instances": len(rows),
        "knowledge_update": {
            "instances": len(ku),
            "answer_supported_rate": _rate(ku, "answer_supported"),
            "stale_returned_rate": _rate(ku, "stale_returned"),
            "answer_in_store_rate": _rate(ku, "answer_in_store"),
            "belief_state_counts": dict(
                Counter(r.get("belief_state") for r in ku_store)
            ),
            "any_deactivated_row_rate": (
                round(
                    sum(1 for r in ku_store if r["store"]["inactive"] > 0)
                    / len(ku_store),
                    3,
                )
                if ku_store
                else None
            ),
            "mean_active_facts": (
                round(sum(r["store"]["active"] for r in ku_store) / len(ku_store), 2)
                if ku_store
                else None
            ),
            "metric_note": (
                "answer_supported and stale_returned are token-coverage proxies "
                f"at a {COVERAGE_THRESHOLD} bar, not the paper's LLM judge. "
                "belief_state_counts separates the engine failing to supersede "
                "from the extractor never producing the old fact"
            ),
        },
        "abstention": {
            "instances": len(abstention),
            "returned_something_rate": (
                round(
                    sum(1 for r in abstention if r["returned_count"] > 0)
                    / len(abstention),
                    3,
                )
                if abstention
                else None
            ),
            "mean_returned_count": (
                round(
                    sum(r["returned_count"] for r in abstention) / len(abstention), 2
                )
                if abstention
                else None
            ),
            "metric_note": (
                "the engine has no abstention path: /retrieve ranks and returns "
                "top_k with no confidence floor, so a non-zero rate here is the "
                "expected result, and it is the gap being measured"
            ),
        },
    }


def select(
    instances: List[Dict[str, Any]],
    question_types: Sequence[str],
    include_abstention: bool,
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    chosen: List[Dict[str, Any]] = []
    for instance in instances:
        is_abs = instance["question_id"].endswith("_abs")
        if is_abs and not include_abstention:
            continue
        if not is_abs and question_types and instance["question_type"] not in question_types:
            continue
        chosen.append(instance)
    if limit is not None:
        # Take from each bucket so a small --limit still covers both abilities.
        abs_rows = [i for i in chosen if i["question_id"].endswith("_abs")][:limit]
        other = [i for i in chosen if not i["question_id"].endswith("_abs")][:limit]
        chosen = other + abs_rows
    return chosen


async def main_async(args: argparse.Namespace) -> int:
    if not DATA.exists():
        print(
            f"{DATA} not found. Fetch it with:\n"
            "  curl -sL -o benchmarks/data/longmemeval_oracle.json \\\n"
            "    https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/"
            "resolve/main/longmemeval_oracle.json"
        )
        return 1

    instances = json.loads(DATA.read_text())
    selected = select(
        instances,
        question_types=[t.strip() for t in args.question_types.split(",") if t.strip()],
        include_abstention=not args.no_abstention,
        limit=args.limit,
    )
    print(f"{len(selected)} instances selected out of {len(instances)}")

    rows: List[Dict[str, Any]] = []
    started = time.time()
    async with EngineClient(base_url=args.engine, timeout=args.timeout) as client:
        for position, instance in enumerate(selected, start=1):
            row = await run_instance(client, instance, args.memory_db, args.top_k)
            rows.append(row)
            print(
                f"[{position}/{len(selected)}] {row['question_id']} "
                f"type={row['question_type']}{' abs' if row['abstention'] else ''} "
                f"ingested={row['utterances_ingested']}/{row['utterances_total']} "
                f"answer_cov={row['answer_coverage']} "
                f"stale_cov={row['stale_coverage']} "
                f"returned={row['returned_count']} "
                f"store_active={row['store'].get('active')} "
                f"store_inactive={row['store'].get('inactive')}"
            )

    report = {
        "generated_at": time.time(),
        "wall_clock_seconds": round(time.time() - started, 1),
        "benchmark": {
            "name": "LongMemEval (oracle split)",
            "source": "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned",
            "paper": "arXiv 2410.10813 (ICLR 2025)",
            "file": str(DATA.relative_to(ROOT)),
            "instances_available": len(instances),
        },
        "engine": {
            "base_url": args.engine,
            "memory_db": args.memory_db,
            "top_k": args.top_k,
        },
        "metrics": {
            "coverage_threshold": COVERAGE_THRESHOLD,
            "judge": "none — token coverage proxy, not the paper's LLM judge",
        },
        "summary": summarise(rows),
        "instances": rows,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report["summary"], indent=2))
    print(f"wrote {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine",
        default="http://localhost:8010",
        help="engine base URL; use an instance with its own database",
    )
    parser.add_argument(
        "--memory-db",
        default=str(ROOT / "benchmarks" / "data" / "bench_memory.db"),
        help="the memory.db that engine writes to, read for store-level metrics",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--question-types",
        default="knowledge-update",
        help="comma-separated LongMemEval question_type values",
    )
    parser.add_argument(
        "--no-abstention", action="store_true", help="skip the _abs instances"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="instances per bucket (answerable, _abs)"
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
