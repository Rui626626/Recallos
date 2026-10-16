"""Host adapters.

Each subpackage isolates everything that is specific to one host. The rule that
makes the eventual WPS port cheap: **nothing under `brain/core/` may import
from here**, and nothing here may be imported by `core/`.

* `dsh/`  — DeepSeek Harness (current host)
* `wps/`  — WPS AI Skill (planned; not started)
"""

__all__: list[str] = []
