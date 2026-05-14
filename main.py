import csv
import base64
import json
import re
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, quote, urlparse


PORT = 8000
ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "TableToGraph"
INDEX_FILE = ROOT / "index.html"
CSV_URL_RE = re.compile(r"https://docs\.google\.com/spreadsheets/[^\"]+?export\?format=csv&gid=\d+")
SMOOTHED_RE = re.compile(
    r"df\[['\"]smoothed['\"]\]\s*=\s*df\[['\"]([^'\"]+)['\"]\]\.rolling\(window=(\d+)\)\.mean\(\)"
)
PLOT_RE = re.compile(r"plt\.plot\(([^)\n]+(?:\)[^,\n]*)?(?:,[^\n]*)?)\)")
TITLE_RE = re.compile(r"plt\.title\(['\"]([^'\"]+)")
YLABEL_RE = re.compile(r"plt\.ylabel\(['\"]([^'\"]+)")
COLUMN_RE = re.compile(r"df\[['\"]([^'\"]+)['\"]\]")
ROLLING_COLUMN_RE = re.compile(r"df\[['\"]([^'\"]+)['\"]\]\.rolling\(window=(\d+)\)")
LABEL_RE = re.compile(r"label=['\"]([^'\"]+)")
CACHE_SECONDS = 300
_cache = {"time": 0, "sources": None}
SEU_TLE = (
    "1 63237U 25052AD  26128.22866333  .00012896  00000-0  43681-3 0  9998",
    "2 63237  97.3984  23.6624 0004129 290.4131  69.6666 15.30699981 63911",
)


