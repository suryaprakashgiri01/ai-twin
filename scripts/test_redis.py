import os
import sys
import uuid
from pathlib import Path

import redis
from dotenv import load_dotenv


def main() -> int:
    # Resolve .env from the project root.
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    redis_url = os.getenv("UPSTASH_REDIS_URL")

    if not redis_url:
        raise ValueError("UPSTASH_REDIS_URL is missing from .env")

    try:
        client = redis.Redis.from_url(
            redis_url,
            socket_connect_timeout=10,
            socket_timeout=10,
            decode_responses=True,
        )

        try:
            # 1. Test connectivity and authentication.
            result = client.ping()
            print(f"[PASS] Redis PING: {result}")

            # 2. Write a uniquely named temporary key.
            key = f"connection-test:python:{uuid.uuid4().hex}"
            value = "Upstash Redis connection successful"

            client.set(key, value, ex=60)
            print("[PASS] Redis SET")

            # 3. Read and validate the value.
            retrieved = client.get(key)

            if retrieved != value:
                print("[FAIL] Redis GET returned an unexpected value")
                return 1

            print(f"[PASS] Redis GET: {retrieved}")

            # 4. Remove the temporary key.
            client.delete(key)
            print("[PASS] Temporary key deleted")

            print("\nSUCCESS: Redis TLS, authentication, and read/write operations work.")
            return 0

        finally:
            client.close()

    except redis.exceptions.AuthenticationError:
        print("[AUTH ERROR] Redis rejected the credentials.")
        return 1

    except redis.exceptions.TimeoutError:
        print("[TIMEOUT] Redis did not respond within the timeout.")
        return 1

    except redis.exceptions.RedisError as exc:
        print(f"[REDIS ERROR] {type(exc).__name__}: {exc}")
        return 1

    except ValueError:
        print("[CONFIG ERROR] UPSTASH_REDIS_PORT must be a valid integer.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
