from localghost.compose import routing_problem


def _model(*, network=True, labels=True, service_network=True):
    service = {"labels": {}, "networks": {}}
    if labels:
        service["labels"]["traefik.enable"] = "true"
    if service_network:
        service["networks"]["localghost"] = None
    return {
        "networks": {"localghost": {"external": True}} if network else {},
        "services": {"web": service},
    }


def test_a_wired_project_has_no_problem():
    assert routing_problem(_model()) is None


def test_a_bare_project_reports_no_routing():
    assert "no service is configured to route" in routing_problem(
        _model(network=False, labels=False, service_network=False)
    )


def test_labels_without_the_network_names_that_half():
    problem = routing_problem(_model(service_network=False))

    assert "not attached to the localghost network" in problem


def test_the_network_without_labels_names_that_half():
    problem = routing_problem(_model(labels=False))

    assert "traefik.enable" in problem
