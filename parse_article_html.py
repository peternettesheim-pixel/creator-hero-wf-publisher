#!/usr/bin/env python3
"""
parse_article_html.py
─────────────────────
Parses a Google Doc HTML export (from the Drive MCP download_file_content)
and outputs structured JSON for Webflow CMS publishing.

Usage:
    python3 parse_article_html.py --html-file /path/to/doc.html

Output (stdout): JSON with keys:
    cms_name, slug, title, meta_description, short_desc, category, date,
    background_color, one_min_read, author_name_1, author_page_1,
    author_status_1, author_name_2, author_page_2, author_status_2,
    richtext_body, richtext_02, richtext_03, richtext_conclusion,
    richtext_toc, faqs (list of {question, answer}), faq_meta (dict),
    images (dict: label → base64_png_string)

No network calls. Self-contained.
"""

from __future__ import annotations
import argparse
import base64
import json
import re
import sys
from html import unescape
from bs4 import BeautifulSoup, NavigableString, Tag

# ── Meta field mapping: text in doc → output key ────────────────────────────
_META_FIELDS = {
    "name":             "cms_name",
    "slug":             "slug",
    "short desc":       "short_desc",
    "title":            "title",
    "meta description": "meta_description",
    "category":         "category",
    "date":             "date",
    "background color": "background_color",
    "1 min read":       "one_min_read",
    "one min read":     "one_min_read",
    "author name 1":    "author_name_1",
    "author page 1":    "author_page_1",
    "author status 1":  "author_status_1",
    "author name 2":    "author_name_2",
    "author page 2":    "author_page_2",
    "author status 2":  "author_status_2",
    "canonical":        "canonical",
}

_IMAGE_LABELS = {
    "MAIN IMAGE":       "hero",
    "HERO IMAGE":       "hero",
    "SORT THUMBNAILS":  "thumbnail",
    "THUMBNAIL IMAGE":  "thumbnail",
    "AUTHOR IMAGE 1":   "author_image_1",
    "AUTHOR IMAGE 2":   "author_image_2",
    "IMAGE 01":         "image_01",
    "IMAGE 02":         "image_02",
    "IMAGE 03":         "image_03",
}

# Heading level shift: Google h1 → Webflow h2, h2 → h3, etc.
_HEADING_SHIFT = 1


def _get_text(el) -> str:
    """Plain text of element, whitespace-normalised."""
    return unescape(el.get_text(" ", strip=True))


def _is_empty_p(el) -> bool:
    """True if <p> has no meaningful text/images."""
    if el.name != "p":
        return False
    text = _get_text(el).strip()
    if text:
        return False
    imgs = el.find_all("img")
    return len(imgs) == 0


def _is_bold_text(el) -> bool:
    """True if the element or its first meaningful span/strong is bold."""
    style = el.get("style", "")
    if "font-weight:700" in style or "font-weight: 700" in style:
        return True
    # mammoth outputs <strong> tags
    if el.find("strong"):
        return True
    for span in el.find_all("span"):
        s = span.get("style", "")
        if "font-weight:700" in s or "font-weight: 700" in s:
            return True
    return False


def _clean_inline_html(el) -> str:
    """
    Convert a soup element's inner content to clean inline HTML.
    Keeps <a>, <strong>, <em>, <b>, <i>, <img> but strips all style/class attrs.
    """
    parts = []
    for child in el.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif child.name in ("br",):
            parts.append("<br>")
        elif child.name in ("a",):
            href = child.get("href", "")
            # Google Docs wraps links in redirect URLs — strip that
            if "google.com/url" in href:
                m = re.search(r"[?&]q=([^&]+)", href)
                if m:
                    href = m.group(1)
            inner = _clean_inline_html(child)
            parts.append(f'<a href="{href}">{inner}</a>')
        elif child.name in ("strong", "b"):
            inner = _clean_inline_html(child)
            parts.append(f"<strong>{inner}</strong>")
        elif child.name in ("em", "i"):
            inner = _clean_inline_html(child)
            parts.append(f"<em>{inner}</em>")
        elif child.name == "span":
            s = child.get("style", "")
            inner = _clean_inline_html(child)
            is_bold = "font-weight:700" in s or "font-weight: 700" in s
            is_italic = "font-style:italic" in s or "font-style: italic" in s
            if is_bold:
                inner = f"<strong>{inner}</strong>"
            if is_italic:
                inner = f"<em>{inner}</em>"
            parts.append(inner)
        elif child.name == "img":
            # Inline image — replaced by placeholder; caller handles
            src = child.get("src", "")
            parts.append(f'__IMG_PLACEHOLDER_{src[:20]}__')
        else:
            parts.append(_clean_inline_html(child))
    return "".join(parts)


