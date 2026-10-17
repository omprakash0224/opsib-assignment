"""
CLI Utility to export the FastAPI OpenAPI 3.1 specification to openapi.json.

Usage:
    python scripts/export_openapi.py [--output openapi.json]
"""
import os
import sys
import json
import argparse

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app


def export_openapi(output_path: str = "openapi.json"):
    schema = app.openapi()
    target_abs = os.path.abspath(output_path)

    with open(target_abs, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)

    total_paths = len(schema.get("paths", {}))
    total_schemas = len(schema.get("components", {}).get("schemas", {}))

    print(f"[OK] Exported OpenAPI specification to: {target_abs}")
    print(f"     API Title   : {schema.get('info', {}).get('title')}")
    print(f"     Version     : {schema.get('info', {}).get('version')}")
    print(f"     Total Paths : {total_paths} endpoints")
    print(f"     Schemas     : {total_schemas} models")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export FastAPI OpenAPI specification.")
    parser.add_argument("--output", default="openapi.json", help="Destination file path")
    args = parser.parse_args()

    export_openapi(args.output)
