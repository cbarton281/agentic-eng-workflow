#!/usr/bin/env python3
"""
welcomepage-agentic.py

Agentic workflow runner for the Welcomepage project (frontend + backend).

- Reads a feature request from a markdown file
- Creates an isolated workspace under ./agent-work/run-<timestamp>/
- Clones both repos (welcomepage-api + welcomepage-prompts) into that workspace
- Creates a matching feature branch in each
- Invokes Cursor CLI (headless) to implement the feature across both repos
- Deploys both repos to Vercel preview via `vercel deploy --target=preview`
- Writes a run log capturing ALL console output plus structured metadata

Usage:
  python welcomepage-agentic.py features/my-feature.md

Requires:
  pip install python-dotenv
  Vercel CLI installed globally: npm i -g vercel

.env (in same directory as this script):
  CURSOR_API_KEY=...
  WELCOMEPAGE_API_REPO_URL=git@github.com:your-org/welcomepage-api.git
  WELCOMEPAGE_FRONTEND_REPO_URL=git@github.com:your-org/welcomepage-prompts.git
  VERCEL_TOKEN=...
  VERCEL_ORG_ID=...
  VERCEL_API_PROJECT_ID=...
  VERCEL_FRONTEND_PROJECT_ID=...

Optional in .env:
  WELCOMEPAGE_BASE_BRANCH=main
  WELCOMEPAGE_WORK_DIR=./agent-work
"""

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# TeeWriter — duplicates all stdout/stderr to a log file
# ---------------------------------------------------------------------------

