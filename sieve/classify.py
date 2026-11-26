"""Text-only single-classifier interface with NLI GPU and Jev API backends."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from sieve.config import CATEGORIES

logger = logging.getLogger(__name__)

CATEGORY_HYPOTHESES: dict[str, str] = {
    "judol": (
        "This webpage operates online gambling, slot games, casino betting, "
        "or togel lottery."
    ),
    "pinjol": (
        "This webpage offers online cash loans, payday lending, "
        "or fast money borrowing to visitors."
    ),
    "phishing_scam": (
        "This webpage performs account verification phishing, credential theft, "
        "or an advance-fee giveaway scam."
    ),
    "adult_ilegal": (
        "This webpage distributes adult pornography, explicit sex videos, "
        "or escort booking services."
    ),
    "other_ilegal": (
        "This webpage sells controlled drugs without a prescription, counterfeit diplomas or IDs, "
        "or stolen accounts and bank logs."
    ),
    "benign": (
        "This webpage is a legitimate news article, government advisory, encyclopedia, "
        "academic paper, technical documentation, organization homepage, retail store, "
        "blog, or parked domain."
    ),
}

_SLANG_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Judol / gambling obfuscations & jargon
    (re.compile(r"\b(?:jud0l|judol|judi0nline)\b", re.IGNORECASE), "judi online judol"),
    (re.compile(r"\b(?:sl0t|5lot|sl\*t)\b", re.IGNORECASE), "slot"),
    (re.compile(r"\b(?:g4c0r|gac0r|g4cor|gacor)\b", re.IGNORECASE), "gacor slot judi"),
    (re.compile(r"\b(?:m4xw1n|maxw1n|maxwin)\b", re.IGNORECASE), "maxwin jackpot slot judi"),
    (re.compile(r"\b(?:t0gel|t0g3l|togel)\b", re.IGNORECASE), "togel lotere judi"),
    (re.compile(r"\b(?:sc4tter|scater)\b", re.IGNORECASE), "scatter slot judi"),
    (re.compile(r"\bdepo\b", re.IGNORECASE), "deposit"),
    (re.compile(r"\bwd\b", re.IGNORECASE), "withdraw"),
    (
        re.compile(r"\b(?:rtp\s+live|rtp\s+slot|bocoran\s+rtp|pola\s+gacor|pola\s+slot)\b", re.IGNORECASE),
        "return to player slot judi",
    ),
    (re.compile(r"\b(?:anti\s+rungkad|rungkad)\b", re.IGNORECASE), "kalah slot judi online"),
    (re.compile(r"\b(?:anti\s+nawala|nawala)\b", re.IGNORECASE), "blokir situs judi online"),
    (re.compile(r"\bbo\s+(?:togel|slot|resmi|terpercaya)\b", re.IGNORECASE), "bandar online judi"),
    (re.compile(r"\b(?:jp\s+paus|jackpot\s+paus|\bjp\b)\b", re.IGNORECASE), "jackpot menang judi"),
    (
        re.compile(r"\b(?:mix\s+parlay|parlay|sbobet|bandar\s+bola)\b", re.IGNORECASE),
        "taruhan judi bola",
    ),
    (
        re.compile(r"\b(?:pragmatic\s+play|pg\s+soft|habanero|spadegaming|joker123)\b", re.IGNORECASE),
        "provider slot judi",
    ),
    (
        re.compile(r"\b(?:pkv\s+games|idn\s+poker|bandarq|dominoqq|ceme\s+online)\b", re.IGNORECASE),
        "judi poker kartu online",
    ),
    (re.compile(r"\b(?:sabung\s+ayam|s128|sv388)\b", re.IGNORECASE), "judi sabung ayam"),
    (
        re.compile(r"\b(?:colok\s+bebas|colok\s+jitu|paito\s+warna|syair\s+hk|live\s+draw\s+(?:hk|sgp|sdy)|toto\s+macau)\b", re.IGNORECASE),
        "lotere togel judi",
    ),
    (
        re.compile(r"\b(?:garansi\s+kekalahan|bonus\s+new\s+member|bonus\s+rollingan)\b", re.IGNORECASE),
        "bonus taruhan judi",
    ),
    (re.compile(r"\bpulsa\s+tanpa\s+potongan\b", re.IGNORECASE), "deposit pulsa judi"),
    # Pinjol / illegal lending obfuscations
    (re.compile(r"\b(?:p1njol|pinjol)\b", re.IGNORECASE), "pinjaman online pinjol"),
    (re.compile(r"\b(?:galbay|gagal\s+bayar|dc\s+lapangan)\b", re.IGNORECASE), "gagal bayar pinjaman online"),
    (
        re.compile(r"\b(?:dana\s+talangan|uang\s+kilat|cair\s+kilat|langsung\s+cair|tanpa\s+bi\s+checking|tanpa\s+slik)\b", re.IGNORECASE),
        "pinjaman uang tunai cepat cair",
    ),
    # Phishing / scam obfuscations
    (re.compile(r"\bd[a4]na\s+kaget\b", re.IGNORECASE), "penipuan saldo dana kaget phishing"),
    (
        re.compile(r"\b(?:titip\s+dana|profit\s+pasti|garansi\s+profit)\b", re.IGNORECASE),
        "penipuan investasi profit palsu",
    ),
    # Adult / escort obfuscations & Indonesian slang (pure ID expansions)
    (re.compile(r"\b(?:b0kep|b\*kep)\b", re.IGNORECASE), "pornografi video dewasa"),
    (
        re.compile(r"\b(?:open\s+bo|cewek\s+bo|cewe\s+bo|cewek\s+panggilan|pijat\s+plus|video\s+call\s+sex|\bvcs\b)\b", re.IGNORECASE),
        "layanan prostitusi dewasa",
    ),
    (
        re.compile(r"\b(?:colmek|ngewe|ngentot|memek|kontol|toket|tobrut|nekopoi)\b", re.IGNORECASE),
        "konten pornografi eksplisit dewasa",
    ),
    # Other illegal obfuscations
    (
        re.compile(r"\b(?:obat\s+keras|daftar\s+g|pil\s+penggugur|obat\s+aborsi)\b", re.IGNORECASE),
        "penjualan obat keras ilegal",
    ),
    (
        re.compile(r"\b(?:ijazah\s+palsu|ijazah\s+aspal|akun\s+curian|stealer\s+logs|bank\s+logs|cvv\s+dumps)\b", re.IGNORECASE),
        "penjualan dokumen palsu data curian ilegal",
    ),
)

_ID_TARGET_RE = re.compile(
    r"(?:\+62|\b628\d{6,}|\bqris\b|\bgopay\b|\bovo\b|\bshopeepay\b|\blinkaja\b|"
    r"\b(?:via|saldo|ke)\s+dana\b|\bdana\s+kaget\b|\bmandiri\b|\bbca\b|\bbri\b|\bbni\b|\bbsi\b|"
    r"\bbrimo\b|\bmybca\b|\blivin\b|\brupiah\b|\brp\.?\s*\d|\b\d+\s*(?:rb|ribu|jt|juta)\b|"
    r"\bpulsa\b|\btelkomsel\b|\bdaftar\s+sekarang\b|\bminimal\s+depo(?:sit)?\b|\bwd\s+(?:kilat|cepat)\b|"
    r"\bgacor\b|\bmaxwin\b|\btogel\b|\bjudol\b|\bjudi\b|\brungkad\b|\bnawala\b|\bpinjol\b|"
    r"\bpinjaman\b|\bgalbay\b|\bbokep\b|\bopen\s+bo\b|\bsub\s+indo\b|\bojk\b|\bkomdigi\b|\bkominfo\b|"
    r"\bindonesia\b|\bjakarta\b|\bsurabaya\b|\bbandung\b|\bmedan\b|\bsitus\s+resmi\b|"
    r"\blink\s+alternatif\b|\bmasuk\s+akun\b|\bkredit\s+cepat\b|\bcair\s+cepat\b|"
    r"\btanpa\s+(?:agunan|jaminan|potongan|ktp|survey)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ClassifyInput:
    """Normalized page input for classification."""

    url: str
    markdown: str
    title: str = ""


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Calibrated classifier verdict."""

    category: str
    is_id_targeted: bool
    confidence: float
    classifier_id: str
    latency_ms: float
    est_cost_usd: float = 0.0


