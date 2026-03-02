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
- Explores the deployed frontend via headless browser (Playwright): takes
  screenshots, generates a test plan with the vision model, executes it
  (clicking buttons, capturing downloads, etc.), and analyses all artifacts
- Loops back to Cursor for refinements if the vision model finds issues
- Writes a run log capturing ALL console output plus structured metadata

Usage:
  python welcomepage-agentic.py specifications/my-feature.md

Requires:
  pip install python-dotenv playwright anthropic
  python -m playwright install chromium
  Vercel CLI installed globally: npm i -g vercel

.env (in same directory as this script):
  CURSOR_API_KEY=...
  WELCOMEPAGE_API_REPO_URL=git@github.com:your-org/welcomepage-api.git
  WELCOMEPAGE_FRONTEND_REPO_URL=git@github.com:your-org/welcomepage-prompts.git
  VERCEL_TOKEN=...
  VERCEL_ORG_ID=...
  VERCEL_API_PROJECT_ID=...
  VERCEL_FRONTEND_PROJECT_ID=...
  ANTHROPIC_API_KEY=...
  TESTING_AUTH_BYPASS_SECRET=...

Optional in .env:
  WELCOMEPAGE_BASE_BRANCH=main
  WELCOMEPAGE_WORK_DIR=./agent-work
  TESTING_AUTH_EMAIL=your-test@example.com
"""

import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
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


def parse_preview_paths(text: str) -> list[str]:
    """
    Extract preview paths from text (feature markdown or Cursor output).

    Recognises several formats:
      1. HTML comment:    <!-- preview-paths: /path1, /path2 -->
      2. Inline (with optional bold/backtick markdown):
           PREVIEW_PATHS: /path1, /path2
           **PREVIEW_PATHS:** `/path1`, `/path2`
      3. Heading followed by bullets, code blocks, or bare paths:
           ### PREVIEW_PATHS
           - `/path1`
           - `/path2`

    Falls back to ``["/"]`` when nothing is found.
    """
    # Format 1: HTML comment
    m = re.search(r"<!--\s*preview-paths:\s*(.+?)\s*-->", text, re.IGNORECASE)
    if m:
        paths = [p.strip() for p in m.group(1).split(",") if p.strip()]
        if paths:
            return paths

    # Format 2: same-line paths after PREVIEW_PATHS
    # Handles: "PREVIEW_PATHS: /a, /b", "**PREVIEW_PATHS:** `/a`, `/b`", etc.
    # Use [ \t]* instead of \s* to avoid consuming newlines into the next line.
    m = re.search(r"[#*]*[ \t]*PREVIEW_PATHS[*:]*[ \t]*[*]*[ \t]*(.*)", text, re.IGNORECASE)
    if m and m.group(1).strip():
        paths = re.findall(r"(/[a-zA-Z0-9_.[\]/-]+)", m.group(1))
        if paths:
            return paths

    # Format 3: heading followed by bullet lines, code-block lines, or bare paths
    # Handles blank lines, backticks, bullets, and fenced code blocks
    m = re.search(
        r"[#*]*\s*PREVIEW_PATHS[*:]*\s*\n[\s`]*\n?((?:.+\n?){1,12})",
        text,
        re.IGNORECASE,
    )
    if m:
        paths = re.findall(r"(/[a-zA-Z0-9_.[\]/-]+)", m.group(1))
        if paths:
            return paths

    return ["/"]


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
    model: str,
    output_format: str,
) -> tuple[int, str, str]:
    """
    Call Cursor to fix compile / build errors detected during Vercel deploy.

    Targets only the single repo that failed, giving Cursor the full
    build output so it can identify and fix the problem.
    """
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
        "--model", model,
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
    model: str,
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
                model=model,
                output_format=output_format,
            )

    # Should not reach here, but just in case
    raise last_error or RuntimeError(f"Deploy failed for {repo_label}")


# ---------------------------------------------------------------------------
# Visual review helpers
# ---------------------------------------------------------------------------

def _ensure_playwright_browsers() -> None:
    """
    Ensure Playwright can find its Chromium binary.

    When browsers were installed with ``PLAYWRIGHT_BROWSERS_PATH=0`` they
    live inside the venv's site-packages.  Setting the same env var at
    runtime tells Playwright to look there instead of the default system
    cache (``~/Library/Caches/ms-playwright``).

    If no local browsers exist, automatically run ``playwright install``.
    """
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

    import importlib
    pw_pkg = importlib.import_module("playwright")
    local_browsers = Path(pw_pkg.__file__).parent / "driver" / "package" / ".local-browsers"
    if not local_browsers.exists() or not any(local_browsers.iterdir()):
        print("  Playwright browsers not found — installing Chromium …")
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
        )


def _generate_test_plan(
    screenshot_paths: list[Path],
    feature_md: str,
    anthropic_api_key: str,
) -> list[dict]:
    """
    Ask the vision model to produce a test plan for the new feature.

    Given page screenshots and the feature spec, returns a list of actions
    like ``[{"action": "click", "target": "Download combined wave GIF"}, ...]``.

    Supported actions:
      - click(target)          — click a button/link matching the target text
      - type(target, value)    — type value into an input matching target
      - wait_for_download()    — wait for a file download to complete
      - screenshot(label)      — capture a screenshot with the given label
      - scroll(direction)      — scroll "up" or "down"
    """
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_api_key)

    content: list[dict] = []
    for sp in screenshot_paths:
        img_b64 = base64.standard_b64encode(sp.read_bytes()).decode("utf-8")
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": img_b64,
            },
        })

    content.append({
        "type": "text",
        "text": f"""\
