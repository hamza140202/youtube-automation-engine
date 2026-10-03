import asyncio
import edge_tts
import re
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from config import DEFAULT_VOICE, TEMP_DIR

def format_timestamp_ass(seconds_float: float) -> str:
    """Converts seconds float to ASS time format (H:MM:SS.cc)."""
    h = int(seconds_float // 3600)
    m = int((seconds_float % 3600) // 60)
    s = seconds_float % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def convert_srt_to_ass(srt_path: Path, ass_path: Path, font_size: int = 70, margin_v: int = 420):
    """Fallback converter for srt to ass."""
    if not ass_path.exists():
        ass_path.write_text("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n[V4+ Styles]\n[Events]\n", encoding="utf-8")

def generate_phrase_level_ass(scenes: List[Dict[str, Any]], total_duration: float, ass_path: Path, font_size: int = 70, margin_v: int = 420):
    """
    Generates high-retention, kinetic ASS subtitles chunked into punchy 3-4 word phrases
    synchronized with the scene narrations and overall audio duration.
    Style: Bold uppercase, vibrant yellow primary, deep black outline, safe-zone centered.
    """
    ass_header = f"""[Script Info]
Title: Kinetic YouTube Shorts Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: None
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{font_size},&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,30,30,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    num_scenes = max(len(scenes), 1)
    dur_per_scene = total_duration / num_scenes
    curr_time = 0.0

    for s in scenes:
        narration = s.get("narration", "")
        words = narration.split()
        if not words:
            curr_time += dur_per_scene
            continue
            
        # Chunk into punchy 3-4 words (YouTube Shorts high-retention style)
        chunk_size = 4
        chunks = [" ".join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size)]
        chunk_dur = dur_per_scene / len(chunks)
        
        for c in chunks:
            start_str = format_timestamp_ass(curr_time)
            end_str = format_timestamp_ass(min(curr_time + chunk_dur - 0.08, total_duration))
            # Clean text: remove special characters, uppercase
            clean_text = re.sub(r'["\']', '', c).strip().upper()
            dialogue = f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{clean_text}\n"
            events.append(dialogue)
            curr_time += chunk_dur

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_header)
        for ev in events:
            f.write(ev)
            
    print(f"[VoiceSynthesizer] Generated {len(events)} phrase-level kinetic ASS subtitle events in {ass_path}")

async def _synthesize_async(full_text: str, voice: str, output_audio_path: Path) -> float:
    """Async voice synthesis using edge-tts."""
    communicate = edge_tts.Communicate(full_text, voice)
    with open(output_audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
    return 0.0

def synthesize_speech(full_text: str, scenes: List[Dict[str, Any]], voice: str = DEFAULT_VOICE, run_id: str = "run") -> Tuple[Path, Path, Path]:
    """
    Synthesizes speech and produces MP3 audio and synchronized kinetic ASS subtitles.
    Returns: (audio_path, srt_path, ass_path)
    """
    output_audio = TEMP_DIR / f"{run_id}_voiceover.mp3"
    output_srt = TEMP_DIR / f"{run_id}_subtitles.srt"
    output_ass = TEMP_DIR / f"{run_id}_subtitles.ass"
    
    print(f"[VoiceSynthesizer] Synthesizing speech with voice '{voice}'...")
    asyncio.run(_synthesize_async(full_text, voice, output_audio))
    print(f"[VoiceSynthesizer] Audio generated: {output_audio}")
    
    # Estimate total audio duration for subtitle syncing
    import subprocess
    total_dur = 45.0
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(output_audio)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            total_dur = float(res.stdout.strip())
    except Exception:
        # Fallback based on word count (~150 wpm)
        words = len(full_text.split())
        total_dur = max(words / 2.5, 10.0)
        
    print(f"[VoiceSynthesizer] Total narration duration: {total_dur:.2f}s")
    generate_phrase_level_ass(scenes, total_dur, output_ass)
    
    # Dummy srt for backwards compatibility
    output_srt.write_text("1\n00:00:00,000 --> 00:00:05,000\nSubtitles\n", encoding="utf-8")
    
    return output_audio, output_srt, output_ass
