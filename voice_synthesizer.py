import asyncio
import edge_tts
import re
from pathlib import Path
from typing import Dict, Any, List, Tuple
from config import DEFAULT_VOICE, TEMP_DIR

def format_timestamp_srt(ms: int) -> str:
    """Converts milliseconds to SRT format (HH:MM:SS,mmm)."""
    seconds, milliseconds = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

def srt_time_to_ass(time_str: str) -> str:
    """Converts SRT time 'HH:MM:SS,mmm' to ASS time 'H:MM:SS.cc'."""
    parts = time_str.strip().replace(',', '.').split(':')
    if len(parts) == 3:
        h = int(parts[0])
        m = parts[1]
        s, ms = parts[2].split('.')
        cs = ms[:2]
        return f"{h}:{m}:{s}.{cs}"
    return "0:00:00.00"

def convert_srt_to_ass(srt_path: Path, ass_path: Path, font_size: int = 68, margin_v: int = 420):
    """
    Converts SRT subtitles into broadcast-grade ASS kinetic subtitles.
    Styles: Bold uppercase, vibrant yellow primary, deep black outline, safe-zone centered.
    """
    ass_header = f"""[Script Info]
Title: Kinetic Subtitles
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
    if srt_path.exists():
        content = srt_path.read_text(encoding="utf-8", errors="ignore")
        # Parse SRT blocks
        blocks = re.split(r"\n\s*\n", content.strip())
        for block in blocks:
            lines = [l.strip() for l in block.splitlines() if l.strip()]
            if len(lines) >= 3:
                # Line 1 is index, Line 2 is timestamps, Line 3+ is text
                time_match = re.search(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", lines[1])
                if time_match:
                    start_ass = srt_time_to_ass(time_match.group(1))
                    end_ass = srt_time_to_ass(time_match.group(2))
                    text = " ".join(lines[2:]).upper()
                    # Add subtle punchy styling tag
                    dialogue = f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{text}\n"
                    events.append(dialogue)
                    
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_header)
        for ev in events:
            f.write(ev)
            
    print(f"[VoiceSynthesizer] Generated ASS kinetic subtitles: {ass_path} ({len(events)} events)")

async def _synthesize_async(full_text: str, voice: str, output_audio_path: Path, output_srt_path: Path, output_ass_path: Path) -> float:
    """Async voice synthesis using edge-tts with word-boundary event tracking."""
    communicate = edge_tts.Communicate(full_text, voice)
    
    sub_maker = edge_tts.SubMaker()
    with open(output_audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                sub_maker.feed(chunk)
                
    srt_content = sub_maker.get_srt()
    with open(output_srt_path, "w", encoding="utf-8") as srt_file:
        srt_file.write(srt_content)
        
    convert_srt_to_ass(output_srt_path, output_ass_path)
    return 0.0

def synthesize_speech(full_text: str, voice: str = DEFAULT_VOICE, run_id: str = "run") -> Tuple[Path, Path, Path]:
    """
    Synthesizes speech and produces MP3, SRT, and styled ASS subtitles.
    Returns: (audio_path, srt_path, ass_path)
    """
    output_audio = TEMP_DIR / f"{run_id}_voiceover.mp3"
    output_srt = TEMP_DIR / f"{run_id}_subtitles.srt"
    output_ass = TEMP_DIR / f"{run_id}_subtitles.ass"
    
    print(f"[VoiceSynthesizer] Synthesizing audio with voice '{voice}'...")
    asyncio.run(_synthesize_async(full_text, voice, output_audio, output_srt, output_ass))
    print(f"[VoiceSynthesizer] Audio generated: {output_audio}")
    print(f"[VoiceSynthesizer] Subtitles generated: {output_srt} & {output_ass}")
    
    return output_audio, output_srt, output_ass
