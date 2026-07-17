from pathlib import Path

import click
import pytest
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from localhost.generator import (
    Candidate,
    choose_port,
    create_override,
    extend_override,
    load_override,
    rank_services,
    render_override,
    validate_project_name,
    validate_project_name_value,
    write_extended,
    write_new,
)


def candidate(
    name: str = "web", ports: tuple[int, ...] = (8000,)
) -> Candidate:
    return Candidate(
        name=name,
        service={"networks": {"default": None}},
        ports=ports,
        score=0,
    )


@pytest.mark.parametrize(
    "project_name", ["project", "project-2", "2project", "a" * 63]
)
def test_dns_safe_project_names_are_accepted(project_name: str) -> None:
    validate_project_name_value(project_name)
    assert validate_project_name({"name": project_name}) == project_name


@pytest.mark.parametrize(
    "project_name", ["", "Project", "project_name", "-project", "project-", "a" * 64]
)
def test_unsafe_project_names_are_rejected(project_name: str) -> None:
    with pytest.raises(click.ClickException, match="COMPOSE_PROJECT_NAME"):
        validate_project_name_value(project_name)


def test_ranking_prefers_project_and_web_names_over_infrastructure() -> None:
    model = {
        "services": {
            "redis": {"expose": [6379]},
            "api": {"expose": [5000]},
            "shop": {"expose": [9000]},
            "background-web": {"expose": [8080]},
        }
    }

    ranked = rank_services(model, "shop")

    assert [item.name for item in ranked] == [
        "shop",
        "api",
        "background-web",
        "redis",
    ]


def test_ranking_rejects_a_project_without_services() -> None:
    with pytest.raises(click.ClickException, match="defines no services"):
        rank_services({"services": {}}, "project")


@pytest.mark.parametrize(
    ("ports", "requested", "expected"),
    [
        ((9000,), None, 9000),
        ((9000,), 7000, 7000),
        ((3000, 8000), None, 8000),
        ((7000, 9000), None, None),
        ((), None, None),
    ],
)
def test_port_selection_only_guesses_when_the_choice_is_clear(
    ports: tuple[int, ...], requested: int | None, expected: int | None
) -> None:
    assert choose_port(candidate(ports=ports), requested) == expected


def test_new_override_preserves_service_networks_and_uses_dynamic_names() -> None:
    selected = Candidate(
        name="Web.API",
        service={"networks": {"backend": None, "localhost-proxy": None}},
        ports=(8080,),
        score=0,
    )

    rendered = render_override(create_override("shop", selected, 8080))

    assert "backend:" in rendered
    assert rendered.count("localhost-proxy:") == 2
    assert "${COMPOSE_PROJECT_NAME}-web-api.rule" in rendered
    assert "Host(`${COMPOSE_PROJECT_NAME}.localhost`)" in rendered


@pytest.mark.parametrize("collection_type", ["sequence", "mapping"])
def test_existing_override_collections_are_extended_in_place(
    collection_type: str,
) -> None:
    if collection_type == "sequence":
        networks = CommentedSeq(["default"])
        labels = CommentedSeq(["keep=this"])
    else:
        networks = CommentedMap({"default": None})
        labels = CommentedMap({"keep": "this"})
    document = CommentedMap(
        {
            "services": CommentedMap(
                {"web": CommentedMap({"networks": networks, "labels": labels})}
            )
        }
    )
    model = {
        "services": {"web": {"labels": {"keep": "this"}}},
        "networks": {},
    }

    assert extend_override(document, model, "shop", candidate(), 8000) is True
    rendered = render_override(document)
    assert "keep" in rendered
    assert "localhost-proxy" in rendered
    assert "loadbalancer.server.port" in rendered


def test_an_empty_override_is_extended_safely() -> None:
    document = CommentedMap()

    assert extend_override(document, {}, "shop", candidate(), 8000) is True

    rendered = render_override(document)
    assert "web:" in rendered
    assert "localhost-proxy:" in rendered
    assert "loadbalancer.server.port=8000" in rendered


def test_extending_a_complete_override_is_idempotent() -> None:
    document = create_override("shop", candidate(), 8000)
    model = {
        "networks": {"localhost-proxy": {"external": True}},
        "services": {
            "web": {
                "labels": {
                    "traefik.enable": "true",
                    "traefik.docker.network": "localhost-proxy",
                    "traefik.http.routers.shop-web.entrypoints": "web",
                    "traefik.http.routers.shop-web.rule": "Host(`shop.localhost`)",
                    "traefik.http.routers.shop-web.service": "shop-web",
                    "traefik.http.services.shop-web.loadbalancer.server.port": "8000",
                }
            }
        },
    }

    assert extend_override(document, model, "shop", candidate(), 8000) is False