You are a senior QA engineer planning how to test a NEW FEATURE on a web page.
The screenshots show the current state of the deployed page.

FEATURE SPEC:
{feature_md}

Your task: generate a SHORT test plan (JSON array) of browser actions to
exercise the new feature end-to-end.  The goal is to trigger the feature,
capture its output (downloads, modals, visual changes), and take screenshots
of the results.

Available actions (use ONLY these):
  {{"action": "click", "target": "<visible button/link text>"}}
  {{"action": "type", "target": "<input placeholder or label>", "value": "<text>"}}
  {{"action": "wait_for_download"}}
  {{"action": "screenshot", "label": "<descriptive-label>"}}
  {{"action": "scroll", "direction": "down"}}

Rules:
- Keep the plan SHORT (3–8 steps).  Focus on the core feature flow.
- Always end with a screenshot action.
- If the feature triggers a file download, include wait_for_download AFTER
  the click that triggers it, then a screenshot.
- Use EXACT visible text for click targets (e.g. "Download combined wave GIF").
- Do NOT test existing features — only the NEW feature from the spec.
- Respond with ONLY the JSON array.  No explanation, no markdown fences.""",
    })

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if the model included them
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    try:
        plan = json.loads(raw)
        if isinstance(plan, list):
            return plan
    except json.JSONDecodeError:
        eprint(f"  ⚠️  Failed to parse test plan JSON: {raw[:200]}")

    return [{"action": "screenshot", "label": "fallback-page-state"}]


def _execute_test_plan(
    page,
    plan: list[dict],
    screenshot_dir: Path,
    start_index: int = 0,
) -> tuple[list[Path], list[Path]]:
    """
    Execute a vision-model-generated test plan using a Playwright page.

    Returns ``(screenshot_paths, download_paths)``.
    """
    screenshots: list[Path] = []
    downloads: list[Path] = []
    step_idx = start_index

    for step in plan:
        action = step.get("action", "")
        print(f"    Step {step_idx}: {action} {step.get('target', step.get('label', ''))}")

        try:
            if action == "click":
                target = step.get("target", "")
                # Try exact text match first, then partial
                try:
                    locator = page.get_by_role("button", name=target)
                    if locator.count() == 0:
                        locator = page.get_by_text(target, exact=False)
                    locator.first.click(timeout=10000)
                except Exception:
                    # Fallback: try a broad text selector
                    page.locator(f"text={target}").first.click(timeout=10000)
                page.wait_for_timeout(2000)

            elif action == "wait_for_download":
                # If a download was triggered by the previous click, Playwright
                # should have already started capturing it.  We wait up to 30s.
                page.wait_for_timeout(5000)

            elif action == "type":
                target = step.get("target", "")
                value = step.get("value", "")
                page.get_by_placeholder(target).first.fill(value)
                page.wait_for_timeout(1000)

            elif action == "scroll":
                direction = step.get("direction", "down")
                delta = 500 if direction == "down" else -500
                page.mouse.wheel(0, delta)
                page.wait_for_timeout(1000)

            elif action == "screenshot":
                label = step.get("label", f"step-{step_idx}")
                safe_label = re.sub(r"[^a-zA-Z0-9_-]", "-", label)[:60]
                dest = screenshot_dir / f"explore-{step_idx:02d}-{safe_label}.png"
                page.screenshot(path=str(dest), full_page=True)
                screenshots.append(dest)
                print(f"    📸 {dest.name}")

            else:
                print(f"    ⚠️  Unknown action: {action}")

        except Exception as exc:
            eprint(f"    ⚠️  Step {step_idx} failed: {exc}")
            # Take a screenshot of the failure state
            fail_dest = screenshot_dir / f"explore-{step_idx:02d}-error.png"
            try:
                page.screenshot(path=str(fail_dest), full_page=True)
                screenshots.append(fail_dest)
            except Exception:
                pass

        step_idx += 1

    return screenshots, downloads


def explore_and_capture(
    frontend_url: str,
    paths: list[str],
    bypass_secret: str,
    test_email: str,
    feature_md: str,
    anthropic_api_key: str,
    screenshot_dir: Path,
) -> tuple[list[Path], list[Path], list[Path]]:
    """
    Full exploration flow: navigate → screenshot → generate test plan → execute.

    Returns ``(page_screenshots, exploration_screenshots, download_paths)``.
    """
    _ensure_playwright_browsers()
    from playwright.sync_api import sync_playwright

    screenshot_dir.mkdir(parents=True, exist_ok=True)
    page_screenshots: list[Path] = []
    explore_screenshots: list[Path] = []
    download_paths: list[Path] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )
        page = context.new_page()

        # Intercept downloads
        download_dir = screenshot_dir / "downloads"
        download_dir.mkdir(parents=True, exist_ok=True)

        def _handle_download(download):
            try:
                suggested = download.suggested_filename or "download"
                save_path = download_dir / suggested
                download.save_as(str(save_path))
                download_paths.append(save_path)
                print(f"    📥 Downloaded: {save_path.name}")
            except Exception as exc:
                eprint(f"    ⚠️  Download save failed: {exc}")

        page.on("download", _handle_download)

        # --- Authenticate ---
        page.goto(frontend_url, wait_until="domcontentloaded")
        encoded_email = urllib.parse.quote(test_email)
        auth_result = page.evaluate(
            """async ([endpoint, secret]) => {
                const resp = await fetch(endpoint, {
                    headers: { 'x-test-bypass': secret },
                    credentials: 'include'
                });
                return { ok: resp.ok, status: resp.status };
            }""",
            [
                f"/api/auth/test-bypass?email={encoded_email}",
                bypass_secret,
            ],
        )

        if not auth_result.get("ok"):
            eprint(
                f"  ⚠️  Auth bypass returned status {auth_result.get('status')} "
                f"— screenshots may show the login page"
            )

        # --- Phase 1: Navigate and screenshot each path ---
        print("\n--- Taking page screenshots ---")
        for idx, path in enumerate(paths):
            url = f"{frontend_url}{path}"
            print(f"  Navigating to {url} …")
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
            except Exception:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(5000)
                except Exception as e2:
                    eprint(f"  ⚠️  Failed to load {url}: {e2}")
                    continue
            page.wait_for_timeout(3000)

            safe_name = path.strip("/").replace("/", "-") or "home"
            dest = screenshot_dir / f"page-{idx:02d}-{safe_name}.png"
            page.screenshot(path=str(dest), full_page=True)
            page_screenshots.append(dest)
            print(f"  📸 {dest.name}")

        if not page_screenshots:
            browser.close()
            return page_screenshots, explore_screenshots, download_paths

        # --- Phase 2: Generate test plan ---
        print("\n--- Generating exploration test plan ---")
        plan = _generate_test_plan(
            page_screenshots, feature_md, anthropic_api_key,
        )
        print(f"  Test plan ({len(plan)} steps):")
        for i, step in enumerate(plan):
            print(f"    {i}: {step.get('action')} {step.get('target', step.get('label', ''))}")

        # --- Phase 3: Execute test plan ---
        # Navigate back to the primary feature path (last visited)
        primary_path = paths[-1] if paths else "/"
        primary_url = f"{frontend_url}{primary_path}"
        print(f"\n--- Executing exploration on {primary_url} ---")
        try:
            page.goto(primary_url, wait_until="networkidle", timeout=60000)
        except Exception:
            page.goto(primary_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)
        page.wait_for_timeout(2000)

        explore_screenshots, step_downloads = _execute_test_plan(
            page, plan, screenshot_dir, start_index=len(page_screenshots),
        )
        download_paths.extend(step_downloads)

        # If test plan didn't end with a screenshot, capture the final state
        if not explore_screenshots:
            final_dest = screenshot_dir / "explore-final.png"
            page.screenshot(path=str(final_dest), full_page=True)
            explore_screenshots.append(final_dest)
            print(f"  📸 {final_dest.name}")

        browser.close()

    return page_screenshots, explore_screenshots, download_paths


def _image_content_block(image_path: Path) -> dict | None:
    """Build an Anthropic image content block from a file, or None if unreadable."""
    suffix = image_path.suffix.lower()
    media_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = media_map.get(suffix)
    if not media_type:
        return None
    try:
        data = image_path.read_bytes()
        # For GIFs, extract the first frame as PNG for the vision model
        if suffix == ".gif":
            from PIL import Image as PILImage
            with PILImage.open(io.BytesIO(data)) as img:
                buf = io.BytesIO()
                img.convert("RGBA").save(buf, format="PNG")
                data = buf.getvalue()
                media_type = "image/png"
        img_b64 = base64.standard_b64encode(data).decode("utf-8")
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": img_b64,
            },
        }
    except Exception:
        return None


def analyze_artifacts(
    page_screenshots: list[Path],
    explore_screenshots: list[Path],
    download_paths: list[Path],
    feature_md: str,
    anthropic_api_key: str,
) -> tuple[bool, str]:
    """
    Send all captured artifacts to a vision model for QA analysis.

    Includes: page screenshots (before interaction), exploration screenshots
    (during/after interaction), and downloaded files (images/GIFs rendered
    as static images).

    Returns ``(approved, critique_text)``.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_api_key)

    content: list[dict] = []

    # Page screenshots
    if page_screenshots:
        content.append({"type": "text", "text": "PAGE SCREENSHOTS (before interaction):"})
        for sp in page_screenshots:
            block = _image_content_block(sp)
            if block:
                content.append(block)
                content.append({"type": "text", "text": f"({sp.name})"})

    # Exploration screenshots
    if explore_screenshots:
        content.append({"type": "text", "text": "EXPLORATION SCREENSHOTS (during/after testing the feature):"})
        for sp in explore_screenshots:
            block = _image_content_block(sp)
            if block:
                content.append(block)
                content.append({"type": "text", "text": f"({sp.name})"})

    # Downloaded files
    if download_paths:
        content.append({"type": "text", "text": "DOWNLOADED FILES (output produced by the feature):"})
        for dp in download_paths:
            block = _image_content_block(dp)
            if block:
                content.append(block)
                content.append({"type": "text", "text": f"Downloaded file: {dp.name}"})
            else:
                content.append({
                    "type": "text",
                    "text": f"Downloaded file: {dp.name} (non-image, {dp.stat().st_size} bytes)",
                })

    content.append({
        "type": "text",
        "text": f"""\
You are a senior QA engineer reviewing a NEW FEATURE that was just added to an
existing web application.  You have been given:
1. Page screenshots showing the page before interaction
2. Exploration screenshots showing the page during/after exercising the feature
3. Downloaded files that the feature produced (if any)

FEATURE SPEC (describes ONLY the new feature):
{feature_md}

CRITICAL RULES:
- You are reviewing ONLY whether the NEW FEATURE described in the spec was
  implemented correctly.
- Do NOT critique or suggest changes to any EXISTING UI elements, page layouts,
  styling, or components that are not part of the new feature.
- The existing page design (card shapes, image styles, grid layout, colors, etc.)
  is intentional and must NOT be changed.
- Mockup images in the spec show what the NEW FEATURE output should look like,
  NOT how the existing page should be redesigned.
- Pay special attention to DOWNLOADED FILES — these are the primary output of
  features that produce downloadable content.  Compare them carefully against
  the spec and mockups.

Evaluate ONLY:
1. Is the new feature (button, UI element, etc.) present and correctly placed?
2. If the feature produces output (downloads, generated content), does that
   output match the spec?  Check sizing, layout, borders, aspect ratio, etc.
3. Are there visual issues specifically with the NEW feature elements?
4. Any obvious UX problems with the NEW feature?

If the new feature appears correctly implemented, respond with exactly the word
APPROVED on its own line (it may be the first or last line of your response).

If there are issues with the NEW FEATURE ONLY, list specific, actionable fixes
as a numbered list.  Do NOT include the word APPROVED anywhere if there are
issues.  Focus on what needs to change in code for the new feature only.""",
    })

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": content}],
    )

    critique = response.content[0].text.strip()
    lines_upper = [ln.strip().upper() for ln in critique.splitlines() if ln.strip()]
    approved = "APPROVED" in lines_upper
    return approved, critique


