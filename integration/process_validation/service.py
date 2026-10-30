"""Authenticated coordinator adapter. Audits use CPU and existing cloud files."""
import json
import os
from pathlib import Path
import re
import subprocess
import threading


class ValidationService:
    def __init__(self,runs_root,python):
        self.root=Path(runs_root).resolve();self.python=python
        self.lock=threading.RLock();self.processes={}

    def _run(self,run_id):
        if not isinstance(run_id,str) or not re.fullmatch(r"[a-f0-9]{32}",run_id):
            raise ValueError("请输入有效的32位运行编号")
        path=(self.root/run_id).resolve()
        if path.parent!=self.root or not path.is_dir():raise ValueError("找不到该运行的云端记录")
        return path

    def read(self,run_id):
        path=self._run(run_id);folder=path/"process_validation"
        worker=self.processes.get(run_id)
        if worker is not None and worker.poll() is None:
            return {"run_id":run_id,"status":"running","message":"正在校验已有记录与云端录像"}
        status_path=folder/"audit_status.json"
        state=json.loads(status_path.read_text(encoding="utf8")) if status_path.is_file() else {}
        if state.get("status")=="running":
            try:
                os.kill(int(state["pid"]),0)
                return {"run_id":run_id,"status":"running","message":"正在校验已有记录与云端录像"}
            except (ProcessLookupError,ValueError,KeyError):
                return {"run_id":run_id,"status":"interrupted","message":"上次检查进程已退出，可重新检查"}
        target=folder/"report.json"
        if target.is_file():
            report=json.loads(target.read_text(encoding="utf8"))
            from process_validation.repairs import build_repair_plan
            correction=build_repair_plan(path,report)
            from process_validation.continuation import build_continuation_plan
            continuation=build_continuation_plan(path,report)
            return {"run_id":run_id,"status":"ready","report":report,"correction":correction,
                    "continuation":continuation}
        return {"run_id":run_id,"status":"not_checked","message":"这条记录还没有过程校验报告"}

    def start(self,run_id):
        with self.lock:
            path=self._run(run_id)
            if self.read(run_id)["status"]=="running":return self.read(run_id)
            # Do not audit a changing file while an actual renderer is writing.
            manifest=path/"video_manifest.json"
            if not manifest.is_file():raise ValueError("该记录没有动作录像清单；数值结果无动作视频可检验")
            data=json.loads(manifest.read_text(encoding="utf8"))
            if data.get("status") in {"starting","recording"}:raise ValueError("录像仍在录制，结束后自动检查")
            folder=path/"process_validation";folder.mkdir(exist_ok=True)
            log=(folder/"audit.log").open("ab")
            try:
                worker=subprocess.Popen([self.python,"-u",str(Path(__file__).with_name("audit_run.py")),"--run-dir",str(path)],
                                         stdout=log,stderr=subprocess.STDOUT,start_new_session=os.name!="nt")
            finally:log.close()
            self.processes[run_id]=worker
            return {"run_id":run_id,"status":"running","message":"已开始检查原记录；不会重新仿真"}


def start_automatic_audit(run_dir,python):
    run_dir=Path(run_dir)
    return ValidationService(run_dir.parent,python).start(run_dir.name)
