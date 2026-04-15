from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    payout_interval_minutes: int = 10
    payout_decimals: int = 8
    pool_api_base_url: str = ""
    pool_api_key: str = ""
    translator_metrics_url: str = "http://127.0.0.1:9092/metrics"
    db_path: str = "./payouts.db"
    dry_run: bool = True


def load_settings() -> Settings:
    return Settings(
        payout_interval_minutes=int(os.getenv("PAYOUT_INTERVAL_MINUTES", "10")),
        payout_decimals=int(os.getenv("PAYOUT_DECIMALS", "8")),
        pool_api_base_url=os.getenv("POOL_API_BASE_URL", ""),
        pool_api_key=os.getenv("POOL_API_KEY", ""),
        translator_metrics_url=os.getenv("TRANSLATOR_METRICS_URL", "http://127.0.0.1:9092/metrics"),
        db_path=os.getenv("DB_PATH", "./payouts.db"),
        dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
    )
