"""Commands for the self-contained local acceptance lab."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import time

from .runner import acceptance, build_app, configuration, local_network
from .services import LabServices


def main(argv=None):
    parser = argparse.ArgumentParser(description="TestPilot local simulation lab")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-app", help="Compile the C++ / Qt Widgets mock software")
    build.add_argument("--out", default="dist/simulation/qt/MockInstrument.exe")
    serve = commands.add_parser("serve", help="Run mock Zentao, device and parser endpoints")
    serve.add_argument("--http-port", type=int, default=19380)
    serve.add_argument("--ws-port", type=int, default=19381)
    serve.add_argument("--app", action="store_true", help="Also launch the Qt mock app")
    serve.add_argument("--app-exe", default=None, help="Use a prebuilt Qt app instead of rebuilding")
    serve.add_argument("--data-dir", default=".cache/simulation-live")
    test = commands.add_parser("test", help="Run actual engine/RPC acceptance scenarios")
    test.add_argument("--gui", action="store_true", help="Exercise the actual Qt UIA controls")
    test.add_argument("--app-exe", default=None, help="Use a prebuilt Qt app")
    test.add_argument("--out", default=None, help="New isolated evidence directory")
    qt = commands.add_parser("test-qt", help="Run Qt Widgets and Quick/QML specialist acceptance")
    qt.add_argument("--app-exe", required=True)
    qt.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "test-qt":
        import faulthandler
        from .qt_acceptance import qt_acceptance
        faulthandler.dump_traceback_later(120, repeat=False)
        try:
            result = qt_acceptance(args.out, args.app_exe)
        finally:
            faulthandler.cancel_dump_traceback_later()
        return 0 if result["ok"] else 1
    if args.command == "build-app":
        print(build_app(args.out))
        return 0
    if args.command == "test":
        output = args.out or (".cache/simulation-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        import faulthandler
        faulthandler.dump_traceback_later(120, repeat=False)
        try:
            result = acceptance(output, gui=args.gui, app_exe=args.app_exe)
        finally:
            faulthandler.cancel_dump_traceback_later()
        print(json.dumps({"ok": result["ok"], "report": str(Path(output).resolve() / "acceptance.md")}))
        return 0 if result["ok"] else 1
    root = Path(args.data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    process = None
    with local_network(), LabServices(args.http_port, args.ws_port) as lab:
        cfg = configuration(root, lab)
        config_path = root / "config.json"
        config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            if args.app or args.app_exe:
                exe = Path(args.app_exe).resolve() if args.app_exe else build_app(root / "MockInstrument.exe")
                process = subprocess.Popen([str(exe), lab.url, str(root / "exports")])
            print(json.dumps({"simulation": True, "dashboard": lab.url, "emulator": lab.endpoint,
                              "credentials": "demo/demo (local only)", "config": str(config_path),
                              "note": "Set NO_PROXY=127.0.0.1,localhost for other CLI processes"},
                             ensure_ascii=False), flush=True)
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            return 0
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
