from localghost import feedback


class Console:
    def __init__(self):
        self.items = []

    def print(self, item="", **kwargs):
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


def test_title_uses_the_localghost_wordmark_in_interactive_terminals(monkeypatch):
    console = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: True)
    monkeypatch.setattr(feedback, "_console", lambda err: console)

    feedback.title(welcome=True)

    assert len(console.items) == 3
    assert console.items[1] == ""
    assert console.items[2] == "Easy .localhost URLs for your local apps."


def test_title_keeps_the_gap_without_a_welcome_message(monkeypatch):
    console = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: True)
    monkeypatch.setattr(feedback, "_console", lambda err: console)

    feedback.title()

    assert len(console.items) == 2
    assert console.items[1] == ""


def test_next_actions_use_plain_text_outside_interactive_terminals(monkeypatch):
    standard = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: False)
    monkeypatch.setattr(feedback, "_console", lambda err: standard)

    feedback.next_actions(https_enabled=False)

    assert standard.items == [
        "Stop the proxy: uvx localghost down",
        "Add a route: uvx localghost generate for Docker Compose, or "
        "uvx localghost run for a local app.",
        "Enable HTTPS: uvx localghost trust after installing mkcert.",
    ]


def test_choices_use_plain_text_outside_interactive_terminals(monkeypatch):
    standard = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: False)
    monkeypatch.setattr(feedback, "_console", lambda err: standard)

    feedback.choices("Services", [("web", "ports 8000", True)])

    assert standard.items == ["Services:\n  web: ports 8000 (likely)"]


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
    assert len(standard.items) == 5
