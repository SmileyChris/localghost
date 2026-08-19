import json
import socket
from subprocess import CompletedProcess

import click
import pytest

from localghost import runner


def executable(monkeypatch, *names):
    monkeypatch.setattr(
        runner.shutil, "which", lambda name: "/bin/x" if name in names else None
    )


def test_name_precedence_and_validation(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME=from-env\n")
    assert runner.resolve_name(tmp_path) == "from-env"
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "from-shell")
    assert runner.resolve_name(tmp_path) == "from-shell"
    with pytest.raises(click.ClickException):
        runner.validate_name("Not valid")


def test_django_detection_and_runner_precedence(monkeypatch, tmp_path):
    (tmp_path / "manage.py").touch()
    (tmp_path / "uv.lock").touch()
    executable(monkeypatch, "uv")
    assert runner.detect_framework(tmp_path) == "django"
    assert runner.django_command(tmp_path, None)[1][:3] == ("uv", "run", "python")


def test_django_settings_preflight_warns_only_for_missing_values(monkeypatch, tmp_path):
    plan = runner.RunPlan(
        "demo",
        "django",
        ("uv", "run", "python", "manage.py", "runserver", "0"),
        8000,
        "p",
        "",
    )

    def run(command, **kwargs):
        payload = {"allowed_hosts": [], "csrf_trusted_origins": []}
        return CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(runner.subprocess, "run", run)
    warnings = runner.django_settings_warnings(plan, tmp_path)
    assert len(warnings) == 2
    assert "ALLOWED_HOSTS" in warnings[0]
    assert "CSRF_TRUSTED_ORIGINS" in warnings[1]

    def allowed(command, **kwargs):
        payload = {
            "allowed_hosts": [".localhost"],
            "csrf_trusted_origins": ["http://*.localhost"],
        }
        return CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(runner.subprocess, "run", allowed)
    assert runner.django_settings_warnings(plan, tmp_path) == []


def test_django_settings_preflight_uses_effective_public_origin(
    monkeypatch, tmp_path
):
    plan = runner.RunPlan(
        "demo",
        "django",
        ("uv", "run", "python", "manage.py", "runserver", "0"),
        8000,
        "p",
        "",
    )
    payload = {
        "allowed_hosts": ["demo.localhost"],
        "csrf_trusted_origins": ["http://demo.localhost"],
    }
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(
            command, 0, json.dumps(payload), ""
        ),
    )

    warnings = runner.django_settings_warnings(
        plan, tmp_path, public_origin="https://demo.localhost:8443"
    )

    assert warnings == [
        "Django CSRF_TRUSTED_ORIGINS does not include "
        "'https://demo.localhost:8443'; add it if CSRF-protected requests use "
        "this origin."
    ]


def test_django_settings_preflight_ignores_unavailable_settings(monkeypatch, tmp_path):
    plan = runner.RunPlan(
        "demo", "django", ("python", "manage.py", "runserver", "0"), 8000, "p", ""
    )
    assert (
        runner.django_settings_warnings(
            runner.RunPlan("demo", "vite", (), 1, "p", ""), tmp_path
        )
        == []
    )
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(command, 1, "", "settings failed"),
    )
    assert runner.django_settings_warnings(plan, tmp_path) == []
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError()),
    )
    assert runner.django_settings_warnings(plan, tmp_path) == []
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(command, 0, "not json", ""),
    )
    assert runner.django_settings_warnings(plan, tmp_path) == []


def test_django_setting_match_helpers():
    assert runner._host_is_allowed("demo.localhost", ["*"])
    assert runner._host_is_allowed("demo.localhost", [".localhost"])
    assert not runner._host_is_allowed("demo.localhost", ["other.localhost"])
    assert runner._origin_is_trusted("http://demo.localhost", ["http://*.localhost"])
    assert not runner._origin_is_trusted("http://demo.localhost", [])
    assert runner._origin_is_trusted("http://demo.localhost", ["http://demo.localhost"])


