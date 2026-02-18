"""
Claude SDK Client Configuration
===============================

Functions for creating and configuring the Claude Agent SDK client.
"""

import json
import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import HookMatcher

from security import bash_security_hook


# Browser tool configurations
BROWSER_TOOLS = {
    "playwright": {
        "tools": [
            "mcp__playwright__browser_navigate",
            "mcp__playwright__browser_snapshot",
            "mcp__playwright__browser_click",
            "mcp__playwright__browser_type",
            "mcp__playwright__browser_hover",
            "mcp__playwright__browser_select_option",
            "mcp__playwright__browser_wait_for",
            "mcp__playwright__browser_evaluate",
            "mcp__playwright__browser_take_screenshot",
        ],
        "mcp_server": {"command": "npx", "args": ["@anthropic/playwright-mcp-server"]},
        "name": "playwright",
    },
    "puppeteer": {
        "tools": [
            "mcp__puppeteer__puppeteer_navigate",
            "mcp__puppeteer__puppeteer_screenshot",
            "mcp__puppeteer__puppeteer_click",
            "mcp__puppeteer__puppeteer_fill",
            "mcp__puppeteer__puppeteer_select",
            "mcp__puppeteer__puppeteer_hover",
            "mcp__puppeteer__puppeteer_evaluate",
        ],
        "mcp_server": {"command": "npx", "args": ["puppeteer-mcp-server"]},
        "name": "puppeteer",
    },
    "browsermcp": {
        "tools": [
            "mcp__browsermcp__browser_navigate",
            "mcp__browsermcp__browser_snapshot",
            "mcp__browsermcp__browser_click",
            "mcp__browsermcp__browser_type",
            "mcp__browsermcp__browser_hover",
            "mcp__browsermcp__browser_select_option",
            "mcp__browsermcp__browser_wait",
        ],
        "mcp_server": {"command": "npx", "args": ["@anthropic/browsermcp"]},
        "name": "browsermcp",
    },
}

# Built-in tools
BUILTIN_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
    "Skill",  # Enable skill invocation
    "WebSearch",
    "WebFetch",
    "Task",   # Enable sub-agent spawning
]


DEFAULT_SYSTEM_PROMPT = "You are an expert full-stack developer building a production-quality web application."


def create_client(
    project_dir: Path,
    model: str,
    browser_tool: str = "playwright",
    system_prompt: str | None = None,
    max_turns: int = 1000,
    permission_mode: str | None = None,
    resume: str | None = None,
) -> ClaudeSDKClient:
    """
    Create a Claude Agent SDK client with multi-layered security.

    Args:
        project_dir: Directory for the project
        model: Claude model to use
        browser_tool: Browser automation tool to use
        system_prompt: Custom system prompt
        max_turns: Maximum conversation turns
        permission_mode: Permission mode ('default', 'acceptEdits', 'plan', 'bypassPermissions')
        resume: Session ID to resume a previous conversation

    Returns:
        Configured ClaudeSDKClient

    Security layers (defense in depth):
    1. Sandbox - OS-level bash command isolation prevents filesystem escape
    2. Permissions - File operations restricted to project_dir only
    3. Security hooks - Bash commands validated against an allowlist
       (see security.py for ALLOWED_COMMANDS)
    """
    # Get browser configuration
    browser_config = BROWSER_TOOLS.get(browser_tool, BROWSER_TOOLS["playwright"])
    browser_tools = browser_config["tools"]
    browser_mcp_server = browser_config["mcp_server"]
    browser_name = browser_config["name"]

    # API key is optional - Claude Code SDK can use Claude CLI subscription
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    # Note: If api_key is None, SDK will attempt to use Claude Code CLI authentication

    # Create comprehensive security settings
    # Note: Using relative paths ("./**") restricts access to project directory
    # since cwd is set to project_dir
    security_settings = {
        "sandbox": {"enabled": False},
        "permissions": {
            "defaultMode": "acceptEdits",  # Auto-approve edits within allowed directories
            "allow": [
                # Allow all file operations within the project directory
                "Read(./**)",
                "Write(./**)",
                "Edit(./**)",
                "Glob(./**)",
                "Grep(./**)",
                # Bash permission granted here, but actual commands are validated
                # by the bash_security_hook (see security.py for allowed commands)
                "Bash(*)",
                # Allow browser MCP tools for browser automation
                *browser_tools,
            ],
        },
    }

    # Ensure project directory exists before creating settings file
    project_dir.mkdir(parents=True, exist_ok=True)

    # Write settings to a file in the project directory
    settings_file = project_dir / ".claude_settings.json"
    with open(settings_file, "w") as f:
        json.dump(security_settings, f, indent=2)

    sandbox_enabled = security_settings["sandbox"]["enabled"]
    print(f"Created security settings at {settings_file}")
    print(f"   - Sandbox: {'enabled' if sandbox_enabled else 'disabled'}")
    print(f"   - Filesystem restricted to: {project_dir.resolve()}")
    print("   - Bash commands restricted to allowlist (see security.py)")
    print(f"   - MCP servers: {browser_name} (browser automation)")
    print()

    # Build options dict, only include optional params when set
    options_kwargs = dict(
        model=model,
        system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
        allowed_tools=[
            *BUILTIN_TOOLS,
            *browser_tools,
        ],
        setting_sources=["user", "project"],  # Load skills from ~/.claude and project
        mcp_servers={
            browser_name: browser_mcp_server
        },
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[bash_security_hook]),
            ],
        },
        max_turns=max_turns,
        cwd=str(project_dir.resolve()),
        settings=str(settings_file.resolve()),  # Use absolute path
    )

    if permission_mode:
        options_kwargs["permission_mode"] = permission_mode
    if resume:
        options_kwargs["resume"] = resume

    return ClaudeSDKClient(
        options=ClaudeAgentOptions(**options_kwargs)
    )
