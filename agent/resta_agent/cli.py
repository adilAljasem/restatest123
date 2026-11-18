"""أوامر الوكيل:

  resta-agent serve                      تشغيل الوكيل (خدمة systemd)
  resta-agent setup --config FILE        تجهيز السيرفر أول مرة
  resta-agent backup-all                 النسخ الليلي لكل القواعد + تنظيف النسخ القديمة
  resta-agent run OP --params JSON       تنفيذ عملية يدوياً (للدعم الفني)
"""
import argparse
import json
import logging
import sys

import psutil

from . import config as config_mod
from .ops import Operations


def main(argv=None):
    parser = argparse.ArgumentParser(prog="resta-agent")
    parser.add_argument("--config", default=None, help="مسار ملف الإعدادات")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    p_setup = sub.add_parser("setup")
    p_setup.add_argument("--input", required=True, help="رد المنصة على التسجيل (JSON)")
    sub.add_parser("backup-all")
    p_run = sub.add_parser("run")
    p_run.add_argument("op")
    p_run.add_argument("--params", default="{}")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_path = args.config or config_mod.DEFAULT_CONFIG_PATH

    if args.cmd == "setup":
        from .setup import run_setup
        with open(args.input, encoding="utf-8") as fh:
            data = json.load(fh)
        ram_gb = psutil.virtual_memory().total / 1073741824
        result = run_setup(data, config_path, ram_gb)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    settings = config_mod.load(config_path)
    if args.cmd == "serve":
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.bind_port,
                    log_level="info", proxy_headers=False)
        return 0
    ops = Operations(settings)
    if args.cmd == "backup-all":
        result = ops.backup_all()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["failed"] else 0
    if args.cmd == "run":
        result = ops.dispatch(args.op, json.loads(args.params))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
