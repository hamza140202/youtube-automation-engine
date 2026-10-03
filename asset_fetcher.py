import os
import random
import requests
from pathlib import Path
from typing import List, Dict, Any
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from config import PEXELS_API_KEY, TEMP_DIR, FORMATS

def create_cinematic_backdrop(scene_id: int, query: str, width: int = 1080, height: int = 1920) -> Path:
    """
    Generates a pristine, high-resolution cinematic background image with atmospheric depth,
    lighting particles, and subtle topic ambiance for the scene.
    """
    output_path = TEMP_DIR / f"scene_{scene_id}_bg.png"
    
    # Atmospheric palettes based on mood / topic
    color_palettes = [
        [(15, 23, 42), (30, 41, 59), (51, 65, 85), (56, 189, 248)],     # Deep cosmic cyan
        [(24, 24, 27), (39, 39, 42), (88, 28, 135), (192, 132, 252)],   # Mysterious purple
        [(17, 24, 39), (31, 41, 55), (6, 78, 59), (52, 211, 153)],      # Oceanic emerald
        [(18, 18, 18), (38, 20, 20), (127, 29, 29), (248, 113, 113)],   # Dramatic crimson
        [(10, 15, 30), (20, 30, 60), (30, 58, 138), (96, 165, 250)],    # Deep space blue
    ]
    
    palette = color_palettes[scene_id % len(color_palettes)]
    img = Image.new("RGB", (width, height), palette[0])
    draw = ImageDraw.Draw(img)
    
    # 1. Base gradient
    steps = 100
    for i in range(steps):
        ratio = i / steps
        r = int(palette[0][0] * (1 - ratio) + palette[1][0] * ratio)
        g = int(palette[0][1] * (1 - ratio) + palette[1][1] * ratio)
        b = int(palette[0][2] * (1 - ratio) + palette[1][2] * ratio)
        y_start = int(height * (i / steps))
        y_end = int(height * ((i + 1) / steps))
        draw.rectangle([(0, y_start), (width, y_end)], fill=(r, g, b))
        
    # 2. Glowing focal orb / nebula aura in center
    aura_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    aura_draw = ImageDraw.Draw(aura_img)
    center_x = width // 2 + random.randint(-50, 50)
    center_y = height // 2 + random.randint(-100, 100)
    radius = min(width, height) // 2
    
    aura_color = palette[3]
    aura_draw.ellipse(
        [(center_x - radius, center_y - radius), (center_x + radius, center_y + radius)],
        fill=(aura_color[0], aura_color[1], aura_color[2], 40)
    )
    aura_img = aura_img.filter(ImageFilter.GaussianBlur(radius=80))
    
    # Merge aura
    img.paste(aura_img, (0, 0), aura_img)
    
    # 3. Particle constellations / cinematic stars
    star_draw = ImageDraw.Draw(img)
    random.seed(scene_id * 42)
    for _ in range(120):
        px = random.randint(0, width)
        py = random.randint(0, height)
        size = random.choice([1, 2, 3])
        brightness = random.randint(140, 255)
        star_draw.ellipse([(px, py), (px + size, py + size)], fill=(brightness, brightness, brightness))
        
    # 4. Focal Graphic Card & Scene Highlights
    card_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    c_draw = ImageDraw.Draw(card_overlay)
    
    # Pill badge at top (y=220)
    badge_w, badge_h = 360, 50
    badge_x = (width - badge_w) // 2
    badge_y = 220
    c_draw.rounded_rectangle([(badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h)], radius=25, fill=(0, 0, 0, 180), outline=palette[3], width=2)
    badge_text = f"● RESEARCH LOG #{scene_id:02d}"
    c_draw.text((badge_x + 60, badge_y + 14), badge_text, fill=(255, 255, 255, 240))
    
    # Modern focal card in upper third (y=340 to 520)
    card_w = int(width * 0.84)
    card_h = 160
    card_x = (width - card_w) // 2
    card_y = 340
    c_draw.rounded_rectangle([(card_x, card_y), (card_x + card_w, card_y + card_h)], radius=20, fill=(15, 23, 42, 210), outline=(255, 255, 255, 40), width=2)
    # Accent glowing underline
    c_draw.line([(card_x + 30, card_y + card_h - 12), (card_x + card_w - 30, card_y + card_h - 12)], fill=palette[3], width=4)
    
    # Card text: Clean uppercase visual topic
    clean_title = " ".join([w.capitalize() for w in query.split()[:4]])
    c_draw.text((card_x + 40, card_y + 45), clean_title, fill=(255, 255, 255, 255))
    
    # 5. Cinematic top and bottom letterbox / vignette gradient
    vignette = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    v_draw = ImageDraw.Draw(vignette)
    v_steps = 40
    for v in range(v_steps):
        alpha = int(210 * (1 - (v / v_steps)))
        # Top shadow
        v_draw.rectangle([(0, v * 10), (width, (v + 1) * 10)], fill=(0, 0, 0, alpha))
        # Bottom shadow
        v_draw.rectangle([(0, height - (v + 1) * 10), (width, height - v * 10)], fill=(0, 0, 0, alpha))
        
    img.paste(card_overlay, (0, 0), card_overlay)
    img.paste(vignette, (0, 0), vignette)
    
    img.save(output_path, "PNG", quality=95)
    return output_path

def download_pexels_media(query: str, scene_id: int, format_type: str = "shorts") -> Path:
    """Fetches high-definition media from Pexels API if API key is provided."""
    headers = {"Authorization": PEXELS_API_KEY}
    url = f"https://api.pexels.com/v1/search?query={query}&per_page=3&orientation={'portrait' if format_type == 'shorts' else 'landscape'}"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            photos = data.get("photos", [])
            if photos:
                img_url = photos[0]["src"]["large2x"]
                img_res = requests.get(img_url, timeout=15)
                output_path = TEMP_DIR / f"scene_{scene_id}_bg.jpg"
                with open(output_path, "wb") as f:
                    f.write(img_res.content)
                return output_path
    except Exception as e:
        print(f"[AssetFetcher] Pexels download fallback: {e}")
        
    specs = FORMATS.get(format_type, FORMATS["shorts"])
    return create_cinematic_backdrop(scene_id, query, specs["width"], specs["height"])

def fetch_assets_for_scenes(scenes: List[Dict[str, Any]], format_type: str = "shorts") -> List[Path]:
    """Retrieves or renders visuals for each scene in the script."""
    asset_paths = []
    specs = FORMATS.get(format_type, FORMATS["shorts"])
    
    for scene in scenes:
        scene_id = scene.get("scene_id", 1)
        query = scene.get("visual_query", "cinematic nature depth of field")
        print(f"[AssetFetcher] Generating asset for Scene {scene_id}: '{query}'")
        
        if PEXELS_API_KEY:
            asset = download_pexels_media(query, scene_id, format_type)
        else:
            asset = create_cinematic_backdrop(scene_id, query, specs["width"], specs["height"])
            
        asset_paths.append(asset)
        
    return asset_paths

if __name__ == "__main__":
    p = create_cinematic_backdrop(1, "deep space")
    print(f"Created sample background: {p}")
