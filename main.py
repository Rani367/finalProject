import csv
import base64
import json
import re
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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

        if not urls and not image_path:
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
                    "columns": [],
                    "rows": [],
                    "row_count": 0,
                }
            )
            continue

        for index, url in enumerate(urls, start=1):
            label = notebook.stem if len(urls) == 1 else f"{notebook.stem} ({index})"
            fetch_jobs.append((notebook, metric, label, url, chart, image_path))

    with ThreadPoolExecutor(max_workers=8) as executor:
        sources.extend(executor.map(lambda job: fetch_csv_source(*job), fetch_jobs))

    _cache["time"] = monotonic()
    _cache["sources"] = sources
    return sources


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


def notebook_image(notebook):
    data = json.loads(notebook.read_text(encoding="utf-8"))
    for cell in data.get("cells", []):
        for output in cell.get("outputs", []):
            image = output.get("data", {}).get("image/png")
            if image:
                return base64.b64decode(image)
    return None


def fetch_csv_source(notebook, metric, label, url, chart, image_path):
    source = {
        "satellite": notebook.parent.name,
        "name": label,
        "metric": metric,
        "chart": chart,
        "image_url": image_path,
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


if __name__ == "__main__":
    if not INDEX_FILE.exists():
        raise SystemExit("index.html is missing")

    server_address = ("", PORT)
    with ThreadingHTTPServer(server_address, Handler) as httpd:
        print(f"Serving at http://localhost:{PORT}")
        httpd.serve_forever()