def test_django_runner_errors(monkeypatch, tmp_path):
    (tmp_path / "manage.py").touch()
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    with pytest.raises(click.ClickException, match="no supported"):
        runner.django_command(tmp_path, None)
    (tmp_path / "uv.lock").touch()
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    with pytest.raises(click.ClickException, match="was not found"):
        runner.django_command(tmp_path, None)


@pytest.mark.parametrize(
    ("marker", "tool", "prefix"),
    [
        ("poetry.lock", "poetry", ("poetry", "run", "python")),
        ("Pipfile", "pipenv", ("pipenv", "run", "python")),
    ],
)
def test_django_project_runner_variants(monkeypatch, tmp_path, marker, tool, prefix):
    (tmp_path / "manage.py").touch()
    (tmp_path / marker).touch()
    executable(monkeypatch, tool)
    assert runner.django_command(tmp_path, 8123) == (
        8123,
        (*prefix, "manage.py", "runserver", "0.0.0.0:{port}"),
    )


def test_django_virtualenv_variants(monkeypatch, tmp_path):
    (tmp_path / "manage.py").touch()
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch(mode=0o755)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert runner.django_command(tmp_path, None)[1][0] == str(python)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "missing"))
    with pytest.raises(click.ClickException, match="active virtualenv"):
        runner.django_command(tmp_path, None)


def test_vite_detection_manager_and_priority(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "x"}})
    )
    (tmp_path / "pnpm-lock.yaml").touch()
    executable(monkeypatch, "pnpm")
    assert runner.detect_framework(tmp_path) == "vite"
    assert runner.vite_command(tmp_path, None)[1] == (
        "pnpm", "run", "dev", "--host", "0.0.0.0", "--port", "{port}",
        "--strictPort",
    )
    (tmp_path / "yarn.lock").touch()
    assert runner.package_manager(tmp_path, runner._vite_manifest(tmp_path)) == "pnpm"
    (tmp_path / "package-lock.json").touch()
    assert runner.package_manager(tmp_path, runner._vite_manifest(tmp_path)) == "pnpm"
    executable(monkeypatch, "npm")
    assert runner.package_manager(tmp_path, runner._vite_manifest(tmp_path)) == "npm"
    (tmp_path / "bun.lock").touch()
    executable(monkeypatch, "bun", "pnpm", "npm")
    assert runner.package_manager(tmp_path, runner._vite_manifest(tmp_path)) == "bun"


@pytest.mark.parametrize("manifest", ["[]", '{"scripts": {}}'])
def test_ineligible_vite_manifest(tmp_path, manifest):
    (tmp_path / "package.json").write_text(manifest)
    if manifest == "[]":
        with pytest.raises(click.ClickException, match="object"):
            runner._vite_manifest(tmp_path)
    else:
        assert runner._vite_manifest(tmp_path) is None


def test_invalid_vite_json(tmp_path):
    (tmp_path / "package.json").write_text("{")
    with pytest.raises(click.ClickException, match="valid package"):
        runner._vite_manifest(tmp_path)


def test_framework_ambiguity_and_custom_plan(monkeypatch, tmp_path):
    (tmp_path / "manage.py").touch()
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"vite": "x"}})
    )
    with pytest.raises(click.ClickException, match="both django and vite"):
        runner.detect_framework(tmp_path)
    monkeypatch.setattr(runner, "_port_available", lambda _: True)
    plan = runner.build_plan(tmp_path, "hello", None, 3000, ("echo", "ok"))
    assert plan.framework == "custom" and plan.command == ("echo", "ok")
    with pytest.raises(click.ClickException, match="requires --port"):
        runner.build_plan(tmp_path, "hello", None, None, ("echo",))


def test_port_selection(monkeypatch):
    monkeypatch.setattr(runner, "_port_available", lambda port: port == 8002)
    assert runner.select_port(8000, False) == 8002
    with pytest.raises(click.ClickException, match="already"):
        runner.select_port(8000, True)
    monkeypatch.setattr(runner, "_port_available", lambda _: False)
    with pytest.raises(click.ClickException, match="no free"):
        runner.select_port(65535, False)


