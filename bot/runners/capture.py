"""
采集运行器 —— 用同一套 EnvSpec / Environment / Recorder 做「生产环境学习」

两种采集：
  kind="page"     采集当前（生产）页面代码 / 控件 / 下拉 / 表格，并可与 mock 做结构差异
  kind="actions"  在（生产）浏览器里记录人工操作，输出流程改进草案

产出统一落一份 RunRecord（data/compare），与其它运行方式的记录同结构、可比对。
"""
from __future__ import annotations

from bot.core.records import Recorder, RunRecord
from bot.envs import EnvSpec, build_environment
from bot.logging_system import log_error, log_run, setup
from bot.runtime import BotStopped, clear_stop

from .base import finalize_record


async def run_capture(name: str, spec: EnvSpec, *, kind: str = "page",
                      duration_s: int = 600, headless: bool = False,
                      with_mock_compare: bool = False,
                      snapshot_pages: bool = True,
                      stop_event=None, logger=None) -> RunRecord:
    """kind=page → 页面结构采集；kind=actions → 人工操作录制"""
    setup()
    clear_stop()

    def emit(line: str):
        print("  " + line)
        if logger:
            try:
                logger(line)
            except Exception:
                pass

    rec = Recorder(RunRecord(mode=f"capture_{kind}", env_portal=spec.portal,
                             env_market=spec.market, claim_no=name)).begin()
    log_run(f"采集启动 | kind={kind} | name={name} | {spec.label}")

    status = "failed"
    data: dict = {}

    # 人工操作录制必须可见浏览器
    if kind == "actions":
        headless = False

    async with build_environment(spec, headless=headless) as env:
        try:
            print(f"\n{'='*70}")
            print(f"  {'📸 采集生产页面' if kind == 'page' else '🖐 录制人工操作'} —— {name}")
            print(f"  环境: {spec.label}")
            print(f"{'='*70}")

            async with rec.step("CAPTURE 1", "打开首页并等待就绪"):
                try:
                    await env.portal.go_home()
                    await env.portal.ensure_ready()
                except Exception as exc:
                    emit(f"⚠️ 门户就绪检查未通过（不影响采集）: {exc}")

            if kind == "page":
                from bot.capture import capture_environment, write_report

                # 一车一件页面也打开一次首页，否则真实环境只能采到空白页
                try:
                    await env.market.go_home()
                except Exception as exc:
                    emit(f"⚠️ 一车一件首页未打开（不影响门户采集）: {exc}")

                result = await capture_environment(env, name, screenshot=True)
                for i, snap in enumerate(result["snapshots"], 1):
                    rec.mark(f"PAGE {i}", "ok",
                             name=snap.get("name"), url=snap.get("url"),
                             controls=len(snap.get("controls", [])),
                             tables=len(snap.get("tables", [])),
                             files=snap.get("files"))
                    emit(f"📸 {snap.get('name')} 控件={len(snap.get('controls', []))} "
                         f"下拉={len(snap.get('selects', []))} 表格={len(snap.get('tables', []))}")

                mock_snaps = None
                if with_mock_compare:
                    emit("🧪 同时采集 mock 环境做结构对比…")
                    async with build_environment(EnvSpec.mock(), headless=True) as menv:
                        try:
                            await menv.portal.go_home()
                        except Exception:
                            pass
                        try:
                            await menv.market.go_home()
                        except Exception:
                            pass
                        mock_res = await capture_environment(menv, name, screenshot=False)
                        mock_snaps = mock_res["snapshots"]
                    write_report(result["snapshots"], result["dir"],
                                 title=f"页面结构采集 · {name}",
                                 mock_snapshots=mock_snaps)

                data = {
                    "kind": kind,
                    "dir": result["dir"],
                    "pages": [s.get("name") for s in result["snapshots"]],
                    "urls": [s.get("url") for s in result["snapshots"]],
                    "with_mock_compare": bool(with_mock_compare),
                }
                emit(f"📂 采集目录: {result['dir']}")
                emit(f"📄 报告: {result['dir']}\\report.md")
                status = "done"

            elif kind == "actions":
                from bot.capture import record_actions

                context = getattr(env.portal, "context", None) or getattr(env.portal, "_ctx", None)
                if context is None:
                    raise RuntimeError("当前环境不支持上下文录制（未取到 BrowserContext）")

                emit("⏺ 请在浏览器里手动操作 —— 程序自动记录点击/输入/跳转")
                res = await record_actions(
                    context, name,
                    duration_s=duration_s,
                    stop_event=stop_event,
                    snapshot_pages=snapshot_pages,
                    on_step=lambda s: emit(
                        f"· {getattr(s, 'action', '')} "
                        f"{(getattr(s, 'label', '') or '')[:16]} "
                        f"{(getattr(s, 'selector', '') or '')[:52]}"),
                    meta={"portal": spec.portal, "market": spec.market},
                )

                for i, item in enumerate(res["steps"], 1):
                    rec.mark(f"ACTION {i}", "ok",
                             action=item.get("action"), selector=item.get("selector"),
                             text=item.get("text"), value=item.get("value"))
                data = {
                    "kind": kind,
                    "dir": res["dir"],
                    "actions": len(res["steps"]),
                    "duration_s": res["duration_s"],
                    "workflow": res["workflow"],
                    "markdown": res["markdown"],
                    "snapshots": [s.get("dir") for s in res.get("snapshots", [])],
                }
                emit(f"📂 录制目录: {res['dir']}")
                emit(f"📄 改进草案: {res['markdown']}")
                emit(f"🗂 页面快照: {len(res.get('snapshots', []))} 份")
                status = "done"

            else:
                raise ValueError(f"未知采集类型: {kind}")

        except BotStopped:
            status = "stopped"
            emit("⏹ 采集已被停止")
            log_run(f"采集停止 | kind={kind} | {name}")
        except Exception as exc:
            status = "failed"
            log_error(f"采集失败 kind={kind} name={name}", exc)
            emit(f"❌ 采集失败: {exc}")

    finalize_record(rec, status=status, note=f"采集 {kind}: {name}",
                    data=data, ignore_env=True)
    return rec.record
