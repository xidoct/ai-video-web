import argparse
import asyncio
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import wave
from pathlib import Path

import edge_tts
import PIL.Image

# Pillow 10+ 删除了 Image.ANTIALIAS，而 MoviePy 1.x 仍会调用它。
# 这里做兼容映射，避免视频 resize/crop 阶段报错。
if not hasattr(PIL.Image, "ANTIALIAS"):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS

from moviepy.editor import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    VideoFileClip,
    concatenate_videoclips,
    afx,
    vfx,
)


DEFAULT_CONFIG = {
    "project_root": r"E:\test\3",
    "material_root": r"E:\test\ai剪辑2\A鞋",
    "bgm_root": r"E:\test\3\bgm",
    "output_root": r"E:\test\3\output",
    "temp_root": r"E:\test\3\temp",
    "output_filename": "final.mp4",
    "video_size": [1080, 1920],
    "fps": 24,
    "tts_voice": "zh-CN-XiaoxiaoNeural",
    "tts_rate": "+0%",
    "tts_volume": "+0%",
    "lead_time": 1.0,
    "max_overlay_duration": 3.0,
    "min_gap": 1.5,
    "min_video_duration": 1.0,
    "fade_duration": 0.25,
    "bgm_volume": 0.2,
    "voice_volume": 1.0,
    "core_selling_points": {
        "软底软面防滑": ["软底", "软面", "防滑"],
        "轻便": ["轻便"],
        "增高细节": ["增高", "细节"],
        "防水防污": ["防水", "防污"],
        "多色展示": ["多色", "颜色", "配色"],
    },
    "core_material_aliases": {
        "软底软面防滑": ["软底软面防滑", "软底软面", "防滑"],
        "轻便": ["轻便"],
        "增高细节": ["增高细节", "增高", "细节"],
        "防水防污": ["防水防污"],
        "多色展示": ["多色展示"],
    },
    "background_folders": ["开箱向上动作", "防尘袋", "穿鞋", "上脚动作", "手持"],
    "video_exts": [".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"],
    "audio_exts": [".mp3", ".wav", ".m4a", ".aac", ".flac"],
}


def load_config(config_path):
    config = DEFAULT_CONFIG.copy()
    path = Path(config_path)
    if path.exists():
        user_config = json.loads(path.read_text(encoding="utf-8"))
        config.update(user_config)
    config["video_size"] = tuple(config["video_size"])
    return config


def ensure_dir(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def list_media_files(folder, exts):
    folder = Path(folder)
    if not folder.exists() or not folder.is_dir():
        return []
    return sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts],
        key=lambda p: p.name,
    )


def classify_material_folders(config):
    """把素材目录分成 A 类触发素材和 B 类背景素材。"""
    material_root = Path(config["material_root"])
    if not material_root.exists():
        raise FileNotFoundError(f"找不到素材目录：{material_root}")

    all_dirs = [p.name for p in material_root.iterdir() if p.is_dir()]
    core_names = set(config["core_selling_points"].keys())
    aliases = config.get("core_material_aliases", {})

    core_dirs = {}
    core_physical_names = set()
    for canonical_name in config["core_selling_points"]:
        candidate_names = aliases.get(canonical_name, [canonical_name])
        for candidate in candidate_names:
            folder = material_root / candidate
            if folder.is_dir():
                core_dirs[canonical_name] = folder
                core_physical_names.add(candidate)
                break

    bg_names = []
    for name in config["background_folders"]:
        if name in all_dirs and name not in bg_names:
            bg_names.append(name)

    for name in all_dirs:
        if name not in core_names and name not in core_physical_names and name not in bg_names:
            bg_names.append(name)

    return core_dirs, [material_root / name for name in bg_names]


def fit_vertical_clip(clip, size):
    """等比放大填满竖屏画布，再居中裁切。"""
    target_w, target_h = size
    scale = max(target_w / clip.w, target_h / clip.h)
    clip = clip.resize(scale)
    return clip.crop(
        x_center=clip.w / 2,
        y_center=clip.h / 2,
        width=target_w,
        height=target_h,
    )


def safe_video_clip(path, min_duration):
    """安全读取视频，坏文件、空文件、过短视频会自动跳过。"""
    try:
        clip = VideoFileClip(str(path))
        if not clip.duration or clip.duration < min_duration:
            clip.close()
            return None
        return clip
    except Exception as exc:
        print(f"跳过无法读取的视频：{path}，原因：{exc}")
        return None


