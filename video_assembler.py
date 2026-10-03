import os
import subprocess
import json
from pathlib import Path
from typing import List, Dict, Any
from config import FORMATS, OUTPUT_DIR, TEMP_DIR

def get_audio_duration(audio_path: Path) -> float:
    """Uses ffprobe to get exact duration in seconds."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        print(f"[VideoAssembler] ffprobe fallback check: {e}")
        # Rough fallback: 150 words per minute ~ 2.5 words per sec
        return 45.0

def escape_ffmpeg_path(path: Path) -> str:
    """Escapes file path properly for ffmpeg filter arguments."""
    posix_path = path.as_posix()
    # In ffmpeg filters, colons and backslashes must be escaped
    return posix_path.replace(":", "\\:").replace("'", "\\'")

def create_scene_clip(image_path: Path, duration: float, scene_id: int, format_type: str = "shorts") -> Path:
    """
    Renders an individual scene video clip with a cinematic Ken Burns zoom/pan effect.
    """
    specs = FORMATS.get(format_type, FORMATS["shorts"])
    width = specs["width"]
    height = specs["height"]
    fps = specs["fps"]
    total_frames = int(duration * fps) + 5
    output_clip = TEMP_DIR / f"clip_{scene_id}.mp4"
    
    # Alternate zoom-in vs zoom-out per scene
    if scene_id % 2 == 0:
        zoom_expr = "min(zoom+0.0008,1.15)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    else:
        zoom_expr = "max(1.15-0.0008*on,1.0)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(image_path),
        "-vf", f"scale=1440:-2,zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':d={total_frames}:s={width}x{height}:fps={fps}",
        "-t", f"{duration:.2f}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
        str(output_clip)
    ]
    
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print(f"[VideoAssembler] Clip generation warning: {res.stderr[-300:]}")
        # Simple loop fallback if zoompan fails
        fallback_cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(image_path),
            "-t", f"{duration:.2f}",
            "-s", f"{width}x{height}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            str(output_clip)
        ]
        subprocess.run(fallback_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
    return output_clip

def assemble_full_video(
    scene_assets: List[Path],
    audio_path: Path,
    srt_path: Path,
    output_filename: str = "final_video.mp4",
    format_type: str = "shorts"
) -> Path:
    """
    Merges all scene clips, syncs with voiceover audio, burns kinetic subtitles,
    and produces the final high-definition MP4.
    """
    specs = FORMATS.get(format_type, FORMATS["shorts"])
    total_audio_duration = get_audio_duration(audio_path)
    num_scenes = max(len(scene_assets), 1)
    duration_per_scene = total_audio_duration / num_scenes
    
    print(f"[VideoAssembler] Audio duration: {total_audio_duration:.2f}s | {num_scenes} scenes (~{duration_per_scene:.2f}s each)")
    
    # 1. Render animated clips for each scene
    clip_paths = []
    for idx, asset in enumerate(scene_assets):
        print(f"[VideoAssembler] Rendering motion clip for Scene {idx + 1}...")
        clip = create_scene_clip(asset, duration_per_scene, idx + 1, format_type)
        clip_paths.append(clip)
        
    # 2. Create concat manifest
    concat_list_path = TEMP_DIR / "concat_list.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for clip in clip_paths:
            f.write(f"file '{clip.as_posix()}'\n")
            
    unsubbed_video = TEMP_DIR / "stitched_raw.mp4"
    
    # 3. Concatenate all clips
    concat_cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list_path),
        "-c", "copy",
        str(unsubbed_video)
    ]
    subprocess.run(concat_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    
    # 4. Final render: Merge audio + burn stylized subtitles
    final_output = OUTPUT_DIR / output_filename
    margin_v = specs["subtitle_margin_v"]
    font_size = specs["subtitle_font_size"]
    
    import shutil
    local_srt = TEMP_DIR / "current_subtitles.srt"
    shutil.copy2(srt_path, local_srt)
    escaped_srt = local_srt.as_posix()
    
    # High-retention subtitle styling: Bright yellow (&H0000FFFF), bold black border, centered
    # Note: commas in force_style MUST be escaped as \, for FFmpeg AVFilterGraph parser
    style = f"FontName=DejaVu Sans\\,FontSize={font_size}\\,PrimaryColour=&H0000FFFF\\,OutlineColour=&H00000000\\,BackColour=&H80000000\\,Bold=1\\,Outline=3\\,Alignment=2\\,MarginV={margin_v}"
    subtitles_filter = f"subtitles={escaped_srt}:force_style='{style}'"
    
    final_cmd = [
        "ffmpeg", "-y",
        "-i", str(unsubbed_video),
        "-i", str(audio_path),
        "-vf", subtitles_filter,
        "-c:v", "libx264",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        str(final_output)
    ]
    
    print("[VideoAssembler] Burning subtitles and exporting final MP4...")
    try:
        subprocess.run(final_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[VideoAssembler] Subtitle filter error ({e}). Retrying without burned subtitles...")
        # Fallback if font/subtitles filter has system-specific font issues
        fallback_cmd = [
            "ffmpeg", "-y",
            "-i", str(unsubbed_video),
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            str(final_output)
        ]
        subprocess.run(fallback_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
        
    print(f"[VideoAssembler] Video exported successfully: {final_output}")
    return final_output
