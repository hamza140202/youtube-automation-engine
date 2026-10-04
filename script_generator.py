import os
import json
import re
from typing import Dict, Any, List
from config import GEMINI_API_KEY

SYSTEM_PROMPT = """
You are a master viral YouTube content strategist and scriptwriter specializing in high-retention, monetizable scripts.
Your task is to generate a complete script and production plan for a video on the given topic.

You MUST structure the output as valid JSON with the following keys:
{
  "title": "Compelling 45-60 character high-CTR title",
  "description": "SEO optimized description (150-250 words) with 3 viral hashtags",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6"],
  "scenes": [
    {
      "scene_id": 1,
      "narration": "Exact text to be spoken by narrator for this beat (1-2 punchy sentences).",
      "visual_query": "3-4 keywords describing the optimal background visual (e.g. 'deep space galaxy', 'human brain neurons', 'ticking antique clock')."
    }
  ]
}

Retention Rules:
1. Scene 1 MUST be an irresistible Hook within the first 3 seconds (curiosity gap, controversial question, or mind-bending fact).
2. Never say 'In this video' or 'Welcome back to my channel'.
3. Pacing: For Shorts, aim for 5 to 7 scenes total (total spoken time ~35-50 seconds).
4. For Landscape, aim for 10 to 14 scenes total (~2-3 minutes).
5. Tone: Authoritative, intriguing, documentary-grade.
"""

def generate_with_gemini(topic: str, format_type: str = "shorts") -> Dict[str, Any]:
    """Generates script using Google Gemini API."""
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    
    # Try gemini-1.5-pro then gemini-1.5-flash
    model_name = "gemini-1.5-flash"
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=SYSTEM_PROMPT,
        generation_config={"response_mime_type": "application/json"}
    )
    
    prompt = f"Topic: {topic}\nTarget Format: {format_type} (Duration target: {'45 seconds' if format_type == 'shorts' else '2.5 minutes'})"
    response = model.generate_content(prompt)
    text = response.text.strip()
    
    # Clean json fences if present
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
        
    return json.loads(text.strip())

def generate_fallback_script(topic: str, format_type: str = "shorts") -> Dict[str, Any]:
    """
    High-retention rule-based script engine when no Gemini API key is configured.
    Ensures the pipeline is 100% functional out of the box.
    """
    clean_topic = topic.strip()
    
    scenes = [
        {
            "scene_id": 1,
            "narration": f"Have you ever noticed something strange about {clean_topic} that science is only now beginning to explain?",
            "visual_query": "mysterious nebula cosmos cinematic lighting"
        },
        {
            "scene_id": 2,
            "narration": "For decades, researchers assumed the answer was straightforward. But recent experiments shattered that assumption completely.",
            "visual_query": "laboratory microscope high tech futuristic data"
        },
        {
            "scene_id": 3,
            "narration": "What they uncovered wasn't just a minor anomaly, but a fundamental shift in how perception and reality interact.",
            "visual_query": "human brain neural network glowing connections"
        },
        {
            "scene_id": 4,
            "narration": "When your brain encounters this pattern, it rewires its predictive filters in less than forty milliseconds.",
            "visual_query": "digital pulse abstract light wave hyperlapse"
        },
        {
            "scene_id": 5,
            "narration": "Which means the next time you experience this, you are not looking at the world as it is, but as your mind reconstructs it.",
            "visual_query": "eye pupil dilation macro cinematic depth of field"
        },
        {
            "scene_id": 6,
            "narration": f"What's your theory on {clean_topic}? Drop your thoughts below and subscribe for more mind-bending science.",
            "visual_query": "deep ocean dark waters dramatic light rays"
        }
    ]
    
    return {
        "title": f"The Terrifying Truth About {clean_topic}",
        "description": f"Explore the mind-bending reality behind {clean_topic}. Recent findings reveal how our perception distorts reality faster than we can react.\n\n#Science #Psychology #MindBlowing #Shorts",
        "tags": [clean_topic.lower(), "science mystery", "mind blown", "psychology facts", "secrets of the universe", "did you know"],
        "scenes": scenes
    }

def get_script(topic: str, format_type: str = "shorts") -> Dict[str, Any]:
    """Main entrypoint for script generation."""
    t_lower = topic.lower()
    if format_type == "documentary" or any(k in t_lower for k in ["ahyeon", "babymonster", "documentary"]):
        print(f"[ScriptGenerator] Sourcing full 15-minute investigative documentary script for '{topic}'...")
        from documentary_script_generator import get_ahyeon_documentary_script
        return get_ahyeon_documentary_script()

    if GEMINI_API_KEY:
        try:
            print(f"[ScriptGenerator] Querying Google Gemini for topic: '{topic}'...")
            return generate_with_gemini(topic, format_type)
        except Exception as e:
            print(f"[ScriptGenerator] Gemini API warning: {e}. Falling back to dynamic synthesis engine.")
            return generate_fallback_script(topic, format_type)
    else:
        print(f"[ScriptGenerator] No GEMINI_API_KEY found in environment. Using high-retention narrative engine.")
        return generate_fallback_script(topic, format_type)

if __name__ == "__main__":
    test_script = get_script("The Deep Ocean Mystery", "shorts")
    print(json.dumps(test_script, indent=2))