def test_bridge_model_is_ephemeral():
    text = runner.render_override(runner.create_run_bridge_compose("demo", 8123))
    assert "caddy:2.11.4-alpine" in text
    assert "restart: no" in text and "external: true" in text
    assert "localghost:" in text
    assert "traefik.docker.network=localghost" in text
    assert "Host(`demo.localhost`)" in text
    assert "io.localghost.managed=true" in text
    assert "io.localghost.tls-domains=demo.localhost" in text
    assert "ports:" not in text


def test_collision_inspects_any_router(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "ps":
            return CompletedProcess(command, 0, "abc\n", "")
        container = {
            "Id": "abc",
            "Config": {
                "Labels": {"traefik.http.routers.other.rule": "Host(`demo.localhost`)"}
            },
        }
        return CompletedProcess(command, 0, json.dumps([container]), "")

    monkeypatch.setattr(runner.subprocess, "run", run)
    assert runner.find_route_collision("demo") == "abc"
    assert len(calls) == 2


def test_collision_detects_compound_host_rule(monkeypatch):
    def run(command, **kwargs):
        if command[1] == "ps":
            return CompletedProcess(command, 0, "abc\n", "")
        container = {
            "Id": "abc",
            "Config": {
                "Labels": {
                    "traefik.http.routers.other.rule": (
                        "Host(`demo.localhost`) && PathPrefix(`/api`)"
                    )
                }
            },
        }
        return CompletedProcess(command, 0, json.dumps([container]), "")

    monkeypatch.setattr(runner.subprocess, "run", run)
    assert runner.find_route_collision("demo") == "abc"


def test_collision_none_and_docker_errors(monkeypatch):
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(command, 0, "", ""),
    )
    assert runner.find_route_collision("demo") is None


def test_vite_declared_manager_and_missing_executable(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "packageManager": "bun@1.0.0",
                "scripts": {"dev": "vite"},
                "dependencies": {"vite": "x"},
            }
        )
    )
    manifest = runner._vite_manifest(tmp_path)
    assert runner.package_manager(tmp_path, manifest) == "bun"
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    with pytest.raises(click.ClickException, match="was not found"):
        runner.vite_command(tmp_path, None)


def test_astro_detection_and_command(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "scripts": {"dev": "astro dev"},
            "devDependencies": {"astro": "x"},
        })
    )
    executable(monkeypatch, "npm")
    assert runner.detect_framework(tmp_path) == "astro"
    assert runner._astro_manifest(tmp_path) is not None
    assert runner.astro_command(tmp_path, None) == (4321, (
        "npm", "run", "dev", "--",
        "--port", "{port}", "--host", "0.0.0.0",
    ))


def test_astro_without_dev_script(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"astro": "x"}})
    )
    assert runner._astro_manifest(tmp_path) is None


def test_astro_without_dependency(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}})
    )
    assert runner._astro_manifest(tmp_path) is None


def test_astro_vite_ambiguity(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "scripts": {"dev": "astro dev"},
            "dependencies": {"astro": "x", "vite": "x"},
        })
    )
    with pytest.raises(click.ClickException, match="both vite and astro"):
        runner.detect_framework(tmp_path)


def test_astro_django_ambiguity(monkeypatch, tmp_path):
    (tmp_path / "manage.py").touch()
    (tmp_path / "package.json").write_text(
        json.dumps({
            "scripts": {"dev": "astro dev"},
            "devDependencies": {"astro": "x"},
        })
    )
    with pytest.raises(click.ClickException, match="both django and astro"):
        runner.detect_framework(tmp_path)


def test_astro_command_missing_manifest(monkeypatch, tmp_path):
    with pytest.raises(click.ClickException, match="Astro requires"):
        runner.astro_command(tmp_path, None)


