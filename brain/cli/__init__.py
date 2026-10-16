"""Command implementations for `bin/recall.py`.

P1: `init` / `doctor` / `add` / `index` —— 收进来、记得住、体检得了。
P2: `search` / `alias` —— 能找回去（含错别字与中英互指）。
P3: `archive` —— 对话记录入库（按轮次切标题，检索结果能说出「第几轮」）。
P4: `compose` / `verify` / `memory` —— 成品汇编 + 出处回查 + 长期记忆。
P5: `search --in` / `install-skill` —— 零设置就能搜；一条命令装好技能。

新的命令一律走这个清单注册，不要在 `bin/recall.py` 里单独 import —— 那是「命令行
能做什么」的唯一真相来源。
"""

from . import (add, alias, archive, compose, doctor, index, init,  # noqa: F401
               install_skill, memory, search, verify)

__all__ = ["add", "alias", "archive", "compose", "doctor", "index", "init",
           "install_skill", "memory", "search", "verify"]