def normalize_indonesian_slang(text: str) -> str:
    """Expand obfuscated Indonesian gambling/lending/adult slang prior to NLI."""
    out = text
    for pattern, repl in _SLANG_REPLACEMENTS:
        out = pattern.sub(repl, out)
    return out


def detect_indonesian_targeting(url: str, text: str) -> bool:
    """Detect whether a page targets Indonesian users via TLD, currency, banks, or copy."""
    lower_url = url.lower()
    if lower_url.endswith(".id") or ".id/" in lower_url:
        return True
    matches = _ID_TARGET_RE.findall(text)
    return len(matches) >= 1


class Classifier(Protocol):
    """Single swappable zero-shot classifier protocol."""

    classifier_id: str

    def classify_batch(self, items: Sequence[ClassifyInput]) -> list[ClassificationResult]: ...


class NliClassifier:
    """Local FP16 NLI zero-shot classifier using MoritzLaurer/bge-m3-zeroshot-v2.0."""

    def __init__(
        self,
        model_id: str = "MoritzLaurer/bge-m3-zeroshot-v2.0",
        batch_size: int = 16,
        device: str | None = None,
        max_length: int = 512,
    ) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.model_id = model_id
        self.classifier_id = model_id.split("/")[-1]
        self.batch_size = batch_size
        self.max_length = max_length
        self._torch = torch

        if device is not None:
            self.device = device
        elif torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"

        self._dtype = torch.float16 if self.device.startswith("cuda") else torch.float32
        logger.info("Loading NLI model %s on %s (%s)", model_id, self.device, self._dtype)
        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            model_id,
            dtype=self._dtype,
        )
        self.model.to(self.device)
        self.model.eval()

        label2id = {str(k).lower(): int(v) for k, v in self.model.config.label2id.items()}
        self._entailment_idx = label2id.get("entailment", 0)
        self._non_entailment_idx = next(
            (idx for name, idx in label2id.items() if name != "entailment"),
            1,
        )

    def _score_pairs(self, premises: list[str], hypotheses: list[str]) -> list[float]:
        torch = self._torch
        scores: list[float] = []
        with torch.inference_mode():
            for start in range(0, len(premises), self.batch_size):
                batch_p = premises[start : start + self.batch_size]
                batch_h = hypotheses[start : start + self.batch_size]
                encoded = self.tokenizer(
                    batch_p,
                    batch_h,
                    truncation=True,
                    max_length=self.max_length,
                    padding=True,
                    return_tensors="pt",
                )
                encoded = {k: v.to(self.device) for k, v in encoded.items()}
                logits = self.model(**encoded).logits.float()
                if logits.shape[-1] == 1:
                    scores.extend(float(x) for x in logits[:, 0].detach().cpu().tolist())
                elif logits.shape[-1] == 2:
                    log_odds = logits[:, self._entailment_idx] - logits[:, self._non_entailment_idx]
                    scores.extend(float(x) for x in log_odds.detach().cpu().tolist())
                else:
                    contradiction_idx = next(
                        (
                            idx
                            for name, idx in self.model.config.label2id.items()
                            if "contradiction" in str(name).lower()
                        ),
                        self._non_entailment_idx,
                    )
                    log_odds = logits[:, self._entailment_idx] - logits[:, contradiction_idx]
                    scores.extend(float(x) for x in log_odds.detach().cpu().tolist())
        return scores

    def classify_batch(self, items: Sequence[ClassifyInput]) -> list[ClassificationResult]:
        if not items:
            return []
        t0 = time.perf_counter()
        categories = list(CATEGORIES)
        hypotheses_list = [CATEGORY_HYPOTHESES[cat] for cat in categories]

        premises: list[str] = []
        hypotheses: list[str] = []
        normalized_texts: list[str] = []

        for item in items:
            combined = f"{item.title}\n{item.markdown}".strip()
            norm = normalize_indonesian_slang(combined)
            normalized_texts.append(norm)
            for hyp in hypotheses_list:
                premises.append(norm)
                hypotheses.append(hyp)

        raw_log_odds = self._score_pairs(premises, hypotheses)
        total_ms = (time.perf_counter() - t0) * 1000.0
        per_item_ms = total_ms / len(items)

        torch = self._torch
        results: list[ClassificationResult] = []
        num_cats = len(categories)
        for idx, item in enumerate(items):
            lo_vec = torch.tensor(raw_log_odds[idx * num_cats : (idx + 1) * num_cats])
            probs = torch.softmax(lo_vec, dim=0).tolist()
            best_pos = max(range(num_cats), key=lambda i: probs[i])
            best_cat = categories[best_pos]
            confidence = round(float(probs[best_pos]), 6)
            results.append(
                ClassificationResult(
                    category=best_cat,
                    is_id_targeted=detect_indonesian_targeting(item.url, normalized_texts[idx]),
                    confidence=confidence,
                    classifier_id=self.classifier_id,
                    latency_ms=round(per_item_ms, 2),
                    est_cost_usd=0.0,
                )
            )
        return results


