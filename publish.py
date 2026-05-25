from __future__ import annotations

"""
Creator Hero — Webflow Blog Publisher
--------------------------------------
Reads Google Docs from a Drive folder, parses content, uploads images as WebP,
creates Webflow CMS items, publishes the site, and renames each doc to
[PUBLISHED YYYY-MM-DD] so it is skipped on the next run.

Google Doc structure expected (see test article for reference):
  • Metadata block at top: bold "Label: value" lines, image labels + inline images
  • "—DESCRIPTION 1—" line separates metadata from article body
  • Body: headings, paragraphs, bullets, [EMBED: <html>] anchors, HTML tables, CTA widget
  • "Table of Content:" near the bottom → goes to the table-of-content CMS field
  • "FAQs" line → stop parsing (handled separately if needed)

Usage:
    python3 publish.py --site creator-hero [--dry-run] [--doc-id DOC_ID]
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import requests
from PIL import Image
from google.oauth2 import service_account
from googleapiclient.discovery import build


# ─── Constants ────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly",
]

HEADING_LEVEL_MAP = {
    "HEADING_1": 2,
    "HEADING_2": 3,
    "HEADING_3": 4,
    "HEADING_4": 5,
    "HEADING_5": 6,
}

VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Maps "Label" text in the doc (lowercase) → result dict key.
# Supports both old format (Name:, Title:, Category:) and new format
# (Article name:, SEO Title:, Categories:, Author name:, Article intro text:).
_META_FIELDS: dict[str, str] = {
    "name":               "cms_name",
    "article name":       "cms_name",        # new format
    "slug":               "slug",
    "short desc":         "short_desc",
    "article intro text": "short_desc",      # new format
    "title":              "title",
    "seo title":          "title",           # new format
    "meta description":   "meta_description",
    "category":           "category",
    "categories":         "category",        # new format
    "date":               "date",
    "background color":   "background_color",
    "one min read":       "one_min_read",
    "1 min read":         "one_min_read",
    "article readtime":   "one_min_read",    # new format
    "author":             "author_text",
    "author name":        "author_name_1",   # new format (single author)
    "author name 1":      "author_name_1",
    "author page 1":      "author_page_1",
    "author status 1":    "author_status_1",
    "author name 2":      "author_name_2",
    "author page 2":      "author_page_2",
    "author status 2":    "author_status_2",
    "canonical":          "canonical",
}

# Image label text (uppercase, stripped of []) → destination
# "hero" → hero_image_id, "thumbnail" → thumbnail_id, else → named_images key
_IMAGE_LABEL_MAP: dict[str, str] = {
    "MAIN IMAGE":      "hero",
    "HERO IMAGE":      "hero",
    "SORT THUMBNAILS": "thumbnail",
    "THUMBNAIL IMAGE": "thumbnail",
    "AUTHOR IMAGE 1":  "AUTHOR IMAGE 1",
    "AUTHOR IMAGE 2":  "AUTHOR IMAGE 2",
    "IMAGE 01":        "IMAGE 01",
    "IMAGE 02":        "IMAGE 02",
    "IMAGE 03":        "IMAGE 03",
}

# Maps section name → field_mapping key (for optional [RICHTEXT 2] etc.)
_SECTION_TO_FIELD_KEY: dict[str, str] = {
    "default":    "richtext_body",
    "richtext_1": "richtext_body",
    "richtext_2": "richtext_02",
    "richtext_3": "richtext_03",
    "conclusion": "richtext_conclusion",
    "toc":        "richtext_toc",
}


# ─── WebP Conversion ──────────────────────────────────────────────────────────

def convert_to_webp(image_bytes: bytes, filename: str) -> tuple[bytes, str]:
    """
    Convert image bytes to lossy WebP (quality=85, method=6) via Pillow.
    Animated GIFs are passed through unchanged.
    """
    img = Image.open(io.BytesIO(image_bytes))
    if img.format == "GIF" and getattr(img, "is_animated", False):
        return image_bytes, filename
    if img.mode == "P":
        img = img.convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=85, method=6)
    new_name = filename.rsplit(".", 1)[0] + ".webp"
    return buf.getvalue(), new_name


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config(site_slug: str) -> dict:
    config_path = Path(__file__).parent / "sites" / f"{site_slug}.json"
    if not config_path.exists():
        print(f"❌  Config not found: {config_path}")
        sys.exit(1)
    with open(config_path) as f:
        cfg = json.load(f)
    # Allow environment variable overrides (used in GitHub Actions / CI)
    if os.environ.get("WEBFLOW_TOKEN"):
        cfg["webflow_token"] = os.environ["WEBFLOW_TOKEN"]

    for key in ("webflow_token", "webflow_site_id", "blog_collection_id", "drive_folder_id"):
        if not cfg.get(key) or "FILL_IN" in str(cfg.get(key, "")):
            print(f"❌  Config missing required key: {key}")
            sys.exit(1)
    return cfg


# ─── Google API ───────────────────────────────────────────────────────────────

def build_google_services(credentials_path: str = "credentials.json"):
    # In CI/cloud: GOOGLE_CREDENTIALS_JSON holds the full JSON content as a secret
    creds_json_env = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json_env:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tmp.write(creds_json_env)
        tmp.flush()
        creds_file = Path(tmp.name)
    else:
        creds_file = Path(__file__).parent / credentials_path

    if not creds_file.exists():
        print(f"❌  Google credentials not found: {creds_file}")
        sys.exit(1)
    creds = service_account.Credentials.from_service_account_file(
        str(creds_file), scopes=SCOPES
    )
    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    return docs_service, drive_service


def list_unpublished_docs(
    drive_service, folder_id: str, doc_id_filter: str | None = None
) -> list[dict]:
    """Recursively collect unpublished Google Docs from folder and all subfolders."""
    results = []

    def _collect(fid: str) -> None:
        page_token = None
        while True:
            resp = (
                drive_service.files()
                .list(
                    q=(
                        f"'{fid}' in parents "
                        "and trashed=false"
                    ),
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageToken=page_token,
                )
                .execute()
            )
            for f in resp.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    _collect(f["id"])
                elif f["mimeType"] in (
                    "application/vnd.google-apps.document",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ):
                    if "[PUBLISHED" in f["name"]:
                        print(f"   ⏭   Skipping (already published): {f['name']}")
                        continue
                    if doc_id_filter and f["id"] != doc_id_filter:
                        continue
                    results.append(f)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    _collect(folder_id)
    return results


def rename_doc_published(drive_service, doc_id: str, original_name: str) -> None:
    today = date.today().strftime("%Y-%m-%d")
    new_name = f"[PUBLISHED {today}] {original_name}"
    drive_service.files().update(fileId=doc_id, body={"name": new_name}).execute()
    print(f"   ✅  Renamed doc → {new_name}")


def fetch_google_doc(docs_service, doc_id: str) -> dict:
    return docs_service.documents().get(documentId=doc_id).execute()


def fetch_and_parse_docx(drive_service, doc_id: str) -> dict:
    """
    Download a .docx from Drive, convert to HTML via mammoth,
    parse with parse_article_html.parse_html(), and return a dict
    compatible with publish_article().
    """
    import io as _io
    import os
    import tempfile
    import mammoth
    from googleapiclient.http import MediaIoBaseDownload
    from parse_article_html import parse_html as _parse_html

    # ── Download the .docx bytes ──────────────────────────────────────────
    print("   ⬇️   Downloading .docx from Drive...")
    request = drive_service.files().get_media(fileId=doc_id)
    buf = _io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    docx_bytes = buf.getvalue()

    # ── Convert .docx → HTML ─────────────────────────────────────────────
    print("   🔄  Converting .docx → HTML with mammoth...")
    mammoth_result = mammoth.convert_to_html(_io.BytesIO(docx_bytes))
    html_content = mammoth_result.value
    for msg in mammoth_result.messages[:5]:
        print(f"   ℹ️   mammoth: {msg}")

    # ── Parse HTML metadata + body ────────────────────────────────────────
    html_parsed = _parse_html(html_content)

    # ── Save extracted images to temp files → LOCALFILE: IDs ─────────────
    tmp_dir = tempfile.mkdtemp(prefix="wf_docx_")
    hero_image_id = ""
    thumbnail_id = ""
    named_images: dict[str, str] = {}

    # parse_html keys → named_images label (must match label.lower().replace(" ","_") → field_key)
    _img_key_to_label: dict[str, str] = {
        "author_image_1": "AUTHOR IMAGE 1",
        "author_image_2": "AUTHOR IMAGE 2",
        "image_01":       "IMAGE 01",
        "image_02":       "IMAGE 02",
        "image_03":       "IMAGE 03",
    }

    for img_key, b64_str in html_parsed.pop("images", {}).items():
        if not b64_str:
            continue
        img_path = os.path.join(tmp_dir, f"{img_key}.png")
        with open(img_path, "wb") as fh:
            fh.write(base64.b64decode(b64_str))
        local_id = f"LOCALFILE:{img_path}"
        if img_key == "hero":
            hero_image_id = local_id
            print(f"   🖼️   Hero image saved → {img_path}")
        elif img_key == "thumbnail":
            thumbnail_id = local_id
            print(f"   🖼️   Thumbnail saved → {img_path}")
        else:
            label = _img_key_to_label.get(img_key, img_key.upper().replace("_", " "))
            named_images[label] = local_id
            print(f"   🖼️   Named image '{label}' saved → {img_path}")

    # ── Extract pre-built richtext sections ───────────────────────────────
    pre_built_richtext: dict[str, str] = {}
    for src_key, section_key in (
        ("richtext_body",       "default"),
        ("richtext_02",         "richtext_2"),
        ("richtext_03",         "richtext_3"),
        ("richtext_conclusion", "conclusion"),
    ):
        val = html_parsed.pop(src_key, "")
        if val:
            pre_built_richtext[section_key] = val

    toc_html = html_parsed.pop("richtext_toc", "")
    html_parsed.pop("errors", None)

    return {
        **html_parsed,
        "hero_image_id": hero_image_id,
        "thumbnail_id": thumbnail_id,
        "named_images": named_images,
        "inline_objects": {},
        "body_blocks": [],
        "pre_built_richtext": pre_built_richtext,
        "toc_html": toc_html,
    }


def download_image_bytes(drive_service, content_uri: str) -> bytes:
    http = drive_service._http
    response, content = http.request(content_uri)
    if response.status != 200:
        raise RuntimeError(f"Image download failed (HTTP {response.status})")
    return content


# ─── Webflow API ──────────────────────────────────────────────────────────────

def _wf_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def webflow_upload_asset(
    token: str, site_id: str, filename: str, image_bytes: bytes
) -> tuple[str, str]:
    """Upload image to Webflow Assets (with WebP conversion). Returns (asset_id, cdn_url)."""
    image_bytes, filename = convert_to_webp(image_bytes, filename)

    md5 = base64.b64encode(hashlib.md5(image_bytes).digest()).decode()

    init_resp = requests.post(
        f"https://api.webflow.com/v2/sites/{site_id}/assets",
        headers=_wf_headers(token),
        json={"fileName": filename, "fileHash": md5},
    )
    if init_resp.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"Webflow asset init failed ({init_resp.status_code}): {init_resp.text[:300]}"
        )

    init_data = init_resp.json()
    upload_details = init_data.get("uploadDetails") or {}
    asset_id = init_data.get("id", "")
    hosted_url = init_data.get("hostedUrl") or init_data.get("url") or ""

    upload_url = init_data.get("uploadUrl") or upload_details.get("url", "")
    if not upload_url:
        bucket = upload_details.get("bucket", "")
        if bucket:
            upload_url = f"https://{bucket}.s3.amazonaws.com"
    if not upload_url:
        raise RuntimeError(f"No S3 upload URL in Webflow response: {init_data}")

    form_fields = [(k, v) for k, v in upload_details.items() if k not in ("url", "bucket")]
    content_type = "image/webp" if filename.endswith(".webp") else "image/png"
    form_fields.append(("file", (filename, image_bytes, content_type)))

    s3_resp = requests.post(upload_url, files=form_fields)
    if s3_resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"S3 upload failed ({s3_resp.status_code}): {s3_resp.text[:200]}")

    if not hosted_url:
        key = upload_details.get("key", filename)
        hosted_url = f"https://uploads-ssl.webflow.com/{site_id}/{key}"

    print(f"   📸  Uploaded: {filename} → {hosted_url[:70]}...")
    return asset_id, hosted_url


def webflow_create_cms_item(
    token: str, collection_id: str, field_data: dict,
    locale_ids: list[str] | None = None,
) -> str:
    # Create in primary locale
    resp = requests.post(
        f"https://api.webflow.com/v2/collections/{collection_id}/items",
        headers=_wf_headers(token),
        json={"fieldData": field_data, "isArchived": False, "isDraft": False},
    )
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(
            f"CMS item creation failed ({resp.status_code}): {resp.text[:400]}"
        )
    item_id = resp.json().get("id", "")

    # Publish in primary locale
    pub_resp = requests.post(
        f"https://api.webflow.com/v2/collections/{collection_id}/items/publish",
        headers=_wf_headers(token),
        json={"itemIds": [item_id]},
    )
    if pub_resp.status_code not in (200, 201, 202):
        print(f"   ⚠️   Item publish warning ({pub_resp.status_code}): {pub_resp.text[:200]}")

    print(f"   ✅  CMS item created: {item_id}")

    # Push copies into each secondary locale via PATCH + cmsLocaleId query param
    # This creates the locale shell so the translation task can fill in content
    for locale_id in (locale_ids or []):
        loc_resp = requests.patch(
            f"https://api.webflow.com/v2/collections/{collection_id}/items/{item_id}",
            headers=_wf_headers(token),
            params={"cmsLocaleId": locale_id},
            json={"fieldData": {"name": field_data.get("name", "")}, "isDraft": False},
        )
        if loc_resp.status_code not in (200, 201, 202):
            print(f"   ⚠️   Locale copy failed ({locale_id}): {loc_resp.status_code} – {loc_resp.text[:200]}")
        else:
            print(f"   🌍  Locale copy created: {locale_id}")

    # Publish locale copies if any
    if locale_ids:
        requests.post(
            f"https://api.webflow.com/v2/collections/{collection_id}/items/publish",
            headers=_wf_headers(token),
            json={"itemIds": [item_id], "cmsLocaleIds": locale_ids},
        )

    return item_id


def webflow_patch_cms_item(
    token: str, collection_id: str, item_id: str, field_data: dict
) -> None:
    resp = requests.patch(
        f"https://api.webflow.com/v2/collections/{collection_id}/items/{item_id}",
        headers=_wf_headers(token),
        json={"fieldData": field_data},
    )
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"CMS PATCH failed ({resp.status_code}): {resp.text[:400]}")
    requests.post(
        f"https://api.webflow.com/v2/collections/{collection_id}/items/publish",
        headers=_wf_headers(token),
        json={"itemIds": [item_id]},
    )
    print(f"   ✅  CMS item patched: {item_id}")


def webflow_publish_site(token: str, site_id: str) -> None:
    resp = requests.post(
        f"https://api.webflow.com/v2/sites/{site_id}/publish",
        headers=_wf_headers(token),
        json={"publishToWebflowSubdomain": True},
    )
    if resp.status_code not in (200, 201, 202):
        print(f"   ⚠️   Site publish warning ({resp.status_code}): {resp.text[:200]}")
    else:
        print("   🚀  Site published")


# ─── Google Doc Parser ────────────────────────────────────────────────────────

def _para_full_text(elements: list) -> str:
    return "".join(
        el["textRun"].get("content", "") for el in elements if "textRun" in el
    ).strip("\n")


def _html_tag_depth(html: str) -> int:
    depth = 0
    for m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*?>", html, re.DOTALL):
        is_close = m.group(1) == "/"
        tag = m.group(2).lower()
        if tag in VOID_ELEMENTS:
            continue
        if m.group(0).rstrip(">").rstrip().endswith("/"):
            continue
        depth += -1 if is_close else 1
    return depth


def _extract_runs(elements: list) -> list[dict]:
    runs = []
    for el in elements:
        if "textRun" in el:
            tr = el["textRun"]
            text = tr.get("content", "").rstrip("\n")
            if not text:
                continue
            style = tr.get("textStyle", {})
            runs.append({
                "type": "text",
                "text": text,
                "bold": bool(style.get("bold")),
                "italic": bool(style.get("italic")),
                "link": (style.get("link") or {}).get("url", ""),
            })
        elif "inlineObjectElement" in el:
            runs.append({
                "type": "image",
                "object_id": el["inlineObjectElement"]["inlineObjectId"],
            })
    return runs


def _runs_to_html(runs: list[dict]) -> str:
    parts = []
    for run in runs:
        if run["type"] == "image":
            parts.append(f'[[IMAGE:{run["object_id"]}]]')
            continue
        text = run["text"]
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if run.get("link"):
            text = f'<a href="{run["link"]}" target="_blank">{text}</a>'
        if run.get("bold"):
            text = f"<strong>{text}</strong>"
        if run.get("italic"):
            text = f"<em>{text}</em>"
        parts.append(text)
    return "".join(parts)


def _merge_consecutive_embeds(blocks: list) -> list:
    """Merge back-to-back embed blocks (e.g. <style>+<script> CTA widget → one embed)."""
    merged = []
    for block in blocks:
        if block["type"] == "embed" and merged and merged[-1]["type"] == "embed":
            merged[-1]["html"] += "\n" + block["html"]
        else:
            merged.append(dict(block))
    return merged


def parse_google_doc(doc: dict) -> dict:
    """
    Parse a Creator Hero Google Doc into a structured article dict.

    Phases:
      "meta"  — before "—DESCRIPTION 1—": parse Label: value lines and image labels
      "body"  — after separator until "Table of Content:" or "FAQs"
      "toc"   — collect TOC numbered items
      "done"  — stop (FAQs section)
    """
    doc_title = doc.get("title", "Untitled")

    result: dict = {
        "doc_title": doc_title,
        "cms_name": "",
        "title": "",
        "slug": "",
        "short_desc": "",
        "meta_description": "",
        "category": "",
        "date": "",
        "background_color": "",
        "one_min_read": "",
        "author_text": "",
        "author_name_1": "", "author_page_1": "", "author_status_1": "",
        "author_name_2": "", "author_page_2": "", "author_status_2": "",
        "canonical": "",
        "hero_image_id": None,
        "thumbnail_id": None,
        "named_images": {},   # e.g. {"AUTHOR IMAGE 1": "object_id"}
        "body_blocks": [],
        "toc_html": "",
        "faqs": [],
        "inline_objects": doc.get("inlineObjects", {}),
    }

    content = doc.get("body", {}).get("content", [])

    phase = "meta"
    html_accumulating = False
    html_buffer: list[str] = []
    next_image_label: str | None = None
    toc_items: list[tuple[int, str]] = []

    def flush_html():
        nonlocal html_buffer, html_accumulating
        combined = "\n".join(html_buffer).strip()
        if combined:
            result["body_blocks"].append({"type": "embed", "html": combined})
        html_buffer.clear()
        html_accumulating = False

    for element in content:
        if "paragraph" not in element:
            continue

        para = element["paragraph"]
        para_elements = para.get("elements", [])
        para_style = para.get("paragraphStyle", {})
        named_style = para_style.get("namedStyleType", "NORMAL_TEXT")
        bullet = para.get("bullet")

        full_text = _para_full_text(para_elements)
        # Strip zero-width / invisible Unicode chars
        clean = re.sub(r"[​‌‍﻿­]", "", full_text).strip()

        if phase == "done":
            continue

        # ── HTML accumulation (body phase only) ──────────────────────────
        if html_accumulating:
            html_buffer.append(full_text)
            combined = "\n".join(html_buffer)
            if _html_tag_depth(combined) <= 0 and len(html_buffer) > 1:
                flush_html()
            continue

        # ════════════════════════════════════════════════════════════════════
        # PHASE: META  (before the first —DESCRIPTION 1— / —RICHTEXT 01— marker)
        # ════════════════════════════════════════════════════════════════════
        if phase == "meta":
            # Detect section separator line — supports old (DESCRIPTION 1) and
            # new (RICHTEXT 01) doc formats, with em-dashes, hyphens, or both.
            if re.search(r'(?:DESCRIPTION\s*1|RICHTEXT\s*0?1)\b', clean, re.IGNORECASE) and re.search(r'[—\-]', clean):
                phase = "body"
                next_image_label = None
                continue

            # Check for inline image BEFORE empty-line guard — image paragraphs have no
            # text runs so clean == "" and would be skipped otherwise.
            runs = _extract_runs(para_elements)
            image_runs = [r for r in runs if r["type"] == "image"]
            text_runs = [r for r in runs if r["type"] == "text" and r["text"].strip()]
            if image_runs and not text_runs:
                if next_image_label:
                    obj_id = image_runs[0]["object_id"]
                    dest = _IMAGE_LABEL_MAP.get(next_image_label, "")
                    if dest == "hero":
                        result["hero_image_id"] = obj_id
                    elif dest == "thumbnail":
                        result["thumbnail_id"] = obj_id
                    else:
                        result["named_images"][next_image_label] = obj_id
                    next_image_label = None
                continue

            if not clean:
                continue

            # Detect image labels: [SORT THUMBNAILS], [MAIN IMAGE], [AUTHOR IMAGE 1]
            img_label = re.sub(r"[\[\]]", "", clean).strip().upper()
            if img_label in _IMAGE_LABEL_MAP:
                next_image_label = img_label
                continue

            # "Label: value" lines
            label_m = re.match(r'^(.+?):\s*(.+)$', clean)
            if label_m:
                label_key = label_m.group(1).strip().lower()
                value = label_m.group(2).strip()
                # Inline TOC string: "Item A • Item B • Item C" → toc_items
                if label_key in ("toc", "table of content", "table of contents"):
                    parts = [p.strip() for p in re.split(r"\s*[•·│|]\s*|\s{2,}•\s{2,}", value) if p.strip()]
                    for idx, part in enumerate(parts, 1):
                        toc_items.append((idx, part))
                elif label_key in _META_FIELDS:
                    result[_META_FIELDS[label_key]] = value
            continue

        # ════════════════════════════════════════════════════════════════════
        # PHASE: BODY
        # ════════════════════════════════════════════════════════════════════
        if phase == "body":
            # Transition to TOC
            if re.match(r'^Table of Content[s]?:?$', clean, re.IGNORECASE):
                phase = "toc"
                continue

            # Transition to FAQ phase — match all common variants
            if re.match(r'^FAQs?[:\.]?\s*$|^Frequently Asked Questions[:\.]?\s*$', clean, re.IGNORECASE):
                phase = "faq"
                continue

            # Section markers within body: —RICHTEXT 02—, —RICHTEXT 03—, —CONCLUSION—
            # (and [RICHTEXT 2] bracket variant). Emit section_start blocks so the
            # splitter routes subsequent content into the matching field.
            section_m = re.match(
                r'^[—\-\[]+\s*(RICHTEXT\s*0?(\d+)|CONCLUSION)\s*[—\-\]]+\s*$',
                clean, re.IGNORECASE,
            )
            if section_m:
                tok = section_m.group(1).upper().replace(" ", "").replace("0", "")
                if "CONCLUSION" in tok:
                    section_name = "conclusion"
                else:
                    num = section_m.group(2)
                    section_name = f"richtext_{int(num)}"
                # —RICHTEXT 01— is the meta→body separator; in body phase treat it
                # as a no-op (everything after is already the body's "default" section).
                if section_name not in ("richtext_1",):
                    result["body_blocks"].append({"type": "section_start", "section": section_name})
                continue

            # Stray meta labels that appear inside the body (SEO Title, Meta Description,
            # Canonical, etc.) — capture them as meta and don't emit body content.
            stray_meta = re.match(r'^([A-Za-z][A-Za-z0-9 ]{0,30}):\s*(.+)$', clean)
            if stray_meta:
                key = stray_meta.group(1).strip().lower()
                val = stray_meta.group(2).strip()
                if key in _META_FIELDS and not result.get(_META_FIELDS[key]):
                    result[_META_FIELDS[key]] = val
                    continue

            # Pure-image paragraphs have no text runs → clean == "".
            # Detect them BEFORE the empty-line guard so they aren't skipped.
            if not html_accumulating:
                _runs = _extract_runs(para_elements)
                _imgs = [r for r in _runs if r["type"] == "image"]
                _txts = [r for r in _runs if r["type"] == "text" and r["text"].strip()]
                if _imgs and not _txts:
                    for img_run in _imgs:
                        result["body_blocks"].append(
                            {"type": "image", "object_id": img_run["object_id"]}
                        )
                    continue

            if not clean:
                continue

            # Single-line [EMBED: <html>] — TOC anchors etc.
            embed_m = re.match(r"^\[EMBED:\s*(.*?)\]$", clean, re.DOTALL)
            if embed_m:
                result["body_blocks"].append({"type": "embed", "html": embed_m.group(1).strip()})
                continue

            # Multi-line HTML block (table, style/CTA, script, div)
            if re.match(r"^<(table|style|script|div)\b", clean, re.IGNORECASE):
                html_buffer = [full_text]
                html_accumulating = True
                if _html_tag_depth(full_text) <= 0:
                    flush_html()
                continue

            # Parse rich-text runs
            runs = _extract_runs(para_elements)
            image_runs = [r for r in runs if r["type"] == "image"]
            text_runs = [r for r in runs if r["type"] == "text" and r["text"].strip()]

            # Heading
            if named_style in HEADING_LEVEL_MAP:
                level = HEADING_LEVEL_MAP[named_style]
                result["body_blocks"].append(
                    {"type": "heading", "level": level, "html": _runs_to_html(runs)}
                )
                continue

            # Bullet list item
            if bullet:
                nesting = bullet.get("nestingLevel", 0)
                html = _runs_to_html(runs)
                result["body_blocks"].append(
                    {"type": "bullet", "html": "    " * nesting + html}
                )
                continue

            # Normal paragraph
            html = _runs_to_html(runs)
            if html.strip():
                result["body_blocks"].append({"type": "paragraph", "html": html})
            continue

        # ════════════════════════════════════════════════════════════════════
        # PHASE: TOC
        # ════════════════════════════════════════════════════════════════════
        if phase == "toc":
            if re.match(r'^FAQs?[:\.]?\s*$|^Frequently Asked Questions[:\.]?\s*$', clean, re.IGNORECASE):
                phase = "faq"
                continue
            if not clean:
                continue
            # "1. Item text" or "1) Item text"
            num_m = re.match(r'^(\d+)[.)]\s*(.+)$', clean)
            if num_m:
                toc_items.append((int(num_m.group(1)), num_m.group(2).strip()))
            else:
                # Bullet TOC item — just add with auto-number
                runs = _extract_runs(para_elements)
                text = "".join(r["text"] for r in runs if r["type"] == "text").strip()
                text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # strip markdown bold
                if text:
                    toc_items.append((len(toc_items) + 1, text))

        # ════════════════════════════════════════════════════════════════════
        # PHASE: FAQ
        # ════════════════════════════════════════════════════════════════════
        if phase == "faq":
            if not clean:
                continue
            # Skip repeated FAQ headings and metadata lines (Name:, Slug:, Blog:)
            if re.match(r'^FAQs?[:\.]?\s*$|^Frequently Asked Questions[:\.]?\s*$', clean, re.IGNORECASE):
                continue
            if re.match(r'^(Name|Slug|Blog)\s*:', clean, re.IGNORECASE):
                continue
            runs = _extract_runs(para_elements)
            text_runs = [r for r in runs if r["type"] == "text" and r["text"].strip()]
            if not text_runs:
                continue

            is_heading = named_style in HEADING_LEVEL_MAP

            # Case 1: heading-styled paragraph (H1–H5). Entire text is the question.
            if is_heading:
                question = "".join(r["text"] for r in text_runs).strip()
                if question and not re.match(r'^[A-Za-z ]{1,30}:\s', question):
                    result["faqs"].append({"question": question, "answer": []})
                continue

            # Case 2: plain paragraph with mixed bold/non-bold runs. Many docs put
            # the question (bold) and the start of the answer (plain) in a SINGLE
            # paragraph separated by a soft line break — split on the bold/plain
            # boundary so we capture both halves.
            bold_prefix = []
            plain_suffix = []
            seen_plain = False
            for r in text_runs:
                if not seen_plain and r.get("bold", False):
                    bold_prefix.append(r)
                else:
                    seen_plain = True
                    plain_suffix.append(r)

            if bold_prefix:
                question = "".join(r["text"] for r in bold_prefix).strip()
                # Skip stray "Label: value" lines that survive metadata filters
                if not question or re.match(r'^[A-Za-z ]{1,30}:\s', question):
                    continue
                result["faqs"].append({"question": question, "answer": []})
                # Same-paragraph answer fragment
                if plain_suffix:
                    answer_html = _runs_to_html(plain_suffix).strip()
                    if answer_html:
                        result["faqs"][-1]["answer"].append(f"<p>{answer_html}</p>")
                continue

            # Case 3: all-plain paragraph → append to the previous question's answer
            if result["faqs"]:
                html = _runs_to_html(runs)
                if html.strip():
                    result["faqs"][-1]["answer"].append(html)

    # Flush any unclosed HTML buffer
    if html_accumulating and html_buffer:
        flush_html()

    # Merge consecutive embed blocks (<style> + <script> = one CTA widget)
    result["body_blocks"] = _merge_consecutive_embeds(result["body_blocks"])

    # Build TOC HTML (anchor links matching [EMBED: <a name="N">] in body)
    if toc_items:
        result["toc_html"] = "\n".join(
            f'<p><a href="#{num}">{text}</a></p>' for num, text in toc_items
        )

    # Fallback: use cms_name as title if Title: line was missing
    if not result["title"] and result["cms_name"]:
        result["title"] = re.sub(r'\s+[Tt]est\s*$', '', result["cms_name"]).strip()

    return result


# ─── Richtext Builder ─────────────────────────────────────────────────────────

def _replace_image_placeholders(html: str, inline_image_urls: dict) -> str:
    def replacer(m):
        url = inline_image_urls.get(m.group(1), "")
        return f'<img src="{url}" loading="lazy">' if url else ""
    return re.sub(r"\[\[IMAGE:([^\]]+)\]\]", replacer, html)


def build_richtext_html(blocks: list, inline_image_urls: dict) -> str:
    """
    Convert body blocks to Webflow-compatible richtext HTML.
    Embeds → <div class="w-embed">...</div>
    Bullets → <p>• text</p> (Webflow strips <ul>/<li> in CMS v2)
    """
    parts = []
    for block in blocks:
        btype = block["type"]
        if btype == "heading":
            html = _replace_image_placeholders(block["html"], inline_image_urls)
            parts.append(f"<h{block['level']}>{html}</h{block['level']}>")
        elif btype == "paragraph":
            html = _replace_image_placeholders(block["html"], inline_image_urls)
            parts.append(f"<p>{html}</p>")
        elif btype == "bullet":
            html = _replace_image_placeholders(block["html"], inline_image_urls)
            parts.append(f"<p>• {html}</p>")
        elif btype == "image":
            url = inline_image_urls.get(block["object_id"], "")
            if url:
                parts.append(f'<img src="{url}" loading="lazy">')
            else:
                print(f"   ⚠️   No URL for inline image: {block['object_id']}")
        elif btype == "embed":
            # Webflow richtext HTML embed.
            # Outer element must be <figure> with w-richtext classes.
            # Inner div needs w-script class when embed contains <style>/<script>.
            inner_html = block["html"]
            has_script = bool(re.search(r"<(style|script)\b", inner_html, re.IGNORECASE))
            inner_class = "w-embed w-script" if has_script else "w-embed"
            parts.append(
                '<figure class="w-richtext-figure-type-image w-richtext-align-fullwidth">'
                f'<div class="{inner_class}">{inner_html}</div>'
                '</figure>'
            )
    return "\n".join(parts)


def split_into_sections(body_blocks: list) -> dict[str, list]:
    """Split blocks at section_start markers (optional [RICHTEXT 2] etc.)."""
    sections: dict[str, list] = {"default": []}
    current = "default"
    for block in body_blocks:
        if block["type"] == "section_start":
            current = block["section"]
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(block)
    return sections


# ─── Image Fetching ───────────────────────────────────────────────────────────

def get_image_bytes(
    drive_service, object_id: str, inline_objects: dict
) -> tuple[bytes, str]:
    # Local file saved from .docx extraction
    if object_id.startswith("LOCALFILE:"):
        path = object_id[len("LOCALFILE:"):]
        with open(path, "rb") as fh:
            img_bytes = fh.read()
        return img_bytes, Path(path).name

    obj = inline_objects.get(object_id, {})
    embedded = obj.get("inlineObjectProperties", {}).get("embeddedObject", {})
    img_props = embedded.get("imageProperties", {})
    uri = img_props.get("contentUri") or img_props.get("sourceUri", "")
    if not uri:
        raise RuntimeError(f"No image URI for object {object_id}")
    image_bytes = download_image_bytes(drive_service, uri)
    title = embedded.get("title") or embedded.get("description") or object_id[:16]
    title_slug = re.sub(r"[^a-z0-9]", "-", title.lower())[:40].strip("-") or "image"
    ext = "png"
    lower_uri = uri.lower()
    if ".jpg" in lower_uri or ".jpeg" in lower_uri:
        ext = "jpg"
    elif ".gif" in lower_uri:
        ext = "gif"
    elif ".webp" in lower_uri:
        ext = "webp"
    return image_bytes, f"{title_slug}.{ext}"


# ─── FAQ Publishing ───────────────────────────────────────────────────────────

def publish_faqs(
    faqs: list,
    blog_item_id: str,
    article_name: str,
    config: dict,
    dry_run: bool = False,
    locale_ids: list[str] | None = None,
) -> None:
    """
    Create ONE FAQ CMS item per article with up to 5 numbered Q&A pairs.
    Schema: name, blog (Reference), 1-question/1-answer … 5-question/5-answer

    For secondary locale copies the blog reference is stripped — Webflow returns
    400 "Referenced item not found" when a primary-locale item ID is referenced
    in a secondary locale context. Q&A content is left blank for the translation
    pipeline to fill in.
    """
    faq_collection_id = config.get("faq_collection_id", "")
    if not faq_collection_id or not faqs:
        return

    token = config["webflow_token"]
    faq_map = config.get("faq_field_mapping", {})
    locale_ids = locale_ids or []

    if dry_run:
        locale_note = f" + {len(locale_ids)} locale(s)" if locale_ids else ""
        print(f"   [DRY RUN] FAQs: would create 1 FAQ item{locale_note} with {len(faqs)} Q&A(s)")
        for i, faq in enumerate(faqs[:5], 1):
            print(f"      Q{i}: {faq['question'][:80]}")
        return

    field_data: dict = {faq_map.get("name", "name"): article_name}

    blog_ref = faq_map.get("blog_reference", "blog")
    if blog_item_id:
        field_data[blog_ref] = blog_item_id

    for i, faq in enumerate(faqs[:5], 1):
        q_field = faq_map.get(f"question_{i}", f"{i}-question")
        a_field = faq_map.get(f"answer_{i}", f"{i}-answer")
        field_data[q_field] = faq["question"].strip()
        answer_html = "\n".join(f"<p>{a}</p>" for a in faq["answer"])
        field_data[a_field] = answer_html

    try:
        webflow_create_cms_item(token, faq_collection_id, field_data, locale_ids)
        print(f"   ✅  FAQ item created ({min(len(faqs), 5)} Q&As)"
              + (f" + {len(locale_ids)} locale(s)" if locale_ids else ""))
    except Exception as e:
        print(f"   ⚠️   FAQ item error: {e}")


# ─── Article Publishing ────────────────────────────────────────────────────────

def publish_article(
    parsed: dict,
    config: dict,
    drive_service,
    dry_run: bool = False,
) -> str | None:
    token = config["webflow_token"]
    site_id = config["webflow_site_id"]
    collection_id = config["blog_collection_id"]
    field_map = config.get("blog_field_mapping", {})
    inline_objects = parsed.get("inline_objects", {})

    display_name = parsed.get("title") or parsed.get("cms_name", "Untitled")
    print(f"\n📄  Article: {display_name}")

    if dry_run:
        print("    [DRY RUN] — no Webflow writes")
        print(f"    CMS name:    {parsed.get('cms_name', '')}")
        print(f"    Title:       {parsed.get('title', '')}")
        print(f"    Slug:        {parsed.get('slug') or '(none)'}")
        print(f"    Meta desc:   {parsed.get('meta_description', '')[:80] or '(none)'}")
        print(f"    Short desc:  {parsed.get('short_desc', '')[:60] or '(falls back to meta)'}")
        print(f"    Category:    {parsed.get('category', '') or '(none)'}")
        print(f"    Date:        {parsed.get('date', '') or '(none)'}")
        print(f"    BG color:    {parsed.get('background_color', '') or '(none)'}")
        print(f"    Read time:   {parsed.get('one_min_read', '') or '(none)'}")
        print(f"    Author 1:    {parsed.get('author_name_1', '') or '(none)'}")
        print(f"    Hero img:    {'✓' if parsed.get('hero_image_id') else '(none)'}")
        print(f"    Thumbnail:   {'✓' if parsed.get('thumbnail_id') else '(none)'}")
        print(f"    Author img:  {'✓' if 'AUTHOR IMAGE 1' in parsed.get('named_images', {}) else '(none)'}")
        body_blocks = parsed.get("body_blocks", [])
        embeds = [b for b in body_blocks if b["type"] == "embed"]
        images = [b for b in body_blocks if b["type"] == "image"]
        if parsed.get("pre_built_richtext"):
            total_chars = sum(len(v) for v in parsed["pre_built_richtext"].values())
            print(f"    Richtext:    pre-built ({len(parsed['pre_built_richtext'])} section(s), {total_chars} chars)")
        else:
            print(f"    Body blocks: {len(body_blocks)} "
                  f"({len(embeds)} embeds, {len(images)} inline images)")
        if parsed.get("toc_html"):
            print(f"    TOC:         ✓ ({parsed['toc_html'].count('<p>')} items)")
        for i, e in enumerate(embeds[:5]):
            print(f"    Embed {i+1}: {e['html'][:80].strip()}...")
        if parsed.get("faqs"):
            print(f"    FAQs:        {len(parsed['faqs'])} Q&A(s) found")
            publish_faqs(parsed["faqs"], "", parsed.get("cms_name", ""), config, dry_run=True)
        return None

    def upload_img(label: str, obj_id: str) -> tuple[str, str]:
        print(f"   ⬆️   Uploading {label}...")
        img_bytes, img_filename = get_image_bytes(drive_service, obj_id, inline_objects)
        return webflow_upload_asset(token, site_id, img_filename, img_bytes)

    # ── Upload all images ─────────────────────────────────────────────────
    hero_asset_id = hero_url = ""
    if parsed["hero_image_id"]:
        try:
            hero_asset_id, hero_url = upload_img("hero image", parsed["hero_image_id"])
        except Exception as e:
            print(f"   ⚠️   Hero image error: {e}")

    thumb_asset_id = thumb_url = ""
    if parsed["thumbnail_id"]:
        try:
            thumb_asset_id, thumb_url = upload_img("thumbnail", parsed["thumbnail_id"])
        except Exception as e:
            print(f"   ⚠️   Thumbnail error: {e}")

    # Named images (author images, IMAGE 01 etc.)
    named_image_results: dict[str, tuple[str, str]] = {}
    for label, obj_id in parsed["named_images"].items():
        try:
            named_image_results[label] = upload_img(label, obj_id)
        except Exception as e:
            print(f"   ⚠️   {label} error: {e}")

    # Inline body images
    inline_image_urls: dict[str, str] = {}
    all_inline_ids: set[str] = set()
    for block in parsed["body_blocks"]:
        if block["type"] == "image":
            all_inline_ids.add(block["object_id"])
        elif block["type"] in ("paragraph", "heading", "bullet"):
            for m in re.finditer(r"\[\[IMAGE:([^\]]+)\]\]", block["html"]):
                all_inline_ids.add(m.group(1))

    if all_inline_ids:
        print(f"   ⬆️   Uploading {len(all_inline_ids)} inline image(s)...")
    for obj_id in all_inline_ids:
        try:
            _, cdn_url = upload_img(f"inline {obj_id[:12]}", obj_id)
            inline_image_urls[obj_id] = cdn_url
        except Exception as e:
            print(f"   ⚠️   Inline image error ({obj_id[:12]}): {e}")

    # ── Build richtext HTML ───────────────────────────────────────────────
    if parsed.get("pre_built_richtext"):
        # .docx path: richtext already built by parse_article_html
        section_html: dict[str, str] = {
            k: v for k, v in parsed["pre_built_richtext"].items() if v
        }
    else:
        # Google Doc path: build from body_blocks
        sections = split_into_sections(parsed["body_blocks"])
        section_html = {
            sec: build_richtext_html(blocks, inline_image_urls)
            for sec, blocks in sections.items()
            if blocks
        }

    # ── Build field data ──────────────────────────────────────────────────
    field_data: dict = {}

    # CMS item name (required by Webflow)
    cms_name = parsed.get("cms_name") or parsed.get("title", display_name)
    field_data[field_map.get("name", "name")] = cms_name

    # Display title
    if parsed.get("title") and field_map.get("title"):
        field_data[field_map["title"]] = parsed["title"]

    # Slug
    if parsed.get("slug") and field_map.get("slug"):
        field_data[field_map["slug"]] = parsed["slug"]

    # Meta description
    if parsed.get("meta_description") and field_map.get("meta_description"):
        field_data[field_map["meta_description"]] = parsed["meta_description"]

    # Short desc (fallback to meta_description)
    short = parsed.get("short_desc") or parsed.get("meta_description", "")
    if short and field_map.get("short_desc"):
        field_data[field_map["short_desc"]] = short

    # Date — parse MM/DD/YYYY → ISO 8601
    if parsed.get("date") and field_map.get("date"):
        try:
            dt = datetime.strptime(parsed["date"], "%m/%d/%Y")
            field_data[field_map["date"]] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            field_data[field_map["date"]] = parsed["date"]

    # Background color
    if parsed.get("background_color") and field_map.get("background_color"):
        field_data[field_map["background_color"]] = parsed["background_color"]

    # One min read
    if parsed.get("one_min_read") and field_map.get("one_min_read"):
        field_data[field_map["one_min_read"]] = parsed["one_min_read"]

    # Canonical URL
    if parsed.get("canonical") and field_map.get("canonical"):
        field_data[field_map["canonical"]] = parsed["canonical"]

    # Author plain-text fields (name, status)
    for key in ("author_name_1", "author_status_1", "author_name_2", "author_status_2"):
        if parsed.get(key) and field_map.get(key):
            field_data[field_map[key]] = parsed[key]

    # Author page link fields
    for key in ("author_page_1", "author_page_2"):
        if parsed.get(key) and field_map.get(key):
            field_data[field_map[key]] = parsed[key]

    # Hero image — always use URL object (Webflow CMS v2 image fields require it)
    if field_map.get("hero_image") and hero_url:
        field_data[field_map["hero_image"]] = {"url": hero_url, "alt": display_name}

    # Thumbnail
    if field_map.get("thumbnail") and thumb_url:
        field_data[field_map["thumbnail"]] = {"url": thumb_url, "alt": display_name}

    # Author images and other named images
    for label, (asset_id, cdn_url) in named_image_results.items():
        field_key = label.lower().replace(" ", "_")
        if field_map.get(field_key) and cdn_url:
            field_data[field_map[field_key]] = {"url": cdn_url, "alt": label}

    # Richtext sections
    for section_name, html in section_html.items():
        field_key = _SECTION_TO_FIELD_KEY.get(section_name)
        if field_key and field_map.get(field_key) and html.strip():
            field_data[field_map[field_key]] = html
            print(f"   📝  Section '{section_name}' → {field_map[field_key]} ({len(html)} chars)")

    # TOC
    if parsed.get("toc_html") and field_map.get("richtext_toc"):
        field_data[field_map["richtext_toc"]] = parsed["toc_html"]
        print(f"   📝  TOC → {field_map['richtext_toc']}")

    # Category reference
    cat_map = config.get("category_map", {})
    if parsed.get("category") and field_map.get("category"):
        cat_id = cat_map.get(parsed["category"], "")
        if cat_id and not cat_id.startswith("FILL_IN"):
            field_data[field_map["category"]] = cat_id
        else:
            print(f"   ⚠️   Category '{parsed['category']}' not in category_map — skipping")

    # Static extra fields from config
    for k, v in config.get("extra_fields", {}).items():
        if not k.startswith("_"):
            field_data[k] = v

    # ── Create CMS item ───────────────────────────────────────────────────
    locale_ids = config.get("secondary_locale_ids", [])
    if locale_ids:
        print(f"   🌍  Creating item in primary + {len(locale_ids)} locale(s)...")
    else:
        print("   📝  Creating Webflow CMS item...")
    item_id = webflow_create_cms_item(token, collection_id, field_data, locale_ids)

    # ── Publish FAQs ──────────────────────────────────────────────────────
    if parsed.get("faqs") and config.get("faq_collection_id"):
        article_name = parsed.get("cms_name") or display_name
        locale_note = f" + {len(locale_ids)} locale(s)" if locale_ids else ""
        print(f"   📝  Publishing FAQ item ({len(parsed['faqs'])} Q&As{locale_note})...")
        publish_faqs(parsed["faqs"], item_id, article_name, config, dry_run=False, locale_ids=locale_ids)

    return item_id


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Creator Hero — Webflow Blog Publisher")
    parser.add_argument("--site", required=True, help="Site config slug (e.g. creator-hero)")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no Webflow writes")
    parser.add_argument("--doc-id", help="Publish a single Google Doc by ID")
    parser.add_argument("--no-publish", action="store_true", help="Skip site-level publish")
    args = parser.parse_args()

    config = load_config(args.site)
    docs_service, drive_service = build_google_services()

    docs = list_unpublished_docs(drive_service, config["drive_folder_id"], args.doc_id)

    if not docs:
        print("✅  No unpublished articles found.")
        return

    print(f"\n📋  Found {len(docs)} article(s) to publish:")
    for d in docs:
        print(f"   • {d['name']} ({d['id']})")

    _DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    published_count = 0
    for doc_meta in docs:
        doc_id = doc_meta["id"]
        doc_name = doc_meta["name"]
        print(f"\n{'─' * 64}")
        print(f"🔄  Processing: {doc_name}")
        try:
            if doc_meta.get("mimeType") == _DOCX_MIME:
                parsed = fetch_and_parse_docx(drive_service, doc_id)
            else:
                doc = fetch_google_doc(docs_service, doc_id)
                parsed = parse_google_doc(doc)
            item_id = publish_article(parsed, config, drive_service, dry_run=args.dry_run)
            if not args.dry_run and item_id:
                rename_doc_published(drive_service, doc_id, doc_name)
                published_count += 1
        except Exception as exc:
            print(f"   ❌  Error: {exc}")
            import traceback
            traceback.print_exc()
            continue

    if not args.dry_run and not args.no_publish and published_count > 0:
        print(f"\n🚀  Publishing site...")
        webflow_publish_site(config["webflow_token"], config["webflow_site_id"])

    print(f"\n{'─' * 64}")
    if args.dry_run:
        print(f"✅  Dry run complete. {len(docs)} article(s) parsed.")
    else:
        print(f"✅  Done. {published_count}/{len(docs)} article(s) published.")


if __name__ == "__main__":
    main()