def _p_to_html(p) -> str:
    """Convert a <p> element to clean HTML paragraph or return empty string."""
    text = _get_text(p)
    if not text and not p.find("img"):
        return ""
    inner = _clean_inline_html(p)
    inner = inner.strip()
    if not inner:
        return ""
    return f"<p>{inner}</p>"


def _li_to_html(li) -> str:
    inner = _clean_inline_html(li).strip()
    return f"<li>{inner}</li>" if inner else ""


def _heading_to_html(h) -> str:
    level = int(h.name[1]) + _HEADING_SHIFT
    level = min(level, 6)
    inner = _clean_inline_html(h).strip()
    return f"<h{level}>{inner}</h{level}>" if inner else ""


def _element_text_stripped(el) -> str:
    """Strip all HTML tags and return plain text."""
    return re.sub(r"\s+", " ", _get_text(el)).strip()


def _parse_key_value(el) -> tuple[str, str] | None:
    """
    If element looks like 'Key: Value' (with bold key), return (key_lower, value).
    Returns None otherwise.
    """
    text = _element_text_stripped(el)
    # Must contain ': '
    if ": " not in text and ":" not in text:
        return None
    # Check there's a bold span or <strong> tag that ends with ':'
    bold_candidates = list(el.find_all("span")) + list(el.find_all("strong"))
    for bold_el in bold_candidates:
        s_text = bold_el.get_text(strip=True)
        s_style = bold_el.get("style", "")
        is_bold_tag = bold_el.name == "strong"
        is_bold_style = "font-weight:700" in s_style or "font-weight: 700" in s_style
        if (is_bold_tag or is_bold_style) and s_text.rstrip().endswith(":"):
            key = unescape(s_text.rstrip()[:-1]).strip().lower()
            # Value is everything after the bold key element
            full = unescape(text)
            sep = s_text.rstrip()[:-1].strip() + ":"
            idx = full.find(sep)
            if idx != -1:
                value = full[idx + len(sep):].strip()
                if value:
                    return key, value
    # Fallback: if entire paragraph is bold ("Name: value" all in <strong>)
    strong = el.find("strong")
    if strong:
        full_text = unescape(strong.get_text(strip=True))
        if ":" in full_text:
            colon_idx = full_text.index(":")
            key = full_text[:colon_idx].strip().lower()
            value = full_text[colon_idx+1:].strip()
            if key and value:
                return key, value
    return None


def _is_separator(el) -> bool:
    """True if element is the —DESCRIPTION 1— separator."""
    text = _element_text_stripped(el)
    return bool(re.search(r"[-—]{1,3}\s*DESCRIPTION\s*1\s*[-—]{1,3}", text, re.IGNORECASE))


def _is_toc_start(el) -> bool:
    text = _element_text_stripped(el)
    return bool(re.match(r"Table\s+of\s+Content", text, re.IGNORECASE))


def _is_faqs(el) -> bool:
    text = _element_text_stripped(el).strip()
    return text == "FAQs" or text == "FAQ"


def _is_image_label(el) -> str | None:
    """Return normalised label like 'HERO IMAGE' if element is an image label, else None."""
    text = _element_text_stripped(el).strip()
    # Remove surrounding [ ]
    text = re.sub(r"^\[|\]$", "", text).strip().upper()
    if text in _IMAGE_LABELS:
        return text
    return None


def _extract_image_b64(el) -> str | None:
    """Extract base64 PNG data from an element that contains an <img> tag."""
    img = el.find("img")
    if not img:
        # Try parent
        return None
    src = img.get("src", "")
    if src.startswith("data:"):
        # data:image/png;base64,<data>
        m = re.match(r"data:[^;]+;base64,(.+)", src)
        if m:
            return m.group(1)
    return None