class JevClassifier:
    """TypeSafe jev-1.14.0 HTTP API zero-shot decision client."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.typesafe.ai/v1",
        model: str = "jev-1.14.0",
        timeout_s: float = 15.0,
        price_per_1m_input_tokens: float = 0.20,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("TYPESAFE_API_KEY is required for JevClassifier")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.classifier_id = model
        self.timeout_s = timeout_s
        self.price_per_1m_input_tokens = price_per_1m_input_tokens
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _classify_one(self, item: ClassifyInput) -> ClassificationResult:
        t0 = time.perf_counter()
        text = normalize_indonesian_slang(f"{item.title}\n{item.markdown}".strip())[:4000]
        payload: dict[str, Any] = {
            "model": self.classifier_id,
            "input": f"URL: {item.url}\n\n{text}",
            "instruction": (
                "Classify this website content into the single best matching enforcement category "
                "for Indonesian illegal-web triage."
            ),
            "options": list(CATEGORIES),
        }
        resp = self._client.post("/decisions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        latency_ms = (time.perf_counter() - t0) * 1000.0

        choice = str(data.get("choice") or data.get("decision") or "benign")
        if choice not in CATEGORIES:
            choice = "benign"

        probs = data.get("probabilities") or {}
        if isinstance(probs, dict) and choice in probs:
            confidence = float(probs[choice])
        else:
            confidence = float(data.get("confidence", 0.5))

        usage = data.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        est_cost = (input_tokens / 1_000_000.0) * self.price_per_1m_input_tokens

        return ClassificationResult(
            category=choice,
            is_id_targeted=detect_indonesian_targeting(item.url, text),
            confidence=round(confidence, 6),
            classifier_id=self.classifier_id,
            latency_ms=round(latency_ms, 2),
            est_cost_usd=round(est_cost, 8),
        )

    def classify_batch(self, items: Sequence[ClassifyInput]) -> list[ClassificationResult]:
        return [self._classify_one(item) for item in items]


def build_classifier(
    backend: str,
    *,
    nli_model_id: str = "MoritzLaurer/bge-m3-zeroshot-v2.0",
    nli_batch_size: int = 16,
    typesafe_api_key: str = "",
    typesafe_base_url: str = "https://api.typesafe.ai/v1",
) -> Classifier:
    """Instantiate the configured single classifier backend."""
    normalized = backend.strip().lower()
    if normalized == "nli":
        return NliClassifier(model_id=nli_model_id, batch_size=nli_batch_size)
    if normalized == "jev":
        return JevClassifier(api_key=typesafe_api_key, base_url=typesafe_base_url)
    raise ValueError(f"Unsupported classifier backend: {backend!r} (expected 'nli' or 'jev')")
