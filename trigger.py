"""
flstudio MCP server entry point.

The code lives in fl_bridge.py (the MIDI/SysEx link to FL Studio) and
fl_tools.py (the tools). The reload_server tool re-imports both from disk and
tells the client the tool list changed, so code changes take effect without
restarting the Claude app. Only changes to this file need a full restart.
"""
import importlib
import py_compile

import anyio
import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.stdio import stdio_server

import fl_bridge
import fl_tools

mcp = FastMCP("flstudio")
_registered_tools = []

def register_tools():
    """(Re)register every tool in fl_tools.TOOLS, replacing the previous set"""
    for name in _registered_tools:
        mcp.remove_tool(name)
    _registered_tools.clear()
    for fn in fl_tools.TOOLS:
        mcp.add_tool(fn)
        _registered_tools.append(fn.__name__)

@mcp.tool()
async def reload_server(ctx: Context) -> str:
    """
    Reload the flstudio MCP server's code from disk (fl_bridge.py and fl_tools.py)
    and the FL Studio script's request handlers (mcp_handlers.py)

    Call this after editing either side so the changes take effect without
    restarting the Claude app or reopening FL Studio's MIDI Settings. Open MIDI
    ports and the FL Studio script log are kept. If a server file has a syntax
    error, nothing is reloaded. Changes to trigger.py, or to the main FL script
    (device_test.py), still need a restart / manual reload.
    """
    for module in (fl_bridge, fl_tools):
        try:
            py_compile.compile(module.__file__, doraise=True)
        except py_compile.PyCompileError as e:
            return f"Not reloaded, nothing changed: {module.__name__} has an error.\n{e.msg}"
    old_tools = set(_registered_tools)
    try:
        importlib.reload(fl_bridge)
        importlib.reload(fl_tools)
    except Exception as e:
        return f"Reload failed, the previous tools are still active: {type(e).__name__}: {e}"
    register_tools()
    await ctx.session.send_tool_list_changed()
    added = sorted(set(_registered_tools) - old_tools)
    removed = sorted(old_tools - set(_registered_tools))
    # fl_tools.after_reload also reloads the FL Studio script's request handlers
    hook = getattr(fl_tools, "after_reload", None)
    hook_result = await anyio.to_thread.run_sync(hook) if hook else None
    return (f"Reloaded {len(_registered_tools)} tools."
            + (f" Added: {', '.join(added)}." if added else "")
            + (f" Removed: {', '.join(removed)}." if removed else "")
            + (f" {hook_result}" if hook_result else ""))

register_tools()

async def main():
    # FastMCP's stdio runner, but advertising tools.listChanged so the client
    # re-reads the tool list when reload_server sends a list-changed notification
    async with stdio_server() as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(NotificationOptions(tools_changed=True)),
        )

if __name__ == "__main__":
    anyio.run(main)
