"""
Security Hooks for Autonomous Coding Agent
==========================================

Pre-tool-use hooks that validate bash commands for security.
Uses an allowlist approach - only explicitly permitted commands can run.
"""

import os
import shlex


# Base allowed commands for development tasks
# These are always available for all tasks
BASE_ALLOWED_COMMANDS = {
    # File inspection
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "echo",
    "printf",
    # File operations (agent uses SDK tools for most file ops, but cp/mkdir needed occasionally)
    "cp",
    "mv",
    "rm",
    "mkdir",
    "touch",
    "chmod",  # For making scripts executable; validated separately
    # Directory navigation
    "pwd",
    "cd",
    # Node.js development
    "npm",
    "pnpm",
    "npx",
    "node",
    # Python development
    "python",
    "python3",
    # Version control
    "git",
    "gh",  # GitHub CLI for PR operations
    # iOS / Xcode development
    "xcodebuild",
    "xcode-select",
    "swift",
    "swiftc",
    "xcrun",
    "pod",
    "xcpretty",
    "simctl",
    # Process management
    "ps",
    "lsof",
    "sleep",
    "find",
    "pkill",  # For killing dev servers; validated separately
    # Script execution
    "init.sh",  # Init scripts; validated separately
    "curl",
    "kill",
    "xargs",
    # Browser automation
    "playwright-cli",
    # Common shell utilities
    "true",
    "false",
    "test",
    "[",
    "diff",  # read-only file comparison
    "bash",
    "sh",
    "zsh",
    # Environment & path utilities
    "which",
    "export",
    "env",
    "readlink",
    "basename",
    "dirname",
    # Text processing (agent sometimes uses these for batch operations)
    "sed",
    "awk",
    "sort",
    "uniq",
    "tr",
    "cut",
    "tee",
    "bundle",
    "gem",
    "fastlane",
    "sentry-cli",
    "date",
    "gradlew",
    "gradle",
    "gcloud",
    "keytool",
    "az",
    "ssh",
    "pip",
    "pip3"
}

# Task-specific allowed commands (set at runtime)
_task_allowed_commands: set[str] = set()


def set_task_allowed_commands(commands: list[str]) -> None:
    """
    Set task-specific allowed commands.

    These commands are merged with BASE_ALLOWED_COMMANDS to form
    the complete allowlist for the current task.

    Args:
        commands: List of additional command names to allow
    """
    global _task_allowed_commands
    _task_allowed_commands = set(commands)


def get_allowed_commands() -> set[str]:
    """
    Get the complete set of allowed commands.

    Returns:
        Union of BASE_ALLOWED_COMMANDS and task-specific commands
    """
    return BASE_ALLOWED_COMMANDS | _task_allowed_commands


# Commands that need additional validation even when in the allowlist
COMMANDS_NEEDING_EXTRA_VALIDATION = {"pkill", "chmod", "init.sh"}


def split_command_segments(command_string: str) -> list[str]:
    """
    Split a compound command into individual command segments.

    Handles command chaining (&&, ||, ;) but not pipes (those are single commands).

    Args:
        command_string: The full shell command

    Returns:
        List of individual command segments
    """
    import re

    # Split on && and || while preserving the ability to handle each segment
    # This regex splits on && or || that aren't inside quotes
    segments = re.split(r"\s*(?:&&|\|\|)\s*", command_string)

    # Further split on semicolons
    result = []
    for segment in segments:
        sub_segments = re.split(r'(?<!["\'])\s*;\s*(?!["\'])', segment)
        for sub in sub_segments:
            sub = sub.strip()
            if sub:
                result.append(sub)

    return result


