"""Console-script entry point for `pip install` users.

源码布局下仍然可以直接运行 `python scripts/rail_transfer.py`；
安装后则用 `rail-transfer ...`。安装包里核心模块叫 `rail_transfer_core`，
避免占用过于通用的顶层包名 `scripts`。
"""

try:
    from rail_transfer_core.rail_transfer import main
except ImportError:  # 源码布局：没有安装映射时退回 scripts 包
    from scripts.rail_transfer import main

if __name__ == "__main__":
    raise SystemExit(main())