def build_background_clip(config, bg_dirs, total_duration):
    """B 类素材按目录顺序循环铺满全片，确保永远不黑屏。"""
    folder_video_map = []
    for folder in bg_dirs:
        files = list_media_files(folder, config["video_exts"])
        if files:
            folder_video_map.append(files)

    if not folder_video_map:
        raise RuntimeError("B 类背景素材为空，无法生成视频。")

    clips = []
    cursor = 0.0
    folder_index = 0
    failed_attempts = 0
    max_failed_attempts = 50

    while cursor < total_duration:
        files = folder_video_map[folder_index % len(folder_video_map)]
        folder_index += 1

        raw = safe_video_clip(random.choice(files), config["min_video_duration"])
        if raw is None:
            failed_attempts += 1
            if failed_attempts >= max_failed_attempts:
                raise RuntimeError("连续读取背景素材失败，请检查 B 类视频文件是否可用。")
            continue

        use_duration = min(raw.duration, total_duration - cursor)
        clip = fit_vertical_clip(raw.subclip(0, use_duration), config["video_size"])
        clips.append(clip)
        cursor += use_duration

    return concatenate_videoclips(clips, method="compose").set_duration(total_duration)


async def synthesize_tts_with_timestamps(config, text, voice_path):
    """用 edge-tts 生成口播，并提取词级时间戳。"""
    words = []
    communicate = edge_tts.Communicate(
        text=text,
        voice=config["tts_voice"],
        rate=config["tts_rate"],
        volume=config["tts_volume"],
    )

    with open(voice_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append({
                    "text": chunk.get("text", ""),
                    "start": chunk.get("offset", 0) / 10_000_000,
                    "duration": chunk.get("duration", 0) / 10_000_000,
                })

    return words


def synthesize_windows_tts(text, voice_path):
    """
    edge-tts 网络不可用时的本地兜底方案。
    使用 Windows 自带 System.Speech 生成 wav，没有词时间戳，后续会用文本位置估算触发点。
    """
    if os.name != "nt":
        synthesize_silent_wav(text, voice_path)
        return

    text_path = Path(voice_path).with_suffix(".txt")
    script_path = Path(voice_path).with_suffix(".ps1")
    text_path.write_text(text, encoding="utf-8")

    ps_script = r"""
param(
    [string]$TextPath,
    [string]$VoicePath
)
Add-Type -AssemblyName System.Speech
$text = Get-Content -LiteralPath $TextPath -Raw -Encoding UTF8
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speaker.Rate = 0
$speaker.Volume = 100
$speaker.SetOutputToWaveFile($VoicePath)
$speaker.Speak($text)
$speaker.Dispose()
"""
    script_path.write_text(ps_script, encoding="utf-8")

    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            str(text_path),
            str(voice_path),
        ],
        check=True,
    )


