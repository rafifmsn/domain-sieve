"""A/B evaluation harness comparing classifier backends on labeled benchmark_set.jsonl."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sieve.classify import ClassificationResult, Classifier, ClassifyInput, build_classifier
from sieve.config import CATEGORIES, ILLEGAL_CATEGORIES, Settings
from sieve.store import SieveStore, now_wib_iso


@dataclass(frozen=True, slots=True)
class EvalSample:
    """Single labeled evaluation sample."""

    sample_id: str
    url: str
    title: str
    markdown: str
    label: str
    is_id_targeted: bool
    notes: str = ""


@dataclass(frozen=True, slots=True)
class EvalMetrics:
    """Summary metrics for one classifier run on the benchmark set."""

    classifier_id: str
    total: int
    precision: float
    recall: float
    f1: float
    macro_f1: float
    accuracy: float
    p50_ms: float
    p95_ms: float
    est_cost_usd: float


def load_benchmark_set(path: Path) -> list[EvalSample]:
    """Load JSONL evaluation samples."""
    samples: list[EvalSample] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        raw = json.loads(stripped)
        samples.append(
            EvalSample(
                sample_id=str(raw["id"]),
                url=str(raw["url"]),
                title=str(raw.get("title", "")),
                markdown=str(raw["markdown"]),
                label=str(raw["label"]),
                is_id_targeted=bool(raw.get("is_id_targeted", False)),
                notes=str(raw.get("notes", "")),
            )
        )
    return samples


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[idx]


def compute_eval_metrics(
    classifier_id: str,
    samples: list[EvalSample],
    predictions: list[ClassificationResult],
) -> EvalMetrics:
    """Compute binary illegal-vs-benign precision/recall/F1, macro F1, and latency percentiles."""
    tp = fp = fn = exact_matches = 0
    per_cat_tp = {c: 0 for c in CATEGORIES}
    per_cat_fp = {c: 0 for c in CATEGORIES}
    per_cat_fn = {c: 0 for c in CATEGORIES}
    latencies: list[float] = []
    total_cost = 0.0

    for sample, pred in zip(samples, predictions, strict=True):
        gold_illegal = sample.label in ILLEGAL_CATEGORIES
        pred_illegal = pred.category in ILLEGAL_CATEGORIES
        if gold_illegal and pred_illegal:
            tp += 1
        elif not gold_illegal and pred_illegal:
            fp += 1
        elif gold_illegal and not pred_illegal:
            fn += 1

        if sample.label == pred.category:
            exact_matches += 1
            per_cat_tp[sample.label] = per_cat_tp.get(sample.label, 0) + 1
        else:
            per_cat_fp[pred.category] = per_cat_fp.get(pred.category, 0) + 1
            per_cat_fn[sample.label] = per_cat_fn.get(sample.label, 0) + 1

        latencies.append(pred.latency_ms)
        total_cost += pred.est_cost_usd

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    accuracy = exact_matches / len(samples) if samples else 0.0

    cat_f1s: list[float] = []
    for cat in CATEGORIES:
        c_tp = per_cat_tp[cat]
        c_fp = per_cat_fp[cat]
        c_fn = per_cat_fn[cat]
        if c_tp + c_fp + c_fn == 0:
            continue
        c_prec = c_tp / (c_tp + c_fp) if (c_tp + c_fp) > 0 else 0.0
        c_rec = c_tp / (c_tp + c_fn) if (c_tp + c_fn) > 0 else 0.0
        c_f1 = (2 * c_prec * c_rec / (c_prec + c_rec)) if (c_prec + c_rec) > 0 else 0.0
        cat_f1s.append(c_f1)
    macro_f1 = statistics.mean(cat_f1s) if cat_f1s else 0.0

    return EvalMetrics(
        classifier_id=classifier_id,
        total=len(samples),
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        macro_f1=round(macro_f1, 4),
        accuracy=round(accuracy, 4),
        p50_ms=round(_percentile(latencies, 50), 2),
        p95_ms=round(_percentile(latencies, 95), 2),
        est_cost_usd=round(total_cost, 6),
    )


def run_ab_eval(
    *,
    dataset_path: Path,
    reports_dir: Path,
    classifiers: list[Classifier],
    store: SieveStore | None = None,
) -> dict[str, Any]:
    """Evaluate one or more classifiers on benchmark_set.jsonl and write reports."""
    samples = load_benchmark_set(dataset_path)
    inputs = [ClassifyInput(url=s.url, markdown=s.markdown, title=s.title) for s in samples]
    reports_dir.mkdir(parents=True, exist_ok=True)

    all_metrics: list[EvalMetrics] = []
    preds_by_id: dict[str, list[ClassificationResult]] = {}

    for clf in classifiers:
        preds = clf.classify_batch(inputs)
        metrics = compute_eval_metrics(clf.classifier_id, samples, preds)
        all_metrics.append(metrics)
        preds_by_id[clf.classifier_id] = preds

        if store is not None:
            store.record_eval_run(
                eval_id=f"eval-{clf.classifier_id}-{uuid.uuid4().hex[:8]}",
                dataset_path=str(dataset_path),
                classifier_id=clf.classifier_id,
                metrics={
                    "precision": metrics.precision,
                    "recall": metrics.recall,
                    "f1": metrics.f1,
                    "macro_f1": metrics.macro_f1,
                    "accuracy": metrics.accuracy,
                    "p50_ms": metrics.p50_ms,
                    "p95_ms": metrics.p95_ms,
                    "est_cost_usd": metrics.est_cost_usd,
                },
            )

    stamp = now_wib_iso().replace(":", "-")
    disagreements_csv = reports_dir / f"disagreements_{stamp}.csv"
    with disagreements_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        clf_ids = [m.classifier_id for m in all_metrics]
        header = ["id", "url", "gold_label", *[f"{cid}_pred" for cid in clf_ids], *[f"{cid}_conf" for cid in clf_ids]]
        writer.writerow(header)
        for idx, sample in enumerate(samples):
            row_preds = [preds_by_id[cid][idx] for cid in clf_ids]
            unique_cats = {p.category for p in row_preds}
            if len(unique_cats) > 1 or any(p.category != sample.label for p in row_preds):
                writer.writerow(
                    [
                        sample.sample_id,
                        sample.url,
                        sample.label,
                        *[p.category for p in row_preds],
                        *[p.confidence for p in row_preds],
                    ]
                )

    report_md = reports_dir / f"ab_report_{stamp}.md"
    lines = [
        "# Classifier A/B Evaluation Report",
        "",
        f"Dataset: `{dataset_path}` ({len(samples)} samples)",
        "",
        "| Classifier | Precision | Recall | Binary F1 | Macro F1 | Accuracy | p50 (ms) | p95 (ms) | Est. Cost (USD) |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for m in all_metrics:
        lines.append(
            f"| `{m.classifier_id}` | {m.precision:.4f} | {m.recall:.4f} | {m.f1:.4f} | "
            f"{m.macro_f1:.4f} | {m.accuracy:.4f} | {m.p50_ms:.2f} | {m.p95_ms:.2f} | ${m.est_cost_usd:.6f} |"
        )
    lines.append("")
    lines.append(f"Disagreements CSV: `{disagreements_csv}`")
    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "report_md": str(report_md),
        "disagreements_csv": str(disagreements_csv),
        "metrics": all_metrics,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run A/B classifier evaluation")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("data/eval/benchmark_set.jsonl"),
        help="Path to labeled benchmark JSONL",
    )
    parser.add_argument(
        "--backends",
        nargs="+",
        default=["nli"],
        choices=["nli", "jev"],
        help="Classifier backends to benchmark",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    classifiers: list[Classifier] = []
    for backend in args.backends:
        if backend == "jev" and not settings.typesafe_api_key:
            raise SystemExit("TYPESAFE_API_KEY is not set; pass --backends nli or configure .env")
        classifiers.append(
            build_classifier(
                backend,
                nli_model_id=settings.nli_model_id,
                nli_batch_size=settings.nli_batch_size,
                typesafe_api_key=settings.typesafe_api_key,
                typesafe_base_url=settings.typesafe_base_url,
            )
        )

    with SieveStore(settings.db_path) as store:
        result = run_ab_eval(
            dataset_path=args.dataset,
            reports_dir=settings.eval_dir / "reports",
            classifiers=classifiers,
            store=store,
        )
    print(f"Report written to {result['report_md']}")
    for m in result["metrics"]:
        print(
            f"{m.classifier_id}: P={m.precision:.4f} R={m.recall:.4f} F1={m.f1:.4f} "
            f"MacroF1={m.macro_f1:.4f} p50={m.p50_ms}ms p95={m.p95_ms}ms cost=${m.est_cost_usd:.6f}"
        )


if __name__ == "__main__":
    main()
