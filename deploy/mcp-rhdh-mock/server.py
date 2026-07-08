"""Mock RHDH Catalog MCP Server.

Provides hardcoded software catalog data for CVE patch workflow demos.
Exposes 4 MCP tools: list_components, get_component, check_vulnerabilities,
get_cve_details.
"""

import json
from pathlib import Path

from fastmcp import FastMCP

DATA_PATH = Path(__file__).parent / "catalog-data.json"

with open(DATA_PATH) as f:
    CATALOG = json.load(f)

mcp = FastMCP("rhdh-catalog-mock")


@mcp.tool()
def list_components() -> list[dict]:
    """List all registered components in the RHDH software catalog.

    Returns a summary of each component with name, owner, and tech stack.
    """
    return [
        {
            "name": c["name"],
            "owner": c["owner"],
            "tech_stack": c["tech_stack"],
            "dependency_count": len(c["dependencies"]),
        }
        for c in CATALOG["components"]
    ]


@mcp.tool()
def get_component(name: str) -> dict:
    """Get detailed information about a specific component.

    Args:
        name: The component name (e.g. 'payment-gateway').

    Returns component details including repo, owner, tech stack,
    and all dependencies with their versions.
    """
    for c in CATALOG["components"]:
        if c["name"] == name:
            return c
    return {"error": f"Component '{name}' not found"}


@mcp.tool()
def check_vulnerabilities(component: str) -> dict:
    """Check a component for known CVE vulnerabilities.

    Args:
        component: The component name to check.

    Returns a list of vulnerable dependencies with CVE IDs
    and patched versions.
    """
    for c in CATALOG["components"]:
        if c["name"] == component:
            vulns = [
                {
                    "dependency": d["name"],
                    "current_version": d["version"],
                    "cve_id": d["cve"],
                    "patched_version": d["patched"],
                }
                for d in c["dependencies"]
                if d.get("cve")
            ]
            return {
                "component": component,
                "vulnerable": len(vulns) > 0,
                "vulnerabilities": vulns,
            }
    return {"error": f"Component '{component}' not found"}


@mcp.tool()
def get_cve_details(cve_id: str) -> dict:
    """Get detailed information about a specific CVE.

    Args:
        cve_id: The CVE identifier (e.g. 'CVE-2024-22234').

    Returns CVE description, severity, affected versions,
    and patched version.
    """
    cve_db = CATALOG.get("cve_database", {})
    if cve_id in cve_db:
        return {"cve_id": cve_id, **cve_db[cve_id]}
    return {"error": f"CVE '{cve_id}' not found"}


if __name__ == "__main__":
    mcp.run()
