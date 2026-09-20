"""scripts/cost-sweep.sh: find what bills the project that is not the node.

The August bill was $193.84 and July's the same; $189.73 of it was a Cloud
Workstations cluster in us-central1 billing its control plane every hour.
Nothing in this repo ever used one. The sweep must name it, delete it only
when told to (DELETE_IDLE=1), never touch VMs/SQL/GKE (list only), and exit
non-zero while anything idle still bills so launch.sh stops before deploying.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "cost-sweep.sh"
LAUNCH = REPO_ROOT / "scripts" / "launch.sh"

CLUSTER = "https://workstations.googleapis.com/v1/projects/resolver-time/locations/us-central1/workstationClusters/cluster-1"


def _run(tmp_path, *, clusters="", vms="", delete=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gcloud.log"
    (bin_dir / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{log}"\n'
        'case "$*" in\n'
        f'  *"workstations clusters list"*) printf "%s\\n" "{clusters}" ;;\n'
        '  *"workstations configs list"*) echo projects/p/locations/us-central1/workstationClusters/cluster-1/workstationConfigs/cfg ;;\n'
        '  *"workstations list"*) echo projects/p/.../workstations/ws-1 ;;\n'
        '  *"workstations"*"delete"*) exit 0 ;;\n'
        f'  *"compute instances list"*) printf "%s" "{vms}" ;;\n'
        '  *"sql instances list"*) exit 0 ;;\n'
        '  *"container clusters list"*) exit 0 ;;\n'
        '  *"run services describe"*) echo 0 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (bin_dir / "gcloud").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env.pop("DELETE_IDLE", None)
    if delete:
        env["DELETE_IDLE"] = "1"
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60)
    return result, (log.read_text() if log.exists() else "")



def test_a_workstation_cluster_is_named_and_the_sweep_fails(tmp_path):
    result, log = _run(tmp_path, clusters=CLUSTER)
    assert result.returncode == 1
    assert "workstation cluster 'cluster-1' in us-central1" in result.stdout
    assert "DELETE_IDLE=1" in result.stdout
    assert "delete" not in log, "deleted something without being told to"


def test_delete_idle_removes_workstations_then_configs_then_the_cluster(tmp_path):
    result, log = _run(tmp_path, clusters=CLUSTER, delete=True)
    lines = [ln for ln in log.splitlines() if "delete" in ln]
    kinds = ["workstations delete" if ("workstations delete " in ln) else
             "configs delete" if "configs delete" in ln else
             "clusters delete" if "clusters delete" in ln else "?" for ln in lines]
    assert kinds == ["workstations delete", "configs delete", "clusters delete"], kinds
    assert "--region=us-central1" in lines[-1]
    assert "--quiet" in lines[-1]
    assert "deleted cluster cluster-1" in result.stdout


def test_vms_are_listed_never_deleted(tmp_path):
    result, log = _run(tmp_path, vms="vm-1\tus-central1-a\tRUNNING", delete=True)
    assert result.returncode == 1
    assert "Compute Engine VMs" in result.stdout
    assert "vm-1" in result.stdout
    assert "compute instances delete" not in log


def test_nothing_idle_exits_zero(tmp_path):
    result, _ = _run(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "nothing idle is billing" in result.stdout


def test_launch_sweeps_after_the_checkout_and_before_the_deploy():
    text = LAUNCH.read_text()
    assert text.index("Checkout at origin/main") < text.index("cost-sweep.sh") < text.index(
        "repair-and-deploy.sh ||"
    )
    assert 'DELETE_IDLE="${DELETE_IDLE:-1}"' in text, "launch must remove idle billers by default"


def test_gcloud_can_never_prompt_the_sweep(tmp_path):
    """`gcloud sql instances list` asks "enable the API? (y/N)" on a project
    without it; with stderr silenced that prompt hung the sweep for the owner
    on 2026-09-04. Prompts are disabled for every gcloud call."""
    assert "CLOUDSDK_CORE_DISABLE_PROMPTS=1" in SCRIPT.read_text()
    assert "CLOUDSDK_CORE_DISABLE_PROMPTS=1" in LAUNCH.read_text()


def test_a_short_cluster_name_still_gets_a_region(tmp_path):
    """value(name) printed the short name, the region parsed to "" and the
    delete ran with --region= and failed. Listing is by --uri now, and a bare
    name still falls back to the one region ever billed."""
    result, log = _run(tmp_path, clusters="cluster-msqekho0", delete=True)
    deletes = [ln for ln in log.splitlines() if "clusters delete" in ln]
    assert deletes, log
    assert "--region=us-central1" in deletes[0]
    assert "--region= " not in deletes[0]
