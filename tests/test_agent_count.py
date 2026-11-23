"""Unit tests for agent-count helpers in s1am.cli — no network calls."""

# ── _agent_counts_by_platform ────────────────────────────────────────────────

class _FakeCountClient:
    """Answers GET agents/count from a {osType or None: total} map."""

    def __init__(self, totals, machine=None):
        self.totals = totals
        self.machine = machine or {}
        self.calls = []

    def request(self, method, path, params=None, payload=None):
        params = dict(params or {})
        self.calls.append((method, path, params))
        if "machineTypes" in params:
            return {"data": {"total": self.machine.get(params["machineTypes"], 0)}}
        return {"data": {"total": self.totals.get(params.get("osTypes"), 0)}}


def test_agent_counts_by_platform_breakdown():
    from s1am.cli import _agent_counts_by_platform
    client = _FakeCountClient(
        {None: 120, "windows": 80, "windows_legacy": 5, "macos": 20, "linux": 10}
    )
    assert _agent_counts_by_platform(client) == {
        "Total": 120, "Windows": 80, "Windows Legacy": 5,
        "macOS": 20, "Linux": 10, "Other OS": 5,
        "Desktop": 0, "Laptop": 0, "Server": 0, "Kubernetes Node": 0,
        "Kubernetes Pod": 0, "ECS Task": 0, "Storage": 0, "Other Type": 120,
    }
    assert all(path == "agents/count" for _, path, _ in client.calls)


def test_agent_counts_by_machine_type():
    from s1am.cli import _agent_counts_by_platform
    client = _FakeCountClient({None: 100}, machine={"desktop": 60, "laptop": 30, "server": 7})
    counts = _agent_counts_by_platform(client, workers=1)
    assert (counts["Desktop"], counts["Laptop"], counts["Server"]) == (60, 30, 7)
    assert counts["Other Type"] == 3          # unknown / Kubernetes helper remainder
    assert counts["Other OS"] == 100          # no OS counts in this fake
    assert {p.get("machineTypes") for _, _, p in client.calls if "machineTypes" in p} == {
        "desktop", "laptop", "server", "kubernetes node", "kubernetes pod", "ecs task", "storage"}


def test_agent_counts_by_platform_other_never_negative():
    from s1am.cli import _agent_counts_by_platform
    client = _FakeCountClient({None: 10, "windows": 11})
    assert _agent_counts_by_platform(client)["Other OS"] == 0


def test_agent_counts_by_platform_passes_account_filter():
    from s1am.cli import _agent_counts_by_platform
    client = _FakeCountClient({})
    _agent_counts_by_platform(client, account_id="42")
    assert all(params.get("accountIds") == "42" for _, _, params in client.calls)


def test_agent_counts_by_platform_handles_empty_response():
    from s1am.cli import _count_from_response
    assert _count_from_response(None) == 0
    assert _count_from_response({"data": None}) == 0


# ── _agent_version_counts ────────────────────────────────────────────────────

class _FakeVersionClient:
    """A console whose agents are {(version, osType): n}. Answers GET agents (limit 1,
    agentVersionsNin, osTypes) and GET agents/count (agentVersions, osTypes)."""

    def __init__(self, agents):
        self.agents = agents
        self.calls = []

    def request(self, method, path, params=None, payload=None):
        params = dict(params or {})
        self.calls.append((path, params))
        os_type = params.get("osTypes")
        if path == "agents":
            excluded = set((params.get("agentVersionsNin") or "").split(",")) - {""}
            for (v, o), n in sorted(self.agents.items()):
                if o == os_type and v not in excluded and n:
                    return {"data": [{"agentVersion": v, "osType": o}]}
            return {"data": []}
        if path == "agents/count":
            v = params.get("agentVersions")
            total = sum(n for (vv, o), n in self.agents.items()
                        if (v is None or vv == v) and (os_type is None or o == os_type))
            return {"data": {"total": total}}
        raise AssertionError(path)


def test_agent_version_counts_discovers_every_version_per_os():
    from s1am.cli import _agent_version_counts
    client = _FakeVersionClient({
        ("26.1.2.177", "windows"): 50, ("25.2.6.442", "windows"): 20,
        ("26.1.2.8763", "macos"): 5, ("25.1.3.6", "linux"): 3,
    })
    out = _agent_version_counts(client, workers=1)
    assert out["Total"] == 78 and out["Other"] == 0
    assert out["versions"] == [
        {"version": "26.1.2.8763", "os": "macOS", "count": 5},
        {"version": "26.1.2.177", "os": "Windows", "count": 50},
        {"version": "25.2.6.442", "os": "Windows", "count": 20},
        {"version": "25.1.3.6", "os": "Linux", "count": 3},
    ]
    # discovery: one list call per version found plus one empty call per OS (4 OS types)
    list_calls = [p for path, p in client.calls if path == "agents"]
    assert len(list_calls) == 4 + 4
    assert all(p["limit"] == 1 and p["skipCount"] == "true" for p in list_calls)
    # the walk excludes what it has seen so far
    win = [p for p in list_calls if p["osTypes"] == "windows"]
    assert win[2]["agentVersionsNin"].split(",") == ["25.2.6.442", "26.1.2.177"]


def test_agent_version_counts_other_is_unaccounted_remainder():
    from s1am.cli import _agent_version_counts

    class _Client(_FakeVersionClient):
        def request(self, method, path, params=None, payload=None):
            if path == "agents/count" and not params:
                return {"data": {"total": 10}}      # 10 agents, only 7 on a known OS
            return super().request(method, path, params, payload)

    out = _agent_version_counts(_Client({("1.0.0.1", "linux"): 7}), workers=1)
    assert out["Other"] == 3


def test_agent_version_counts_passes_account_filter():
    from s1am.cli import _agent_version_counts
    client = _FakeVersionClient({("1.0.0.1", "windows"): 1})
    _agent_version_counts(client, account_id="42", workers=1)
    assert all(p.get("accountIds") == "42" for _, p in client.calls)


def test_version_key_sorts_numerically():
    from s1am.cli import _version_key
    versions = ["26.1.2.8763", "26.1.2.177", "25.2.6.442", "9.9.9.9"]
    assert sorted(versions, key=_version_key) == ["9.9.9.9", "25.2.6.442", "26.1.2.177", "26.1.2.8763"]