@pytest.mark.parametrize(
    ("model", "message"),
    [
        (
            {"networks": {"localhost-proxy": {"external": False}}},
            "network is not external",
        ),
        (
            {
                "networks": {
                    "localhost-proxy": {"external": True, "name": "another-network"}
                }
            },
            "does not use the fixed name",
        ),
        (
            {
                "services": {
                    "web": {
                        "labels": {
                            (
                                "traefik.http.services.shop-web."
                                "loadbalancer.server.port"
                            ): "9000"
                        }
                    }
                }
            },
            "conflicts with the generated value",
        ),
        (
            {
                "services": {
                    "web": {
                        "labels": {
                            "traefik.http.routers.somewhere-else.rule": (
                                "Host(`shop.localhost`)"
                            )
                        }
                    }
                }
            },
            "already uses",
        ),
        (
            {
                "services": {
                    "worker": {
                        "labels": {
                            "traefik.http.routers.shop-web.rule": (
                                "Host(`shop.localhost`)"
                            )
                        }
                    }
                }
            },
            "already defined for service 'worker'",
        ),
        (
            {
                "services": {
                    "worker": {
                        "labels": {
                            "traefik.http.routers.shop-web.middlewares": "auth"
                        }
                    }
                }
            },
            "already defined for service 'worker'",
        ),
    ],
)
def test_extension_refuses_semantic_collisions(
    model: dict, message: str
) -> None:
    with pytest.raises(click.ClickException, match=message):
        extend_override(CommentedMap(), model, "shop", candidate(), 8000)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (CommentedMap({"services": CommentedSeq()}), "services.*mapping"),
        (
            CommentedMap({"services": CommentedMap({"web": "invalid"})}),
            "service 'web'.*mapping",
        ),
        (
            CommentedMap(
                {
                    "services": CommentedMap(
                        {"web": CommentedMap({"networks": "invalid"})}
                    )
                }
            ),
            "networks.*cannot be extended",
        ),
        (
            CommentedMap(
                {
                    "services": CommentedMap(
                        {"web": CommentedMap({"labels": "invalid"})}
                    )
                }
            ),
            "labels.*cannot be extended",
        ),
        (
            CommentedMap(
                {
                    "services": CommentedMap(
                        {
                            "other": "invalid",
                        }
                    )
                }
            ),
            "service 'other'.*mapping",
        ),
        (
            CommentedMap(
                {
                    "services": CommentedMap(
                        {
                            "other": CommentedMap(
                                {"labels": CommentedSeq([CommentedMap()])}
                            ),
                        }
                    )
                }
            ),
            "label sequences.*only strings",
        ),
        (CommentedMap({"networks": CommentedSeq()}), "networks.*mapping"),
    ],
)
def test_extension_refuses_structurally_unsafe_documents(
    document: CommentedMap, message: str
) -> None:
    with pytest.raises(click.ClickException, match=message):
        extend_override(document, {}, "shop", candidate(), 8000)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            CommentedMap(
                {
                    "networks": CommentedMap(
                        {"localhost-proxy": CommentedMap({"external": False})}
                    )
                }
            ),
            "network is not external",
        ),
        (
            CommentedMap(
                {
                    "services": CommentedMap(
                        {
                            "web": CommentedMap(
                                {
                                    "labels": CommentedMap(
                                        {
                                            "traefik.http.services.shop-web."
                                            "loadbalancer.server.port": "9000"
                                        }
                                    )
                                }
                            )
                        }
                    )
                }
            ),
            "conflicts with the generated value",
        ),
        (
            CommentedMap(
                {
                    "services": CommentedMap(
                        {
                            "worker": CommentedMap(
                                {
                                    "labels": CommentedSeq(
                                        [
                                            "traefik.http.routers.other.rule="
                                            "Host(`${COMPOSE_PROJECT_NAME}.localhost`)"
                                        ]
                                    )
                                }
                            )
                        }
                    )
                }
            ),
            "already uses",
        ),
    ],
)
def test_extension_refuses_collisions_in_the_unresolved_document(
    document: CommentedMap, message: str
) -> None:
    with pytest.raises(click.ClickException, match=message):
        extend_override(document, {}, "shop", candidate(), 8000)


def test_load_override_requires_a_yaml_mapping(tmp_path: Path) -> None:
    override = tmp_path / "compose.override.yaml"
    override.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(click.ClickException, match="YAML mapping"):
        load_override(override)


def test_load_override_reports_invalid_yaml(tmp_path: Path) -> None:
    override = tmp_path / "compose.override.yaml"
    override.write_text("services: [\n", encoding="utf-8")

    with pytest.raises(click.ClickException, match="not valid YAML"):
        load_override(override)


def test_load_override_reports_read_errors(tmp_path: Path) -> None:
    override = tmp_path / "missing.yaml"

    with pytest.raises(click.ClickException, match="could not read"):
        load_override(override)


def test_new_writes_refuse_to_overwrite_a_racing_file(tmp_path: Path) -> None:
    override = tmp_path / "compose.override.yaml"
    override.write_text("keep\n", encoding="utf-8")

    with pytest.raises(click.ClickException, match="refusing to overwrite"):
        write_new(override, CommentedMap({"services": CommentedMap()}))

    assert override.read_text(encoding="utf-8") == "keep\n"


def test_new_writes_report_output_errors(tmp_path: Path) -> None:
    override = tmp_path / "missing" / "compose.override.yaml"

    with pytest.raises(click.ClickException, match="could not write"):
        write_new(override, CommentedMap({"services": CommentedMap()}))


def test_extended_writes_use_numbered_non_destructive_backups(tmp_path: Path) -> None:
    override = tmp_path / "compose.override.yaml"
    override.write_text("# original\nservices: {}\n", encoding="utf-8")
    override.chmod(0o640)
    override.with_name("compose.override.yaml.bak").write_text(
        "first backup\n", encoding="utf-8"
    )

    backup = write_extended(override, CommentedMap({"services": CommentedMap()}))

    assert backup.name == "compose.override.yaml.bak.1"
    assert backup.read_text(encoding="utf-8") == "# original\nservices: {}\n"
    assert override.read_text(encoding="utf-8") == "services: {}\n"
    assert override.stat().st_mode & 0o777 == 0o640


def test_extended_writes_refuse_to_replace_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    target.write_text("keep\n", encoding="utf-8")
    override = tmp_path / "compose.override.yaml"
    override.symlink_to(target)

    with pytest.raises(click.ClickException, match="refusing to replace symlink"):
        write_extended(override, CommentedMap({"services": CommentedMap()}))

    assert override.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep\n"
