"""pytest 配置 — 确保 src 包正确导入"""
import sys
import os

# 将 src 添加到 sys.path
src_path = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, src_path)
