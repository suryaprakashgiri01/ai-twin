"""Smoke-test the Gemini key. IPv4 DNS resolution is forced first."""

import socket
from typing import Any

_original_getaddrinfo = socket.getaddrinfo


def ipv4_getaddrinfo(
    host: Any,
    port: Any,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> Any:
    return _original_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = ipv4_getaddrinfo

# Import LangChain only after applying the workaround
import argparse  # noqa: E402
import os  # noqa: E402
import random  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from langchain_google_genai import ChatGoogleGenerativeAI  # noqa: E402

DEFAULT_MODEL = "gemini-3.8-flash"


def chat(
    message: str,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    timeout: int = 240,
    temperature: float = 0.7,
) -> str:
    """
    Send a message to Gemini using LangChain and return the response.

    Args:
        message: User prompt.
        model: Gemini model ID.
        api_key: API key; defaults to GEMINI_API_KEY.
        timeout: Request timeout in seconds.

    Returns:
        Assistant response text.

    Raises:
        ValueError: If the API key is missing.
        RuntimeError: If the request fails or the response is invalid.
    """
    api_key = api_key or os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise ValueError("Missing API key. Set GEMINI_API_KEY in your .env file.")

    llm = ChatGoogleGenerativeAI(
        model=model,
        google_api_key=api_key,
        temperature=temperature,
        timeout=timeout,
        max_retries=2,
    )

    max_retries = 3

    for attempt in range(max_retries + 1):
        try:
            response = llm.invoke(message)

            usage = response.usage_metadata if hasattr(response, "usage_metadata") else None

            print(f"Model: {model}")
            print(f"Token usage: {usage or 'Not available'}")

            content = response.content

            if not isinstance(content, str):
                return "\n".join(
                    block["text"]  # pyright: ignore[reportIndexIssue]
                    for block in (content if isinstance(content, list) else [])
                    if isinstance(block, dict) and "text" in block
                )
            return content

        except Exception as exc:
            status_code = getattr(
                getattr(exc, "response", None),
                "status_code",
                None,
            )
            status_code = status_code or getattr(exc, "code", None)

            error_text = str(exc).lower()
            is_rate_limited = (
                status_code == 429
                or "429" in error_text
                or "resource_exhausted" in error_text
                or "resource exhausted" in error_text
            )

            if not is_rate_limited or attempt == max_retries:
                raise RuntimeError(f"Gemini request failed: {type(exc).__name__}: {exc}") from exc

            delay = min(2**attempt, 10) + random.uniform(0, 1)

            print(
                f"Rate limited. Retrying in {delay:.1f}s ({attempt + 1}/{max_retries})...",
                file=sys.stderr,
            )
            time.sleep(delay)

    # Every loop turn returns or raises, so control never falls out of the loop.
    raise RuntimeError("Gemini request failed after retries.")


def main() -> int:
    # Resolve .env from the project root.
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    parser = argparse.ArgumentParser(description="Test Gemini API using LangChain.")

    parser.add_argument(
        "message",
        nargs="*",
        help="Message to send to Gemini.",
    )

    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Gemini model ID (default: {DEFAULT_MODEL})",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Request timeout in seconds (default: 60).",
    )

    parser.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="Generation temperature (default: 0.7).",
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
        print("Sending request to Gemini...\n")

        answer = chat(
            message=message,
            model=args.model,
            timeout=args.timeout,
            temperature=args.temperature,
        )

        print("\nAssistant:")
        print(answer)
        return 0

    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    except RuntimeError as exc:
        print(f"Gemini error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
