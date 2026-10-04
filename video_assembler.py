import os
import shutil
import subprocess
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from config import FORMATS, OUTPUT_DIR, TEMP_DIR
from voice_synthesizer import convert_srt_to_ass

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
        print(f"[VideoAssembler] ffprobe check fallback: {e}")
        return 45.0

def create_scene_clip(asset_path: Path, duration: float, scene_id: int, format_type: str = "shorts") -> Path:
    """
    Renders an individual scene video clip.
    If the asset is a real MP4/WebM/OGV video, trims past intro, scales, crops, and loops it.
    If the asset is an image, applies a cinematic Ken Burns zoom/pan effect.
    """
    specs = FORMATS.get(format_type, FORMATS["shorts"])
    width = specs["width"]
    height = specs["height"]
    fps = specs["fps"]
    total_frames = int(duration * fps) + 5
    output_clip = TEMP_DIR / f"clip_{scene_id}.mp4"
    
    # 1. Real MP4/WebM/OGV Video Footage Processing
    if asset_path.suffix.lower() in [".mp4", ".mov", ".webm", ".mkv", ".ogv"]:
        print(f"[VideoAssembler] Processing REAL video clip for Scene {scene_id} ({asset_path.name})...")
        # Start at 6.0s into clip to skip opening titles, title cards, and fades, capturing prime movement
        cmd = [
            "ffmpeg", "-y",
            "-ss", "00:00:06.000",
            "-stream_loop", "-1",
            "-i", str(asset_path),
            "-t", f"{duration:.2f}",
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-an",
            str(output_clip)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            return output_clip
        print(f"[VideoAssembler] Primary clip cut warning, trying with 2.5s seek: {res.stderr[-200:]}")
        # Secondary fallback with 2.5s seek
        cmd_2s = [
            "ffmpeg", "-y",
            "-ss", "00:00:02.500",
            "-stream_loop", "-1",
            "-i", str(asset_path),
            "-t", f"{duration:.2f}",
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-an",
            str(output_clip)
        ]
        res2 = subprocess.run(cmd_2s, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res2.returncode == 0:
            return output_clip
        cmd_noseek = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", str(asset_path),
            "-t", f"{duration:.2f}",
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-an",
            str(output_clip)
        ]
        res2 = subprocess.run(cmd_noseek, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res2.returncode == 0:
            return output_clip

    # 2. Image Processing with Ken Burns Dynamic Zoom (if ever passed)
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
        "-i", str(asset_path),
        "-vf", f"scale=1440:-2,zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':d={total_frames}:s={width}x{height}:fps={fps}",
        "-t", f"{duration:.2f}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
        str(output_clip)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_clip

def assemble_full_video(
    scene_assets: List[Path],
    audio_path: Path,
    srt_path: Path,
    output_filename: str = "final_video.mp4",
    format_type: str = "shorts",
    ass_path: Optional[Path] = None
) -> Path:
    """
    Merges all scene clips, syncs with voiceover audio, burns kinetic ASS subtitles,
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
    
    # 4. Sound Design: Mix Voiceover with Atmospheric Ambient Soundscape or Emotional Piano Soundtrack
    bg_audio = None
    if format_type in ["landscape", "documentary"]:
        piano_track = TEMP_DIR / "documentary_piano_bed.mp3"
        if not piano_track.exists():
            print("[VideoAssembler] Sourcing emotional cinematic piano background soundtrack...")
            dl_cmd = [
                "yt-dlp",
                "ytsearch1:emotional cinematic piano background music royalty free",
                "--download-sections", "*00:05-04:00",
                "-x", "--audio-format", "mp3",
                "-o", str(piano_track),
                "--force-overwrites",
                "--no-playlist",
                "-q"
            ]
            try:
                subprocess.run(dl_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
            except Exception:
                pass
        if piano_track.exists() and piano_track.stat().st_size > 10000:
            bg_audio = piano_track

    # Fallback to atmospheric pink-noise drone if soundtrack not available
    if not bg_audio:
        ambient_audio = TEMP_DIR / "ambient_drone.wav"
        drone_cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"anoisesrc=d={total_audio_duration + 2}:c=pink:r=44100:a=0.035,lowpass=f=260",
            "-c:a", "pcm_s16le",
            str(ambient_audio)
        ]
        subprocess.run(drone_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bg_audio = ambient_audio if ambient_audio.exists() else None

    mixed_audio = TEMP_DIR / "final_mixed_audio.wav"
    if bg_audio and bg_audio.exists():
        bg_vol = "0.15" if format_type in ["landscape", "documentary"] else "0.22"
        mix_cmd = [
            "ffmpeg", "-y",
            "-i", str(audio_path.resolve()),
            "-stream_loop", "-1",
            "-i", str(bg_audio.resolve()),
            "-filter_complex", f"[0:a]volume=1.0[voice]; [1:a]volume={bg_vol}[bg]; [voice][bg]amix=inputs=2:duration=first[aout]",
            "-map", "[aout]",
            "-c:a", "pcm_s16le",
            str(mixed_audio)
        ]
        res_mix = subprocess.run(mix_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        active_audio = mixed_audio if res_mix.returncode == 0 else audio_path
    else:
        active_audio = audio_path

    # 5. Final render: Merge video + mixed audio + burn stylized ASS kinetic subtitles
    final_output = OUTPUT_DIR / output_filename
    margin_v = specs["subtitle_margin_v"]
    
    local_ass_name = "current_subtitles.ass"
    local_ass = TEMP_DIR / local_ass_name
    if ass_path and ass_path.exists():
        shutil.copy2(ass_path, local_ass)
    else:
        convert_srt_to_ass(srt_path, local_ass, margin_v=margin_v)
        
    subtitles_filter = f"ass={local_ass_name}"
    
    final_cmd = [
        "ffmpeg", "-y",
        "-i", str(unsubbed_video.resolve()),
        "-i", str(active_audio.resolve()),
        "-vf", subtitles_filter,
        "-c:v", "libx264",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        str(final_output.resolve())
    ]
    
    print("[VideoAssembler] Burning ASS kinetic subtitles and exporting final MP4...")
    res = subprocess.run(final_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(TEMP_DIR))
    if res.returncode != 0:
        print(f"[VideoAssembler] ASS filter warning (exit code {res.returncode}):\n{res.stderr[-300:]}")
        print("[VideoAssembler] Retrying with clean video export...")
        fallback_cmd = [
            "ffmpeg", "-y",
            "-i", str(unsubbed_video.resolve()),
            "-i", str(active_audio.resolve()),
            "-c:v", "libx264",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            str(final_output.resolve())
        ]
        subprocess.run(fallback_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
    print(f"[VideoAssembler] Video exported successfully: {final_output}")
    return final_output