def parse_html(html_content: str) -> dict:
    soup = BeautifulSoup(html_content, "lxml")
    body = soup.find("body")
    if not body:
        raise ValueError("No <body> found in HTML")

    result = {
        "cms_name": "", "slug": "", "title": "", "meta_description": "",
        "short_desc": "", "category": "", "date": "", "background_color": "",
        "one_min_read": "", "canonical": "",
        "author_name_1": "", "author_page_1": "", "author_status_1": "",
        "author_name_2": "", "author_page_2": "", "author_status_2": "",
        "richtext_body": "", "richtext_02": "", "richtext_03": "",
        "richtext_conclusion": "", "richtext_toc": "",
        "faqs": [],  # list of {"question": ..., "answer": ...}
        "faq_meta": {},  # {"name": ..., "slug": ..., "blog": ...}
        "images": {},  # label (e.g. "hero") → base64 PNG string
        "errors": [],
    }

    # ── Phase tracking ────────────────────────────────────────────────────────
    PHASE_META    = "meta"
    PHASE_BODY    = "body"
    PHASE_TOC     = "toc"
    PHASE_FAQ_META= "faq_meta"
    PHASE_FAQ_QA  = "faq_qa"
    phase = PHASE_META

    pending_image_label: str | None = None  # label waiting for next <img>
    body_blocks: list[str] = []
    toc_blocks: list[str] = []
    current_section = "default"  # default | richtext_2 | richtext_3 | conclusion

    faq_questions: list[str] = []
    faq_answers: list[str] = []
    last_was_question = False

    # ── Walk top-level elements in body ──────────────────────────────────────
    elements = list(body.children)

    i = 0
    while i < len(elements):
        el = elements[i]
        i += 1

        if isinstance(el, NavigableString):
            continue

        tag = el.name
        if not tag:
            continue

        # ── META PHASE ───────────────────────────────────────────────────────
        if phase == PHASE_META:
            if _is_separator(el):
                phase = PHASE_BODY
                continue

            # Check for image label
            label = _is_image_label(el)
            if label:
                pending_image_label = label
                continue

            # Check if element contains an image (next image after label)
            img_b64 = _extract_image_b64(el)
            if img_b64 is not None:
                if pending_image_label:
                    key = _IMAGE_LABELS[pending_image_label]
                    result["images"][key] = img_b64
                    pending_image_label = None
                continue

            # Check for key: value metadata
            kv = _parse_key_value(el)
            if kv:
                key_raw, value = kv
                if key_raw in _META_FIELDS:
                    field = _META_FIELDS[key_raw]
                    result[field] = value
                # Also catch FAQ meta keys that appear after first FAQs marker
                continue

        # ── BODY PHASE ───────────────────────────────────────────────────────
        elif phase == PHASE_BODY:
            if _is_toc_start(el):
                phase = PHASE_TOC
                continue

            if _is_faqs(el):
                # Check if it's the "stop body" FAQs (standalone bold paragraph)
                phase = PHASE_FAQ_META
                continue

            # Check for section markers like [RICHTEXT 2], [RICHTEXT 3], [CONCLUSION]
            text_upper = _element_text_stripped(el).strip().upper()
            if re.match(r"^\[RICHTEXT\s*2\]$", text_upper):
                current_section = "richtext_2"
                continue
            if re.match(r"^\[RICHTEXT\s*3\]$", text_upper):
                current_section = "richtext_3"
                continue
            if re.match(r"^\[CONCLUSION\]$", text_upper):
                current_section = "conclusion"
                continue

            # Handle EMBED blocks: [EMBED: <html>]
            # Use raw HTML of element since Google Docs renders tags inside embeds
            raw_inner = el.decode_contents()
            embed_match = re.search(r"\[EMBED:\s*(.*?)\]", raw_inner, re.DOTALL | re.IGNORECASE)
            if embed_match:
                embed_html = embed_match.group(1).strip()
                # Unescape HTML entities (Google encodes < > in export)
                embed_html = unescape(embed_html)
                # Strip any zero-width chars
                embed_html = re.sub(r"[​‌‍﻿]", "", embed_html)
                block = f'<div class="w-embed">{embed_html}</div>'
                _add_to_section(body_blocks, current_section, block, result)
                continue

            # Handle headings
            if tag and re.match(r"^h[1-6]$", tag):
                html_out = _heading_to_html(el)
                if html_out:
                    _add_to_section(body_blocks, current_section, html_out, result)
                continue

            # Handle images in body
            img_b64 = _extract_image_b64(el)
            if img_b64 is not None:
                # Inline image in body — store as named body image
                key = f"body_image_{len([k for k in result['images'] if k.startswith('body_image_')]) + 1}"
                result["images"][key] = img_b64
                # We'll replace inline images with a placeholder for now
                # (they'll be uploaded to Drive and URL substituted later)
                block = f'<p>__BODY_IMAGE_{key}__</p>'
                _add_to_section(body_blocks, current_section, block, result)
                continue

            # Handle lists
            if tag in ("ul", "ol"):
                items = [_li_to_html(li) for li in el.find_all("li", recursive=False) if _li_to_html(li)]
                if items:
                    list_html = f'<{tag}>{"".join(items)}</{tag}>'
                    _add_to_section(body_blocks, current_section, list_html, result)
                continue

            # Handle paragraphs
            if tag == "p":
                if _is_empty_p(el):
                    continue
                html_out = _p_to_html(el)
                if html_out:
                    _add_to_section(body_blocks, current_section, html_out, result)
                continue

        # ── TOC PHASE ────────────────────────────────────────────────────────
        elif phase == PHASE_TOC:
            if _is_faqs(el):
                phase = PHASE_FAQ_META
                continue

            if tag in ("ul", "ol"):
                items = [_li_to_html(li) for li in el.find_all("li", recursive=False) if _li_to_html(li)]
                if items:
                    toc_blocks.append(f'<{tag}>{"".join(items)}</{tag}>')
                continue

            if tag == "p" and not _is_empty_p(el):
                html_out = _p_to_html(el)
                if html_out:
                    toc_blocks.append(html_out)
                continue

        # ── FAQ META PHASE (Name/Slug/Blog) ──────────────────────────────────
        elif phase == PHASE_FAQ_META:
            if _is_faqs(el):
                phase = PHASE_FAQ_QA
                last_was_question = False
                continue
            kv = _parse_key_value(el)
            if kv:
                key_raw, value = kv
                result["faq_meta"][key_raw] = value
            continue

        # ── FAQ Q&A PHASE ────────────────────────────────────────────────────
        elif phase == PHASE_FAQ_QA:
            if tag == "p" and _is_empty_p(el):
                continue
            if tag != "p":
                continue

            text_content = _element_text_stripped(el)
            is_bold = _is_bold_text(el)

            if is_bold and text_content:
                faq_questions.append(text_content)
                last_was_question = True
            elif text_content and last_was_question:
                faq_answers.append(text_content)
                last_was_question = False
            elif text_content:
                # Non-bold paragraph after answer — might be continuation
                if faq_answers:
                    faq_answers[-1] += " " + text_content

    # ── Finalise sections ────────────────────────────────────────────────────
    if body_blocks:
        result["richtext_body"] = "\n".join(body_blocks)

    if toc_blocks:
        result["richtext_toc"] = "\n".join(toc_blocks)

    # Pair FAQ questions and answers
    for q, a in zip(faq_questions, faq_answers):
        result["faqs"].append({"question": q, "answer": a})

    return result


def _add_to_section(body_blocks: list, section: str, html: str, result: dict):
    """Route block to the correct richtext section."""
    if section == "default":
        body_blocks.append(html)
    elif section == "richtext_2":
        result["richtext_02"] = result.get("richtext_02", "") + html + "\n"
    elif section == "richtext_3":
        result["richtext_03"] = result.get("richtext_03", "") + html + "\n"
    elif section == "conclusion":
        result["richtext_conclusion"] = result.get("richtext_conclusion", "") + html + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--html-file", required=True, help="Path to exported HTML file")
    args = parser.parse_args()

    with open(args.html_file, encoding="utf-8") as f:
        html_content = f.read()

    parsed = parse_html(html_content)

    # Output images separately (large base64 strings) — write to sibling files
    import os
    base_dir = os.path.dirname(args.html_file)
    image_paths = {}
    for label, b64 in parsed.pop("images", {}).items():
        img_path = os.path.join(base_dir, f"img_{label}.png")
        with open(img_path, "wb") as f:
            f.write(base64.b64decode(b64))
        image_paths[label] = img_path

    parsed["image_paths"] = image_paths
    print(json.dumps(parsed, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
