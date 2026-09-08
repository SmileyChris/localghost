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
        type="vite",
        command=("npm", "run", "dev"),
        port=5173,
        url="http://demo.localhost",
        dry_run=True,
    )

    assert standard.items == ["Starting", "Done"]
    assert errors.items[0:2] == [
        "Warning: Settings: First",
        "Warning: Settings: Second",
    ]
    assert "Dry run:" in errors.items[2]
    assert "Command: npm run dev" in errors.items[2]


def test_rich_feedback_uses_compact_components(monkeypatch):
    console = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: True)
    monkeypatch.setattr(feedback, "_console", lambda err: console)

    feedback.info("Starting")
    feedback.warning("Settings", ["Missing host"])
    feedback.run_plan(
        type="django",
        command=("python", "manage.py", "runserver"),
        port=8000,
        url="http://demo.localhost",
        dry_run=False,
    )
    feedback.action("Try", "localghost run", " in a project")
    feedback.details([("Key", "Value")], title="Status")
    feedback.choices("Pick", [("web", "port 8000", True)])

    assert len(console.items) == 8


def test_next_actions_highlight_each_runnable_command(monkeypatch):
    console = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: True)
    monkeypatch.setattr(feedback, "_console", lambda err: console)

    feedback.next_actions(https_enabled=True)

    route_hint = console.items[1]
    assert route_hint.plain == (
        "Save a setup: uvx localghost save, or "
        "uvx localghost run to run a local app."
    )
    assert [
        (route_hint.plain[span.start : span.end], span.style)
        for span in route_hint.spans
    ] == [
        ("Save a setup: ", "bold"),
        ("uvx localghost save", feedback.LIME),
        (", or ", "default"),
        ("uvx localghost run", feedback.LIME),
        (" to run a local app.", "default"),
    ]


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


def test_title_shows_the_wordmark_only_once_per_process(monkeypatch):
    """Commands compose -- `save --run` re-enters `run`, which prints
    a title of its own -- so the wordmark has to be a per-invocation mark
    rather than a per-command one, or a single command line prints it
    twice."""
    console = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: True)
    monkeypatch.setattr(feedback, "_console", lambda err: console)

    feedback.title()
    feedback.title()

    assert len(console.items) == 2


def test_title_does_not_burn_the_wordmark_on_a_plain_stream(monkeypatch):
    """The guard is set only when the mark is actually drawn: a piped
    command must not consume the one title a later interactive command in
    the same process would have shown."""
    plain = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: False)
    monkeypatch.setattr(feedback, "_console", lambda err: plain)
    feedback.title()

    rich = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: True)
    monkeypatch.setattr(feedback, "_console", lambda err: rich)
    feedback.title()

    assert plain.items == []
    assert len(rich.items) == 2


def test_next_actions_use_plain_text_outside_interactive_terminals(monkeypatch):
    standard = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: False)
    monkeypatch.setattr(feedback, "_console", lambda err: standard)

    feedback.next_actions(https_enabled=False)

    assert standard.items == [
        "Stop the hub: uvx localghost hub down",
        "Save a setup: uvx localghost save, or "
        "uvx localghost run to run a local app.",
        "Enable HTTPS: uvx localghost trust install after installing mkcert.",
    ]


def test_choices_use_plain_text_outside_interactive_terminals(monkeypatch):
    standard = Console()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: False)
    monkeypatch.setattr(feedback, "_console", lambda err: standard)

    feedback.choices("Services", [("web", "ports 8000", True)])
    feedback.details([("Key", "Value")], title="Status")
    feedback.action("Try", "localghost run")

    assert standard.items == [
        "Services:\n  web: ports 8000 (likely)",
        "Status\nKey: Value",
        "Try: localghost run",
    ]


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


class KwargsConsole:
    def __init__(self):
        self.calls = []

    def print(self, item="", **kwargs):
        self.calls.append((item, kwargs))


def test_plain_blocks_never_hard_wrap(monkeypatch):
    """Piped output must keep long paths on one line."""
    console = KwargsConsole()
    monkeypatch.setattr(feedback, "_rich_terminal", lambda err: False)
    monkeypatch.setattr(feedback, "_console", lambda err: console)

    feedback.run_plan(
        type="custom",
        command=("./server",),
        port=8080,
        url="http://demo.localhost",
        dry_run=True,
        project_root=__import__("pathlib").Path("/" + "x" * 120),
    )
    feedback.compose_dry_run(project="demo", url="http://demo.localhost")
    feedback.choices("Services", [("web", "ports 8000", True)])
    feedback.routes([("demo.localhost", "/" + "y" * 120)])

    assert console.calls, "nothing printed"
    for item, kwargs in console.calls:
        assert kwargs.get("soft_wrap") is True, item
