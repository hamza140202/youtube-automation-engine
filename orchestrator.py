import argparse
import json
import time
import re
from pathlib import Path
from config import DEFAULT_VOICE, OUTPUT_DIR, TEMP_DIR
from script_generator import get_script
from voice_synthesizer import synthesize_speech
from asset_fetcher import fetch_assets_for_scenes
from video_assembler import assemble_full_video
from telegram_notifier import send_telegram_update, send_telegram_video

def slugify(text: str) -> str:
    """Converts a topic into a filesystem-safe slug."""
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[-\s]+", "_", text)[:40]

def run_pipeline(topic: str, format_type: str = "shorts", voice: str = DEFAULT_VOICE):
    """Executes the entire YouTube automated video generation pipeline."""
    start_time = time.time()
    run_slug = slugify(topic)
    print("=" * 60)
    print(f"STARTING YOUTUBE VIDEO GENERATION")
    print(f"Topic:  {topic}")
    print(f"Format: {format_type.upper()}")
    print(f"Voice:  {voice}")
    print("=" * 60)
    
    send_telegram_update(f"🎬 <b>Engine Started:</b> <i>{topic}</i>\nFormat: <code>{format_type.upper()}</code>\nStatus: Initializing pipeline...")

    # 1. Script Generation
    print("\n[Step 1/4] Generating High-Retention Script & Visual Cues...")
    send_telegram_update("📝 <b>[1/4] Script Generation:</b> Crafting high-retention narration & scene cues via Gemini...")
    script_data = get_script(topic, format_type)
    scenes = script_data.get("scenes", [])
    full_narration = " ".join([s["narration"] for s in scenes])
    print(f"[OK] Title: {script_data.get('title')}")
    print(f"[OK] Generated {len(scenes)} scenes.")
    
    # 2. Voice & Subtitle Synthesis
    print("\n[Step 2/4] Synthesizing Neural Voiceover & Word Timestamps...")
    send_telegram_update("🎙 <b>[2/4] Voice & Subtitles:</b> Synthesizing Edge-TTS neural voiceover & word timestamps...")
    audio_path, srt_path, ass_path = synthesize_speech(full_narration, scenes, voice, run_slug, format_type=format_type)
    print(f"[OK] Audio: {audio_path.name}")
    print(f"[OK] Subtitles: {srt_path.name} & {ass_path.name}")
    
    # 3. Asset Retrieval
    print("\n[Step 3/4] Preparing HD Scene Visuals & Motion Backdrops...")
    send_telegram_update(f"🛰 <b>[3/4] Asset Harvesting:</b> Sourcing 100% real moving 1080p footage for {len(scenes)} scenes via NASA SVS & AVD...")
    scene_assets = fetch_assets_for_scenes(scenes, format_type)
    print(f"[OK] Prepared {len(scene_assets)} scene visuals.")
    
    # 4. Assembly & Subtitle Burn-In
    print("\n[Step 4/4] Assembling Final MP4 (Real Footage + Kinetic ASS Subtitles)...")
    send_telegram_update("⚡ <b>[4/4] FFmpeg Render:</b> Stitching 1080x1920 clips, mixing audio drone, and burning kinetic ASS subtitles...")
    output_filename = f"{run_slug}_{format_type}.mp4"
    final_video_path = assemble_full_video(
        scene_assets=scene_assets,
        audio_path=audio_path,
        srt_path=srt_path,
        output_filename=output_filename,
        format_type=format_type,
        ass_path=ass_path
    )
    
    # Save YouTube Metadata
    metadata_path = OUTPUT_DIR / f"{run_slug}_metadata.json"
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(script_data, f, indent=2)
        
    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print(f"VIDEO GENERATION COMPLETE IN {elapsed:.1f} SECONDS!")
    print(f"Output Video:    {final_video_path}")
    print(f"Metadata & SEO:  {metadata_path}")
    print("=" * 60)
    
    send_telegram_update(f"🎉 <b>Video Render Complete!</b> ({elapsed:.1f}s)\nFile: <code>{output_filename}</code>\nReady for YouTube!")
    send_telegram_video(str(final_video_path), caption=f"🎬 {script_data.get('title', topic)}")
    
    return final_video_path, metadata_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous YouTube Video Generator")
    parser.add_argument("--topic", type=str, default="The Simulation Theory Mystery", help="Video topic")
    parser.add_argument("--format", type=str, default="shorts", choices=["shorts", "landscape"], help="Video format")
    parser.add_argument("--voice", type=str, default=DEFAULT_VOICE, help="Edge-TTS voice")
    args = parser.parse_args()
    
    run_pipeline(args.topic, args.format, args.voice)