def test_astro_plan_with_detection(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "scripts": {"dev": "astro dev"},
            "devDependencies": {"astro": "x"},
        })
    )
    executable(monkeypatch, "npm")
    monkeypatch.setattr(runner, "_port_available", lambda _: True)
    plan = runner.build_plan(tmp_path, "astro-site", None, None, ())
    assert plan.framework == "astro"
    assert plan.port == 4321
    assert plan.command == (
        "npm", "run", "dev", "--",
        "--port", "4321", "--host", "0.0.0.0",
    )


def test_astro_plan_explicit_framework(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "scripts": {"dev": "astro dev"},
            "devDependencies": {"astro": "x"},
        })
    )
    executable(monkeypatch, "npm")
    monkeypatch.setattr(runner, "_port_available", lambda _: True)
    plan = runner.build_plan(tmp_path, "site", "astro", None, ())
    assert plan.framework == "astro"


def test_nested_framework_root_drives_name_and_working_directory(
    monkeypatch, tmp_path
):
    root = tmp_path / "customer-portal"
    nested = root / "src" / "accounts"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "manage.py").touch()
    (root / "uv.lock").touch()
    executable(monkeypatch, "uv")
    monkeypatch.setattr(runner, "_port_available", lambda _: True)

    plan = runner.build_plan(nested, None, None, None, ())

    assert plan.framework == "django"
    assert plan.name == "customer-portal"
    assert plan.project_root == root
    assert plan.working_directory == root


def test_framework_search_stops_at_worktree_boundary(tmp_path):
    outside = tmp_path / "outside"
    checkout = outside / "checkout"
    nested = checkout / "src"
    nested.mkdir(parents=True)
    (checkout / ".git").touch()
    (outside / "manage.py").touch()

    with pytest.raises(click.ClickException, match="could not detect"):
        runner.detect_framework(nested)


def test_modern_cakephp_detection_and_plan(monkeypatch, tmp_path):
    root = tmp_path / "bakery"
    webroot = root / "webroot"
    cake = root / "bin" / "cake"
    webroot.mkdir(parents=True)
    cake.parent.mkdir()
    cake.touch()
    cake.chmod(0o755)
    (root / "composer.json").write_text(
        json.dumps({"require": {"cakephp/cakephp": "^5.0"}})
    )
    monkeypatch.setattr(runner, "_port_available", lambda _: True)

    plan = runner.build_plan(webroot, None, None, None, ())

    assert plan.framework == "cakephp"
    assert plan.name == "bakery"
    assert plan.port == 8765
    assert plan.project_root == root
    assert plan.working_directory == root
    assert plan.command == (
        "bin/cake", "server", "-H", "0.0.0.0", "-p", "8765"
    )


def test_modern_cakephp_non_executable_wrapper_uses_php(monkeypatch, tmp_path):
    cake = tmp_path / "bin" / "cake"
    cake.parent.mkdir()
    cake.touch()
    (cake.parent / "cake.php").touch()
    (tmp_path / "composer.json").write_text(
        json.dumps({"require": {"cakephp/cakephp": "^4.0"}})
    )
    executable(monkeypatch, "php")

    port, command, working_directory = runner.cakephp_command(tmp_path, 9000)

    assert port == 9000
    assert command[:2] == ("php", "bin/cake.php")
    assert working_directory == tmp_path


def test_legacy_cakephp_uses_webroot_without_naming_it_webroot(
    monkeypatch, tmp_path
):
    root = tmp_path / "legacy-shop"
    webroot = root / "app" / "webroot"
    config = root / "app" / "Config"
    webroot.mkdir(parents=True)
    config.mkdir()
    (webroot / "index.php").touch()
    (config / "core.php").touch()
    executable(monkeypatch, "php")
    monkeypatch.setattr(runner, "_port_available", lambda _: True)

    plan = runner.build_plan(webroot, None, None, None, ())

    assert plan.framework == "cakephp"
    assert plan.name == "legacy-shop"
    assert plan.project_root == root
    assert plan.working_directory == webroot
    assert plan.command == ("php", "-S", "0.0.0.0:8765")


