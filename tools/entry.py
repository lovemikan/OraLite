"""PyInstaller 打包入口脚本。"""
import sys

from oracle_client.main import main

if __name__ == "__main__":
    raise SystemExit(main())
