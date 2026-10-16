"""
db.py - Centralized FalkorDB connection factory for RingWatch.
Reads FALKORDB_HOST (default localhost), FALKORDB_PORT (default 6379),
FALKORDB_USERNAME, FALKORDB_PASSWORD, and FALKORDB_SSL from env or Streamlit secrets.
Never prints or logs secrets.
"""

import os
from typing import Any, Dict, Optional
from falkordb import FalkorDB, Graph
from dotenv import load_dotenv

# Ensure environment variables are loaded
load_dotenv()


def get_falkordb_config() -> Dict[str, Any]:
    """Retrieve database connection settings from Streamlit secrets (if available) or os.environ."""
    st_secrets: Dict[str, Any] = {}
    try:
        import streamlit as st
        if hasattr(st, "secrets") and st.secrets is not None:
            st_secrets = dict(st.secrets)
    except Exception:
        st_secrets = {}

    host = (
        st_secrets.get("FALKORDB_HOST")
        or os.getenv("FALKORDB_HOST")
        or "localhost"
    )

    port_val = (
        st_secrets.get("FALKORDB_PORT")
        or os.getenv("FALKORDB_PORT")
        or 6379
    )
    port = int(port_val)

    username = (
        st_secrets.get("FALKORDB_USERNAME")
        or os.getenv("FALKORDB_USERNAME")
        or None
    )

    password = (
        st_secrets.get("FALKORDB_PASSWORD")
        or os.getenv("FALKORDB_PASSWORD")
        or None
    )

    ssl_val = (
        st_secrets.get("FALKORDB_SSL")
        or os.getenv("FALKORDB_SSL")
        or False
    )
    if isinstance(ssl_val, str):
        ssl = ssl_val.strip().lower() in ("true", "1", "yes")
    else:
        ssl = bool(ssl_val)

    config: Dict[str, Any] = {
        "host": host,
        "port": port,
    }
    if username:
        config["username"] = username
    if password:
        config["password"] = password
    if ssl:
        config["ssl"] = ssl

    return config


def get_client() -> FalkorDB:
    """Returns a connected FalkorDB client instance without printing credentials."""
    cfg = get_falkordb_config()
    return FalkorDB(**cfg)


def get_db(graph_name: str = "fraud") -> Graph:
    """Returns a FalkorDB Graph object for graph_name without printing credentials."""
    client = get_client()
    return client.select_graph(graph_name)
