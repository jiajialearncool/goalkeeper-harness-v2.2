"""Install only verified executable components from the official pinned npm package."""

import base64
import hashlib
import io
import json
import platform
import tarfile

import httpx
from goalkeeper.codex import CODEX, PINNED_VERSION


def main():
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    if platform.system() != "Darwin":
        raise RuntimeError("This comparator is macOS-only")
    version = PINNED_VERSION + "-darwin-" + arch
    response = httpx.get("https://registry.npmjs.org/@openai%2Fcodex/" + version, timeout=30)
    response.raise_for_status()
    metadata = response.json()
    response = httpx.get(metadata["dist"]["tarball"], timeout=180, follow_redirects=True)
    response.raise_for_status()
    raw = response.content
    algorithm, checksum = metadata["dist"]["integrity"].split("-", 1)
    if base64.b64encode(hashlib.new(algorithm, raw).digest()).decode() != checksum:
        raise RuntimeError("Package integrity mismatch")
    CODEX.parent.mkdir(parents=True, exist_ok=True)
    components = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive.getmembers():
            from pathlib import Path

            name = Path(member.name).name
            if member.isfile() and name in ["codex", "codex-code-mode-host", "rg"]:
                path = CODEX.parent / name
                path.write_bytes(archive.extractfile(member).read())
                path.chmod(0o755)
                components[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if set(components) != {"codex", "codex-code-mode-host", "rg"}:
        raise RuntimeError("Missing official executable components")
    (CODEX.parent / "provenance.json").write_text(
        json.dumps(
            {
                "package": metadata["name"],
                "version": version,
                "integrity": metadata["dist"]["integrity"],
                "tarball": metadata["dist"]["tarball"],
                "components": components,
            },
            indent=2,
        )
    )
    print(json.dumps({"version": PINNED_VERSION, "components": components}, indent=2))


if __name__ == "__main__":
    main()
