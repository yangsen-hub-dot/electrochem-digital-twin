"""Cloud-side MP4 decode, frame timeline and conservative image screening."""
import math
from pathlib import Path
import subprocess
import numpy as np


def check_timeline(manifest, report):
    frames = manifest.get("frames", [])
    fps = manifest.get("fps")
    if not isinstance(fps, (int,float)) or not math.isfinite(fps) or fps <= 0:
        report.issue("video", "fps", "fail", "录像清单缺有效帧率")
        return
    previous_time = -1.0
    bad = 0
    for index, frame in enumerate(frames):
        now = frame.get("simulation_time_s")
        if frame.get("frame") != index or not math.isclose(frame.get("video_time_s",-1),index/fps,abs_tol=1e-7) or not isinstance(now,(int,float)) or not math.isfinite(now) or now < previous_time:
            bad += 1
        previous_time = now if isinstance(now,(int,float)) else previous_time
    if bad or not frames:
        report.issue("video", "timeline", "fail", "录像帧号、播放时刻或仿真时刻不连续", bad_frames=bad)
    else:
        report.check("video", "timeline", "pass", f"{len(frames)} 帧清单顺序及时间基准正常")
    if manifest.get("frame_count") != len(frames):
        report.issue("video", "manifest_count", "fail", "录像清单帧数与帧列表长度不一致")


def check_video(run_dir, manifest, policy, report):
    video = run_dir/"first_transfer.mp4"
    if not video.is_file():
        video = run_dir/"first_transfer.partial.mp4"
    if not video.is_file():
        report.check("video", "file", "uncovered", "没有可读取的视频文件")
        return None
    try:
        import imageio_ffmpeg
    except ImportError:
        report.check("video", "decoder", "uncovered", "当前检查环境缺少imageio-ffmpeg，未解码录像")
        return video
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    # Explicit full decode with -xerror catches damaged streams rather than
    # treating a partially decoded file or just a valid header as success.
    decoded = subprocess.run([ffmpeg,"-v","error","-xerror","-i",str(video),"-map","0:v:0","-f","null","-"],capture_output=True,text=True,timeout=240)
    if decoded.returncode or decoded.stderr.strip():
        report.issue("video", "full_decode", "fail", "录像全片解码失败", decoder_message=decoded.stderr[-1800:])
        return video
    report.check("video", "full_decode", "pass", "云端录像全片解码通过，未下载视频")
    width,height = policy["video_analysis_size"]
    reader = imageio_ffmpeg.read_frames(str(video), output_params=["-vf",f"scale={width}:{height}"])
    meta = next(reader)
    source_width,source_height = meta.get("source_size",meta["size"])
    report.data["video"] = {"cloud_path":str(video),"size":[source_width,source_height],"fps":meta["fps"],"duration_s":meta.get("duration")}
    if source_width < policy["minimum_video_size"][0] or source_height < policy["minimum_video_size"][1]:
        report.issue("video", "resolution", "fail", "实际编码尺寸未达到动作视频规范", actual_size=[source_width,source_height])
    else:
        report.check("video", "resolution", "pass", f"实际编码尺寸 {source_width}×{source_height}")
    fps = float(meta["fps"])
    count, black_start, freeze_start, previous, last_pose = 0, None, None, None, None
    entries = manifest.get("frames", [])
    for raw in reader:
        pixels = np.frombuffer(raw,dtype=np.uint8).reshape(height,width,3).astype(float)
        # Ignore the deliberately dark right information panel. Screen the
        # overview portion only; macro/transparent-material semantics uncovered.
        scene = pixels[:, :max(1,int(width*.68)), :]
        luma = scene@np.array([.2126,.7152,.0722])
        black = float(np.mean(luma < policy["black_luma_threshold_0_255"])) >= policy["black_fraction_review"]
        black_start = count if black and black_start is None else black_start if black else None
        frame = entries[count] if count<len(entries) else {}
        location = {"frame_index":count,"video_time_s":count/fps,"step_id":frame.get("step_id")}
        if black_start is not None and (count-black_start+1)/fps >= policy["black_duration_review_s"]:
            report.issue("video", "black_screen", "review", "全景区域持续接近黑屏，请核对相机/遮挡", **location)
        pose = frame.get("robot_grasp_frame_m")
        moved = pose is not None and last_pose is not None and np.linalg.norm(np.asarray(pose)-last_pose) > .0001
        delta = float(np.mean(np.abs(scene-previous))) if previous is not None else math.inf
        frozen = delta <= policy["freeze_mean_difference_0_255"]
        freeze_start = count if frozen and freeze_start is None else freeze_start if frozen else None
        if moved and freeze_start is not None and (count-freeze_start+1)/fps >= policy["freeze_duration_review_s"]:
            report.issue("video", "frozen_while_moving", "review", "末端记录在变化但画面长期近似不动，可能停帧或动作被遮挡", **location)
        previous, last_pose, count = scene, np.asarray(pose) if pose is not None else None, count+1
    expected = len(entries)
    if count != expected:
        report.issue("video", "encoded_count", "fail", "实际可解码帧数与本次帧清单不一致", encoded_frames=count, manifest_frames=expected)
    else:
        report.check("video", "encoded_count", "pass", f"实际解码 {count} 帧，与本次帧清单一致")
    if not math.isclose(fps,float(manifest.get("fps",0)),abs_tol=.01):
        report.issue("video", "encoded_fps", "fail", "实际视频帧率与清单不一致")
    return video


def export_issue_frames(video, manifest, root, policy, report):
    if video is None:
        return
    try:
        import imageio_ffmpeg
    except ImportError:
        return
    selected = []
    for row in sorted(report.data["issues"],key=lambda row: row["status"] != "fail"):
        index = row.get("frame_index")
        if isinstance(index,int) and index not in selected and len(selected)<policy["max_evidence_frames"]:
            selected.append(index)
    if not selected:
        return
    evidence = root/"evidence";evidence.mkdir(exist_ok=True)
    for index in selected:
        target = evidence/f"frame_{index:06d}.jpg"
        result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),"-v","error","-y","-i",str(video),"-vf",f"select=eq(n\\,{index}),scale=1280:-1","-frames:v","1",str(target)],capture_output=True,timeout=90)
        if result.returncode == 0 and target.is_file():
            for row in report.data["issues"]:
                if row.get("frame_index") == index:
                    row["evidence_image"] = "evidence/"+target.name
