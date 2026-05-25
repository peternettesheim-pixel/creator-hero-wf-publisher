from __future__ import annotations

"""
Creator Hero — Article Repair Tool
------------------------------------
PATCHes already-published Webflow CMS items without creating duplicates.
Also repairs missing FAQ items and pushes FAQ locale copies.

Usage:
    python3 repair_articles.py --site creator-hero              # repair blog + FAQ
    python3 repair_articles.py --site creator-hero --faq-only   # only fix FAQs
"""

import argparse
import re
import sys
import requests
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
    publish_faqs,
    _wf_headers,
)


# ── REPAIR MAP ────────────────────────────────────────────────────────────────
# "Article Title" (exact CMS name from the doc, without [PUBLISHED] prefix): "webflow_blog_item_id"

REPAIR_MAP: dict[str, str] = {
    "10 Best Ainfluencer Alternatives for Influencer Marketing": "6a146093fa16cd2d5e1669ff",
    "Top 10 Best Creator Management Tools": "6a1460b71f9775e57e9d8637",
}

# ── FAQ ITEM MAP ──────────────────────────────────────────────────────────────
# If a FAQ item was already created for an article (English only, missing locale copies),
# put its ID here → the repair will PATCH locale copies without creating a duplicate.
# If left as "" or not present → the repair will create a brand-new FAQ item from scratch.

FAQ_ITEM_MAP: dict[str, str] = {
    "10 Best Ainfluencer Alternatives for Influencer Marketing": "",        # no FAQ item yet → will create
    "Top 10 Best Creator Management Tools": "6a1460bc1f9775e57e9d88a2",    # exists → will add locale copies only
}


# ─────────────────────────────────────────────────────────────────────────────

def find_doc_by_title(drive_service, folder_id: str, target_title: str) -> dict | None:
    """
    Recursively search the Drive folder for a doc whose name contains target_title.
    Matches both published ([PUBLISHED ...] prefix) and unpublished docs.
    """
    def _search(fid: str) -> dict | None:
        page_token = None
        while True:
            resp = (
                drive_service.files()
                .list(
                    q=(f"'{fid}' in parents and trashed=false"),
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageToken=page_token,
                )
                .execute()
            )
            for f in resp.get("files", []):
                if f["mimeType"] == "application/vnd.google-apps.folder":
                    found = _search(f["id"])
                    if found:
                        return found
                elif f["mimeType"] == "application/vnd.google-apps.document":
                    clean_name = re.sub(r"^\[PUBLISHED[^\]]*\]\s*", "", f["name"]).strip()
                    if target_title.lower() in clean_name.lower():
                        return f
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return None

    return _search(folder_id)


def patch_faq_locale_copies(
    token: str,
    faq_collection_id: str,
    faq_item_id: str,
    item_name: str,
    locale_ids: list[str],
) -> None:
    """
    PATCH locale shells for an existing FAQ item.
    Only sends 'name' — translation pipeline fills in the Q&A content.
    """
    for locale_id in locale_ids:
        resp = requests.patch(
            f"https://api.webflow.com/v2/collections/{faq_collection_id}/items/{faq_item_id}",
            headers=_wf_headers(token),
            params={"cmsLocaleId": locale_id},
            json={"fieldData": {"name": item_name}, "isDraft": False},
        )
        if resp.status_code not in (200, 201, 202):
            print(f"   ⚠️   FAQ locale copy failed ({locale_id}): {resp.status_code} – {resp.text[:200]}")
        else:
            print(f"   🌍  FAQ locale copy created: {locale_id}")

    # Publish the locale copies
    if locale_ids:
        requests.post(
            f"https://api.webflow.com/v2/collections/{faq_collection_id}/items/publish",
            headers=_wf_headers(token),
            json={"itemIds": [faq_item_id], "cmsLocaleIds": locale_ids},
        )


def repair_article(
    title: str,
    item_id: str,
    config: dict,
    docs_service,
    drive_service,
    faq_only: bool = False,
) -> None:
    token = config["webflow_token"]
    site_id = config["webflow_site_id"]
    collection_id = config["blog_collection_id"]
    field_map = config.get("blog_field_mapping", {})
    locale_ids = config.get("secondary_locale_ids", [])

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

    # ── Blog article repair ──────────────────────────────────────────────────
    if not faq_only:
        def upload_img(label: str, obj_id: str) -> tuple[str, str]:
            print(f"   ⬆️   Uploading {label}...")
            img_bytes, img_filename = get_image_bytes(drive_service, obj_id, inline_objects)
            return webflow_upload_asset(token, site_id, img_filename, img_bytes)

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

        richtext_html = build_richtext_html(parsed["body_blocks"], inline_image_urls)

        field_data: dict = {}
        name_field = field_map.get("name", "name")
        field_data[name_field] = parsed.get("title") or parsed.get("cms_name", title)

        if parsed["slug"] and field_map.get("slug"):
            field_data[field_map["slug"]] = parsed["slug"]

        if parsed["meta_description"] and field_map.get("meta_description"):
            field_data[field_map["meta_description"]] = parsed["meta_description"]

        if field_map.get("hero_image") and hero_url:
            field_data[field_map["hero_image"]] = {"url": hero_url, "alt": parsed.get("title", title)}
        if field_map.get("thumbnail") and thumb_url:
            field_data[field_map["thumbnail"]] = {"url": thumb_url, "alt": parsed.get("title", title)}

        if field_map.get("richtext_body"):
            field_data[field_map["richtext_body"]] = richtext_html

        webflow_patch_cms_item(token, collection_id, item_id, field_data)

    # ── FAQ repair ───────────────────────────────────────────────────────────
    faqs = parsed.get("faqs", [])
    faq_collection_id = config.get("faq_collection_id", "")

    if not faq_collection_id:
        print("   ℹ️   No faq_collection_id in config — skipping FAQ repair")
        return

    if not faqs:
        print("   ℹ️   No FAQs found in doc — skipping FAQ repair")
        return

    existing_faq_id = FAQ_ITEM_MAP.get(title, "")
    article_name = parsed.get("cms_name") or parsed.get("title") or title

    if existing_faq_id:
        # FAQ item exists in English — just push locale copies
        print(f"   📝  FAQ item already exists ({existing_faq_id}) — adding locale copies...")
        if locale_ids:
            patch_faq_locale_copies(token, faq_collection_id, existing_faq_id, article_name, locale_ids)
            print(f"   ✅  FAQ locale copies created ({len(locale_ids)} locale(s))")
        else:
            print("   ℹ️   No secondary_locale_ids configured — nothing to add")
    else:
        # No FAQ item at all — create from scratch (English + all locales)
        locale_note = f" + {len(locale_ids)} locale(s)" if locale_ids else ""
        print(f"   📝  Creating new FAQ item ({len(faqs)} Q&As{locale_note})...")
        publish_faqs(faqs, item_id, article_name, config, dry_run=False, locale_ids=locale_ids)


def main() -> None:
    ap = argparse.ArgumentParser(description="Creator Hero — Article Repair Tool")
    ap.add_argument("--site", required=True, help="Site config slug")
    ap.add_argument("--faq-only", action="store_true", help="Only repair FAQs, skip blog PATCH")
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
            repair_article(title, item_id, config, docs_service, drive_service, faq_only=args.faq_only)
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
