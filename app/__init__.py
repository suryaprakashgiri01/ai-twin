"""ai-twin API package."""

import os

# Settings reads `.env` itself (pydantic-settings, `dotenv_values`). Some libraries call
# `dotenv.load_dotenv()` on import (magika, via markitdown) and would copy every `.env` value,
# provider keys included, into `os.environ`, overriding what callers configured. Turn that off.
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
