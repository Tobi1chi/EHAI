from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from ehai import json_loads
from ehai.application.orchestrator import OrchestrationError
from ehai.application.service import ExecutionService
from ehai.interfaces import cli


def test_cli_maps_orchestration_errors_to_json(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    def fail_to_build(
        _database: Path,
        _artifacts: Path,
        *,
        worker_kind: str,
        worker_workspace: Path | None,
    ) -> ExecutionService:
        del worker_kind, worker_workspace
        raise OrchestrationError("controlled orchestration failure")

    monkeypatch.setattr(cli, "build_service", fail_to_build)

    result = cli.main(
        [
            "--database",
            str(tmp_path / "state.sqlite3"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "create-project",
            "--idempotency-key",
            "project",
            "--name",
            "project",
        ]
    )

    captured = capsys.readouterr()
    error = json_loads(captured.err.strip())
    assert result == 2
    assert error == {
        "error": "controlled orchestration failure",
        "error_type": "OrchestrationError",
    }
    assert captured.out == ""
