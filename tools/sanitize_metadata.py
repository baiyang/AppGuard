"""Remove private Git URL userinfo from installed distribution provenance."""

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def sanitize(root: Path):
    count = 0
    for path in root.rglob("direct_url.json"):
        payload = json.loads(path.read_text())
        url = urlsplit(payload.get("url", ""))
        if url.username is not None or url.password is not None:
            host = url.hostname or ""
            if url.port:
                host += f":{url.port}"
            payload["url"] = urlunsplit((url.scheme, host, url.path, url.query, url.fragment))
            path.write_text(json.dumps(payload))
            count += 1
    print(f"Sanitized {count} installed provenance URLs")


if __name__ == "__main__":
    sanitize(Path(sys.argv[1]))
