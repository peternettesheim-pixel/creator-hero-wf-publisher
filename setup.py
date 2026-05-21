"""
Creator Hero — Setup Script
-----------------------------
Discovers your Webflow site, collections, and field slugs via the API,
then writes a ready-to-use sites/creator-hero.json config file.

Run once before the first publish:
    python3 setup.py --site creator-hero
"""

import argparse
import json
import sys
from pathlib import Path

import requests


def wf_get(token: str, path: str) -> dict:
    resp = requests.get(
        f"https://api.webflow.com/v2/{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code not in (200, 201):
        print(f"❌  Webflow API error ({resp.status_code}): {resp.text[:300]}")
        sys.exit(1)
    return resp.json()


def pick(items: list, label_key: str, prompt: str) -> dict:
    print()
    for i, item in enumerate(items):
        print(f"  {i + 1}. {item.get(label_key, '')}  [{item.get('id', '')}]")
    while True:
        raw = input(f"{prompt}: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        print(f"   Enter a number between 1 and {len(items)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="creator-hero", help="Config slug to create")
    args = ap.parse_args()

    print("=" * 60)
    print("  Creator Hero — Webflow Publisher Setup")
    print("=" * 60)

    # ── API key ───────────────────────────────────────────────────────────
    api_key = input("\nPaste your Webflow API key: ").strip()
    if not api_key:
        print("❌  No API key entered.")
        sys.exit(1)

    # ── Site ID ───────────────────────────────────────────────────────────
    # Site-level tokens can't list all sites, so we ask for the ID directly.
    # Find it in: Webflow Dashboard → open your site → Site Settings → General → Site ID
    # It also appears in the designer URL: webflow.com/design/SITE-ID
    print("\nFind your Site ID in Webflow: Site Settings → General → Site ID")
    site_id = input("Paste Site ID: ").strip()
    if not site_id:
        print("❌  No Site ID entered.")
        sys.exit(1)

    # Verify the token works against this site
    test = wf_get(api_key, f"sites/{site_id}/collections")
    print(f"   ✅  Token verified — {len(test.get('collections', []))} collection(s) found")

    # ── Select blog collection ────────────────────────────────────────────
    collections = test.get("collections", [])
    if not collections:
        print("❌  No collections found for this site.")
        sys.exit(1)

    print("\nCMS Collections:")
    col = pick(collections, "displayName", "Select blog collection number")
    collection_id = col["id"]
    print(f"   ✅  Selected: {col.get('displayName')} ({collection_id})")

    # ── Fetch fields ──────────────────────────────────────────────────────
    col_detail = wf_get(api_key, f"collections/{collection_id}")
    fields = col_detail.get("fields", [])

    print(f"\nFields in '{col.get('displayName')}':")
    print(f"  {'Slug':<35} {'Type':<20} Display Name")
    print("  " + "-" * 70)
    for f in fields:
        print(f"  {f.get('slug', ''):<35} {f.get('type', ''):<20} {f.get('displayName', '')}")

    # ── Build field mapping interactively ────────────────────────────────
    print("\n" + "=" * 60)
    print("  Map your CMS fields")
    print("  (press Enter to skip optional fields)")
    print("=" * 60)

    def ask_slug(label: str, required: bool = False, default: str = "") -> str:
        hint = f" [{default}]" if default else (" (required)" if required else " (optional)")
        val = input(f"  {label}{hint}: ").strip()
        if not val and default:
            return default
        if not val and required:
            print(f"  ⚠️   '{label}' is required")
        return val

    field_mapping: dict = {}
    field_mapping["name"] = ask_slug("Title field slug", required=True, default="name")
    field_mapping["slug"] = ask_slug("URL slug field slug", default="slug")
    field_mapping["meta_description"] = ask_slug("Meta description field slug")
    field_mapping["hero_image"] = ask_slug("Hero image field slug")
    field_mapping["thumbnail"] = ask_slug("Thumbnail field slug")
    field_mapping["richtext_body"] = ask_slug("Main richtext body field slug")
    field_mapping["category"] = ask_slug("Category reference field slug")

    # Remove empty optional entries
    field_mapping = {k: v for k, v in field_mapping.items() if v}

    # ── Category map ──────────────────────────────────────────────────────
    category_map: dict = {}
    print("\n  Category IDs (from Webflow CMS → Categories collection → each item ID)")
    print("  Leave blank when done.")
    while True:
        cat_name = input("  Category name (e.g. 'Influencer Marketing'): ").strip()
        if not cat_name:
            break
        cat_id = input(f"  Webflow item ID for '{cat_name}': ").strip()
        if cat_id:
            category_map[cat_name] = cat_id

    # ── Write config ──────────────────────────────────────────────────────
    config = {
        "webflow_token": api_key,
        "webflow_site_id": site_id,
        "blog_collection_id": collection_id,
        "drive_folder_id": "1KyBmiE2X48v4cjeSwOlPNDSMhwayM259",
        "blog_field_mapping": field_mapping,
        "category_map": category_map,
        "extra_fields": {},
    }

    sites_dir = Path(__file__).parent / "sites"
    sites_dir.mkdir(exist_ok=True)
    config_path = sites_dir / f"{args.site}.json"

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    print(f"\n✅  Config saved → {config_path}")
    print("\nNext steps:")
    print(f"  1. Review {config_path} — adjust field mappings if needed")
    print(f"  2. python3 publish.py --site {args.site} --dry-run")
    print(f"  3. python3 publish.py --site {args.site} --doc-id 1w7ts-G4JrX7t68Z0vupIrhC-UYOiY9K_-zv6aQ3UCdA")


if __name__ == "__main__":
    main()