def notebook_sources(refresh=False):
    now = monotonic()
    if not refresh and _cache["sources"] is not None and now - _cache["time"] < CACHE_SECONDS:
        return _cache["sources"]

    sources = []
    fetch_jobs = []
    for notebook in sorted(DATA_ROOT.glob("Tevel*/*.ipynb")):
        try:
            data = json.loads(notebook.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            sources.append(
                {
                    "satellite": notebook.parent.name,
                    "name": notebook.stem,
                    "path": str(notebook.relative_to(ROOT)),
                    "error": f"Could not read notebook: {error}",
                    "columns": [],
                    "rows": [],
                    "row_count": 0,
                }
            )
            continue

        text = "\n".join(
            "".join(cell.get("source", []))
            for cell in data.get("cells", [])
            if cell.get("cell_type") == "code"
        )
        urls = sorted(set(CSV_URL_RE.findall(text)))
        metric = notebook.stem.replace(f"_{notebook.parent.name}", "").replace("_Graph", "")
        chart = chart_definition(text, metric)
        image_path = (
            f"/api/notebook-image?path={quote(str(notebook.relative_to(ROOT)))}"
            if notebook_has_image(data)
            else None
        )
        output_path = (
            f"/api/notebook-output?path={quote(str(notebook.relative_to(ROOT)))}"
            if notebook_has_html(data)
            else None
        )

        if not urls and not image_path and not output_path:
            sources.append(
                {
                    "satellite": notebook.parent.name,
                    "name": notebook.stem,
                    "metric": metric,
                    "chart": chart,
                    "path": str(notebook.relative_to(ROOT)),
                    "error": "No Google Sheets CSV URL found in notebook.",
                    "columns": [],
                    "rows": [],
                    "row_count": 0,
                }
            )
            continue

        if not urls:
            sources.append(
                {
                    "satellite": notebook.parent.name,
                    "name": notebook.stem,
                    "metric": metric,
                    "chart": chart,
                    "path": str(notebook.relative_to(ROOT)),
                    "image_url": image_path,
                    "output_url": output_path,
                    "columns": [],
                    "rows": [],
                    "row_count": 0,
                }
            )
            continue

        for index, url in enumerate(urls, start=1):
            label = notebook.stem if len(urls) == 1 else f"{notebook.stem} ({index})"
            fetch_jobs.append((notebook, metric, label, url, chart, image_path, output_path))

    with ThreadPoolExecutor(max_workers=8) as executor:
        sources.extend(executor.map(lambda job: fetch_csv_source(*job), fetch_jobs))

    _cache["time"] = monotonic()
    _cache["sources"] = sources
    return sources


def derived_sources(sources):
    derived = []
    for satellite in ("Tevel11", "Tevel19"):
        seu_source = find_source(sources, satellite, "SEU")
        if seu_source:
            derived.append(seu_globe_source(seu_source))

        solar_source = find_source(sources, satellite, "SolarPanels_Temp")
        if solar_source:
            derived.append(spin_speed_source(solar_source))
    return derived


def find_source(sources, satellite, metric):
    for source in sources:
        if source.get("satellite") == satellite and source.get("metric") == metric and source.get("rows"):
            return source
    return None


def seu_globe_source(source):
    result = {
        "satellite": source["satellite"],
        "name": f"Globus_SEU_Unatural_{source['satellite']}",
        "metric": "SEU Globe",
        "view": "geo",
        "chart": {"title": "SEU Globe", "y_label": "SEU counter", "x_column": "time", "series": []},
        "path": f"derived/{source['satellite']}/SEU_Globe",
        "url": source.get("url"),
        "columns": ["time", "SEU counter", "latitude", "longitude", "altitude_km"],
        "rows": [],
        "row_count": 0,
    }
    try:
        from skyfield.api import EarthSatellite, load
    except ImportError:
        result["error"] = "Install skyfield to compute SEU globe locations: python3 -m pip install -r requirements.txt"
        return result

    try:
        satellite = EarthSatellite(SEU_TLE[0], SEU_TLE[1], "TEVEL2_9")
        timescale = load.timescale()
        rows = []
        for row in source.get("rows", []):
            seu_counter = to_float(row.get("SEU counter"))
            if seu_counter <= 0:
                continue
            dt = parse_datetime(row.get("time"))
            if dt is None:
                continue
            point = satellite.at(timescale.from_datetime(dt)).subpoint()
            rows.append(
                {
                    "time": dt.isoformat(),
                    "SEU counter": format_float(seu_counter),
                    "latitude": format_float(point.latitude.degrees),
                    "longitude": format_float(point.longitude.degrees),
                    "altitude_km": format_float(point.elevation.km),
                }
            )
        result["rows"] = rows
        result["row_count"] = len(rows)
    except Exception as error:
        result["error"] = f"Could not compute SEU globe locations: {error}"
    return result


def spin_speed_source(source):
    result = {
        "satellite": source["satellite"],
        "name": f"SpinSpeed_{source['satellite']}",
        "metric": "SpinSpeed",
        "view": "chart",
        "chart": {
            "title": "Satellite Spin Rate Over Time",
            "y_label": "Rotations Per Minute (RPM)",
            "x_column": "time_min",
            "display": "points",
            "series": [{"column": f"Panel {index}", "label": f"Panel {index}", "window": 1} for index in range(6)],
        },
        "path": f"derived/{source['satellite']}/SpinSpeed",
        "url": source.get("url"),
        "columns": ["time_min", "Panel 0", "Panel 1", "Panel 2", "Panel 3", "Panel 4", "Panel 5"],
        "rows": [],
        "row_count": 0,
    }
    try:
        import numpy as np
        from scipy.fft import fft, fftfreq
        from scipy.signal import find_peaks
    except ImportError:
        result["error"] = "Install numpy and scipy to compute spin speed: python3 -m pip install -r requirements.txt"
        return result

    try:
        samples = []
        for row in source.get("rows", []):
            dt = parse_datetime(row.get("Ground Time"))
            if dt is None:
                continue
            sample = {"datetime": dt}
            for index in range(6):
                sample[f"solar_panels{index}"] = to_float(row.get(f"solar_panels{index}"))
            samples.append(sample)
        samples.sort(key=lambda row: row["datetime"])
        if not samples:
            return result

        start_time = samples[0]["datetime"]
        for sample in samples:
            sample["seconds"] = (sample["datetime"] - start_time).total_seconds()

        panel_results = {}
        for index in range(6):
            panel_results[f"Panel {index}"] = extract_rpm_over_time(
                samples,
                f"solar_panels{index}",
                np,
                fft,
                fftfreq,
                find_peaks,
            )

        time_points = sorted({point["time_min"] for points in panel_results.values() for point in points})
        rows = []
        for time_min in time_points:
            row = {"time_min": format_float(time_min)}
            for panel, points in panel_results.items():
                match = next((point for point in points if point["time_min"] == time_min), None)
                row[panel] = format_float(match["rpm"]) if match else ""
            rows.append(row)

        result["rows"] = rows
        result["row_count"] = len(rows)
    except Exception as error:
        result["error"] = f"Could not compute spin speed: {error}"
    return result


def extract_rpm_over_time(samples, panel_col, np, fft, fftfreq, find_peaks, window_size_sec=1800, step_size_sec=600):
    results = []
    max_time = max(sample["seconds"] for sample in samples)

    for start in np.arange(0, max_time - window_size_sec, step_size_sec):
        end = start + window_size_sec
        window = [
            sample
            for sample in samples
            if start <= sample["seconds"] < end and to_float(sample.get(panel_col)) == to_float(sample.get(panel_col))
        ]
        if len(window) < 20:
            continue

        signal = np.array([sample[panel_col] for sample in window], dtype=float)
        times = np.array([sample["seconds"] for sample in window], dtype=float)
        diffs = np.diff(times)
        if not len(diffs):
            continue

        dt = np.mean(diffs)
        if not np.isfinite(dt) or dt <= 0:
            continue

        centered = signal - np.mean(signal)
        yf = np.abs(fft(centered))
        xf = fftfreq(len(signal), dt)
        mask = (xf > 0.0001) & (xf < 0.2)
        xf_f = xf[mask]
        yf_f = yf[mask]
        if len(yf_f) == 0:
            continue

        peaks, _ = find_peaks(yf_f, prominence=(np.max(yf_f) * 0.05) + 0.001)
        if len(peaks) == 0:
            continue

        best_peak_idx = peaks[np.argmax(yf_f[peaks])]
        results.append(
            {
                "time_min": round(float((start + (window_size_sec / 2)) / 60), 6),
                "rpm": float(xf_f[best_peak_idx] * 60),
            }
        )
    return results


def parse_datetime(value):
    if value is None or value == "":
        return None
    text = str(value).strip()
    for day_first in (True, False):
        match = re.match(r"^(\d{1,2})[\/.-](\d{1,2})[\/.-](\d{2,4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", text)
        if not match:
            continue
        first, second, year, hour, minute, second_value = match.groups()
        year = int(year) + 2000 if len(year) == 2 else int(year)
        day = int(first if day_first else second)
        month = int(second if day_first else first)
        try:
            return datetime(
                year,
                month,
                day,
                int(hour or 0),
                int(minute or 0),
                int(second_value or 0),
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_float(value):
    if value is None or value == "":
        return float("nan")
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return float("nan")


def format_float(value):
    return f"{float(value):.10g}"


def chart_definition(text, metric):
    title = next(iter(TITLE_RE.findall(text)), metric)
    y_label = next(iter(YLABEL_RE.findall(text)), metric)
    smoothed = SMOOTHED_RE.findall(text)
    plots = PLOT_RE.findall(text)
    series = []
    x_column = None

    if smoothed:
        column, window = smoothed[0]
        x_column = plotted_x_column(plots[0]) if plots else None
        series.append({"column": column, "label": column, "window": int(window)})
    else:
        for plot in plots:
            columns = COLUMN_RE.findall(plot)
            rolling = ROLLING_COLUMN_RE.search(plot)
            label = next(iter(LABEL_RE.findall(plot)), None)
            if columns and x_column is None:
                x_column = columns[0]
            if rolling:
                column, window = rolling.groups()
                series.append({"column": column, "label": label or column, "window": int(window)})
            elif len(columns) >= 2:
                series.append({"column": columns[1], "label": label or columns[1], "window": 1})

    return {
        "title": title,
        "y_label": y_label,
        "x_column": x_column,
        "series": series,
    }


def plotted_x_column(plot):
    columns = COLUMN_RE.findall(plot)
    return columns[0] if columns else None


def notebook_has_image(data):
    return any(
        "image/png" in output.get("data", {})
        for cell in data.get("cells", [])
        for output in cell.get("outputs", [])
    )


def notebook_has_html(data):
    return any(
        "text/html" in output.get("data", {}) or "application/vnd.plotly.v1+json" in output.get("data", {})
        for cell in data.get("cells", [])
        for output in cell.get("outputs", [])
    )


def notebook_image(notebook):
    data = json.loads(notebook.read_text(encoding="utf-8"))
    for cell in data.get("cells", []):
        for output in cell.get("outputs", []):
            image = output.get("data", {}).get("image/png")
            if image:
                return base64.b64decode(image)
    return None


def notebook_html(notebook):
    data = json.loads(notebook.read_text(encoding="utf-8"))
    for cell in data.get("cells", []):
        for output in cell.get("outputs", []):
            output_data = output.get("data", {})
            html = output_data.get("text/html")
            if html:
                return "".join(html) if isinstance(html, list) else str(html)
            plotly = output_data.get("application/vnd.plotly.v1+json")
            if plotly:
                return plotly_html(plotly)
    return None


def plotly_html(plotly):
    spec = json.dumps(plotly, ensure_ascii=False)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    html, body, #plot {{
      width: 100%;
      height: 100%;
      margin: 0;
    }}
  </style>
