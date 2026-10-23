"""Client configuration shared by the GUI; importing this needs no MCP SDK."""
import json
import os
import platform
import sys


MCP_EXE_NAME = "MtgCardsMCP.exe" if platform.system() == "Windows" else "MtgCardsMCP"


def claude_desktop_config_path() -> str:
    if platform.system() == "Windows":
        return os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "Claude", "claude_desktop_config.json")
    if platform.system() == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
    return os.path.expanduser("~/.config/Claude/claude_desktop_config.json")


def codex_config_path() -> str:
    return os.path.join(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"), "config.toml")


def packaged_mcp_executable():
    """Find the console server beside the executable or beside a macOS .app."""
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates = [os.path.join(exe_dir, MCP_EXE_NAME)]
    if platform.system() == "Darwin" and exe_dir.endswith(os.path.join("Contents", "MacOS")):
        app_parent = os.path.dirname(os.path.dirname(os.path.dirname(exe_dir)))
        candidates.append(os.path.join(app_parent, MCP_EXE_NAME))
    return next((c for c in candidates if os.path.isfile(c)), None)


def server_entry() -> dict:
    if getattr(sys, "frozen", False):
        command = packaged_mcp_executable() or os.path.join(
            os.path.dirname(os.path.abspath(sys.executable)), MCP_EXE_NAME)
        return {"command": command, "args": []}
    server = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")
    # pythonw cannot serve stdio on Windows. Use its console sibling instead.
    command = sys.executable
    if os.path.basename(command).lower() == "pythonw.exe":
        command = os.path.join(os.path.dirname(command), "python.exe")
    return {"command": command, "args": [server]}


def claude_desktop_snippet() -> str:
    return json.dumps({"mcpServers": {"mtg-cards": server_entry()}}, indent=2)


def codex_snippet() -> str:
    entry = server_entry()
    # JSON strings with literal Unicode are also TOML basic strings. This
    # escapes Windows backslashes/quotes without JSON's surrogate-pair escapes.
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    return ('[mcp_servers.mtg-cards]\n'
            f'command = {quote(entry["command"])}\n'
            f'args = [{", ".join(quote(arg) for arg in entry["args"])}]\n'
            'startup_timeout_sec = 30\n'
            'tool_timeout_sec = 120\n')