def extract_commands(command_string: str) -> list[str]:
    """
    Extract command names from a shell command string.

    Handles pipes, command chaining (&&, ||, ;), and subshells.
    Returns the base command names (without paths).

    Args:
        command_string: The full shell command

    Returns:
        List of command names found in the string
    """
    commands = []

    # shlex doesn't treat ; as a separator, so we need to pre-process
    import re

    # Pre-pass: extract subshell `$(...)` contents and recurse on them, then
    # blank them out of the outer command. Without this, a command like
    #   TOKEN=$(az account get-access-token ...)
    # gets shlex-tokenised as ["TOKEN=$(az", "account", ...]; the second
    # token is treated as a fresh command, blocking valid usage. Backtick
    # subshells `…` are handled the same way.
    subshell_inners: list[str] = []
    # Iterate; the regex doesn't handle nested $( ) but that's rare in
    # practice and we'd rather under-extract (fail safe) than over-extract.
    _subshell_re = re.compile(r"\$\(([^()]*?)\)|`([^`]*?)`")
    while True:
        m = _subshell_re.search(command_string)
        if not m:
            break
        inner = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        if inner:
            subshell_inners.append(inner)
        command_string = (
            command_string[: m.start()] + " " + command_string[m.end() :]
        )

    for inner in subshell_inners:
        commands.extend(extract_commands(inner))

    # Split on semicolons that aren't inside quotes and aren't escaped (\;)
    # This handles common cases like "echo hello; ls"
    # but preserves find -exec ... \; patterns
    segments = re.split(r'(?<!\\)(?<!["\'])\s*;\s*(?!["\'])', command_string)

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        try:
            tokens = shlex.split(segment)
        except ValueError:
            # Malformed command (unclosed quotes, etc.)
            # Return empty to trigger block (fail-safe)
            return []

        if not tokens:
            continue

        # Track when we expect a command vs arguments
        expect_command = True
        # Track find -exec ... \; blocks — everything between -exec and \;/+ is find's args
        in_find_exec = False
        # `for VAR in …` and `while/until <cond> do …` — skip the loop
        # variable name and iteration values until we reach `do`.
        skip_until_do = False

        for token in tokens:
            # Handle find -exec block: skip tokens until terminator
            if in_find_exec:
                if token in (";", "\\;", "+"):
                    in_find_exec = False
                continue

            # Stop on shell comment marker — everything after a bare `#`
            # token is the rest-of-line comment, not commands. (URL fragments
            # like `https://x#frag` survive: shlex keeps them as one token,
            # so this only fires for true `# foo` comments.)
            if token == "#" or (token.startswith("#") and len(token) > 1):
                break

            # In a for/while/until preamble — keep eating tokens until we
            # hit the `do` that introduces the loop body.
            if skip_until_do:
                if token == "do":
                    skip_until_do = False
                    expect_command = True
                continue

            # Shell operators indicate a new command follows
            if token in ("|", "||", "&&", "&"):
                expect_command = True
                continue

            # for/while/until starts a loop preamble — skip until `do`
            if token in ("for", "while", "until"):
                skip_until_do = True
                continue

            # Skip shell keywords that precede commands
            if token in (
                "if",
                "then",
                "else",
                "elif",
                "fi",
                "do",
                "done",
                "case",
                "esac",
                "in",
                "!",
                "{",
                "}",
            ):
                continue

            # Skip flags/options (but detect -exec for find)
            if token.startswith("-"):
                if token in ("-exec", "-execdir"):
                    in_find_exec = True
                continue

            # Skip variable assignments (VAR=value)
            if "=" in token and not token.startswith("="):
                continue

            if expect_command:
                # Extract the base command name (handle paths like /usr/bin/python)
                cmd = os.path.basename(token)
                commands.append(cmd)
                expect_command = False

    return commands


def validate_pkill_command(command_string: str) -> tuple[bool, str]:
    """
    Validate pkill commands - only allow killing dev-related processes.

    Uses shlex to parse the command, avoiding regex bypass vulnerabilities.

    Returns:
        Tuple of (is_allowed, reason_if_blocked)
    """
    # Allowed process names for pkill
    allowed_process_names = {
        "node",
        "npm",
        "npx",
        "vite",
        "next",
    }

    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse pkill command"

    if not tokens:
        return False, "Empty pkill command"

    # Separate flags from arguments
    args = []
    for token in tokens[1:]:
        if not token.startswith("-"):
            args.append(token)

    if not args:
        return False, "pkill requires a process name"

    # The target is typically the last non-flag argument
    target = args[-1]

    # For -f flag (full command line match), extract the first word as process name
    # e.g., "pkill -f 'node server.js'" -> target is "node server.js", process is "node"
    if " " in target:
        target = target.split()[0]

    if target in allowed_process_names:
        return True, ""
    return False, f"pkill only allowed for dev processes: {allowed_process_names}"


