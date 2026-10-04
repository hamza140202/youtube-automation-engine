import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "output"

TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# Format specifications
FORMATS = {
    "shorts": {
        "width": 1080,
        "height": 1920,
        "aspect_ratio": "9:16",
        "fps": 30,
        "max_duration_seconds": 58,
        "subtitle_font_size": 22,
        "subtitle_margin_v": 380,  # Center-bottom safe zone for Shorts UI
    },
    "landscape": {
        "width": 1920,
        "height": 1080,
        "aspect_ratio": "16:9",
        "fps": 30,
        "max_duration_seconds": 180,
        "subtitle_font_size": 26,
        "subtitle_margin_v": 120,
    },
    "documentary": {
        "width": 1920,
        "height": 1080,
        "aspect_ratio": "16:9",
        "fps": 30,
        "max_duration_seconds": 960,
        "subtitle_font_size": 24,
        "subtitle_margin_v": 100,
    }
}

# Neural Voices (Edge-TTS)
DEFAULT_VOICE = "en-US-ChristopherNeural"  # Deep, narrative, documentary style
ALTERNATIVE_VOICES = [
    "en-US-JennyNeural",       # Energetic female
    "en-US-GuyNeural",         # Conversational male
    "en-GB-RyanNeural",        # British documentary male
    "en-GB-SoniaNeural"        # British female
]

# API Keys from environment
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
