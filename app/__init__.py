"""ecommerce-agent 顶层包：DDD 分层的跨境电商导购 Agent。

模型缓存自动配置：在导入任何会读取 HOME/HF 缓存的第三方库之前，
把缓存根目录固化到项目旁的 .p1-tmp，避免 PyCharm/终端分别重新下载
Chroma ONNX 模型与 HuggingFace reranker 权重。用户显式设置的环境变量优先。
"""

from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CACHE_ROOT = _PROJECT_ROOT.parent / ".p1-tmp"
_CACHE_ROOT = Path(os.environ.get("MODEL_CACHE_ROOT", str(_DEFAULT_CACHE_ROOT)))

os.environ.setdefault("HOME", str(_CACHE_ROOT))
os.environ.setdefault("USERPROFILE", str(_CACHE_ROOT))
os.environ.setdefault("HF_HOME", str(_CACHE_ROOT / ".cache" / "huggingface"))

