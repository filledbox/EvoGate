"""Launch the local SQL assistant or import a BIRD dataset."""

import argparse
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .data import import_bird
from .patches import add_edit_arguments, budget_from_arguments
from .recording import load_environment
from .service import Service, serializable

STATIC = Path(__file__).parent / "static"


def handler(service):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, value):
            data = json.dumps(serializable(value), ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/state":
                with service.lock:
                    try:
                        self.reply(200, service.public_state())
                    except ValueError as exc:
                        self.reply(400, {"error": str(exc)})
                return
            files = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
            if path not in files:
                self.reply(404, {"error": "页面不存在。"})
                return
            file = STATIC / files[path]
            data = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self.reply(403, {"error": "不允许跨站调用本地应用。"})
                return
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("请求必须为 JSON。")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 256000:
                    raise ValueError("请求大小无效。")
                payload = json.loads(self.rfile.read(length))
                routes = {
                    "/api/query": service.query, "/api/feedback": service.feedback,
                    "/api/optimize": lambda db_id: service.schedule(db_id, manual=True),
                    "/api/rollback": service.rollback,
                    "/api/compare": service.compare,
                    "/api/evolve": lambda db_id: service.schedule(db_id, manual=True),
                    "/api/skills/import": service.import_skill,
                    "/api/skills/bind": service.bind_skill,
                    "/api/skills/switch": service.switch_branch,
                    "/api/databases": service.register_database,
                    "/api/cases": service.add_cases,
                    "/api/evolution/config": service.configure_evolution,
                }
                action = routes.get(urlparse(self.path).path)
                if not action:
                    self.reply(404, {"error": "接口不存在。"})
                    return
                if urlparse(self.path).path in {"/api/query", "/api/compare"}:
                    result = action(**payload)
                else:
                    with service.lock:
                        result = action(**payload)
                self.reply(200, result)
            except (ValueError, TypeError, KeyError, StopIteration) as exc:
                self.reply(400, {"error": str(exc) or "输入无效。"})
            except Exception as exc:
                self.log_error("%s: %s", type(exc).__name__, str(exc))
                self.reply(500, {"error": "操作失败，请查看终端日志。已有版本和记录保留。"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="EvoGate · 支持 Skill 自优化的 SQL 分析助手")
    parser.add_argument("--version", action="version", version=f"EvoGate {__version__}")
    parser.add_argument("--workspace", type=Path, help="本地运行数据目录")
    parser.add_argument("--live", action="store_true", help="启用真实模型，会产生 API 调用")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--import-bird", type=Path, help="导入 BIRD JSON / JSONL 后退出")
    parser.add_argument("--db-root", type=Path)
    parser.add_argument("--db-ids", nargs="+")
    parser.add_argument("--per-split", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--env-file", type=Path, help="显式加载模型环境变量文件")
    parser.add_argument("--database", type=Path, help="注册任意 SQLite 文件后退出")
    parser.add_argument("--db-id", help="数据库标识")
    parser.add_argument("--context", type=Path, help="数据库字段及业务说明文本")
    parser.add_argument("--skill", type=Path, help="导入初始 SKILL.md，可与数据库注册一起使用")
    parser.add_argument("--cases", type=Path, help="导入开发、选择与保留案例 JSON 数组")
    parser.add_argument("--auto-evolve", action="store_true", help="启用反馈触发的后台进化")
    parser.add_argument("--demo-report", action="store_true", help="离线执行优化和对比后退出")
    add_edit_arguments(parser)
    args = parser.parse_args()
    if args.env_file:
        load_environment(args.env_file)
    root = args.workspace or Path("workspaces") / (
        "live" if args.live or args.import_bird else "demo"
    )
    service = Service(root, live=args.live, edit_budget=budget_from_arguments(args))
    skill_id = "sql-analysis"
    if args.skill:
        skill_id = service.import_skill(args.skill.read_text())["id"]
    if args.database:
        if not args.db_id:
            parser.error("--database 需要 --db-id")
        service.register_database(args.db_id, str(args.database),
                                  context=args.context.read_text() if args.context else "",
                                  skill_id=skill_id)
    elif args.skill and args.db_id:
        service.bind_skill(args.db_id, skill_id)
    if args.cases:
        if not args.db_id:
            parser.error("--cases 需要 --db-id")
        service.add_cases(args.db_id, json.loads(args.cases.read_text()))
    if args.auto_evolve:
        service.configure_evolution(enabled=True)
    if args.database or args.cases or args.skill and not args.import_bird:
        print(json.dumps({"workspace": str(root.resolve()), "skill_id": skill_id,
                          "db_id": args.db_id}, ensure_ascii=False))
        return
    if args.import_bird:
        if not args.db_root or not args.db_ids:
            parser.error("导入需要 --db-root 和 --db-ids")
        databases = import_bird(
            args.import_bird, args.db_root, args.db_ids, args.per_split, args.seed,
        )
        service.add_databases(databases, skill_id)
        print(json.dumps({"workspace": str(root.resolve()), "imported": [
            {"db_id": db["id"], "tasks": len(db["tasks"])} for db in databases
        ]}, ensure_ascii=False, indent=2))
        return
    if args.demo_report:
        if args.live:
            parser.error("--demo-report 不能与 --live 同时使用")
        if not service.state["reports"]:
            service.optimize("sales-demo")
        report = service.compare("sales-demo")
        print(json.dumps({key: report[key] for key in (
            "kind", "baseline", "optimized", "gain", "count", "improved", "regressed"
        )}, ensure_ascii=False, indent=2))
        return
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(service))
    print(f"EvoGate: http://127.0.0.1:{args.port}", flush=True)
    print(f"Mode: {service.model.info()['mode']} | Workspace: {root.resolve()}", flush=True)
    stop = threading.Event()

    def scheduler():
        while not stop.wait(5):
            with service.lock:
                database_ids = list(service.state["databases"])
            for db_id in database_ids:
                try:
                    service.schedule(db_id)
                except ValueError:
                    continue

    threading.Thread(target=scheduler, name="evogate-scheduler", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
