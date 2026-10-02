"""
Manga Studio Server with Multi-Project Management and Gemini AI Pipeline.
Serves static files on port 8000 and provides REST endpoints for:
- Creating dedicated manga project folders (with references/ and pages/ subfolders)
- Generating character reference sheets directly to disk using gemini-3.1-flash-lite-image
- Generating graphic novel pages directly to disk
- Story bible saving and project listing
"""

import os
import sys
import json
import re
import io
import time
import base64
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
PROJECTS_DIR = BASE_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

MODEL_NAME = "gemini-3.1-flash-lite-image"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"

SUPPORTED_ASPECT_RATIOS = ["1:1", "3:4", "4:3", "9:16", "16:9"]
DEFAULT_ASPECT_RATIO = "3:4"

def get_default_api_key():
    return os.environ.get("GEMINI_API_KEY", "")

LANGUAGES = {
    "en": {"name": "ENGLISH", "note": ""},
    "fr": {"name": "FRENCH", "note": ", with correct French accents (é, è, ê, à, ç) and punctuation"},
}
DEFAULT_LANGUAGE = "en"

def parse_language(payload: dict):
    """Returns the requested manga text language code, the default when omitted, or None when unsupported."""
    lang = str(payload.get("language") or "").strip().lower() or DEFAULT_LANGUAGE
    return lang if lang in LANGUAGES else None

def language_rule(lang: str) -> str:
    """Upper-case name plus any typography note, e.g. 'FRENCH ONLY, with correct French accents ...'."""
    return f"{LANGUAGES[lang]['name']} ONLY{LANGUAGES[lang]['note']}"

def parse_aspect_ratio(payload: dict):
    """Returns the requested ratio, the default when omitted, or None when unsupported."""
    ratio = str(payload.get("aspect_ratio") or "").strip() or DEFAULT_ASPECT_RATIO
    return ratio if ratio in SUPPORTED_ASPECT_RATIOS else None

def select_reference_sheets(refs_dir: Path, prompt_text: str, limit: int = 3):
    """Prefers sheets whose character name (from the filename) appears in the prompt."""
    ref_files = sorted(list(refs_dir.glob("*.jpg")) + list(refs_dir.glob("*.png")))
    text = re.sub(r"[^a-z0-9]+", " ", prompt_text.lower())

    def is_mentioned(ref: Path) -> bool:
        words = ref.stem.lower().replace("_reference_sheet", "").split("_")
        return any(len(w) >= 3 and re.search(rf"\b{re.escape(w)}\b", text) for w in words)

    matched = [f for f in ref_files if is_mentioned(f)]
    return (matched or ref_files)[:limit]

def reference_label(ref: Path) -> str:
    return ref.stem.lower().replace("_reference_sheet", "").replace("_", " ").title()

def enforce_aspect_ratio(image_bytes: bytes, aspect_ratio: str) -> bytes:
    """Center-crops the image to exactly aspect_ratio and returns JPEG bytes.

    The model's native sizes only approximate each ratio (e.g. 864x1184 for 3:4),
    and it can ignore imageConfig entirely, so every saved image is normalized here.
    """
    ratio_w, ratio_h = (int(n) for n in aspect_ratio.split(":"))
    target = ratio_w / ratio_h
    with Image.open(io.BytesIO(image_bytes)) as img:
        width, height = img.size
        if width / height > target:
            new_w, new_h = round(height * target), height
        else:
            new_w, new_h = width, round(width / target)

        if (new_w, new_h) == (width, height) and img.format == "JPEG":
            return image_bytes

        left = (width - new_w) // 2
        top = (height - new_h) // 2
        cropped = img.crop((left, top, left + new_w, top + new_h)).convert("RGB")
        out = io.BytesIO()
        cropped.save(out, format="JPEG", quality=95)
        return out.getvalue()

def sanitize_slug(name: str) -> str:
    slug = re.sub(r'[^a-zA-Z0-9_\-]', '_', name.lower().strip())
    slug = re.sub(r'_+', '_', slug).strip('_')
    return slug or f"manga_{int(time.time())}"

def encode_local_image(path: Path):
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    if path.suffix.lower() == ".webp":
        mime = "image/webp"
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return {"mimeType": mime, "data": data}

class MangaStudioHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/projects":
            self.handle_list_projects()
            return
        elif path == "/api/project_info":
            project_id = query.get("project", [""])[0]
            self.handle_project_info(project_id)
            return

        # Default static file serving from BASE_DIR
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
        try:
            payload = json.loads(post_data.decode('utf-8'))
        except Exception:
            payload = {}

        if path == "/api/create_project":
            self.handle_create_project(payload)
        elif path == "/api/save_project_state":
            self.handle_save_project_state(payload)
        elif path == "/api/generate_reference":
            self.handle_generate_reference(payload)
        elif path == "/api/generate_page":
            self.handle_generate_page(payload)
        elif path == "/api/save_story":
            self.handle_save_story(payload)
        else:
            self.send_json({"error": f"Endpoint not found: {path}"}, status=404)

    def handle_list_projects(self):
        projects = []
        
        # 1. Grimoire (default)
        grimoire_refs = [f.name for f in (BASE_DIR / "references").glob("*.jpg")] + [f.name for f in (BASE_DIR / "references").glob("*.png")]
        grimoire_pages = [f.name for f in (BASE_DIR / "pages").glob("*.jpg")] + [f.name for f in (BASE_DIR / "pages").glob("*.png")]
        projects.append({
            "id": "grimoire",
            "title": "GRIMOIRE (Dark Fantasy Novella)",
            "path": "",
            "references_dir": "references",
            "pages_dir": "pages",
            "reference_count": len(grimoire_refs),
            "page_count": len(grimoire_pages),
            "references": sorted(grimoire_refs),
            "pages": sorted(grimoire_pages),
            "is_default": True
        })

        # 2. Custom projects in /projects/
        if PROJECTS_DIR.exists():
            for p_dir in sorted(PROJECTS_DIR.iterdir()):
                if p_dir.is_dir():
                    refs_dir = p_dir / "references"
                    pages_dir = p_dir / "pages"
                    refs = [f.name for f in refs_dir.glob("*.*") if f.suffix.lower() in [".jpg", ".png", ".webp"]] if refs_dir.exists() else []
                    pages = [f.name for f in pages_dir.glob("*.*") if f.suffix.lower() in [".jpg", ".png", ".webp"]] if pages_dir.exists() else []
                    
                    # Read metadata if exists
                    meta_file = p_dir / "project.json"
                    meta = {}
                    if meta_file.exists():
                        try:
                            with open(meta_file, "r", encoding="utf-8") as mf:
                                meta = json.load(mf)
                        except Exception:
                            pass

                    title = meta.get("title", p_dir.name.replace('_', ' ').title())
                    projects.append({
                        "id": p_dir.name,
                        "title": title,
                        "path": f"projects/{p_dir.name}",
                        "references_dir": f"projects/{p_dir.name}/references",
                        "pages_dir": f"projects/{p_dir.name}/pages",
                        "reference_count": len(refs),
                        "page_count": len(pages),
                        "references": sorted(refs),
                        "pages": sorted(pages),
                        "meta": meta,
                        "is_default": False
                    })

        self.send_json({"projects": projects})

    def handle_project_info(self, project_id: str):
        if not project_id or project_id.lower() == "grimoire":
            refs = [f"references/{f.name}" for f in (BASE_DIR / "references").glob("*.*") if f.suffix.lower() in [".jpg", ".png", ".webp"]]
            pages = [f"pages/{f.name}" for f in (BASE_DIR / "pages").glob("*.*") if f.suffix.lower() in [".jpg", ".png", ".webp"]]
            grimoire_meta = {}
            g_meta_file = BASE_DIR / "grimoire.json"
            if g_meta_file.exists():
                try:
                    with open(g_meta_file, "r", encoding="utf-8") as gf:
                        grimoire_meta = json.load(gf)
                except Exception:
                    pass
            self.send_json({
                "id": "grimoire",
                "title": grimoire_meta.get("title", "GRIMOIRE (Dark Fantasy Novella)"),
                "folder": "",
                "references": sorted(refs),
                "pages": sorted(pages),
                "meta": grimoire_meta,
                "bible_path": "story_bible.md"
            })
            return

        p_dir = PROJECTS_DIR / project_id
        if not p_dir.exists():
            self.send_json({"error": f"Project '{project_id}' not found."}, status=404)
            return

        refs_dir = p_dir / "references"
        pages_dir = p_dir / "pages"
        refs = [f"projects/{project_id}/references/{f.name}" for f in refs_dir.glob("*.*") if f.suffix.lower() in [".jpg", ".png", ".webp"]] if refs_dir.exists() else []
        pages = [f"projects/{project_id}/pages/{f.name}" for f in pages_dir.glob("*.*") if f.suffix.lower() in [".jpg", ".png", ".webp"]] if pages_dir.exists() else []

        meta_file = p_dir / "project.json"
        meta = {}
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as mf:
                    meta = json.load(mf)
            except Exception:
                pass

        bible_file = p_dir / "story_bible.md"
        bible_text = bible_file.read_text(encoding="utf-8") if bible_file.exists() else ""

        self.send_json({
            "id": project_id,
            "title": meta.get("title", project_id.replace('_', ' ').title()),
            "folder": f"projects/{project_id}",
            "references": sorted(refs),
            "pages": sorted(pages),
            "meta": meta,
            "bible_text": bible_text
        })

    def handle_create_project(self, payload: dict):
        title = payload.get("title", "").strip() or "Untitled Manga"
        slug = payload.get("slug", "").strip() or sanitize_slug(title)
        genre = payload.get("genre", "whimsical_fantasy")
        concept = payload.get("concept", "").strip()
        characters = payload.get("characters", "").strip()
        page_count = payload.get("page_count", 50)
        emotional_impact = payload.get("emotional_impact", "")
        acts = payload.get("acts", [])
        pages_blueprint = payload.get("pages_blueprint", [])

        proj_dir = PROJECTS_DIR / slug
        refs_dir = proj_dir / "references"
        pages_dir = proj_dir / "pages"

        proj_dir.mkdir(parents=True, exist_ok=True)
        refs_dir.mkdir(parents=True, exist_ok=True)
        pages_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "id": slug,
            "title": title,
            "genre": genre,
            "concept": concept,
            "characters": characters,
            "page_count": page_count,
            "emotional_impact": emotional_impact,
            "aspect_ratio": payload.get("aspect_ratio", "3:4"),
            "language": payload.get("language", DEFAULT_LANGUAGE),
            "acts": acts,
            "pages_blueprint": pages_blueprint,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(proj_dir / "project.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        # Write story_bible.md
        bible_lines = [
            f"# {title} — Story & Script Bible ({page_count} Pages)",
            "",
            "## Master Story Concept",
            concept,
            "",
            f"**Emotional Resonance:** {emotional_impact}",
            "",
            "## Character Cast & Archetypes",
            characters,
            "",
            "## The 5 Movements",
            ""
        ]
        if acts:
            for act in acts:
                bible_lines.append(f"### {act.get('title', 'Movement')} ({act.get('range', '')})")
                bible_lines.append(act.get('text', ''))
                bible_lines.append("")

        if pages_blueprint:
            bible_lines.append(f"## Page-by-Page Director's Blueprint ({len(pages_blueprint)} Pages)")
            bible_lines.append("")
            for p in pages_blueprint:
                bible_lines.append(f"### Page {p.get('page')}: {p.get('title')} ({p.get('movement')})")
                bible_lines.append(f"**Scene Nature & Visual Framing:**\n{p.get('nature', '')}\n")
                bible_lines.append(f"**Emotional Resonance & Impact:**\n{p.get('impact', '')}\n")
                if p.get('script'):
                    bible_lines.append(f"**Panel Script:**\n{p.get('script')}\n")

        with open(proj_dir / "story_bible.md", "w", encoding="utf-8") as f:
            f.write("\n".join(bible_lines))

        self.send_json({
            "status": "ok",
            "message": f"Manga project '{title}' initialized successfully.",
            "project": {
                "id": slug,
                "title": title,
                "genre": genre,
                "folder": f"projects/{slug}",
                "references_dir": f"projects/{slug}/references",
                "pages_dir": f"projects/{slug}/pages"
            }
        })

    def handle_save_project_state(self, payload: dict):
        slug = payload.get("id", "").strip() or payload.get("project", "").strip()
        if not slug:
            self.send_json({"error": "Missing project slug."}, status=400)
            return

        title = payload.get("title", slug.replace('_', ' ').title())
        genre = payload.get("genre", "whimsical_fantasy")
        concept = payload.get("concept", "")
        characters = payload.get("characters", "")
        page_count = payload.get("page_count", 50)
        emotional_impact = payload.get("emotional_impact", "")
        acts = payload.get("acts", [])
        pages_blueprint = payload.get("pages_blueprint", [])

        if slug == "grimoire":
            meta_file = BASE_DIR / "grimoire.json"
        else:
            p_dir = PROJECTS_DIR / slug
            p_dir.mkdir(parents=True, exist_ok=True)
            (p_dir / "references").mkdir(parents=True, exist_ok=True)
            (p_dir / "pages").mkdir(parents=True, exist_ok=True)
            meta_file = p_dir / "project.json"

        meta = {
            "id": slug,
            "title": title,
            "genre": genre,
            "concept": concept,
            "characters": characters,
            "page_count": page_count,
            "emotional_impact": emotional_impact,
            "aspect_ratio": payload.get("aspect_ratio", "3:4"),
            "language": payload.get("language", DEFAULT_LANGUAGE),
            "acts": acts,
            "pages_blueprint": pages_blueprint,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        # Update story_bible.md
        if slug != "grimoire":
            p_dir = PROJECTS_DIR / slug
            bible_file = p_dir / "story_bible.md"
            bible_lines = [
                f"# {title} — Story & Script Bible ({page_count} Pages)",
                "",
                "## Master Story Concept",
                concept,
                "",
                f"**Emotional Resonance:** {emotional_impact}",
                "",
                "## Character Cast & Archetypes",
                characters,
                "",
                "## The 5 Movements",
                ""
            ]
            for act in acts:
                bible_lines.append(f"### {act.get('title', 'Movement')} ({act.get('range', '')})")
                bible_lines.append(act.get('text', ''))
                bible_lines.append("")
            if pages_blueprint:
                bible_lines.append(f"## Page-by-Page Director's Blueprint ({len(pages_blueprint)} Pages)")
                bible_lines.append("")
                for p in pages_blueprint:
                    bible_lines.append(f"### Page {p.get('page')}: {p.get('title')} ({p.get('movement')})")
                    bible_lines.append(f"**Scene Nature & Visual Framing:**\n{p.get('nature', '')}\n")
                    bible_lines.append(f"**Emotional Resonance & Impact:**\n{p.get('impact', '')}\n")
                    if p.get('script'):
                        bible_lines.append(f"**Panel Script:**\n{p.get('script')}\n")
            with open(bible_file, "w", encoding="utf-8") as f:
                f.write("\n".join(bible_lines))

        self.send_json({"status": "ok", "message": f"Project '{title}' state saved successfully."})

    def handle_save_story(self, payload: dict):
        slug = payload.get("project", "").strip() or "grimoire"
        if slug == "grimoire":
            target_bible = BASE_DIR / "story_bible.md"
        else:
            p_dir = PROJECTS_DIR / slug
            p_dir.mkdir(parents=True, exist_ok=True)
            target_bible = p_dir / "story_bible.md"

        bible_text = payload.get("bible_text", "")
        with open(target_bible, "w", encoding="utf-8") as f:
            f.write(bible_text)

        self.send_json({"status": "ok", "saved_to": str(target_bible.relative_to(BASE_DIR))})

    def handle_generate_reference(self, payload: dict):
        slug = payload.get("project", "").strip()
        if not slug:
            self.send_json({"error": "Missing 'project' parameter."}, status=400)
            return

        char_name = payload.get("character_name", "").strip() or "Character"
        char_desc = payload.get("description", "").strip()
        custom_prompt = payload.get("prompt", "").strip()
        api_key = payload.get("api_key", "").strip() or get_default_api_key()
        style_ref = payload.get("style_image", "style.png")
        aspect_ratio = parse_aspect_ratio(payload)
        language = parse_language(payload)

        if not api_key:
            self.send_json({"error": "No Google API Key provided or found in environment."}, status=400)
            return
        if not aspect_ratio:
            self.send_json({"error": f"Unsupported aspect_ratio. Use one of: {', '.join(SUPPORTED_ASPECT_RATIOS)}."}, status=400)
            return
        if not language:
            self.send_json({"error": f"Unsupported language. Use one of: {', '.join(LANGUAGES)}."}, status=400)
            return

        # Prepare directory
        if slug == "grimoire":
            refs_dir = BASE_DIR / "references"
        else:
            refs_dir = PROJECTS_DIR / slug / "references"
        refs_dir.mkdir(parents=True, exist_ok=True)

        clean_name = sanitize_slug(char_name)
        out_filename = f"{clean_name}_reference_sheet.jpg"
        out_path = refs_dir / out_filename

        # Determine genre art style
        genre_style = payload.get("style_prompt", "").strip()
        if not genre_style:
            if slug == "grimoire":
                genre_style = "Kentaro Miura dark fantasy manga linework, stark black ink crosshatching, dramatic chiaroscuro"
            else:
                genre_style = "Clean expressive manga character design, detailed linework, dynamic lighting"

        # Construct generation prompt
        if custom_prompt:
            prompt_text = custom_prompt
        else:
            prompt_text = (
                f"Character concept reference sheet of: {char_name}. {char_desc}. "
                f"Full body design, dynamic profile turnaround poses, ornate clothing, intricate accessories and expressive facial detail callouts. "
                f"Art style: {genre_style}. "
                f"CRITICAL: ALL text, annotations, titles, and labels MUST be in {language_rule(language)}. Zero Japanese characters."
            )

        parts = []

        # Style reference conditioning - only attach if user uploaded custom style or explicitly requested for Grimoire
        if style_ref and style_ref not in ["style.png", "default"]:
            if isinstance(style_ref, str) and style_ref.startswith("data:"):
                try:
                    meta, data_b64 = style_ref.split(",", 1)
                    mime = meta.split(";")[0].split(":")[1]
                    parts.append({"inlineData": {"mimeType": mime, "data": data_b64}})
                except Exception as e:
                    print(f"Error decoding base64 style: {e}")
            elif isinstance(style_ref, str):
                local_style = BASE_DIR / style_ref
                if local_style.exists():
                    parts.append({"inlineData": encode_local_image(local_style)})
        elif slug == "grimoire":
            # Grimoire only: attach root style.png
            local_style = BASE_DIR / "style.png"
            if local_style.exists():
                parts.append({"inlineData": encode_local_image(local_style)})

        prompt_text += f" Aspect Ratio: {aspect_ratio}."
        parts.append({"text": prompt_text})

        gen_config = {
            "responseModalities": ["IMAGE", "TEXT"],
            "imageConfig": {"aspectRatio": aspect_ratio}
        }

        payload_gemini = {
            "contents": [{"parts": parts}],
            "generationConfig": gen_config
        }

        url = f"{GEMINI_API_URL}?key={api_key}"
        try:
            resp = requests.post(url, json=payload_gemini, headers={"Content-Type": "application/json"}, timeout=120)
            if resp.status_code != 200:
                self.send_json({"error": f"Gemini API Error ({resp.status_code}): {resp.text[:300]}"}, status=resp.status_code)
                return

            res_json = resp.json()
            candidates = res_json.get("candidates", [])
            if not candidates:
                self.send_json({"error": "No candidates returned by Gemini API."}, status=500)
                return

            saved = False
            for part in candidates[0].get("content", {}).get("parts", []):
                if "inlineData" in part:
                    raw_data = enforce_aspect_ratio(base64.b64decode(part["inlineData"]["data"]), aspect_ratio)
                    with open(out_path, "wb") as f:
                        f.write(raw_data)
                    saved = True
                    break

            if not saved:
                self.send_json({"error": "Model did not return image data."}, status=500)
                return

            rel_url = f"{refs_dir.relative_to(BASE_DIR).as_posix()}/{out_filename}"
            self.send_json({
                "status": "ok",
                "character_name": char_name,
                "filename": out_filename,
                "url": rel_url,
                "folder": str(refs_dir.relative_to(BASE_DIR).as_posix())
            })

        except Exception as ex:
            self.send_json({"error": str(ex)}, status=500)

    def handle_generate_page(self, payload: dict):
        slug = payload.get("project", "").strip()
        if not slug:
            self.send_json({"error": "Missing 'project' parameter."}, status=400)
            return

        page_num = int(payload.get("page_num", 1))
        prompt_text = payload.get("prompt", "").strip()
        api_key = payload.get("api_key", "").strip() or get_default_api_key()
        aspect_ratio = parse_aspect_ratio(payload)
        language = parse_language(payload)

        if not api_key:
            self.send_json({"error": "No Google API Key provided or found in environment."}, status=400)
            return
        if not aspect_ratio:
            self.send_json({"error": f"Unsupported aspect_ratio. Use one of: {', '.join(SUPPORTED_ASPECT_RATIOS)}."}, status=400)
            return
        if not language:
            self.send_json({"error": f"Unsupported language. Use one of: {', '.join(LANGUAGES)}."}, status=400)
            return

        if slug == "grimoire":
            pages_dir = BASE_DIR / "pages"
            refs_dir = BASE_DIR / "references"
        else:
            pages_dir = PROJECTS_DIR / slug / "pages"
            refs_dir = PROJECTS_DIR / slug / "references"
        pages_dir.mkdir(parents=True, exist_ok=True)

        out_filename = f"page_{page_num:02d}.jpg"
        out_path = pages_dir / out_filename

        parts = []

        # Attach up to 3 reference sheets, labelled so the model knows which design is whose
        if refs_dir.exists():
            for rf in select_reference_sheets(refs_dir, prompt_text):
                parts.append({"text": f"Reference sheet for {reference_label(rf)}:"})
                parts.append({"inlineData": encode_local_image(rf)})

        # The previous page carries vehicle, outfit and setting designs forward between pages
        prev_path = pages_dir / f"page_{page_num - 1:02d}.jpg"
        if payload.get("attach_previous_page") and page_num > 1 and prev_path.exists():
            parts.append({"text": "Previous page of this comic, for visual continuity only (character designs, vehicles, setting, lighting). Do not copy its panels, layout or text:"})
            parts.append({"inlineData": encode_local_image(prev_path)})

        if not prompt_text:
            genre_style = payload.get("style_prompt", "").strip()
            if not genre_style:
                genre_style = "Kentaro Miura Berserk ink style, deep shadows and crosshatching" if slug == "grimoire" else "High quality graphic novel manga style, clean dynamic line-art"
            prompt_text = (
                f"Graphic novel comic book Page {page_num}. Art style: {genre_style}. "
                f"Multi-panel graphic novel layout with cinematic paneling and expressive character acting. "
                f"Speech balloons and narration boxes in {LANGUAGES[language]['name']} ONLY. "
                f"CRITICAL: ALL dialogue, titles, sound effects MUST be in clean {language_rule(language)}. Zero Japanese characters."
            )
        else:
            prompt_text += f" CRITICAL: ALL text, speech balloons, sound effects in clean {language_rule(language)}. Absolutely no Japanese."

        prompt_text += f" Aspect Ratio: {aspect_ratio}."
        parts.append({"text": prompt_text})

        gen_config = {
            "responseModalities": ["IMAGE", "TEXT"],
            "imageConfig": {"aspectRatio": aspect_ratio}
        }

        payload_gemini = {
            "contents": [{"parts": parts}],
            "generationConfig": gen_config
        }

        url = f"{GEMINI_API_URL}?key={api_key}"
        try:
            resp = requests.post(url, json=payload_gemini, headers={"Content-Type": "application/json"}, timeout=120)
            if resp.status_code != 200:
                self.send_json({"error": f"Gemini API Error ({resp.status_code}): {resp.text[:300]}"}, status=resp.status_code)
                return

            res_json = resp.json()
            candidates = res_json.get("candidates", [])
            if not candidates:
                self.send_json({"error": "No candidates returned by Gemini API."}, status=500)
                return

            saved = False
            for part in candidates[0].get("content", {}).get("parts", []):
                if "inlineData" in part:
                    raw_data = enforce_aspect_ratio(base64.b64decode(part["inlineData"]["data"]), aspect_ratio)
                    with open(out_path, "wb") as f:
                        f.write(raw_data)
                    saved = True
                    break

            if not saved:
                self.send_json({"error": "Model did not return image data."}, status=500)
                return

            rel_url = f"{pages_dir.relative_to(BASE_DIR).as_posix()}/{out_filename}"
            self.send_json({
                "status": "ok",
                "page_num": page_num,
                "filename": out_filename,
                "url": rel_url,
                "folder": str(pages_dir.relative_to(BASE_DIR).as_posix())
            })

        except Exception as ex:
            self.send_json({"error": str(ex)}, status=500)

def run(port=8000):
    server_address = ('', port)
    httpd = HTTPServer(server_address, MangaStudioHandler)
    print(f" Manga Studio Server running on http://localhost:{port}")
    print(f" Projects Root Directory: {PROJECTS_DIR}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        httpd.server_close()

if __name__ == "__main__":
    port = 8000
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    run(port)
