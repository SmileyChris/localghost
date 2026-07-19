from click.testing import CliRunner

from localhost.cli import cli


def test_trust_status_reports_an_absent_root_without_starting_proxy(tmp_path) -> None:
    result = CliRunner().invoke(
        cli, ["trust", "--status"], env={"LOCALHOST_STATE_DIR": str(tmp_path)}
    )

    assert result.exit_code == 0, result.output
    assert "HTTPS: disabled" in result.output


def test_trust_rejects_combined_remove_and_status() -> None:
    result = CliRunner().invoke(cli, ["trust", "--remove", "--status"])

    assert result.exit_code != 0
    assert "cannot be used together" in result.output
