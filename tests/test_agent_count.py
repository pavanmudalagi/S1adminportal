"""Unit tests for agent-count helpers in s1am.cli — no network calls."""

# ── _agent_counts_by_platform ────────────────────────────────────────────────

class _FakeCountClient:
    """Answers GET agents/count from a {osType or None: total} map."""

    def __init__(self, totals):
        self.totals = totals
        self.calls = []

    def request(self, method, path, params=None, payload=None):
        self.calls.append((method, path, dict(params or {})))
        return {"data": {"total": self.totals.get((params or {}).get("osTypes"), 0)}}


def test_agent_counts_by_platform_breakdown():
    from s1am.cli import _agent_counts_by_platform
    client = _FakeCountClient(
        {None: 120, "windows": 80, "windows_legacy": 5, "macos": 20, "linux": 10}
    )
    assert _agent_counts_by_platform(client) == {
        "Total": 120, "Windows": 80, "Windows Legacy": 5,
        "macOS": 20, "Linux": 10, "Other": 5,
    }
    assert all(path == "agents/count" for _, path, _ in client.calls)


def test_agent_counts_by_platform_other_never_negative():
    from s1am.cli import _agent_counts_by_platform
    client = _FakeCountClient({None: 10, "windows": 11})
    assert _agent_counts_by_platform(client)["Other"] == 0


def test_agent_counts_by_platform_passes_account_filter():
    from s1am.cli import _agent_counts_by_platform
    client = _FakeCountClient({})
    _agent_counts_by_platform(client, account_id="42")
    assert all(params.get("accountIds") == "42" for _, _, params in client.calls)


def test_agent_counts_by_platform_handles_empty_response():
    from s1am.cli import _count_from_response
    assert _count_from_response(None) == 0
    assert _count_from_response({"data": None}) == 0
