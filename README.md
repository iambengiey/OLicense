# OLicense Monitoring Toolkit

This repository adds a lightweight Prometheus exporter and a ready-to-import
Grafana dashboard to monitor an OLicense Server deployment.

## Prometheus exporter

`exporter/olicense_exporter.py` polls the OLicense Server status output (either by
executing the `OLicenseServer -status` command or by reading a captured status
file) and exposes normalized metrics at an HTTP endpoint suitable for
Prometheus.

### Installation

1. Install Python 3.9 or newer on the host that can reach the OLicense Server
   CLI.
2. Install the exporter dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

### Running the exporter

```bash
python exporter/olicense_exporter.py \
  --listen-address 0.0.0.0 \
  --listen-port 9877 \
  --poll-interval 15 \
  --status-command /opt/olicense/bin/OLicenseServer -status --json
```

* Use `--status-file /path/to/sample.txt` when iterating locally without access
  to the real server.
* The exporter automatically detects JSON status output. If the command only
  emits plain text, ensure the output contains key/value totals and per-feature
  rows similar to the Quickstart documentation.
* Because `--status-command` captures the remainder of the CLI, place it last or
  use `--status-command -- /opt/... -status` if the status command itself needs
  leading hyphen arguments.
* Metrics are exposed on `http://<host>:9877/metrics`. Point Prometheus (or
  Grafana Agent in Prometheus mode) at this endpoint.

### Exported metrics

| Metric | Description |
| ------ | ----------- |
| `olicense_server_total_licenses` | Total configured license seats. |
| `olicense_server_licenses_in_use` | Seats currently checked out. |
| `olicense_server_licenses_available` | Seats reported as free. |
| `olicense_server_denials_total` | Denials reported by the status command. |
| `olicense_server_heartbeat_timestamp` | Timestamp of the last server heartbeat. |
| `olicense_feature_total_licenses{feature=""}` | Feature-level capacity. |
| `olicense_feature_licenses_in_use{feature=""}` | Feature-level utilization. |
| `olicense_feature_licenses_borrowed{feature=""}` | Borrowed/offline seats per feature. |
| `olicense_feature_denials_total{feature=""}` | Feature-level denials. |
| `olicense_exporter_scrape_success` | `1` when the last poll succeeded. |
| `olicense_exporter_scrape_duration_seconds` | Poll duration in seconds. |

## Grafana dashboard

Import `dashboard/olicense_grafana_dashboard.json` into Grafana (Dashboard ➜
New ➜ Import). Select your Prometheus data source when prompted.

The dashboard includes:

* Overall license utilization stat panel.
* Time-series visualization of total versus in-use seats.
* Instant table summarizing per-feature totals, usage, and denials.
* Time-series view of denials per feature with a template variable for quick
  filtering.

## Alerting suggestions

With the exporter metrics in Prometheus you can create alert rules such as:

* Utilization exceeds 90% for 10 minutes: `olicense_server_licenses_in_use /
  olicense_server_total_licenses > 0.9`.
* Any denials within the last 5 minutes: `increase(olicense_feature_denials_total[5m]) > 0`.
* Exporter unreachable: `absent(olicense_exporter_scrape_success == 1)`.

Attach these rules to your existing Alertmanager or Grafana Alerting pipeline to
receive notifications by email, Slack, Teams, or other channels.
