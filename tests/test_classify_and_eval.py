"""Tests for classifier normalization, Jev HTTP client, and A/B eval harness."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import httpx

from sieve.classify import (
    ClassificationResult,
    ClassifyInput,
    JevClassifier,
    detect_indonesian_targeting,
    normalize_indonesian_slang,
)
from sieve.eval import run_ab_eval
from sieve.store import SieveStore


class _StubClassifier:
    def __init__(self, classifier_id: str, fail_on_benign: bool = False) -> None:
        self.classifier_id = classifier_id
        self.fail_on_benign = fail_on_benign

    def classify_batch(self, items: Sequence[ClassifyInput]) -> list[ClassificationResult]:
        out: list[ClassificationResult] = []
        for item in items:
            text = normalize_indonesian_slang(f"{item.title} {item.markdown}").lower()
            if "slot" in text or "togel" in text or "casino" in text:
                cat = "judol"
            elif "pinjol" in text or "pinjaman" in text or "payday" in text:
                cat = "pinjol"
            elif "otp" in text or "phishing" in text or "seed phrase" in text:
                cat = "phishing_scam"
            elif "porn" in text or "bokep" in text or "adult" in text:
                cat = "adult_ilegal"
            else:
                cat = "benign"
            if self.fail_on_benign and "komdigi" in text:
                cat = "judol"
            out.append(
                ClassificationResult(
                    category=cat,
                    is_id_targeted=detect_indonesian_targeting(item.url, text),
                    confidence=0.9,
                    classifier_id=self.classifier_id,
                    latency_ms=12.0,
                )
            )
        return out


def test_slang_normalization_and_id_targeting() -> None:
    norm = normalize_indonesian_slang(
        "Situs jud0l g4c0r m4xw1n anti rungkad bo togel depo QRIS open bo galbay"
    )
    assert "judi online" in norm
    assert "gacor slot judi" in norm
    assert "maxwin jackpot slot judi" in norm
    assert "kalah slot judi online" in norm
    assert "bandar online judi" in norm
    assert "layanan prostitusi dewasa" in norm
    assert "gagal bayar pinjaman online" in norm
    assert detect_indonesian_targeting("https://slot88.xyz", norm) is True
    # English deposit/withdraw should not falsely trigger Indonesian targeting
    assert (
        detect_indonesian_targeting(
            "https://royalspin.bet",
            "Play slots and table games. Instant USD deposit and withdraw.",
        )
        is False
    )


def test_jev_classifier_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/decisions"
        body = json.loads(request.content)
        assert body["model"] == "jev-1.14.0"
        assert "judol" in body["options"]
        return httpx.Response(
            200,
            json={
                "choice": "judol",
                "probabilities": {"judol": 0.94, "benign": 0.06},
                "usage": {"input_tokens": 120},
            },
        )

    transport = httpx.MockTransport(handler)
    clf = JevClassifier(api_key="test-key", transport=transport)
    res = clf.classify_batch([ClassifyInput(url="https://slot.xyz", markdown="Slot gacor QRIS")])
    assert len(res) == 1
    assert res[0].category == "judol"
    assert res[0].confidence == 0.94
    assert res[0].is_id_targeted is True
    assert res[0].est_cost_usd > 0
    clf.close()


def test_ab_eval_writes_report_and_disagreements(tmp_path: Path) -> None:
    dataset = Path("data/eval/benchmark_set.jsonl")
    reports_dir = tmp_path / "reports"
    with SieveStore(tmp_path / "sieve.db") as store:
        summary = run_ab_eval(
            dataset_path=dataset,
            reports_dir=reports_dir,
            classifiers=[_StubClassifier("stub-a"), _StubClassifier("stub-b", fail_on_benign=True)],
            store=store,
        )
        assert Path(summary["report_md"]).is_file()
        assert Path(summary["disagreements_csv"]).is_file()
        assert len(summary["metrics"]) == 2
