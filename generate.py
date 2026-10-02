"""
Nano Banana 2 Lite (gemini-3.1-flash-lite-image) Generation Script
Supports text-to-image and image-to-image (character/style reference) generation.
"""

import os
import sys
import json
import base64
import argparse
from datetime import datetime
from pathlib import Path
import requests

MODEL_NAME = "gemini-3.1-flash-lite-image"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"

def get_api_key():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("Error: GEMINI_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return key

def encode_image(image_path: Path):
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    if image_path.suffix.lower() == ".webp":
        mime = "image/webp"
    with open(image_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")
    return {"mimeType": mime, "data": encoded}

def generate_image(prompt: str, reference_images=None, output_path=None):
    api_key = get_api_key()
    parts = []

    # If reference images are supplied, include them as image parts
    if reference_images:
        for ref in reference_images:
            ref_path = Path(ref)
            if not ref_path.exists():
                print(f"Warning: Reference image {ref} does not exist. Skipping.", file=sys.stderr)
                continue
            print(f"Adding reference image: {ref_path}")
            parts.append({"inlineData": encode_image(ref_path)})

    parts.append({"text": prompt})

    payload = {
        "contents": [{
            "parts": parts
        }],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"]
        }
    }

    url = f"{API_URL}?key={api_key}"
    print(f"Calling {MODEL_NAME} (Nano Banana 2 Lite)...")
    resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"})

    if resp.status_code != 200:
        print(f"API Error ({resp.status_code}):", resp.text, file=sys.stderr)
        return None

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        print("No candidates returned from model:", json.dumps(data, indent=2), file=sys.stderr)
        return None

    response_parts = candidates[0].get("content", {}).get("parts", [])
    saved_files = []

    for i, part in enumerate(response_parts):
        if "inlineData" in part:
            raw_data = base64.b64decode(part["inlineData"]["data"])
            mime_type = part["inlineData"].get("mimeType", "image/jpeg")
            ext = ".png" if "png" in mime_type else ".jpg"

            if output_path:
                target = Path(output_path)
                if target.suffix == "":
                    target = target.with_suffix(ext)
            else:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                target = Path(f"output_{timestamp}_{i+1}{ext}")

            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as f:
                f.write(raw_data)
            print(f" Image saved to: {target.resolve()}")
            saved_files.append(target)
        elif "text" in part:
            print(f"Model note: {part['text']}")

    return saved_files

def main():
    parser = argparse.ArgumentParser(description="Generate images with gemini-3.1-flash-lite-image (Nano Banana 2 Lite)")
    parser.add_argument("prompt", type=str, nargs="?", help="Text prompt for image generation")
    parser.add_argument("-o", "--output", type=str, default=None, help="Output image path")
    parser.add_argument("-r", "--ref", type=str, nargs="*", default=[], help="Path(s) to reference image(s)")

    args = parser.parse_args()
    if not args.prompt:
        parser.print_help()
        sys.exit(0)

    generate_image(args.prompt, reference_images=args.ref, output_path=args.output)

if __name__ == "__main__":
    main()
