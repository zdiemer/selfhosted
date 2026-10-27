from mcp.server.fastmcp import FastMCP
from ipc import call

mcp = FastMCP('terraria')

@mcp.tool()
def get_context() -> dict:
    """Read current world progression, installed mods, players, caller gear and inventory."""
    return call('context')

@mcp.tool()
def find_recipes(query: str) -> list:
    """Search actual loaded recipes by output item name or Mod/Item id. Includes stations, groups and conditions; maximum 20 results."""
    return call('recipes', {'query': query})

@mcp.tool()
def teleport_to_player(target: str) -> str:
    """On explicit player request, move ONLY the caller to another online player by exact name. Blocked during bosses."""
    return call('teleport_player', {'target': target})

@mcp.tool()
def teleport_home() -> str:
    """On explicit request, move ONLY the caller to their valid bed or world spawn. Blocked during bosses."""
    return call('teleport_home')

@mcp.tool()
def teleport_back() -> str:
    """On explicit request, undo the caller's last teleport this connection. Blocked during bosses."""
    return call('teleport_back')

if __name__ == '__main__':
    mcp.run()
