"""Prometheus exporter for the OLicense Server status command.

This module exposes a small CLI that periodically executes the OLicense
``-status`` command (or reads from a captured status file), parses the output,
and publishes metrics consumable by Prometheus/Grafana.

Example usage::

    python exporter/olicense_exporter.py \
        --status-command /opt/olicense/bin/OLicenseServer -status \
        --listen-address 0.0.0.0 \
        --listen-port 9877

The exporter accepts status output in either JSON form (recommended if the
server supports ``--json``) or in a text form that resembles the reports shown
in the OLicense Server Quickstart guide. The parser is intentionally flexible
so that it can handle minor variations across server versions. Refer to the
``README.md`` for detailed deployment instructions.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from prometheus_client import Gauge, start_http_server

_LOGGER = logging.getLogger(__name__)


@dataclass
class FeatureStatus:
    """Represents the utilization of a single licensed feature."""

    total: float
    in_use: float
    borrowed: float
    denials: float


@dataclass
class ServerStatus:
    """Aggregated status for the server-wide metrics."""

    total: Optional[float] = None
    in_use: Optional[float] = None
    available: Optional[float] = None
    denials: Optional[float] = None
    heartbeat_ts: Optional[float] = None
    features: Dict[str, FeatureStatus] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.features is None:
            self.features = {}


class OLicenseExporter:
    """Main exporter loop that polls the server status command."""

    FEATURE_TOTAL = Gauge(
        "olicense_feature_total_licenses",
        "Configured license capacity for each feature.",
        ["feature"],
    )
    FEATURE_IN_USE = Gauge(
        "olicense_feature_licenses_in_use",
        "Currently consumed licenses per feature.",
        ["feature"],
    )
    FEATURE_BORROWED = Gauge(
        "olicense_feature_licenses_borrowed",
        "Number of borrowed/offline licenses per feature.",
        ["feature"],
    )
    FEATURE_DENIALS = Gauge(
        "olicense_feature_denials_total",
        "Total denials recorded per feature in the status output.",
        ["feature"],
    )

    SERVER_TOTAL = Gauge(
        "olicense_server_total_licenses",
        "Total license seats configured on the server.",
    )
    SERVER_IN_USE = Gauge(
        "olicense_server_licenses_in_use",
        "Total license seats currently consumed on the server.",
    )
    SERVER_AVAILABLE = Gauge(
        "olicense_server_licenses_available",
        "Number of license seats reported as available.",
    )
    SERVER_DENIALS = Gauge(
        "olicense_server_denials_total",
        "Total denials reported by the server status output.",
    )
    SERVER_HEARTBEAT = Gauge(
        "olicense_server_heartbeat_timestamp",
        "Heartbeat timestamp reported by the server (seconds since epoch).",
    )
    SCRAPE_SUCCESS = Gauge(
        "olicense_exporter_scrape_success",
        "1 if the last scrape succeeded, 0 otherwise.",
    )
    SCRAPE_DURATION = Gauge(
        "olicense_exporter_scrape_duration_seconds",
        "Duration of the last scrape in seconds.",
    )

    def __init__(
        self,
        status_command: Optional[Iterable[str]] = None,
        status_file: Optional[Path] = None,
        poll_interval: float = 15.0,
    ) -> None:
        if not status_command and not status_file:
            raise ValueError("Either status_command or status_file must be provided")
        self._status_command = list(status_command) if status_command else None
        self._status_file = status_file
        self._poll_interval = poll_interval
        self._active_features: set[str] = set()

    def run(self) -> None:
        """Start the polling loop."""
        _LOGGER.info("Starting exporter; polling every %.1f seconds", self._poll_interval)
        while True:
            start_time = time.time()
            try:
                raw_output = self._read_status()
                status = parse_status(raw_output)
                self._record_metrics(status)
                self.SCRAPE_SUCCESS.set(1)
            except Exception:  # pragma: no cover - we still want to surface this
                _LOGGER.exception("Failed to collect OLicense status")
                self.SCRAPE_SUCCESS.set(0)
            finally:
                duration = time.time() - start_time
                self.SCRAPE_DURATION.set(duration)
            time.sleep(self._poll_interval)

    def _read_status(self) -> str:
        """Fetch the status output from either a file or an external command."""
        if self._status_file:
            _LOGGER.debug("Reading status from %s", self._status_file)
            return self._status_file.read_text(encoding="utf-8")
        assert self._status_command is not None
        _LOGGER.debug("Executing status command: %s", " ".join(self._status_command))
        completed = subprocess.run(
            self._status_command,
            capture_output=True,
            check=True,
            text=True,
        )
        _LOGGER.debug("Status command completed with %s bytes", len(completed.stdout))
        return completed.stdout

    def _record_metrics(self, status: ServerStatus) -> None:
        """Update Prometheus gauges using the parsed status."""
        _LOGGER.debug("Recording metrics: %s", status)
        if status.total is not None:
            self.SERVER_TOTAL.set(status.total)
        if status.in_use is not None:
            self.SERVER_IN_USE.set(status.in_use)
        if status.available is not None:
            self.SERVER_AVAILABLE.set(status.available)
        if status.denials is not None:
            self.SERVER_DENIALS.set(status.denials)
        if status.heartbeat_ts is not None:
            self.SERVER_HEARTBEAT.set(status.heartbeat_ts)

        seen_features = set()
        for feature_name, feature in status.features.items():
            seen_features.add(feature_name)
            self.FEATURE_TOTAL.labels(feature=feature_name).set(feature.total)
            self.FEATURE_IN_USE.labels(feature=feature_name).set(feature.in_use)
            self.FEATURE_BORROWED.labels(feature=feature_name).set(feature.borrowed)
            self.FEATURE_DENIALS.labels(feature=feature_name).set(feature.denials)

        stale_features = self._active_features - seen_features
        for feature_name in stale_features:
            _LOGGER.debug("Removing stale feature metrics for %s", feature_name)
            self.FEATURE_TOTAL.remove(feature_name)
            self.FEATURE_IN_USE.remove(feature_name)
            self.FEATURE_BORROWED.remove(feature_name)
            self.FEATURE_DENIALS.remove(feature_name)
        self._active_features = seen_features


def parse_status(raw: str) -> ServerStatus:
    """Parse status output from the OLicense server.

    The parser supports two primary formats:

    * **JSON** (preferred) – the status command outputs a JSON document with keys
      such as ``total_licenses``, ``in_use``, ``available``, ``denials``, and a
      ``features`` array that contains per-feature objects. Field names are case
      insensitive and optional.
    * **Plain text** – loosely based on the formatting documented in the
      Quickstart PDF. The parser searches for ``key: value`` pairs and feature
      rows presented either as tables or in a key/value form.

    Parameters
    ----------
    raw:
        The raw string returned by the status command or read from a file.

    Returns
    -------
    ServerStatus
        The structured representation of the server metrics.
    """

    raw = raw.strip()
    if not raw:
        raise ValueError("Status output was empty")

    json_status = _parse_json_status(raw)
    if json_status is not None:
        return json_status

    return _parse_text_status(raw)


_JSON_FIELD_MAP = {
    "total_licenses": "total",
    "total": "total",
    "capacity": "total",
    "in_use": "in_use",
    "used": "in_use",
    "available": "available",
    "free": "available",
    "denials": "denials",
    "heartbeat": "heartbeat_ts",
}


def _parse_json_status(raw: str) -> Optional[ServerStatus]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        raise ValueError("JSON status must be an object")

    status = ServerStatus()
    for key, value in payload.items():
        normalized = _JSON_FIELD_MAP.get(key.lower())
        if normalized is None:
            continue
        if normalized == "heartbeat_ts":
            status.heartbeat_ts = _coerce_timestamp(value)
        else:
            setattr(status, normalized, _coerce_float(value, field=key))

    features = payload.get("features")
    if isinstance(features, list):
        for feature_obj in features:
            if not isinstance(feature_obj, dict):
                continue
            name = str(feature_obj.get("name") or feature_obj.get("feature"))
            if not name or name == "None":
                continue
            feature_status = FeatureStatus(
                total=_coerce_float(feature_obj.get("total"), field="feature.total", default=0.0),
                in_use=_coerce_float(feature_obj.get("in_use"), field="feature.in_use", default=0.0),
                borrowed=_coerce_float(feature_obj.get("borrowed"), field="feature.borrowed", default=0.0),
                denials=_coerce_float(feature_obj.get("denials"), field="feature.denials", default=0.0),
            )
            status.features[name] = feature_status
    return status


_KEY_VALUE_RE = re.compile(r"^(?P<key>[\w\s/-]+?)\s*[:=]\s*(?P<value>.+)$")
_FEATURE_TABLE_RE = re.compile(
    r"^(?P<name>[\w.-]+)\s+(?P<total>\d+)\s+(?P<in_use>\d+)\s+(?P<borrowed>\d+)\s+(?P<denials>\d+)",
    re.IGNORECASE,
)
_FEATURE_KEYVAL_RE = re.compile(
    r"^Feature\s+(?P<name>[\w.-]+)\s*:\s*"
    r"total\s*=\s*(?P<total>\d+)\s+"
    r"in_use\s*=\s*(?P<in_use>\d+)"
    r"(?:\s+borrowed\s*=\s*(?P<borrowed>\d+))?"
    r"(?:\s+denials\s*=\s*(?P<denials>\d+))?",
    re.IGNORECASE,
)
_HEARTBEAT_RE = re.compile(r"heartbeat\s*[:=]\s*(?P<value>.+)$", re.IGNORECASE)


def _parse_text_status(raw: str) -> ServerStatus:
    status = ServerStatus()

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in lines:
        match = _HEARTBEAT_RE.search(line)
        if match:
            status.heartbeat_ts = _coerce_timestamp(match.group("value"))
            continue

        table_match = _FEATURE_TABLE_RE.match(line)
        if table_match:
            feature_status = FeatureStatus(
                total=float(table_match.group("total")),
                in_use=float(table_match.group("in_use")),
                borrowed=float(table_match.group("borrowed")),
                denials=float(table_match.group("denials")),
            )
            status.features[table_match.group("name")] = feature_status
            continue

        keyval_match = _KEY_VALUE_RE.match(line)
        if keyval_match:
            key = keyval_match.group("key").strip().lower()
            value = keyval_match.group("value").strip()
            if key in {"total licenses", "total licence", "licenses total"}:
                status.total = _coerce_float(value, field=key)
            elif key in {"in use", "licenses in use"}:
                status.in_use = _coerce_float(value, field=key)
            elif key in {"available", "licenses available"}:
                status.available = _coerce_float(value, field=key)
            elif key in {"denials", "license denials"}:
                status.denials = _coerce_float(value, field=key)
            else:
                feature_match = _FEATURE_KEYVAL_RE.match(line)
                if feature_match:
                    feature_status = FeatureStatus(
                        total=float(feature_match.group("total")),
                        in_use=float(feature_match.group("in_use")),
                        borrowed=float(feature_match.group("borrowed") or 0.0),
                        denials=float(feature_match.group("denials") or 0.0),
                    )
                    status.features[feature_match.group("name")] = feature_status
            continue

        feature_match = _FEATURE_KEYVAL_RE.match(line)
        if feature_match:
            feature_status = FeatureStatus(
                total=float(feature_match.group("total")),
                in_use=float(feature_match.group("in_use")),
                borrowed=float(feature_match.group("borrowed") or 0.0),
                denials=float(feature_match.group("denials") or 0.0),
            )
            status.features[feature_match.group("name")] = feature_status
            continue

    return status


def _coerce_float(value: Optional[object], *, field: str, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"Failed to parse numeric value '{text}' for {field}") from exc


def _coerce_timestamp(value: Optional[object]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text.replace("Z", ""), fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        _LOGGER.debug("Unable to parse heartbeat timestamp: %s", text)
        return None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prometheus exporter for OLicense Server")
    parser.add_argument(
        "--status-command",
        nargs=argparse.REMAINDER,
        help=(
            "Command that outputs the OLicense status report. "
            "When omitted, --status-file must be provided."
        ),
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        help="Path to a file that contains captured status output (for testing).",
    )
    parser.add_argument(
        "--listen-address",
        default="0.0.0.0",
        help="Address on which to expose the metrics HTTP server.",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=9877,
        help="Port on which to expose the metrics HTTP server.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=15.0,
        help="Seconds between polls of the status command.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"],
        help="Logging verbosity for the exporter.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    status_command = args.status_command if args.status_command else None
    if status_command:
        _LOGGER.info("Using status command: %s", " ".join(status_command))
    elif not args.status_file:
        parser.error("Either --status-command or --status-file must be provided")

    exporter = OLicenseExporter(
        status_command=status_command,
        status_file=args.status_file,
        poll_interval=args.poll_interval,
    )
    start_http_server(args.listen_port, addr=args.listen_address)
    exporter.run()


if __name__ == "__main__":
    main()
