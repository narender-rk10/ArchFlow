"""ArchFlow backend: one structured, versioned project model shared by humans, AI and MCP agents."""

from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env, then the repo-root .env, before any module reads its configuration.
# Real environment variables always win.
_here = Path(__file__).resolve().parent
load_dotenv(_here.parent / ".env")
load_dotenv(_here.parent.parent / ".env")
