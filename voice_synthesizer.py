import asyncio
import edge_tts
from pathlib import Path
from typing import Dict, Any, List, Tuple
from config import DEFAULT_VOICE, TEMP_DIR

def format_timestamp(ms: int) -> str:
    """Converts milliseconds to SRT format (HH:MM:SS,mmm)."""
    seconds, milliseconds = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

async def _synthesize_async(full_text: str, voice: str, output_audio_path: Path, output_srt_path: Path) -> float:
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
        
    return 0.0

def synthesize_speech(full_text: str, voice: str = DEFAULT_VOICE, run_id: str = "run") -> Tuple[Path, Path]:
    """
    Synthesizes speech and produces both an MP3 file and a synchronized SRT subtitle file.
    Returns: (audio_path, srt_path)
    """
    output_audio = TEMP_DIR / f"{run_id}_voiceover.mp3"
    output_srt = TEMP_DIR / f"{run_id}_subtitles.srt"
    
    print(f"[VoiceSynthesizer] Synthesizing audio with voice '{voice}'...")
    asyncio.run(_synthesize_async(full_text, voice, output_audio, output_srt))
    print(f"[VoiceSynthesizer] Audio generated: {output_audio}")
    print(f"[VoiceSynthesizer] Subtitles generated: {output_srt}")
    
    return output_audio, output_srt

if __name__ == "__main__":
    sample_text = "Have you ever wondered what lies at the bottom of the Mariana Trench? The pressure alone is enough to crush titanium."
    audio_p, srt_p = synthesize_speech(sample_text)
    print("Completed sample voice synthesis.")
