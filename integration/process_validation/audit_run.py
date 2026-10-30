"""Independent CLI: audit existing files, never launch/re-run an experiment."""
import argparse
import json
from pathlib import Path
import sys
import traceback
import os
from datetime import datetime, timezone

if __package__ in (None, ""):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from process_validation.report import Report, write_report, atomic_json
from process_validation.record_checks import check_records
from process_validation.video_checks import check_timeline, check_video, export_issue_frames
from process_validation.geometry_checks import check_trace
from process_validation.continuation import source_revisions
from process_validation.repair_advice import build_recommendations
from process_validation.repairs.cycle import record_recheck
from process_validation.chemistry_checks import check_chemistry
from process_validation.repairs.effects.fingerprints import checker_fingerprints


def audit_run(run_dir, *, decode_video=True, export_evidence=True, policy_path=None):
    run_dir=Path(run_dir).resolve()
    policy=json.loads(Path(policy_path or Path(__file__).with_name("policy.json")).read_text(encoding="utf-8"))
    report=Report(run_dir.name,policy)
    report.data.update(checker_fingerprints(policy))
    evidence_before=source_revisions(run_dir)
    root=run_dir/"process_validation";root.mkdir(exist_ok=True)
    try:
        archive=json.loads((run_dir/"run.json").read_text(encoding="utf-8"))
        manifest=json.loads((run_dir/"video_manifest.json").read_text(encoding="utf-8"))
    except (OSError,ValueError) as exc:
        report.issue("records","missing_archive","fail","无法读取本次动作档案或录像清单",detail=str(exc))
        data=report.finish();data["recommendations"] = build_recommendations(data["issues"])
        write_report(root,data);return data
    video=None
    for group,callback in (("records",lambda:check_records(archive,manifest,report)),
                           ("chemistry",lambda:check_chemistry(archive,report)),
                           ("timeline",lambda:check_timeline(manifest,report)),
                           ("geometry",lambda:check_trace(root,archive,manifest,policy,report))):
        try: callback()
        except Exception as exc:
            report.issue(group,"checker_error","fail","检查模块发生异常，未判定通过",detail=str(exc),traceback=traceback.format_exc()[-1800:])
    if decode_video:
        try: video=check_video(run_dir,manifest,policy,report)
        except Exception as exc: report.issue("video","checker_error","fail","视频检查未能完成",detail=str(exc))
    else:
        report.check("video","decoder_skipped","uncovered","本次仅核对记录与几何，没有解码视频")
    if export_evidence:
        try: export_issue_frames(video,manifest,root,policy,report)
        except Exception as exc: report.check("evidence","export","review","异常帧提取未完成",detail=str(exc))
    for message in policy["uncovered_checks"]:
        report.check("coverage","not_implemented","uncovered",message)
    evidence_after=source_revisions(run_dir)
    report.data['source_revisions']=evidence_before
    if evidence_after != evidence_before:
        report.issue('records','evidence_changed','fail','检查期间运行证据发生变化，请重新检查')
    data=report.finish();data["recommendations"] = build_recommendations(data["issues"])
    write_report(root,data)
    try:
        record_recheck(run_dir, data)
    except (OSError, ValueError, TypeError):
        # The independent audit remains authoritative if its optional lifecycle
        # sidecar cannot be written.
        pass
    return data


def main():
    parser=argparse.ArgumentParser(description="校验已有仿真全过程；不会启动仿真或重抽误差")
    parser.add_argument("--run-dir",type=Path,required=True)
    parser.add_argument("--skip-video",action="store_true")
    parser.add_argument("--no-evidence",action="store_true")
    args=parser.parse_args()
    folder=args.run_dir/"process_validation";folder.mkdir(exist_ok=True)
    atomic_json(folder/"audit_status.json",{"status":"running","pid":os.getpid(),"started_at_utc":datetime.now(timezone.utc).isoformat()})
    try:
        result=audit_run(args.run_dir,decode_video=not args.skip_video,export_evidence=not args.no_evidence)
    except Exception as exc:
        atomic_json(folder/"audit_status.json",{"status":"failed","message":str(exc)})
        raise
    atomic_json(folder/"audit_status.json",{"status":"finished","result":result["status"],"finished_at_utc":datetime.now(timezone.utc).isoformat()})
    print(json.dumps({k:result[k] for k in ("run_id","status","status_label","counts")},ensure_ascii=False))
    return 2 if result["status"]=="fail" else 3 if result["status"] in {"review","uncovered"} else 0


if __name__=="__main__":raise SystemExit(main())
