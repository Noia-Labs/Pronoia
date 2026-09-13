"""Pronoia 单点版本号（由 scripts/bump.py 自动维护）。

变更策略：
- patch: bug 修复、文案调整、性能优化（如 fix: / docs: / refactor: / chore:）
- minor: 新功能、新增 skill/agent、向后兼容的改进（如 feat:）
- major: 破坏性变更、架构重构（手动指定 --major）

使用：
    python scripts/bump.py patch "修复右栏默认折叠"
    python scripts/bump.py minor "新增产业链传导预测"
    python scripts/bump.py patch "fix: 修复 X" --commit --push
"""
__version__ = "3.13.0"