def test_laravel_detection_from_public_directory(monkeypatch, tmp_path):
    root = tmp_path / "orders"
    public = root / "public"
    public.mkdir(parents=True)
    (root / "artisan").touch()
    (root / "composer.json").write_text(
        json.dumps({"require": {"laravel/framework": "^12.0"}})
    )
    executable(monkeypatch, "php")
    monkeypatch.setattr(runner, "_port_available", lambda _: True)

    plan = runner.build_plan(public, None, None, None, ())

    assert plan.framework == "laravel"
    assert plan.name == "orders"
    assert plan.working_directory == root
    assert plan.command == (
        "php", "artisan", "serve", "--host=0.0.0.0", "--port=8000"
    )


def test_php_and_javascript_markers_at_same_root_are_ambiguous(tmp_path):
    (tmp_path / "artisan").touch()
    (tmp_path / "composer.json").write_text(
        json.dumps({"require": {"laravel/framework": "^12.0"}})
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}, "dependencies": {"vite": "x"}})
    )

    with pytest.raises(click.ClickException, match="both vite and laravel"):
        runner.detect_framework(tmp_path)


def test_vite_plan_through_build_plan(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({
            "scripts": {"dev": "vite"},
            "devDependencies": {"vite": "x"},
        })
    )
    executable(monkeypatch, "npm")
    monkeypatch.setattr(runner, "_port_available", lambda _: True)
    plan = runner.build_plan(tmp_path, "vite-site", None, None, ())
    assert plan.framework == "vite"
    assert plan.port == 5173
    assert "--host" in plan.command and "--strictPort" in plan.command


def test_django_virtualenv_active(monkeypatch, tmp_path):
    (tmp_path / "manage.py").touch()
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch(mode=0o755)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "venv"))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    assert runner.django_command(tmp_path, None)[1][0] == str(python)


def test_detected_plan_and_small_helpers(monkeypatch, tmp_path):
    (tmp_path / "manage.py").touch()
    (tmp_path / "uv.lock").touch()
    executable(monkeypatch, "uv")
    monkeypatch.setattr(runner, "_port_available", lambda _: True)
    plan = runner.build_plan(tmp_path, "demo", None, None, ())
    assert plan.port == 8000 and "0.0.0.0:8000" in plan.command[-1]
    (tmp_path / ".env").write_text("# comment\nCOMPOSE_PROJECT_NAME='fine'\n")
    assert runner._dotenv_name(tmp_path / ".env") == "fine"
    assert runner._dotenv_name(tmp_path / "none") is None
    assert runner._port_available(0)


def test_package_manager_and_compose_failures(monkeypatch, tmp_path):
    with pytest.raises(click.ClickException, match="unsupported"):
        runner.package_manager(tmp_path, {"packageManager": "madeup@1"})
    plan = runner.RunPlan("x", "custom", ("x",), 1, "p", "services: {}\n")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: CompletedProcess(command, 4),
    )
    with pytest.raises(click.exceptions.Exit):
        runner.stop_bridge(plan)


def test_runner_remaining_failure_helpers(monkeypatch, tmp_path):
    with pytest.raises(click.ClickException, match="Django requires"):
        runner.django_command(tmp_path, None)
    with pytest.raises(click.ClickException, match="Vite requires"):
        runner.vite_command(tmp_path, None)
    with pytest.raises(click.ClickException, match="could not detect"):
        runner.detect_framework(tmp_path)
    with pytest.raises(click.ClickException, match="framework"):
        runner.build_plan(tmp_path, "demo", "wrong", 1, ())
    (tmp_path / ".env").write_text("OTHER=x\n")
    assert runner._dotenv_name(tmp_path / ".env") is None
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    monkeypatch.setattr(runner, "_dotenv_name", lambda _: None)
    safe = tmp_path / "safe-name"
    safe.mkdir()
    assert runner.resolve_name(safe) == "safe-name"