class TeeWriter:
    """Wraps an original stream so every write also goes to a log file."""

    def __init__(self, original: io.TextIOBase, log_file: io.TextIOBase):
        self._original = original
        self._log_file = log_file

    def write(self, data: str) -> int:
        self._original.write(data)
        self._original.flush()
        self._log_file.write(data)
        self._log_file.flush()
        return len(data)

    def flush(self):
        self._original.flush()
        self._log_file.flush()

    def fileno(self):
        return self._original.fileno()

    def isatty(self):
        return self._original.isatty()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def run(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command and stream stdout/stderr to the console (and log via tee)."""
    display = " ".join(cmd)
    print(f"\n$ {display}")
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, check=check)


def run_and_capture(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict | None = None,
) -> tuple[int, str, str]:
    """
    Run a command, tee stdout/stderr to the console in real time,
    and return (returncode, stdout_text, stderr_text) for structured logging.
    Output also flows through the TeeWriter so it lands in the raw log file.
    """
    display = " ".join(cmd)
    print(f"\n$ {display}")

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _tee(stream, sink, output_lines):
        for line in stream:
            sink.write(line)
            sink.flush()
            output_lines.append(line)

    t_out = threading.Thread(target=_tee, args=(proc.stdout, sys.stdout, stdout_lines))
    t_err = threading.Thread(target=_tee, args=(proc.stderr, sys.stderr, stderr_lines))
    t_out.start()
    t_err.start()
    t_out.join()
    t_err.join()
    proc.wait()

    return proc.returncode, "".join(stdout_lines), "".join(stderr_lines)


def read_text_file(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Feature file not found: {path}")
    return path.read_text(encoding="utf-8")


def create_run_directory(base_work_dir: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = base_work_dir / f"run-{ts}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def slugify_branch_name(feature_md: str, max_len: int = 60) -> str:
    """
    Build a branch suffix from the first non-empty line of the feature doc.
    Example: "Add dark mode to settings" -> "add-dark-mode-to-settings"
    """
    title = ""
    for line in feature_md.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            title = s
            break
    if not title:
        title = "feature"

    title = title.lower()
    title = re.sub(r"[^a-z0-9]+", "-", title)
    title = title.strip("-")
    if not title:
        title = "feature"
    return title[:max_len].strip("-")


def generate_vercel_aliases(branch_suffix: str, run_ts: str) -> tuple[str, str]:
    """
    Produce predictable Vercel alias hostnames so both deployments can
    reference each other before either has been deployed.

    Returns (api_alias_host, frontend_alias_host).
    """
    slug = f"{branch_suffix[:30]}-{run_ts}"
    slug = re.sub(r"[^a-z0-9-]", "-", slug.lower()).strip("-")
    api_alias = f"wp-api-{slug}.vercel.app"
    frontend_alias = f"wp-{slug}.vercel.app"
    return api_alias, frontend_alias


def find_cursor_agent_command() -> list[str]:
    if shutil.which("agent"):
        return ["agent"]
    if shutil.which("cursor"):
        return ["cursor", "agent"]
    raise RuntimeError(
        "Could not find Cursor CLI. Ensure `agent` or `cursor` is on PATH."
    )


def require_env(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"{name} not found. Set it in .env next to this script.")
    return val


RULES_FRONTMATTER = "---\nalwaysApply: true\n---\n\n"


def install_cursor_rules(rules_path: str | None, target_dir: Path) -> None:
    """
    Copy a Cursor rules file into ``target_dir/.cursor/rules/`` so the
    Cursor agent automatically applies it.  Prepends ``alwaysApply: true``
    frontmatter if the file doesn't already contain frontmatter.
    """
    if not rules_path:
        return
    src = Path(rules_path).expanduser().resolve()
    if not src.exists():
        print(f"  (cursor-rules file not found at {src} — skipping)")
        return

    content = src.read_text(encoding="utf-8")
    if not content.startswith("---"):
        content = RULES_FRONTMATTER + content

    dest_dir = target_dir / ".cursor" / "rules"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / src.name
    dest_file.write_text(content, encoding="utf-8")
    print(f"  Installed cursor rules: {dest_file}")


# ---------------------------------------------------------------------------
# Repo helpers
# ---------------------------------------------------------------------------

def clone_and_branch(
    repo_url: str,
    dest: Path,
    base_branch: str,
    branch_name: str,
    run_dir: Path,
) -> None:
    """Clone a repo, check out the base branch, and create a feature branch."""
    run(["git", "clone", repo_url, str(dest)], cwd=run_dir)
    run(["git", "fetch", "--all", "--prune"], cwd=dest)
    run(["git", "checkout", base_branch], cwd=dest)
    run(["git", "pull", "--ff-only"], cwd=dest)
    run(["git", "checkout", "-b", branch_name], cwd=dest)


def git_status_summary(repo_dir: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def git_diff_stat(repo_dir: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        return res.stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Vercel helpers
# ---------------------------------------------------------------------------

class VercelBuildError(RuntimeError):
    """Raised when ``vercel deploy`` fails during the build step."""

    def __init__(self, repo_name: str, returncode: int, build_output: str):
        self.repo_name = repo_name
        self.returncode = returncode
        self.build_output = build_output
        super().__init__(
            f"Vercel build failed for {repo_name} (exit {returncode})"
        )


def deploy_to_vercel(
    repo_path: Path,
    vercel_token: str,
    org_id: str,
    project_id: str,
    env_overrides: dict[str, str] | None = None,
) -> str:
    """
    Deploy to Vercel preview and return the preview URL.

    Sets VERCEL_ORG_ID and VERCEL_PROJECT_ID in the subprocess environment
    so the CLI knows which project to target.

    ``env_overrides`` is an optional dict of KEY=VALUE pairs that are passed
    as both ``--env`` (runtime) and ``--build-env`` (build-time) flags so
    the values are available to serverless functions and inlined into the
    Next.js client bundle.

    Raises ``VercelBuildError`` (with the full build output) when the build
    fails, so callers can feed the error back to Cursor for a fix attempt.
    """
    cmd = [
        "vercel", "deploy",
        "--target=preview",
        "--token", vercel_token,
        "--yes",
    ]

    for key, value in (env_overrides or {}).items():
        cmd += ["--env", f"{key}={value}"]
        cmd += ["--build-env", f"{key}={value}"]

    display_parts = ["vercel", "deploy", "--target=preview", "--token", "***", "--yes"]
    for key, value in (env_overrides or {}).items():
        display_parts += ["--env", f"{key}={value}", "--build-env", f"{key}={value}"]
    print(f"\n$ {' '.join(display_parts)}  (in {repo_path.name})")

    deploy_env = os.environ.copy()
    deploy_env["VERCEL_ORG_ID"] = org_id
    deploy_env["VERCEL_PROJECT_ID"] = project_id

    res = subprocess.run(
        cmd,
        cwd=str(repo_path),
        env=deploy_env,
        capture_output=True,
        text=True,
    )

    combined_output = ""
    if res.stdout:
        print(res.stdout)
        combined_output += res.stdout
    if res.stderr:
        eprint(res.stderr)
        combined_output += res.stderr

    if res.returncode != 0:
        raise VercelBuildError(
            repo_name=repo_path.name,
            returncode=res.returncode,
            build_output=combined_output,
        )

    lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
    if not lines:
        raise RuntimeError(f"No output from vercel deploy for {repo_path.name}")
    return lines[-1]


def set_vercel_alias(
    deploy_url: str,
    alias_host: str,
    vercel_token: str,
    vercel_org_id: str,
) -> bool:
    """
    Run ``vercel alias set <deploy_url> <alias_host>``.
    Returns True on success, False on failure (warning logged, not raised).
    """
    cmd = [
        "vercel", "alias", "set",
        deploy_url, alias_host,
        "--token", vercel_token,
        "--scope", vercel_org_id,
    ]
    print(f"\n$ vercel alias set {deploy_url} {alias_host} --token *** --scope ***")

    res = subprocess.run(cmd, capture_output=True, text=True)

    if res.stdout:
        print(res.stdout)
    if res.stderr:
        eprint(res.stderr)

    if res.returncode != 0:
        eprint(
            f"  ⚠️  Alias failed for {alias_host} (exit {res.returncode}). "
            f"Raw deploy URL still works."
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Build-error fix loop
# ---------------------------------------------------------------------------

def _invoke_cursor_build_fix(
    cursor_cmd: list[str],
    repo_dir: Path,
    repo_label: str,
    build_output: str,
    attempt: int,
    cursor_env: dict,
    output_format: str,
) -> tuple[int, str, str]:
    """
    Call Cursor to fix compile / build errors detected during Vercel deploy.

    Targets only the single repo that failed, giving Cursor the full
    build output so it can identify and fix the problem.
    """
    # Truncate very long build logs to the last 300 lines to stay within
    # reasonable prompt limits while keeping the actual errors (at the end).
    log_lines = build_output.splitlines()
    if len(log_lines) > 300:
        build_excerpt = "\n".join(log_lines[-300:])
    else:
        build_excerpt = build_output

    fix_prompt = f"""
The Vercel deployment for {repo_label} failed with build/compile errors
(attempt {attempt}).

Your task: fix every error shown in the build output below. Do NOT add
new features — only fix the build errors so the project compiles cleanly.
Commit your changes when done.

BUILD OUTPUT:
{build_excerpt}
""".strip()

    full_cmd = cursor_cmd + [
        "-p", fix_prompt,
        "--model", "composer-1.5",
        "--force",
        "--print",
        "--output-format", output_format,
    ]

    print(f"\n===== Cursor Fix (attempt {attempt}) for {repo_label} =====")
    rc, stdout, stderr = run_and_capture(full_cmd, cwd=repo_dir, env=cursor_env)

    if rc != 0:
        eprint(f"  ⚠️  Cursor fix exited with code {rc}")
    else:
        print(f"  ✅ Cursor fix completed for {repo_label}")

    return rc, stdout, stderr


def deploy_with_build_fix_loop(
    *,
    repo_dir: Path,
    repo_label: str,
    vercel_token: str,
    vercel_org_id: str,
    vercel_project_id: str,
    env_overrides: dict[str, str] | None,
    max_retries: int,
    cursor_cmd: list[str],
    cursor_env: dict,
    output_format: str,
) -> str:
    """
    Attempt ``vercel deploy``. If the build fails, invoke Cursor to fix the
    errors and retry, up to ``max_retries`` times.

    Returns the preview URL on success, or raises after exhausting retries.
    """
    last_error: VercelBuildError | None = None

    for attempt in range(1, max_retries + 2):  # attempt 1 = first try
        try:
            url = deploy_to_vercel(
                repo_dir, vercel_token, vercel_org_id, vercel_project_id,
                env_overrides=env_overrides,
            )
            if attempt > 1:
                print(f"  ✅ {repo_label} deployed successfully after "
                      f"{attempt - 1} fix attempt(s)")
            return url

        except VercelBuildError as exc:
            last_error = exc
            retries_left = max_retries - (attempt - 1)
            if retries_left <= 0:
                print(f"\n  ❌ Build failed for {repo_label} — no retries left.")
                raise

            print(f"\n  ⚠️  Build failed for {repo_label}. "
                  f"Invoking Cursor to fix ({retries_left} retries left)…")

            _invoke_cursor_build_fix(
                cursor_cmd=cursor_cmd,
                repo_dir=repo_dir,
                repo_label=repo_label,
                build_output=exc.build_output,
                attempt=attempt,
                cursor_env=cursor_env,
                output_format=output_format,
            )

    # Should not reach here, but just in case
    raise last_error or RuntimeError(f"Deploy failed for {repo_label}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    script_dir = Path(__file__).resolve().parent
    load_dotenv(script_dir / ".env")

    parser = argparse.ArgumentParser(
        description="Welcomepage agentic workflow: clone both repos -> branch -> "
                    "Cursor implement -> Vercel preview deploy."
    )
    parser.add_argument(
        "feature_file",
        help="Path to a markdown file describing the feature request.",
    )
    parser.add_argument(
        "--api-repo-url",
        default=os.getenv("WELCOMEPAGE_API_REPO_URL", ""),
        help="Git URL for the backend repo (welcomepage-api).",
    )
    parser.add_argument(
        "--frontend-repo-url",
        default=os.getenv("WELCOMEPAGE_FRONTEND_REPO_URL", ""),
        help="Git URL for the frontend repo (welcomepage-prompts).",
    )
    parser.add_argument(
        "--base-branch",
        default=os.getenv("WELCOMEPAGE_BASE_BRANCH", "main"),
        help="Base branch to branch off in both repos.",
    )
    parser.add_argument(
        "--work-dir",
        default=os.getenv("WELCOMEPAGE_WORK_DIR", str(script_dir / "agent-work")),
        help="Base work directory.",
    )
    parser.add_argument("--branch-prefix", default="feature/", help="Branch name prefix.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Do everything except calling Cursor and deploying to Vercel.",
    )
    parser.add_argument(
        "--skip-deploy", action="store_true",
        help="Run Cursor but skip the Vercel deploy step.",
    )
    parser.add_argument(
        "--cursor-output-format", default="text",
        choices=["text", "json", "stream-json"],
        help="Cursor output format.",
    )
    parser.add_argument(
        "--max-fix-retries", type=int, default=2,
        help="Max times to invoke Cursor to fix build errors per repo before giving up (default: 2).",
    )
    parser.add_argument(
        "--cursor-rules",
        default=os.getenv(
            "WELCOMEPAGE_CURSOR_RULES",
            str(script_dir / "cursor-rules.md"),
        ),
        help="Path to a Cursor rules markdown file to load into the workspace "
             "(default: cursor-rules.md next to this script).",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Validate required config
    # ------------------------------------------------------------------
    feature_path = Path(args.feature_file).expanduser().resolve()
    feature_md = read_text_file(feature_path)

    cursor_key = require_env("CURSOR_API_KEY")

    api_repo_url = args.api_repo_url.strip()
    if not api_repo_url:
        raise RuntimeError(
            "Backend repo URL missing. Provide --api-repo-url or set "
            "WELCOMEPAGE_API_REPO_URL in .env"
        )

    frontend_repo_url = args.frontend_repo_url.strip()
    if not frontend_repo_url:
        raise RuntimeError(
            "Frontend repo URL missing. Provide --frontend-repo-url or set "
            "WELCOMEPAGE_FRONTEND_REPO_URL in .env"
        )

    base_branch = args.base_branch.strip() or "main"

    vercel_token = os.getenv("VERCEL_TOKEN", "").strip()
    vercel_org_id = os.getenv("VERCEL_ORG_ID", "").strip()
    vercel_api_project_id = os.getenv("VERCEL_API_PROJECT_ID", "").strip()
    vercel_frontend_project_id = os.getenv("VERCEL_FRONTEND_PROJECT_ID", "").strip()

    need_deploy = not args.dry_run and not args.skip_deploy
    if need_deploy:
        for name, val in [
            ("VERCEL_TOKEN", vercel_token),
            ("VERCEL_ORG_ID", vercel_org_id),
            ("VERCEL_API_PROJECT_ID", vercel_api_project_id),
            ("VERCEL_FRONTEND_PROJECT_ID", vercel_frontend_project_id),
        ]:
            if not val:
                raise RuntimeError(
                    f"{name} is required for Vercel deploy. Set it in .env or use "
                    "--skip-deploy / --dry-run."
                )

    # ------------------------------------------------------------------
    # Set up workspace and logging
    # ------------------------------------------------------------------
    base_work_dir = Path(args.work_dir).expanduser().resolve()
    base_work_dir.mkdir(parents=True, exist_ok=True)

    run_dir = create_run_directory(base_work_dir)

    # Open a raw log file and tee all stdout/stderr into it so the log
    # captures everything the operator sees on the console.
    log_path = run_dir / "run.log"
    log_file = open(log_path, "w", encoding="utf-8")
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = TeeWriter(original_stdout, log_file)
    sys.stderr = TeeWriter(original_stderr, log_file)

    try:
        _run_workflow(
            args=args,
            feature_path=feature_path,
            feature_md=feature_md,
            cursor_key=cursor_key,
            api_repo_url=api_repo_url,
            frontend_repo_url=frontend_repo_url,
            base_branch=base_branch,
            vercel_token=vercel_token,
            vercel_org_id=vercel_org_id,
            vercel_api_project_id=vercel_api_project_id,
            vercel_frontend_project_id=vercel_frontend_project_id,
            need_deploy=need_deploy,
            run_dir=run_dir,
            log_path=log_path,
        )
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        log_file.close()


def _run_workflow(
    *,
    args,
    feature_path: Path,
    feature_md: str,
    cursor_key: str,
    api_repo_url: str,
    frontend_repo_url: str,
    base_branch: str,
    vercel_token: str,
    vercel_org_id: str,
    vercel_api_project_id: str,
    vercel_frontend_project_id: str,
    need_deploy: bool,
    run_dir: Path,
    log_path: Path,
):
    print("\n===== FEATURE REQUEST =====")
    print(feature_md)
    print("===========================\n")
    print(f"Workspace : {run_dir}")
    print(f"Backend   : {api_repo_url}")
    print(f"Frontend  : {frontend_repo_url}")
    print(f"Log       : {log_path}")

    # ------------------------------------------------------------------
    # 1) Clone both repos and create feature branches
    # ------------------------------------------------------------------
    branch_suffix = slugify_branch_name(feature_md)
    branch_name = f"{args.branch_prefix}{branch_suffix}"

    api_dir = run_dir / "welcomepage-api"
    frontend_dir = run_dir / "welcomepage-prompts"

    print("\n--- Cloning backend repo ---")
    clone_and_branch(api_repo_url, api_dir, base_branch, branch_name, run_dir)

    print("\n--- Cloning frontend repo ---")
    clone_and_branch(frontend_repo_url, frontend_dir, base_branch, branch_name, run_dir)

    print(f"\n🌿 Branch created in both repos: {branch_name}")

    # ------------------------------------------------------------------
    # 1b) Install Cursor rules into the workspace (and each repo, so
    #     build-fix invocations that target a single repo also see them)
    # ------------------------------------------------------------------
    for rules_target in (run_dir, api_dir, frontend_dir):
        install_cursor_rules(args.cursor_rules, rules_target)

    # ------------------------------------------------------------------
    # 2) Run Cursor agent from workspace root (sees both repos)
    # ------------------------------------------------------------------
    if args.dry_run:
        print("\n(dry-run) Skipping Cursor invocation and Vercel deploy.")
        print(f"  Backend  repo: {api_dir}")
        print(f"  Frontend repo: {frontend_dir}")
        _write_structured_log(run_dir, dict(
            feature_path=str(feature_path),
            feature_content=feature_md,
            branch_name=branch_name,
            base_branch=base_branch,
            cursor={"skipped": True},
            vercel={"skipped": True},
        ))
        return

    cursor_cmd = find_cursor_agent_command()

    env = os.environ.copy()
    env["CURSOR_API_KEY"] = cursor_key

    prompt = f"""
You are implementing a feature across two repositories in this workspace:

1. welcomepage-api/      — the backend API
2. welcomepage-prompts/  — the frontend application

FEATURE REQUEST (markdown):
{feature_md}

Hard requirements:
- Make changes to BOTH frontend and backend as needed by the feature.
- Make minimal necessary changes.
- Add or update tests where appropriate.
- Do NOT modify secrets, credentials, or production-only configuration.
- If you need to add config, prefer safe defaults and document it.
- Commit your changes in each repo before finishing.
- At the end, summarize what you changed and list files touched in each repo.
""".strip()

    full_cmd = cursor_cmd + [
        "-p", prompt,
        "--model", "composer-1.5",
        "--force",
        "--print",
        "--output-format", args.cursor_output_format,
    ]

    print("\n===== Running Cursor Agent =====")
    cursor_rc, cursor_stdout, cursor_stderr = run_and_capture(
        full_cmd, cwd=run_dir, env=env
    )

    if cursor_rc != 0:
        eprint(f"\n⚠️  Cursor exited with code {cursor_rc}")

    print("\n✅ Cursor run complete.")

    for name, rdir in [("Backend", api_dir), ("Frontend", frontend_dir)]:
        status = git_status_summary(rdir)
        diff = git_diff_stat(rdir)
        print(f"\n--- {name} git status ---")
        print(status or "(clean)")
        if diff:
            print(diff)

    # ------------------------------------------------------------------
    # 3) Deploy to Vercel preview with bidirectional env wiring
    # ------------------------------------------------------------------
    api_deploy_url = ""
    frontend_deploy_url = ""
    api_alias_url = ""
    frontend_alias_url = ""
    api_alias_ok = False
    frontend_alias_ok = False

    if not need_deploy:
        print("\n(skip-deploy) Skipping Vercel deployment.")
    else:
        # Compute predictable alias hostnames so each deployment can
        # reference the other before either URL exists.
        run_ts = run_dir.name.replace("run-", "")  # e.g. "20260225-111149"
        api_alias_host, frontend_alias_host = generate_vercel_aliases(
            branch_suffix, run_ts,
        )
        api_alias_url = f"https://{api_alias_host}"
        frontend_alias_url = f"https://{frontend_alias_host}"

        print("\n===== Deploying to Vercel Preview =====")
        print(f"  Planned backend  alias: {api_alias_url}")
        print(f"  Planned frontend alias: {frontend_alias_url}")

        # Deploy backend — wire WEBAPP_URL to the frontend alias.
        # If the build fails, Cursor is invoked to fix errors and we retry.
        print("\n--- Deploying backend ---")
        try:
            api_deploy_url = deploy_with_build_fix_loop(
                repo_dir=api_dir,
                repo_label="welcomepage-api (backend)",
                vercel_token=vercel_token,
                vercel_org_id=vercel_org_id,
                vercel_project_id=vercel_api_project_id,
                env_overrides={"WEBAPP_URL": frontend_alias_url},
                max_retries=args.max_fix_retries,
                cursor_cmd=cursor_cmd,
                cursor_env=env,
                output_format=args.cursor_output_format,
            )
            print(f"  🔗 Backend deploy URL: {api_deploy_url}")
        except (VercelBuildError, RuntimeError) as exc:
            api_deploy_url = f"FAILED: {exc}"
            eprint(f"  ❌ {exc}")

        # Deploy frontend — wire NEXT_PUBLIC_FASTAPI_BASE_URL to the backend alias.
        print("\n--- Deploying frontend ---")
        try:
            frontend_deploy_url = deploy_with_build_fix_loop(
                repo_dir=frontend_dir,
                repo_label="welcomepage-prompts (frontend)",
                vercel_token=vercel_token,
                vercel_org_id=vercel_org_id,
                vercel_project_id=vercel_frontend_project_id,
                env_overrides={"NEXT_PUBLIC_FASTAPI_BASE_URL": api_alias_url},
                max_retries=args.max_fix_retries,
                cursor_cmd=cursor_cmd,
                cursor_env=env,
                output_format=args.cursor_output_format,
            )
            print(f"  🔗 Frontend deploy URL: {frontend_deploy_url}")
        except (VercelBuildError, RuntimeError) as exc:
            frontend_deploy_url = f"FAILED: {exc}"
            eprint(f"  ❌ {exc}")

        # Set aliases so the pre-computed hostnames resolve to the deployments
        print("\n--- Setting Vercel aliases ---")
        if not api_deploy_url.startswith("FAILED"):
            api_alias_ok = set_vercel_alias(
                api_deploy_url, api_alias_host, vercel_token, vercel_org_id,
            )
            if api_alias_ok:
                print(f"  ✅ Backend alias : {api_alias_url}")
        else:
            print("  (skipping backend alias — deploy failed)")

        if not frontend_deploy_url.startswith("FAILED"):
            frontend_alias_ok = set_vercel_alias(
                frontend_deploy_url, frontend_alias_host, vercel_token, vercel_org_id,
            )
            if frontend_alias_ok:
                print(f"  ✅ Frontend alias: {frontend_alias_url}")
        else:
            print("  (skipping frontend alias — deploy failed)")

    # ------------------------------------------------------------------
    # 4) Write structured metadata into a separate JSON file
    # ------------------------------------------------------------------
    _write_structured_log(run_dir, dict(
        feature_path=str(feature_path),
        feature_content=feature_md,
        branch_name=branch_name,
        base_branch=base_branch,
        cursor=dict(
            model="composer-1.5",
            output_format=args.cursor_output_format,
            returncode=cursor_rc,
            stdout=cursor_stdout,
            stderr=cursor_stderr,
        ),
        vercel=dict(
            welcomepage_api=dict(
                project_id=vercel_api_project_id,
                deploy_url=api_deploy_url,
                alias_url=api_alias_url,
                alias_ok=api_alias_ok,
            ),
            welcomepage_prompts=dict(
                project_id=vercel_frontend_project_id,
                deploy_url=frontend_deploy_url,
                alias_url=frontend_alias_url,
                alias_ok=frontend_alias_ok,
            ),
        ),
    ))

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n===== Summary =====")
    print(f"  Workspace : {run_dir}")
    print(f"  Branch    : {branch_name}")
    if api_alias_ok:
        print(f"  Backend   : {api_alias_url}  (alias)")
    elif api_deploy_url:
        print(f"  Backend   : {api_deploy_url}  (raw deploy URL)")
    if frontend_alias_ok:
        print(f"  Frontend  : {frontend_alias_url}  (alias)")
    elif frontend_deploy_url:
        print(f"  Frontend  : {frontend_deploy_url}  (raw deploy URL)")
    if api_deploy_url and api_alias_ok:
        print(f"  (raw backend  deploy: {api_deploy_url})")
    if frontend_deploy_url and frontend_alias_ok:
        print(f"  (raw frontend deploy: {frontend_deploy_url})")
    print(f"  Log       : {run_dir / 'run.log'}")
    print(f"  Metadata  : {run_dir / 'run-metadata.json'}")
    print("===================\n")


def _write_structured_log(run_dir: Path, data: dict) -> Path:
    """Write structured run metadata to a separate JSON file."""
    data["timestamp"] = datetime.now().isoformat()
    data["run_directory"] = str(run_dir)
    meta_path = run_dir / "run-metadata.json"
    meta_path.write_text(json.dumps(data, indent=2, default=str))
    print(f"\n📋 Metadata written to: {meta_path}")
    return meta_path


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        eprint("\n❌ Error:", ex)
        sys.exit(1)