</head>
<body>
  <div id="plot"></div>
  <script>
    const spec = {spec};
    Plotly.newPlot("plot", spec.data || [], spec.layout || {{}}, {{
      ...(spec.config || {{}}),
      responsive: true
    }});
  </script>
</body>
</html>"""


def fetch_csv_source(notebook, metric, label, url, chart, image_path, output_path):
    source = {
        "satellite": notebook.parent.name,
        "name": label,
        "metric": metric,
        "chart": chart,
        "image_url": image_path,
        "output_url": output_path,
        "path": str(notebook.relative_to(ROOT)),
        "url": url,
        "columns": [],
        "rows": [],
        "row_count": 0,
    }

    try:
        request = urllib.request.Request(url, headers={"User-Agent": "finalProject-web-server/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            content = response.read().decode("utf-8-sig")
    except urllib.error.URLError as error:
        if "CERTIFICATE_VERIFY_FAILED" not in str(error):
            source["error"] = f"Could not fetch CSV data: {error}"
            return source
        try:
            context = ssl._create_unverified_context()
            with urllib.request.urlopen(request, timeout=20, context=context) as response:
                content = response.read().decode("utf-8-sig")
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as retry_error:
            source["error"] = f"Could not fetch CSV data: {retry_error}"
            return source
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as error:
        source["error"] = f"Could not fetch CSV data: {error}"
        return source

    rows = list(csv.DictReader(content.splitlines()))
    fieldnames = rows[0].keys() if rows else []
    source["columns"] = [column.strip() for column in fieldnames]
    source["rows"] = [{(key.strip() if key else ""): value for key, value in row.items()} for row in rows]
    source["row_count"] = len(rows)
    return source


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/data":
            query = parse_qs(parsed.query)
            self.send_json({"sources": notebook_sources(refresh=query.get("refresh") == ["1"])})
            return
        if parsed.path == "/api/notebook-image":
            query = parse_qs(parsed.query)
            self.send_notebook_image(query.get("path", [""])[0])
            return
        if parsed.path == "/api/notebook-output":
            query = parse_qs(parsed.query)
            self.send_notebook_output(query.get("path", [""])[0])
            return
        if parsed.path in {"/", "/index.html"}:
            self.path = "/index.html"
        super().do_GET()

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_notebook_image(self, requested_path):
        notebook = (ROOT / requested_path).resolve()
        if ROOT not in notebook.parents or notebook.suffix != ".ipynb":
            self.send_error(404, "Image not found")
            return
        try:
            body = notebook_image(notebook)
        except (OSError, json.JSONDecodeError, ValueError):
            body = None
        if not body:
            self.send_error(404, "Image not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_notebook_output(self, requested_path):
        notebook = (ROOT / requested_path).resolve()
        if ROOT not in notebook.parents or notebook.suffix != ".ipynb":
            self.send_error(404, "Output not found")
            return
        try:
            html = notebook_html(notebook)
        except (OSError, json.JSONDecodeError, ValueError):
            html = None
        if not html:
            self.send_error(404, "Output not found")
            return
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    if not INDEX_FILE.exists():
        raise SystemExit("index.html is missing")

    server_address = ("", PORT)
    with ThreadingHTTPServer(server_address, Handler) as httpd:
        print(f"Serving at http://localhost:{PORT}")
        httpd.serve_forever()