def synthesize_silent_wav(text, voice_path):
    """
    Render 等 Linux 环境没有 Windows 本地 TTS。
    如果 edge-tts 也不可用，生成一段静音音频兜底，让视频流程不中断。
    """
    duration = max(6.0, min(120.0, len(text) * 0.22))
    sample_rate = 22050
    frame_count = int(duration * sample_rate)

    with wave.open(str(voice_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * frame_count)


def generate_auto_script():
    """本地模板文案，不依赖外部大模型。"""
    return (
        "这双鞋很适合日常通勤和出门逛街。"
        "软底软面，上脚不硬，走久了也舒服。"
        "鞋底防滑，雨天和光滑地面走起来更安心。"
        "整双鞋非常轻便，穿一整天也没有明显负担。"
        "增高细节自然不夸张，视觉上更显腿长。"
        "鞋面防水防污，日常打理很省心。"
        "还有多色和不同配色可以选择，搭裤子裙子都很好看。"
    )


def load_script(args, config):
    """文案优先级：命令行文案 > 指定文件 > 默认 script.txt > 自动文案。"""
    if args.script_text:
        return args.script_text.strip()

    if args.script_file:
        path = Path(args.script_file)
        if not path.exists():
            raise FileNotFoundError(f"找不到文案文件：{path}")
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text

    default_script = Path(config["project_root"]) / "script.txt"
    if default_script.exists():
        text = default_script.read_text(encoding="utf-8").strip()
        if text:
            return text

    if args.auto_script:
        return generate_auto_script()

    print("未找到有效 script.txt，已使用默认测试文案。")
    return generate_auto_script()


def find_keyword_triggers(config, word_timestamps, full_text):
    """从词时间戳中找出 A 类关键词触发点。"""
    triggers = []
    seen = set()

    for folder_name, keywords in config["core_selling_points"].items():
        for keyword in keywords:
            for item in word_timestamps:
                token = item["text"]
                if keyword and (keyword in token or token in keyword):
                    key = (folder_name, keyword, round(item["start"], 3))
                    if key not in seen:
                        triggers.append({
                            "folder": folder_name,
                            "keyword": keyword,
                            "word_start": float(item["start"]),
                        })
                        seen.add(key)

            if keyword in full_text:
                for i in range(len(word_timestamps)):
                    merged = "".join(x["text"] for x in word_timestamps[i:i + 4])
                    if keyword in merged:
                        start = float(word_timestamps[i]["start"])
                        key = (folder_name, keyword, round(start, 3))
                        if key not in seen:
                            triggers.append({
                                "folder": folder_name,
                                "keyword": keyword,
                                "word_start": start,
                            })
                            seen.add(key)
                        break

    return sorted(triggers, key=lambda x: x["word_start"])


def find_keyword_triggers_by_text_position(config, full_text, total_duration):
    """
    edge-tts 偶尔拿不到 WordBoundary，尤其在某些环境或版本下会返回 0 个词时间戳。
    这种情况下用关键词在文案里的字符位置估算时间点，保证卖点画面仍能触发。
    """
    triggers = []
    seen = set()
    text_length = max(len(full_text), 1)

    for folder_name, keywords in config["core_selling_points"].items():
        for keyword in keywords:
            start_index = full_text.find(keyword)
            if start_index < 0:
                continue

            estimated_start = total_duration * (start_index / text_length)
            key = (folder_name, keyword, round(estimated_start, 3))
            if key in seen:
                continue

            triggers.append({
                "folder": folder_name,
                "keyword": keyword,
                "word_start": float(estimated_start),
            })
            seen.add(key)

    return sorted(triggers, key=lambda x: x["word_start"])


def plan_overlay_segments(config, triggers, total_duration):
    """提前 1 秒切入，最长 3 秒，过近触发自动截断。"""
    segments = []
    for trigger in triggers:
        start = max(0.0, trigger["word_start"] - config["lead_time"])
        end = min(start + config["max_overlay_duration"], total_duration)
        segments.append({**trigger, "start": start, "end": end})

    for index in range(len(segments) - 1):
        current = segments[index]
        next_item = segments[index + 1]
        if next_item["start"] - current["start"] < config["min_gap"]:
            current["end"] = min(current["end"], next_item["start"])

    return [s for s in segments if s["end"] - s["start"] >= 0.25]


def build_overlay_clips(config, core_dirs, segments):
    """A 类素材作为顶层覆盖；素材不可用时自动跳过。"""
    overlays = []
    for seg in segments:
        folder = core_dirs.get(seg["folder"])
        if not folder:
            print(f"跳过卖点覆盖：找不到 A 类目录 {seg['folder']}")
            continue

        files = list_media_files(folder, config["video_exts"])
        if not files:
            print(f"跳过卖点覆盖：目录为空 {folder}")
            continue

        raw = None
        for _ in range(min(len(files), 5)):
            raw = safe_video_clip(random.choice(files), config["min_video_duration"])
            if raw:
                break

        if raw is None:
            print(f"跳过卖点覆盖：目录内没有可用视频 {folder}")
            continue

        duration = min(seg["end"] - seg["start"], raw.duration)
        if duration < 0.25:
            raw.close()
            continue

        fade = min(config["fade_duration"], duration / 3)
        clip = fit_vertical_clip(raw.subclip(0, duration), config["video_size"])
        clip = (
            clip
            .fx(vfx.fadein, fade)
            .fx(vfx.fadeout, fade)
            .set_start(seg["start"])
            .set_duration(duration)
        )
        overlays.append(clip)

        print(
            f"卖点覆盖：{seg['keyword']} -> {seg['folder']}，"
            f"{seg['start']:.2f}s 到 {seg['start'] + duration:.2f}s"
        )

    return overlays


def download_bgm_if_url(url, temp_dir):
    """支持 MP3 链接；本地路径原样返回。"""
    if not url:
        return ""

    lower = url.lower()
    if not (lower.startswith("http://") or lower.startswith("https://")):
        return url

    ensure_dir(temp_dir)
    output = Path(temp_dir) / "downloaded_bgm.mp3"
    urllib.request.urlretrieve(url, output)
    return str(output)


def pick_bgm(config, bgm_arg):
    """用户指定 BGM 优先，否则从 bgm 目录随机选择。"""
    if bgm_arg:
        path = Path(bgm_arg)
        if not path.exists():
            raise FileNotFoundError(f"找不到指定 BGM：{path}")
        return path

    bgm_files = list_media_files(config["bgm_root"], config["audio_exts"])
    return random.choice(bgm_files) if bgm_files else None


def build_audio(config, voice_path, total_duration, bgm_path=None):
    """人声 + 20% 音量 BGM。"""
    voice = AudioFileClip(str(voice_path)).volumex(config["voice_volume"])
    audios = [voice]

    if bgm_path:
        try:
            bgm = AudioFileClip(str(bgm_path)).volumex(config["bgm_volume"])
            if bgm.duration < total_duration:
                bgm = afx.audio_loop(bgm, duration=total_duration)
            else:
                bgm = bgm.subclip(0, total_duration)
            audios.append(bgm)
        except Exception as exc:
            print(f"BGM 读取失败，已忽略：{bgm_path}，原因：{exc}")

    return CompositeAudioClip(audios).set_duration(total_duration)


def render_video(config, script_text, bgm_path):
    ensure_dir(config["output_root"])
    ensure_dir(config["temp_root"])

    with tempfile.TemporaryDirectory(dir=config["temp_root"]) as temp_dir:
        voice_path = Path(temp_dir) / "voice.mp3"

        print("正在生成口播音频和词时间戳...")
        try:
            word_timestamps = asyncio.run(synthesize_tts_with_timestamps(config, script_text, voice_path))
        except Exception as exc:
            print(f"edge-tts 生成失败，改用 Windows 本地语音兜底：{exc}")
            voice_path = Path(temp_dir) / "voice.wav"
            synthesize_windows_tts(script_text, voice_path)
            word_timestamps = []

        voice_audio = AudioFileClip(str(voice_path))
        total_duration = float(voice_audio.duration)
        voice_audio.close()

        print(f"口播总时长：{total_duration:.2f}s")
        print(f"词时间戳数量：{len(word_timestamps)}")

        core_dirs, bg_dirs = classify_material_folders(config)
        triggers = find_keyword_triggers(config, word_timestamps, script_text)
        if not triggers and not word_timestamps:
            print("未获取到词时间戳，改用文案字符位置估算卖点触发时间。")
            triggers = find_keyword_triggers_by_text_position(config, script_text, total_duration)
        segments = plan_overlay_segments(config, triggers, total_duration)

        print(f"识别到卖点触发：{len(triggers)} 个")
        print(f"生成有效覆盖片段：{len(segments)} 个")

        print("正在构建底层背景素材...")
        background = build_background_clip(config, bg_dirs, total_duration)

        print("正在构建顶层卖点素材...")
        overlays = build_overlay_clips(config, core_dirs, segments)

        print("正在混合音频...")
        final_audio = build_audio(config, voice_path, total_duration, bgm_path)

        final_video = CompositeVideoClip(
            [background, *overlays],
            size=config["video_size"],
        ).set_audio(final_audio).set_duration(total_duration)

        output_path = str(Path(config["output_root"]) / config["output_filename"])

        print("正在导出视频...")
        final_video.write_videofile(
            output_path,
            fps=config["fps"],
            codec="libx264",
            audio_codec="aac",
            preset="medium",
            threads=max(1, (os.cpu_count() or 2) - 1),
        )

        final_video.close()
        background.close()
        final_audio.close()

        print(f"完成：{output_path}")
        return output_path


def parse_args():
    parser = argparse.ArgumentParser(description="电商自动剪辑视频系统")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--script-file", default="", help="指定口播文案 txt")
    parser.add_argument("--script-text", default="", help="直接输入口播文案")
    parser.add_argument("--auto-script", action="store_true", help="强制使用默认自动文案")
    parser.add_argument("--bgm", default="", help="BGM 本地路径或 MP3 链接")
    parser.add_argument("--output", default="", help="自定义输出文件名，如 demo.mp4")
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config(args.config)

    if args.output:
        config["output_filename"] = args.output

    ensure_dir(config["project_root"])
    ensure_dir(config["bgm_root"])
    ensure_dir(config["output_root"])
    ensure_dir(config["temp_root"])

    if shutil.which("ffmpeg") is None:
        print("提示：系统 PATH 中未检测到 ffmpeg。MoviePy 可能仍会使用 imageio-ffmpeg。")

    script_text = generate_auto_script() if args.auto_script else load_script(args, config)
    bgm_arg = download_bgm_if_url(args.bgm, config["temp_root"]) if args.bgm else ""
    bgm_path = pick_bgm(config, bgm_arg)

    if bgm_path:
        print(f"使用 BGM：{bgm_path}")
    else:
        print("未找到 BGM，将仅使用口播人声。")

    render_video(config, script_text, bgm_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("用户中断。")
        sys.exit(130)
    except Exception as exc:
        print(f"运行失败：{exc}")
        sys.exit(1)
