from __future__ import annotations

"""
Creator Hero — Article Repair Tool
------------------------------------
PATCHes already-published Webflow CMS items without creating duplicates.
Use this after fixing a parsing bug, image issue, or field mapping error.

Add the article title → Webflow item ID pairs to REPAIR_MAP below, then run:
    python3 repair_articles.py --site creator-hero
"""

import argparse
import re
import sys
from pathlib import Path

# Import shared helpers from publish.py
from publish import (
    load_config,
    build_google_services,
    fetch_google_doc,
    parse_google_doc,
    get_image_bytes,
    webflow_upload_asset,
    webflow_patch_cms_item,
    webflow_publish_site,
    build_richtext_html,
)


# ── REPAIR MAP ────────────────────────────────────────────────────────────────
# Add "Article Title" (exact, without [PUBLISHED ...] prefix): "webflow_item_id"
# Example:
#   "Best Influencer Marketing Software for Marketplaces": "abc123def456",

REPAIR_MAP: dict[str, str] = {
    # "Article Title": "webflow_item_id",
}


# ─────────────────────────────────────────────────────────────────────────────

def find_doc_by_title(drive_service, folder_id: str, target_title: str) -> dict | None:
    """
    Search the Drive folder for a doc whose name contains target_title.
    Matches both published ([PUBLISHED ...] prefix) and unpublished docs.
    """
    page_token = None
    while True:
        resp = (
            drive_service.files()
            .list(
                q=(
                    f"'{folder_id}' in parents "
                    "and mimeType='application/vnd.google-apps.document' "
                    "and trashed=false"
                ),
                fields="nextPageToken, files(id, name)",
                pageToken=page_token,
            )
            .execute()
        )
        for f in resp.get("files", []):
            # Strip [PUBLISHED ...] prefix for comparison
            clean_name = re.sub(r"^\[PUBLISHED[^\]]*\]\s*", "", f["name"]).strip()
            if target_title.lower() in clean_name.lower():
                return f
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return None


def repair_article(
    title: str,
    item_id: str,
    config: dict,
    docs_service,
    drive_service,
) -> None:
    token = config["webflow_token"]
    site_id = config["webflow_site_id"]
    collection_id = config["blog_collection_id"]
    field_map = config.get("blog_field_mapping", {})

    print(f"\n🔧  Repairing: {title}  (item: {item_id})")

    # Find the Google Doc
    doc_meta = find_doc_by_title(drive_service, config["drive_folder_id"], title)
    if not doc_meta:
        print(f"   ❌  Google Doc not found for title: {title}")
        return

    print(f"   📄  Found doc: {doc_meta['name']}")

    doc = fetch_google_doc(docs_service, doc_meta["id"])
    parsed = parse_google_doc(doc)
    inline_objects = parsed["inline_objects"]

    def upload_img(label: str, obj_id: str) -> tuple[str, str]:
        print(f"   ⬆️   Uploading {label}...")
        img_bytes, img_filename = get_image_bytes(drive_service, obj_id, inline_objects)
        return webflow_upload_asset(token, site_id, img_filename, img_bytes)

    # Upload hero
    hero_asset_id = hero_url = ""
    if parsed["hero_image_id"]:
        try:
            hero_asset_id, hero_url = upload_img("hero image", parsed["hero_image_id"])
        except Exception as e:
            print(f"   ⚠️   Hero image error: {e}")

    # Upload thumbnail
    thumb_asset_id = thumb_url = ""
    if parsed["thumbnail_id"]:
        try:
            thumb_asset_id, thumb_url = upload_img("thumbnail", parsed["thumbnail_id"])
        except Exception as e:
            print(f"   ⚠️   Thumbnail error: {e}")

    # Upload inline images
    inline_image_urls: dict[str, str] = {}
    all_inline_ids: set[str] = set()
    for block in parsed["body_blocks"]:
        if block["type"] == "image":
            all_inline_ids.add(block["object_id"])
        elif block["type"] in ("paragraph", "heading", "bullet"):
            import re as _re
            for m in _re.finditer(r"\[\[IMAGE:([^\]]+)\]\]", block["html"]):
                all_inline_ids.add(m.group(1))

    if all_inline_ids:
        print(f"   ⬆️   Uploading {len(all_inline_ids)} inline image(s)...")
    for obj_id in all_inline_ids:
        try:
            _, cdn_url = upload_img(f"inline {obj_id[:12]}", obj_id)
            inline_image_urls[obj_id] = cdn_url
        except Exception as e:
            print(f"   ⚠️   Inline image error ({obj_id[:12]}): {e}")

    richtext_html = build_richtext_html(parsed["body_blocks"], inline_image_urls)

    # Build field data
    field_data: dict = {}

    name_field = field_map.get("name", "name")
    field_data[name_field] = parsed["title"]

    if parsed["slug"] and field_map.get("slug"):
        field_data[field_map["slug"]] = parsed["slug"]

    if parsed["meta_description"] and field_map.get("meta_description"):
        field_data[field_map["meta_description"]] = parsed["meta_description"]

    if field_map.get("hero_image"):
        if hero_asset_id:
            field_data[field_map["hero_image"]] = hero_asset_id
        elif hero_url:
            field_data[field_map["hero_image"]] = {"url": hero_url, "alt": parsed["title"]}

    if field_map.get("thumbnail"):
        if thumb_asset_id:
            field_data[field_map["thumbnail"]] = thumb_asset_id
        elif thumb_url:
            field_data[field_map["thumbnail"]] = {"url": thumb_url, "alt": parsed["title"]}

    if field_map.get("richtext_body"):
        field_data[field_map["richtext_body"]] = richtext_html

    webflow_patch_cms_item(token, collection_id, item_id, field_data)


def main() -> None:
    ap = argparse.ArgumentParser(description="Creator Hero — Article Repair Tool")
    ap.add_argument("--site", required=True, help="Site config slug")
    ap.add_argument("--no-publish", action="store_true", help="Skip site publish")
    args = ap.parse_args()

    if not REPAIR_MAP:
        print("⚠️   REPAIR_MAP is empty.")
        print("    Add article title → Webflow item ID pairs to REPAIR_MAP in this script.")
        sys.exit(0)

    config = load_config(args.site)
    docs_service, drive_service = build_google_services()

    repaired = 0
    for title, item_id in REPAIR_MAP.items():
        try:
            repair_article(title, item_id, config, docs_service, drive_service)
            repaired += 1
        except Exception as e:
            print(f"   ❌  Error repairing '{title}': {e}")
            import traceback
            traceback.print_exc()

    if not args.no_publish and repaired > 0:
        print(f"\n🚀  Publishing site...")
        webflow_publish_site(config["webflow_token"], config["webflow_site_id"])

    print(f"\n✅  Repaired {repaired}/{len(REPAIR_MAP)} article(s).")


if __name__ == "__main__":
    main()