def validate_chmod_command(command_string: str) -> tuple[bool, str]:
    """
    Validate chmod commands - only allow making files executable with +x.

    Returns:
        Tuple of (is_allowed, reason_if_blocked)
    """
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse chmod command"

    if not tokens or tokens[0] != "chmod":
        return False, "Not a chmod command"

    # Look for the mode argument
    # Valid modes: +x, u+x, a+x, etc. (anything ending with +x for execute permission)
    mode = None
    files = []

    for token in tokens[1:]:
        if token.startswith("-"):
            # Skip flags like -R (we don't allow recursive chmod anyway)
            return False, "chmod flags are not allowed"
        elif mode is None:
            mode = token
        else:
            files.append(token)

    if mode is None:
        return False, "chmod requires a mode"

    if not files:
        return False, "chmod requires at least one file"

    # Only allow +x variants (making files executable)
    # This matches: +x, u+x, g+x, o+x, a+x, ug+x, etc.
    import re

    if not re.match(r"^[ugoa]*\+x$", mode):
        return False, f"chmod only allowed with +x mode, got: {mode}"

    return True, ""


def validate_init_script(command_string: str) -> tuple[bool, str]:
    """
    Validate init.sh script execution - only allow ./init.sh.

    Returns:
        Tuple of (is_allowed, reason_if_blocked)
    """
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse init script command"

    if not tokens:
        return False, "Empty command"

    # The command should be exactly ./init.sh (possibly with arguments)
    script = tokens[0]

    # Allow ./init.sh or paths ending in /init.sh
    if script == "./init.sh" or script.endswith("/init.sh"):
        return True, ""

    return False, f"Only ./init.sh is allowed, got: {script}"


def get_command_for_validation(cmd: str, segments: list[str]) -> str:
    """
    Find the specific command segment that contains the given command.

    Args:
        cmd: The command name to find
        segments: List of command segments

    Returns:
        The segment containing the command, or empty string if not found
    """
    for segment in segments:
        segment_commands = extract_commands(segment)
        if cmd in segment_commands:
            return segment
    return ""


# Allowed system path prefixes — executable lookups at command position are OK
ALLOWED_SYSTEM_PREFIXES = (
    "/usr/bin/",
    "/usr/local/bin/",
    "/bin/",
    "/usr/sbin/",
    "/sbin/",
    "/opt/homebrew/bin/",
    "/opt/homebrew/sbin/",
)

# Paths that are always allowed (common shell targets)
ALLOWED_SPECIAL_PATHS = {
    "/dev/null",
    "/dev/stdin",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/zero",
    "/dev/urandom",
    "/dev/random",
}

# Prefixes that are always allowed for arguments (not just commands)
ALLOWED_ARG_PREFIXES = (
    "/tmp/",
    "/var/tmp/",
    "/private/tmp/",  # macOS /tmp is a symlink to /private/tmp
)


