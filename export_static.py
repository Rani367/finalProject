import json
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, parse_qs

import main


ROOT = Path(__file__).resolve().parent
IMAGE_DIR = ROOT / "assets" / "notebook-images"
OUTPUT_DIR = ROOT / "assets" / "notebook-outputs"


def image_name(path):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", path).strip("_") + ".png"


def output_name(path):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", path).strip("_") + ".html"


def export():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sources = main.notebook_sources(refresh=True)

    for source in sources:
        image_url = source.get("image_url")
        if image_url:
            parsed = urlparse(image_url)
            notebook_path = parse_qs(parsed.query).get("path", [""])[0]
            notebook_path = unquote(notebook_path)
            image = main.notebook_image(ROOT / notebook_path)
            if not image:
                source.pop("image_url", None)
            else:
                image_file = image_name(notebook_path)
                (IMAGE_DIR / image_file).write_bytes(image)
                source["image_url"] = f"assets/notebook-images/{quote(image_file)}"

        output_url = source.get("output_url")
        if not output_url:
            continue

        parsed = urlparse(output_url)
        notebook_path = parse_qs(parsed.query).get("path", [""])[0]
        notebook_path = unquote(notebook_path)
        html = main.notebook_html(ROOT / notebook_path)
        if not html:
            source.pop("output_url", None)
            continue

        output_file = output_name(notebook_path)
        (OUTPUT_DIR / output_file).write_text(html, encoding="utf-8")
        source["output_url"] = f"assets/notebook-outputs/{quote(output_file)}"

    (ROOT / "data.json").write_text(
        json.dumps({"sources": sources}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Exported {len(sources)} sources to data.json")


if __name__ == "__main__":
    export()
