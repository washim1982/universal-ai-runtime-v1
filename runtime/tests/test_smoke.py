import httpx

from conftest import headers


async def test_startup_and_health(env):
    st = env.svc.startup_status
    assert st["migrations"] == ["0001_initial"]
    assert st["mcp"] == {"fs": "3 tools", "db": "2 tools"}, st
    assert sorted(st["agents"]) == ["in_app_assistant", "report_generator"]
    async with httpx.AsyncClient(base_url=env.http) as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}
        r = await c.get("/readyz")
        assert r.status_code == 200 and r.json()["ready"] is True
        r = await c.post("/api/v1/inference", json={"model": "local:default", "input": "hi"},
                         headers=headers(env.keys.dev))
        assert r.status_code == 200, r.text
        assert r.json()["content"] == "echo: hi"
