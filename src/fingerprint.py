"""Stack-trace fingerprinting: group identical failures (Sentry-style).

Two failures with the same root cause often have textually different stack
traces (different line numbers, memory addresses, timestamps). Fingerprinting
normalizes frames and hashes the result so one bug = one group, no matter how
many tests hit it.
"""

import hashlib
import re

# Noise that differs between identical failures.
FRAME_NOISE = [
    (re.compile(r'File "([^"]+)", line \d+'), r'File "\1", line N'),   # line numbers
    (re.compile(r"line \d+"), r"line N"),
    (re.compile(r"0x[0-9a-fA-F]+"), r"0xADDR"),                          # addresses
    (re.compile(r"#\d+"), r"#N"),                                       # thread ids
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"), r"<ts>"),    # timestamps
    (re.compile(r"\(session [^)]+\)"), r"(session X)"),
    (re.compile(r"\b\d+\.\d+\.\d+\.\d+\b"), r"<ip>"),                    # IPs
]


def normalize_frame(text: str) -> str:
    for pattern, repl in FRAME_NOISE:
        text = pattern.sub(repl, text)
    return text


def fingerprint(failure) -> str:
    """Stable 12-char group id for a failure's stack trace + error type."""
    blob = f"{failure.message}\n{failure.traceback}"
    normalized = normalize_frame(blob)
    # Keep only the structural skeleton: file/function names + error class.
    skeleton = []
    for line in normalized.splitlines():
        s = line.strip()
        if s.startswith('File "') or s.startswith("at ") or "Error" in s or "Exception" in s:
            skeleton.append(s)
    if not skeleton:  # no trace at all — fall back to the message
        skeleton = [normalize_frame(failure.message)]
    digest = hashlib.sha256("\n".join(skeleton).encode()).hexdigest()
    return digest[:12]


def cluster(failures) -> dict:
    """Group failures by fingerprint. Returns {fingerprint: [Failure, ...]}."""
    groups = {}
    for f in failures:
        groups.setdefault(fingerprint(f), []).append(f)
    return groups


def render_clusters(groups) -> str:
    lines = [f"{len(groups)} distinct failure group(s) from "
             f"{sum(len(v) for v in groups.values())} failures:", ""]
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    for fp, members in ranked:
        rep = members[0]
        lines.append(f"### {fp} — {len(members)} failure(s)")
        lines.append(f"Representative: {rep.test_id}")
        lines.append(f"Message: {rep.message}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
