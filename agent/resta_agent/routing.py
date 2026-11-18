"""توجيه الروابط في nginx.

كل مطعم رابطه: <اسم-القاعدة>.<النطاق>  مثل kaffa.restaerp.com
- routes.map     : أي طبقة (stack) تخدم الرابط. الافتراضي main.
- suspended.map  : الروابط الموقوفة تظهر لها صفحة "الاشتراك متوقف".
"""
import json
from pathlib import Path

from .config import Settings, check_db_name, check_stack_name


class Routing:
    def __init__(self, settings: Settings):
        self.s = settings
        self.state_file = settings.agent_dir / "routing.json"

    def _load(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {"routes": {}, "suspended": []}

    def _save(self, state: dict):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2, sort_keys=True))
        self.render(state)

    def render(self, state: dict | None = None):
        state = state or self._load()
        conf_dir = self.s.nginx_dir / "resta"
        conf_dir.mkdir(parents=True, exist_ok=True)
        routes = "".join(f'"{sub}" {stack};\n' for sub, stack in sorted(state["routes"].items()))
        suspended = "".join(f'"{sub}" 1;\n' for sub in sorted(set(state["suspended"])))
        (conf_dir / "routes.map").write_text(routes)
        (conf_dir / "suspended.map").write_text(suspended)

    def set_route(self, sub: str, stack: str):
        check_db_name(sub)
        check_stack_name(stack)
        state = self._load()
        state["routes"][sub] = stack
        self._save(state)

    def remove(self, sub: str):
        state = self._load()
        state["routes"].pop(sub, None)
        state["suspended"] = [s for s in state["suspended"] if s != sub]
        self._save(state)

    def set_suspended(self, sub: str, suspended: bool):
        check_db_name(sub)
        state = self._load()
        items = set(state["suspended"])
        if suspended:
            items.add(sub)
        else:
            items.discard(sub)
        state["suspended"] = sorted(items)
        self._save(state)

    def is_suspended(self, sub: str) -> bool:
        return sub in self._load()["suspended"]

    def routes(self) -> dict:
        return dict(self._load()["routes"])
