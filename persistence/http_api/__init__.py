"""Read-only SQLite dashboard API with explicitly opt-in frontend example mode."""
from persistence.http_api.app import create_app

__all__ = ["create_app"]
