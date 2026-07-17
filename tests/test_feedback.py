from localhost import feedback


class Console:
    def __init__(self):
        self.items = []

    def print(self, item):
        self.items.append(item)


def test_plain_feedback_is_stable(monkeypatch):
    standard = Console()
    errors = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: False)
    monkeypatch.setattr(feedback, "_console", lambda err: errors if err else standard)

    feedback.info("Starting")
    feedback.success("Done")
    feedback.warning("Settings", ["First", "Second"])
    feedback.run_plan(
        framework="vite",
        command=("npm", "run", "dev"),
        port=5173,
        url="http://demo.localhost",
        dry_run=True,
    )

    assert standard.items == ["Starting", "Done"]
    assert errors.items[0:2] == ["Warning: First", "Warning: Second"]
    assert "Dry run:" in errors.items[2]
    assert "Command: npm run dev" in errors.items[2]


def test_rich_feedback_uses_compact_components(monkeypatch):
    console = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: True)
    monkeypatch.setattr(feedback, "_console", lambda err: console)

    feedback.info("Starting")
    feedback.warning("Settings", ["Missing host"])
    feedback.run_plan(
        framework="django",
        command=("python", "manage.py", "runserver"),
        port=8000,
        url="http://demo.localhost",
        dry_run=False,
    )

    assert len(console.items) == 3


def test_routes_are_plain_or_a_table(monkeypatch):
    standard = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: False)
    monkeypatch.setattr(feedback, "_console", lambda err: standard)
    feedback.routes([])
    feedback.routes([("demo.localhost", "/work/demo")])
    assert standard.items == [
        "No application routes are active yet.",
        "Active routes:\n  demo.localhost: /work/demo",
    ]

    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: True)
    feedback.routes([("demo.localhost", "/work/demo")])
    assert len(standard.items) == 3