def _invoke_cursor_visual_fix(
    cursor_cmd: list[str],
    run_dir: Path,
    critique: str,
    feature_md: str,
    cursor_summary: str,
    attempt: int,
    cursor_env: dict,
    model: str,
    output_format: str,
) -> tuple[int, str, str]:
    """
    Invoke Cursor to fix visual / UI issues identified by the vision model.

    Targets the workspace root so Cursor can modify either repo.
    """
    # Collect git diffs so Cursor knows exactly what was already changed
    diff_sections: list[str] = []
    for repo_name in ("welcomepage-api", "welcomepage-prompts"):
        repo_path = run_dir / repo_name
        if repo_path.exists():
            try:
                res = subprocess.run(
                    ["git", "diff", "HEAD~1", "--stat"],
                    cwd=str(repo_path),
                    capture_output=True, text=True, check=False,
                )
                if res.stdout.strip():
                    diff_sections.append(
                        f"### {repo_name} (changed files)\n{res.stdout.strip()}"
                    )
            except Exception:
                pass

    changes_context = "\n\n".join(diff_sections) if diff_sections else "(no diff available)"

    # Truncate very long summaries
    summary_excerpt = cursor_summary[:3000] if cursor_summary else "(no summary)"

    fix_prompt = f"""\
A QA review of the deployed NEW FEATURE found issues (attempt {attempt}).

IMPORTANT CONTEXT — the feature HAS ALREADY BEEN IMPLEMENTED in the existing
code.  The implementation summary and changed files are shown below.  Your job
is ONLY to fix issues with the NEW FEATURE that was added.

ORIGINAL FEATURE SPEC (describes ONLY the new feature):
{feature_md}

IMPLEMENTATION SUMMARY (from the initial Cursor run):
{summary_excerpt}

FILES ALREADY CHANGED:
{changes_context}

QA CRITIQUE — fix every issue listed below:
{critique}

CRITICAL CONSTRAINTS:
- ONLY fix issues with the NEW FEATURE described in the spec above.
- Do NOT change the existing page layout, styling, card shapes, image styles,
  or any UI elements that existed BEFORE this feature was added.
- The existing page design is intentional.  Mockup images in the spec show
  what the NEW FEATURE output should look like, NOT how the existing page
  should be redesigned.
- Do NOT add new pages, new routes, or new components.
- Do NOT duplicate or move existing pages.
- Work within the files that were already created or modified.
- If the critique asks you to change existing UI that is not part of the new
  feature, IGNORE that part of the critique.
- Commit your changes when done.
- At the end, summarize what you changed."""

    full_cmd = cursor_cmd + [
        "-p", fix_prompt,
        "--model", model,
        "--force",
        "--print",
        "--output-format", output_format,
    ]

    print(f"\n===== Cursor Visual Fix (attempt {attempt}) =====")
    rc, stdout, stderr = run_and_capture(full_cmd, cwd=run_dir, env=cursor_env)

    if rc != 0:
        eprint(f"  ⚠️  Cursor visual fix exited with code {rc}")
    else:
        print("  ✅ Cursor visual fix completed")

    return rc, stdout, stderr


