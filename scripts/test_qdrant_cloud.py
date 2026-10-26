import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    url = os.getenv("PUBLISH_QDRANT_URL", "").rstrip("/")
    api_key = os.getenv("PUBLISH_QDRANT_API_KEY")

    if not url or not api_key:
        print("ERROR: PUBLISH_QDRANT_URL and PUBLISH_QDRANT_API_KEY must be set in .env")
        return 1

    headers = {"api-key": api_key}

    try:
        # 1. Check cluster connectivity and authentication.
        response = requests.get(
            url,
            headers=headers,
            timeout=15,
        )

        if response.status_code != 200:
            print(f"[FAIL] Cluster endpoint returned HTTP {response.status_code}")
            print(response.text[:500])
            return 1

        print("[PASS] Qdrant cluster is reachable and authenticated.")

        # 2. Test database access by listing collections.
        response = requests.get(
            f"{url}/collections",
            headers=headers,
            timeout=15,
        )

        if response.status_code != 200:
            print(f"[FAIL] Collection listing returned HTTP {response.status_code}")
            print(response.text[:500])
            return 1

        data = response.json()
        collections = data.get("result", {}).get("collections", [])

        print("[PASS] Collection listing succeeded.")
        print(f"[INFO] Collections found: {len(collections)}")

        for collection in collections:
            print(f"  - {collection.get('name')}")

        print("\nSUCCESS: Qdrant connection and API key are working.")
        return 0

    except requests.RequestException as exc:
        print(f"[REQUEST ERROR] {type(exc).__name__}: {exc}")
        return 1

    except ValueError as exc:
        print(f"[RESPONSE ERROR] Invalid JSON: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