def validate_path_restriction(
    command_string: str, project_dir: str
) -> tuple[bool, str]:
    """
    Validate that all path-like arguments in a bash command stay within project_dir.

    Checks tokens that look like file paths (start with /, ./, ../, or contain ..)
    and ensures they resolve to within the project directory.

    Args:
        command_string: The full shell command
        project_dir: Absolute path to the allowed project directory

    Returns:
        Tuple of (is_allowed, reason_if_blocked)
    """
    try:
        tokens = shlex.split(command_string)
    except ValueError:
        return False, "Could not parse command for path validation"

    if not tokens:
        return True, ""

    # Normalize project_dir for prefix matching
    project_dir_normalized = os.path.realpath(project_dir)
    if not project_dir_normalized.endswith("/"):
        project_dir_normalized += "/"

    def _is_path_allowed(path_str: str, is_command_position: bool) -> tuple[bool, str]:
        """Check if a single path is within allowed boundaries."""
        # Special paths always allowed
        if path_str in ALLOWED_SPECIAL_PATHS:
            return True, ""

        # /tmp and similar always allowed
        for prefix in ALLOWED_ARG_PREFIXES:
            if path_str.startswith(prefix) or path_str == prefix.rstrip("/"):
                return True, ""

        # System tool paths allowed only at command position
        if is_command_position:
            for prefix in ALLOWED_SYSTEM_PREFIXES:
                if path_str.startswith(prefix):
                    return True, ""

        # Resolve the path: if absolute use as-is, if relative resolve against project_dir
        if os.path.isabs(path_str):
            resolved = os.path.realpath(path_str)
        else:
            resolved = os.path.realpath(os.path.join(project_dir, path_str))

        # Check if resolved path is within project_dir or IS project_dir
        if resolved == project_dir_normalized.rstrip("/"):
            return True, ""
        if resolved.startswith(project_dir_normalized):
            return True, ""

        return False, f"Path '{path_str}' (resolves to '{resolved}') is outside project directory '{project_dir}'"

    # Parse tokens and check path-like arguments
    expect_command = True
    skip_next = False  # for redirect operators

    for i, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            # This token is a redirect target — validate it as a path
            if token.startswith("/") or token.startswith("./") or token.startswith("../") or ".." in token:
                ok, reason = _is_path_allowed(token, is_command_position=False)
                if not ok:
                    return False, reason
            continue

        # Shell operators
        if token in ("|", "||", "&&", "&", ";"):
            expect_command = True
            continue

        # Redirect operators — next token is a file path
        if token in (">", ">>", "<", "2>", "2>>", "&>", "&>>"):
            skip_next = True
            continue

        # Handle combined redirect+path like >/path or >>/path
        for redirect_op in (">>", ">", "2>>", "2>", "&>>", "&>"):
            if token.startswith(redirect_op) and len(token) > len(redirect_op):
                path_part = token[len(redirect_op):]
                if path_part.startswith("/") or path_part.startswith("./") or path_part.startswith("../") or ".." in path_part:
                    ok, reason = _is_path_allowed(path_part, is_command_position=False)
                    if not ok:
                        return False, reason
                break

        # Skip flags
        if token.startswith("-"):
            continue

        # Skip variable assignments
        if "=" in token and not token.startswith("="):
            continue

        # Check if this token looks like a path
        is_path_like = (
            token.startswith("/")
            or token.startswith("./")
            or token.startswith("../")
            or ".." in token
        )

        if is_path_like:
            ok, reason = _is_path_allowed(token, is_command_position=expect_command)
            if not ok:
                return False, reason

        if expect_command:
            expect_command = False

    return True, ""


def make_bash_security_hook(restricted_project_dir: str | None = None):
    """
    Factory that creates a bash security hook with optional path restriction.

    When restricted_project_dir is set, bash commands are validated to ensure
    all path arguments stay within the project directory. This is bound into
    a closure so concurrent sessions with different restrictions don't conflict.

    Args:
        restricted_project_dir: Absolute path to restrict to, or None for no restriction

    Returns:
        An async hook function compatible with Claude Agent SDK PreToolUse hooks
    """

    async def _hook(input_data, tool_use_id=None, context=None):
        if input_data.get("tool_name") != "Bash":
            return {}

        command = input_data.get("tool_input", {}).get("command", "")
        if not command:
            return {}

        # Step 1: Command allowlist check
        commands = extract_commands(command)
        if not commands:
            return {
                "decision": "block",
                "reason": f"Could not parse command for security validation: {command}",
            }

        segments = split_command_segments(command)
        allowed = get_allowed_commands()

        for cmd in commands:
            if cmd not in allowed:
                return {
                    "decision": "block",
                    "reason": f"Command '{cmd}' is not in the allowed commands list",
                }

            if cmd in COMMANDS_NEEDING_EXTRA_VALIDATION:
                cmd_segment = get_command_for_validation(cmd, segments)
                if not cmd_segment:
                    cmd_segment = command

                if cmd == "pkill":
                    is_ok, reason = validate_pkill_command(cmd_segment)
                    if not is_ok:
                        return {"decision": "block", "reason": reason}
                elif cmd == "chmod":
                    is_ok, reason = validate_chmod_command(cmd_segment)
                    if not is_ok:
                        return {"decision": "block", "reason": reason}
                elif cmd == "init.sh":
                    is_ok, reason = validate_init_script(cmd_segment)
                    if not is_ok:
                        return {"decision": "block", "reason": reason}

        # Step 2: Path restriction check (only when restricted)
        if restricted_project_dir:
            for segment in segments:
                is_ok, reason = validate_path_restriction(segment, restricted_project_dir)
                if not is_ok:
                    return {"decision": "block", "reason": reason}

        return {}

    return _hook