def visual_refinement_loop(
    *,
    frontend_url: str,
    frontend_alias_host: str,
    feature_md: str,
    cursor_summary: str,
    preview_paths: list[str],
    bypass_secret: str,
    test_email: str,
    anthropic_api_key: str,
    max_retries: int,
    run_dir: Path,
    frontend_dir: Path,
    vercel_token: str,
    vercel_org_id: str,
    vercel_frontend_project_id: str,
    frontend_env_overrides: dict[str, str] | None,
    cursor_cmd: list[str],
    cursor_env: dict,
    model: str,
    output_format: str,
    max_fix_retries: int,
) -> tuple[bool, str, list[Path]]:
    """
    Explore → analyse → Cursor fix → redeploy loop.

    Each iteration:
      1. Navigates to the preview paths and takes page screenshots
      2. Asks the vision model to generate a test plan for the feature
      3. Executes the plan (clicks, downloads, etc.) via Playwright
      4. Sends ALL artifacts (page screenshots, interaction screenshots,
         downloaded files) to the vision model for review
      5. If not approved, feeds the critique to Cursor and redeploys

    Returns ``(approved, last_critique, all_screenshot_paths)``.
    """
    screenshot_dir = run_dir / "screenshots"
    all_screenshots: list[Path] = []

    for attempt in range(1, max_retries + 1):
        print(f"\n===== Visual Review (attempt {attempt}/{max_retries}) =====")

        # ---- Explore & Capture ----
        attempt_dir = screenshot_dir / f"attempt-{attempt}"
        page_shots, explore_shots, downloads = explore_and_capture(
            frontend_url, preview_paths, bypass_secret, test_email,
            feature_md, anthropic_api_key, attempt_dir,
        )
        all_screenshots.extend(page_shots)
        all_screenshots.extend(explore_shots)

        if not page_shots and not explore_shots:
            print("  ⚠️  No screenshots captured — skipping visual review")
            return True, "", all_screenshots

        if downloads:
            print(f"\n  📦 Captured {len(downloads)} download(s): "
                  f"{', '.join(d.name for d in downloads)}")

        # ---- Analyse all artifacts ----
        print("\n--- Analysing artifacts with vision model ---")
        approved, critique = analyze_artifacts(
            page_shots, explore_shots, downloads,
            feature_md, anthropic_api_key,
        )

        print("\n  Vision model assessment:")
        for line in critique.splitlines():
            print(f"    {line}")

        if approved:
            print(f"\n  ✅ Visual review APPROVED (attempt {attempt})")
            return True, critique, all_screenshots

        if attempt >= max_retries:
            print(
                f"\n  ⚠️  Visual review not approved after {max_retries} "
                f"attempt(s).  Proceeding with last version."
            )
            return False, critique, all_screenshots

        # ---- Fix ----
        _invoke_cursor_visual_fix(
            cursor_cmd=cursor_cmd,
            run_dir=run_dir,
            critique=critique,
            feature_md=feature_md,
            cursor_summary=cursor_summary,
            attempt=attempt,
            cursor_env=cursor_env,
            model=model,
            output_format=output_format,
        )

        # ---- Redeploy frontend ----
        print("\n--- Redeploying frontend after visual fix ---")
        try:
            new_url = deploy_with_build_fix_loop(
                repo_dir=frontend_dir,
                repo_label="welcomepage-prompts (frontend)",
                vercel_token=vercel_token,
                vercel_org_id=vercel_org_id,
                vercel_project_id=vercel_frontend_project_id,
                env_overrides=frontend_env_overrides,
                max_retries=max_fix_retries,
                cursor_cmd=cursor_cmd,
                cursor_env=cursor_env,
                model=model,
                output_format=output_format,
            )
            print(f"  🔗 New frontend deploy URL: {new_url}")

            set_vercel_alias(
                new_url, frontend_alias_host, vercel_token, vercel_org_id,
            )
        except (VercelBuildError, RuntimeError) as exc:
            eprint(f"  ❌ Frontend redeploy failed: {exc}")
            return False, critique, all_screenshots

    return False, "", all_screenshots


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
    parser.add_argument(
        "--model", default="composer-1.5",
        help="Cursor model to use for all agent invocations (default: composer-1.5).",
    )
    parser.add_argument(
        "--max-visual-retries", type=int, default=2,
        help="Max visual-review iterations (screenshot → analyse → fix). "
             "Set to 0 to disable visual review (default: 2).",
    )
    parser.add_argument(
        "--skip-visual-review", action="store_true",
        help="Skip the visual review / refinement loop entirely.",
    )
    parser.add_argument(
        "--test-email",
        default=os.getenv("TESTING_AUTH_EMAIL", ""),
        help="Email address for the auth-bypass endpoint used during visual review.",
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

    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    bypass_secret = os.getenv("TESTING_AUTH_BYPASS_SECRET", "").strip()
    test_email = args.test_email.strip()

    need_visual = (
        need_deploy
        and not args.skip_visual_review
        and args.max_visual_retries > 0
    )
    if need_visual:
        for name, val in [
            ("ANTHROPIC_API_KEY", anthropic_api_key),
            ("TESTING_AUTH_BYPASS_SECRET", bypass_secret),
        ]:
            if not val:
                raise RuntimeError(
                    f"{name} is required for visual review. Set it in .env or use "
                    "--skip-visual-review."
                )
        if not test_email:
            raise RuntimeError(
                "A test email is required for visual review. Provide --test-email "
                "or set TESTING_AUTH_EMAIL in .env."
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
            need_visual=need_visual,
            anthropic_api_key=anthropic_api_key,
            bypass_secret=bypass_secret,
            test_email=test_email,
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
    need_visual: bool,
    anthropic_api_key: str,
    bypass_secret: str,
    test_email: str,
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
- Include a line: PREVIEW_PATHS: /path1, /path2
  listing the frontend routes a reviewer should visit to see your changes.
""".strip()

    full_cmd = cursor_cmd + [
        "-p", prompt,
        "--model", args.model,
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

        frontend_env_overrides: dict[str, str] = {
            "NEXT_PUBLIC_FASTAPI_BASE_URL": api_alias_url,
        }
        if bypass_secret:
            frontend_env_overrides["TESTING_AUTH_BYPASS_SECRET"] = bypass_secret

        # Deploy backend — wire WEBAPP_URL to the frontend alias.
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
                model=args.model,
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
                env_overrides=frontend_env_overrides,
                max_retries=args.max_fix_retries,
                cursor_cmd=cursor_cmd,
                cursor_env=env,
                model=args.model,
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
    # 4) Visual refinement loop
    # ------------------------------------------------------------------
    visual_approved = None
    visual_critique = ""
    visual_screenshots: list[Path] = []

    if need_visual and frontend_alias_ok:
        preview_paths = parse_preview_paths(feature_md)
        cursor_paths = parse_preview_paths(cursor_stdout)
        if cursor_paths != ["/"]:
            for p in cursor_paths:
                if p not in preview_paths:
                    preview_paths.append(p)

        # Drop the generic "/" when we have specific paths — the homepage
        # is rarely relevant and confuses the vision model.
        if len(preview_paths) > 1 and "/" in preview_paths:
            preview_paths.remove("/")

        print(f"\n  Preview paths for visual review: {preview_paths}")

        visual_approved, visual_critique, visual_screenshots = (
            visual_refinement_loop(
                frontend_url=frontend_alias_url,
                frontend_alias_host=frontend_alias_host,
                feature_md=feature_md,
                cursor_summary=cursor_stdout,
                preview_paths=preview_paths,
                bypass_secret=bypass_secret,
                test_email=test_email,
                anthropic_api_key=anthropic_api_key,
                max_retries=args.max_visual_retries,
                run_dir=run_dir,
                frontend_dir=frontend_dir,
                vercel_token=vercel_token,
                vercel_org_id=vercel_org_id,
                vercel_frontend_project_id=vercel_frontend_project_id,
                frontend_env_overrides=frontend_env_overrides,
                cursor_cmd=cursor_cmd,
                cursor_env=env,
                model=args.model,
                output_format=args.cursor_output_format,
                max_fix_retries=args.max_fix_retries,
            )
        )
    elif need_visual and not frontend_alias_ok:
        print("\n  (skipping visual review — frontend deploy/alias failed)")

    # ------------------------------------------------------------------
    # 5) Write structured metadata into a separate JSON file
    # ------------------------------------------------------------------
    _write_structured_log(run_dir, dict(
        feature_path=str(feature_path),
        feature_content=feature_md,
        branch_name=branch_name,
        base_branch=base_branch,
        cursor=dict(
            model=args.model,
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
        visual_review=dict(
            enabled=need_visual,
            approved=visual_approved,
            critique=visual_critique,
            screenshots=[str(s) for s in visual_screenshots],
            max_retries=args.max_visual_retries,
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
    if visual_approved is not None:
        status = "APPROVED" if visual_approved else "NOT APPROVED"
        print(f"  Visual    : {status}")
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
