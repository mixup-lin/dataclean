"""LLM 配置接入验证：.env 加载、标准库 HTTP 兜底、在线规则生成、诊断脱敏。

全程使用本地假端点，不联网、不读取本机 .env 中的真实凭据。
运行：pytest -q tests/test_llm_config.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
FAKE_KEY = "test-key-for-pytest-only"

ONLINE_RULE = {
    "version": "2.0",
    "intent": "在线模型生成的规则",
    "source": {"path": "lots.csv", "format": "csv"},
    "options": {"fill_missing": "mean", "drop_duplicates": True},
    "steps": [],
    "output": {"formats": ["csv", "json"], "report": True},
}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.calls.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "model": body.get("model"),
        })
        payload = {
            "choices": [{
                "message": {"content": json.dumps(ONLINE_RULE, ensure_ascii=False)}
            }]
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def fake_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.calls = []
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", server.calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.fixture
def isolated_llm(monkeypatch):
    """隔离本机配置：阻断自动 .env 加载并清空 LLM_* 环境变量。"""
    import llm_provider as lp

    for key in lp._DOTENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(lp, "load_dotenv", lambda *a, **kw: None)
    return lp


def test_dotenv_loading_and_system_env_priority(tmp_path, monkeypatch):
    import llm_provider as lp

    for key in lp._DOTENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    saved_state = dict(lp._DOTENV_STATE)
    saved_injected = set(lp._DOTENV_INJECTED)
    saved_hash = dict(lp._DOTENV_HASH)
    lp._DOTENV_STATE.update({"path": "", "mtime": -1.0})
    lp._DOTENV_INJECTED.clear()
    lp._DOTENV_HASH.clear()
    try:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\uFEFF# 注释行（含 UTF-8 BOM）\nLLM_API_KEY=dotenv-key\n"
            "LLM_MODEL=model-from-dotenv\nexport LLM_TIMEOUT=15\n"
            'LLM_VISION_MODEL="vision-model"\n',
            encoding="utf-8",
        )
        assert lp.load_dotenv(str(env_file)) == env_file
        assert os.environ["LLM_API_KEY"] == "dotenv-key"
        assert os.environ["LLM_MODEL"] == "model-from-dotenv"
        assert os.environ["LLM_TIMEOUT"] == "15"
        assert os.environ["LLM_VISION_MODEL"] == "vision-model"

        # 显式导出的系统环境变量优先，.env 热更新也不得覆盖
        monkeypatch.setenv("LLM_MODEL", "system-model")
        env_file.write_text("LLM_MODEL=dotenv-model-v2\n", encoding="utf-8")
        lp._DOTENV_STATE.update({"mtime": -1.0})
        lp.load_dotenv(str(env_file))
        assert os.environ["LLM_MODEL"] == "system-model"
    finally:
        for key in list(lp._DOTENV_INJECTED):
            os.environ.pop(key, None)
        lp._DOTENV_INJECTED.clear()
        lp._DOTENV_INJECTED.update(saved_injected)
        lp._DOTENV_HASH.clear()
        lp._DOTENV_HASH.update(saved_hash)
        lp._DOTENV_STATE.update(saved_state)


def test_urllib_transport_calls_online_endpoint(fake_endpoint, isolated_llm, monkeypatch):
    url, calls = fake_endpoint
    monkeypatch.setattr(isolated_llm, "requests", None)
    provider = isolated_llm.OpenAICompatProvider(
        url, FAKE_KEY, "fake-model", timeout=5, max_retries=0
    )
    content = provider.complete("system", "user")
    assert json.loads(content)["version"] == "2.0"
    assert len(calls) == 1
    assert calls[0]["path"] == "/v1/chat/completions"
    assert calls[0]["auth"] == f"Bearer {FAKE_KEY}"
    assert calls[0]["model"] == "fake-model"


def test_get_provider_online_drives_rule_generation(fake_endpoint, isolated_llm, monkeypatch):
    from rules import generate_rules_legacy as generate_rules

    url, calls = fake_endpoint
    monkeypatch.setenv("LLM_BASE_URL", url)
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    monkeypatch.setattr(isolated_llm, "requests", None)

    provider = isolated_llm.get_provider()
    assert provider.name == "openai-compat"
    rule, meta = generate_rules(
        "均值填充缺失", {"path": str(ROOT / "lots.csv"), "format": "csv"}, [], provider
    )
    assert meta["mode"] == "online"
    assert meta["degraded"] is False
    assert rule["options"]["fill_missing"] == "mean"
    assert calls and calls[0]["model"] == "fake-model"


def test_endpoint_already_containing_chat_completions(fake_endpoint, isolated_llm, monkeypatch):
    url, calls = fake_endpoint
    monkeypatch.setattr(isolated_llm, "requests", None)
    provider = isolated_llm.OpenAICompatProvider(
        url + "/chat/completions", FAKE_KEY, "m", timeout=5, max_retries=0
    )
    provider.complete("s", "u")
    assert calls[0]["path"] == "/v1/chat/completions"


def test_diagnose_config_never_exposes_api_key(isolated_llm, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    diag = isolated_llm.diagnose_config()
    assert diag["mode"] == "online"
    assert FAKE_KEY not in json.dumps(diag, ensure_ascii=False)
    assert "值不输出" in diag["api_key"]
    assert diag["endpoint_host"] == "example.invalid"


def test_llm_check_cli_masks_credentials():
    env = {
        **os.environ, "PYTHONIOENCODING": "utf-8",
        "LLM_API_KEY": FAKE_KEY,
    }
    env.pop("LLM_PROVIDER", None)
    p = subprocess.run(
        [PY, "etl_pipeline.py", "llm-check"], cwd=str(ROOT), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert p.returncode == 0, p.stdout + p.stderr
    assert '"mode"' in p.stdout
    assert FAKE_KEY not in p.stdout
