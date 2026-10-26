import argparse
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, cast

import requests
from dotenv import load_dotenv

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Free models available on OpenRouter.
# Availability can change; override with --model if needed.
DEFAULT_MODEL = "openrouter/free"

FREE_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "thinkingmachines/inkling-small:free",
    "cohere/north-mini-code:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3.5-lightning:free",
    "poolside/laguna-s-2.1:free",
    "openai/gpt-oss-20b:free",
    "openrouter/free",
]

FREE_CODING_MODELS = [
    # Coding agents and repository-level tasks
    "poolside/laguna-s-2.1:free",
    "cohere/north-mini-code:free",
    "poolside/laguna-xs-2.1:free",
    # General coding, debugging, and reasoning
    "qwen/qwen3.8-27b:free",
    "thinkingmachines/inkling-small:free",
    "nvidia/nemotron-3.5-lightning:free",
    "recursive/flint-v1.0:free",
    # Automatic free-model selection
    "openrouter/free",
]


def chat(
    message: str,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    timeout: int = 60,
) -> str:
    """
    Send a message to OpenRouter and return the assistant's response.

    Args:
        message: User prompt.
        model: OpenRouter model ID.
        api_key: API key; defaults to OPENROUTER_API_KEY.
        timeout: Request timeout in seconds.

    Returns:
        Assistant's response text.

    Raises:
        ValueError: If the API key is missing.
        requests.HTTPError: If the API returns an HTTP error.
        requests.RequestException: If the request fails.
        RuntimeError: If the response is malformed.
    """
    api_key = api_key or os.getenv("OPENROUTER_API_KEY")

    if not api_key:
        raise ValueError("Missing API key. Set OPENROUTER_API_KEY in your environment.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Optional metadata for OpenRouter rankings.
    site_url = os.getenv("OPENROUTER_SITE_URL") or ""
    site_name = os.getenv("OPENROUTER_SITE_NAME") or ""

    if site_url:
        headers["HTTP-Referer"] = site_url

    if site_name:
        headers["X-OpenRouter-Title"] = site_name

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": message}],
    }

    max_retries = 3

    for attempt in range(max_retries + 1):
        response = requests.post(
            API_URL,
            headers=headers,
            json=cast(Any, payload),
            timeout=timeout,
        )

        if response.status_code != 429 or attempt == max_retries:
            break

        delay = min(2**attempt, 10) + random.uniform(0, 1)
        print(f"Rate limited (429). Retrying in {delay:.1f}s... ({attempt + 1}/{max_retries})")
        time.sleep(delay)

    response.raise_for_status()

    try:
        data = response.json()

        actual_model = data.get("model", model)
        usage = data.get("usage", {})

        print(f"Actual model: {actual_model}")
        print(f"Token usage: {usage}")

        return str(data["choices"][0]["message"]["content"])
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected response from OpenRouter: {response.text}") from exc


def main() -> int:
    # Resolve .env from the project root.
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    parser = argparse.ArgumentParser(description="Test OpenRouter using a free chat model.")

    parser.add_argument(
        "message",
        nargs="*",
        help="Message to send to the model.",
    )

    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenRouter model ID (default: {DEFAULT_MODEL})",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout in seconds (default: 60).",
    )

    args = parser.parse_args()

    message = " ".join(args.message).strip()

    if not message:
        message = input("Enter your message: ").strip()

    if not message:
        print("Error: Message cannot be empty.", file=sys.stderr)
        return 1

    try:
        print(f"Model: {args.model}")
        print("Sending request to OpenRouter...\n")

        answer = chat(
            message=message,
            model=args.model,
            timeout=args.timeout,
        )

        print("Assistant:")
        print(answer)
        return 0

    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    except requests.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)

        if exc.response is not None:
            print(exc.response.text, file=sys.stderr)

        return 1

    except requests.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1

    except RuntimeError as exc:
        print(f"Response error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
