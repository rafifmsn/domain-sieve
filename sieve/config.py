"""Configuration and environment loading for domain-sieve."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

WIB = ZoneInfo("Asia/Jakarta")

CATEGORIES = (
    "judol",
    "pinjol",
    "phishing_scam",
    "adult_ilegal",
    "other_ilegal",
    "benign",
)

ILLEGAL_CATEGORIES = (
    "judol",
    "pinjol",
    "phishing_scam",
    "adult_ilegal",
    "other_ilegal",
)


def _load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from environment variables and CLI overrides."""

    data_dir: Path
    certstream_url: str
    classifier_backend: str
    nli_model_id: str
    typesafe_api_key: str
    typesafe_base_url: str
    deepseek_base_url: str
    deepseek_api_key: str
    deepseek_model: str
    doh_enabled: bool = False
    crawl_concurrency: int = 14
    memory_threshold_pct: float = 70.0
    nli_batch_size: int = 16
    export_threshold: float = 0.70

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sieve.db"

    @property
    def scraped_dir(self) -> Path:
        return self.data_dir / "scraped"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def eval_dir(self) -> Path:
        return self.data_dir / "eval"

    @property
    def dns_mode(self) -> str:
        return "doh" if self.doh_enabled else "isp"

    @classmethod
    def from_env(
        cls,
        *,
        data_dir: Path | None = None,
        doh_enabled: bool = False,
        classifier_backend: str | None = None,
    ) -> Settings:
        _load_dotenv(Path.cwd() / ".env")
        resolved_data = data_dir or Path(os.environ.get("DATA_DIR", "data"))
        resolved_backend = classifier_backend or os.environ.get("SIEVE_CLASSIFIER", "nli")
        return cls(
            data_dir=resolved_data,
            certstream_url=os.environ.get("CERTSTREAM_URL", "ws://127.0.0.1:8080/"),
            classifier_backend=resolved_backend,
            nli_model_id=os.environ.get("NLI_MODEL_ID", "MoritzLaurer/bge-m3-zeroshot-v2.0"),
            typesafe_api_key=os.environ.get("TYPESAFE_API_KEY", ""),
            typesafe_base_url=os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai/v1"),
            deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.netra.ai/v1"),
            deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            deepseek_model=os.environ.get("DEEPSEEK_MODEL", "deepseek-uncensored"),
            doh_enabled=doh_enabled,
        )