def test_collision_and_compose_missing_docker(monkeypatch):
    def no_docker(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(runner.subprocess, "run", no_docker)
    with pytest.raises(click.ClickException, match="docker is required"):
        runner.find_route_collision("demo")
    with pytest.raises(click.ClickException, match="docker is required"):
        runner.start_bridge(runner.RunPlan("x", "c", (), 1, "p", ""))


def test_execute_cleanup_failure_returns_failure(monkeypatch):
    monkeypatch.setattr(runner, "start_bridge", lambda plan: None)

    def fail_stop(plan):
        raise RuntimeError("leak")

    monkeypatch.setattr(runner, "stop_bridge", fail_stop)

    class Child:
        def wait(self):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: Child())
    plan = runner.RunPlan("x", "custom", ("x",), 1, "p", "")
    assert runner.execute(plan, lambda: None) == 1


def test_execute_prefers_planned_working_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "start_bridge", lambda plan: None)
    monkeypatch.setattr(runner, "stop_bridge", lambda plan: None)
    recorded = {}

    class Child:
        def wait(self):
            return 0

    def popen(*args, **kwargs):
        recorded["cwd"] = kwargs["cwd"]
        return Child()

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    planned = tmp_path / "app" / "webroot"
    plan = runner.RunPlan(
        "x", "cakephp", ("php", "-S", "0"), 1, "p", "", tmp_path, planned
    )

    assert runner.execute(plan, lambda: None, cwd=tmp_path) == 0
    assert recorded["cwd"] == planned


def test_execute_interrupt_terminates_child(monkeypatch):
    monkeypatch.setattr(runner, "start_bridge", lambda plan: None)
    monkeypatch.setattr(runner, "stop_bridge", lambda plan: None)

    class Child:
        terminated = False

        def wait(self):
            if not self.terminated:
                raise KeyboardInterrupt
            return 0

        def terminate(self):
            self.terminated = True

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: Child())
    assert runner.execute(runner.RunPlan("x", "c", (), 1, "p", ""), lambda: None) == 130


def test_termination_handler_grace_then_force_quit(monkeypatch):
    clock = iter([0.0, 0.5, 0.8, 3.0])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))
    old_handlers = runner._install_termination_handlers()
    try:
        with pytest.raises(runner._TerminationSignal):
            runner.signal.raise_signal(runner.signal.SIGINT)  # first press
        # A repeat within the grace period is ignored...
        runner.signal.raise_signal(runner.signal.SIGINT)
        runner.signal.raise_signal(runner.signal.SIGTERM)
        # ...but a repeat after it force-quits cleanly.
        with pytest.raises(runner._ForceQuit):
            runner.signal.raise_signal(runner.signal.SIGINT)
    finally:
        runner._restore_termination_handlers(old_handlers)


def test_execute_force_quits_stuck_child(monkeypatch):
    monkeypatch.setattr(runner, "start_bridge", lambda plan: None)
    monkeypatch.setattr(runner, "stop_bridge", lambda plan: None)
    received = []

    def killpg(pid, signum):
        received.append(signum)

    monkeypatch.setattr(runner.os, "killpg", killpg)
    waits = {"count": 0}

    class Child:
        pid = 123

        def wait(self):
            waits["count"] += 1
            if waits["count"] == 1:
                raise KeyboardInterrupt
            raise runner._ForceQuit(2)

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: Child())
    plan = runner.RunPlan("x", "custom", ("x",), 1, "p", "")
    assert runner.execute(plan, lambda: None) == 130
    assert received == [runner.signal.SIGINT, runner.signal.SIGKILL]


def test_execute_bridge_cleanup_interrupted_by_force_quit(monkeypatch):
    monkeypatch.setattr(runner, "start_bridge", lambda plan: None)
    attempts = []

    def stop(plan):
        attempts.append(plan)
        raise runner._ForceQuit(2)

    monkeypatch.setattr(runner, "stop_bridge", stop)

    class Child:
        def wait(self):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: Child())
    plan = runner.RunPlan("x", "custom", ("x",), 1, "p", "")
    assert runner.execute(plan, lambda: None) == 0
    assert len(attempts) == 1


