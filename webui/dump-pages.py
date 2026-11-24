import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import webapp
from webapp import AppState

state = AppState(tempfile.mkdtemp())
pages = {}
for path in ("/", "/extract", "/jobs"):
    r = webapp.handle_request("GET", path, {}, {}, state)
    pages[path] = r.body
# /plan 后页面（含规则/schema面板）——用显式规则离线生成
rule = {
    "version": "2.0", "task_type": "summary",
    "source": {"path": "lots.csv", "format": "csv"},
    "steps": [{"op": "count_rows", "by": ["市"]}],
    "options": {"fill_missing": "none"},
    "output": {"formats": ["csv"], "report": False},
}
r = webapp.handle_request("POST", "/plan", {}, {
    "intent": ["统计条数"], "source_path": ["lots.csv"], "source_format": ["csv"],
    "rule_json": [json.dumps(rule, ensure_ascii=False)], "formats": ["csv"],
}, state)
pages["/plan"] = r.body
print(json.dumps(pages, ensure_ascii=False))
