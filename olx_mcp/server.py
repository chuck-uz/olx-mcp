"""Совместимость: старые настройки Claude запускают olx_mcp/server.py. Сервер теперь — uzparser/server.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uzparser.server import main  # noqa: E402

if __name__ == "__main__":
    main()