def test_execute_retries_bridge_cleanup_after_termination_signal(monkeypatch):
    monkeypatch.setattr(runner, "start_bridge", lambda plan: None)
    attempts = []

    def stop(plan):
        attempts.append(plan)
        if len(attempts) == 1:
            raise runner._TerminationSignal(2)

    monkeypatch.setattr(runner, "stop_bridge", stop)

    class Child:
        def wait(self):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: Child())
    plan = runner.RunPlan("x", "custom", ("x",), 1, "p", "")
    assert runner.execute(plan, lambda: None) == 0
    assert len(attempts) == 2


def test_port_in_use_and_vite_without_dependency(tmp_path):
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        assert not runner._port_available(listener.getsockname()[1])
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "x"}}))
    assert runner._vite_manifest(tmp_path) is None


def test_compose_uses_stdin(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_port_available", lambda _: True)
    plan = runner.build_plan(tmp_path, "demo", None, 3000, ("echo",))
    recorded = {}

    def run(command, **kwargs):
        recorded.update(command=command, **kwargs)
        return CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", run)
    runner.start_bridge(plan)
    assert recorded["command"][0:6] == [
        "docker",
        "compose",
        "--project-name",
        plan.project,
        "--file",
        "-",
    ]
    assert recorded["input"] == plan.bridge_yaml


def test_execute_lifecycle_and_cleanup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "start_bridge", lambda plan: None)
    stopped = []
    monkeypatch.setattr(runner, "stop_bridge", lambda plan: stopped.append(plan))

    class Child:
        def wait(self):
            return 7

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: Child())
    plan = runner.RunPlan("x", "custom", ("x",), 1, "p", "")
    assert runner.execute(plan, lambda: None) == 7
    assert stopped


def test_execute_spawn_and_cleanup_failure(monkeypatch):
    monkeypatch.setattr(runner, "start_bridge", lambda plan: None)

    def fail_spawn(*args, **kwargs):
        raise OSError("nope")

    def fail_stop(plan):
        raise RuntimeError("leak")

    monkeypatch.setattr(runner.subprocess, "Popen", fail_spawn)
    monkeypatch.setattr(runner, "stop_bridge", fail_stop)
    with pytest.raises(click.ClickException, match="could not start"):
        runner.execute(runner.RunPlan("x", "custom", ("x",), 1, "p", ""), lambda: None)


def test_execute_cleans_up_after_partial_bridge_start(monkeypatch):
    cleaned = []

    def partial_start(plan):
        raise click.exceptions.Exit(3)

    monkeypatch.setattr(runner, "start_bridge", partial_start)
    monkeypatch.setattr(runner, "stop_bridge", lambda plan: cleaned.append(plan))

    with pytest.raises(click.exceptions.Exit):
        runner.execute(runner.RunPlan("x", "custom", ("x",), 1, "p", ""), lambda: None)

    assert cleaned


def test_termination_signals_the_child_process_group(monkeypatch):
    received = []

    class Child:
        pid = 123

    def killpg(pid, signum):
        received.append((pid, signum))

    monkeypatch.setattr(runner.os, "killpg", killpg)
    runner._terminate_process_tree(Child(), runner.signal.SIGTERM)
    assert received == [(123, runner.signal.SIGTERM)]


def test_termination_falls_back_to_send_signal(monkeypatch):
    received = []

    class Child:
        pid = 123

        def send_signal(self, signum):
            received.append(signum)

    monkeypatch.setattr(
        runner.os,
        "killpg",
        lambda pid, signum: (_ for _ in ()).throw(ProcessLookupError),
    )
    runner._terminate_process_tree(Child(), runner.signal.SIGTERM)
    assert received == [runner.signal.SIGTERM]
