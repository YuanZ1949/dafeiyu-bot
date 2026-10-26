import os
import subprocess
import sys
from pathlib import Path

from dafeiyu.supervisor.audit import AuditLog

_WRITER = """
import sys
from pathlib import Path

from dafeiyu.supervisor.audit import AuditLog

log = AuditLog(Path(sys.argv[1]), max_bytes=1200, backups=100)
for index in range(40):
    log.write("test", target=sys.argv[2], detail={"index": index})
"""


def test_two_processes_can_rotate_the_same_audit_file(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(path), f"writer-{index}"],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)

    rows = AuditLog(path, backups=100).query(200)
    assert len(rows) == 80
    assert {row["target"] for row in rows} == {"writer-0", "writer-1"}
